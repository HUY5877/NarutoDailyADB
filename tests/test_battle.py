from collections import Counter
from copy import deepcopy
from threading import Barrier, Lock

import pytest

from naruto_daily.battle import run_battle, run_battle_without_first_skill
from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.visual import VisualError


def target(name):
    return dict(name=name, template=name + '.png', left=0, top=0, right=100, bottom=100, threshold=0.85)


def battle_config():
    return dict(start_timeout_seconds=3, timeout_seconds=65, tap_interval_seconds=0,
                battle_marker=target('battle'), end_targets=[target('result')],
                buttons=[dict(name=name, x=i, y=i) for i, name in enumerate(('一技能', '二技能', '大招', '普攻'), 1)])


class Clock:
    def __init__(self):
        self.now = 0.0
        self.lock = Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


class Client:
    def __init__(self, clock, tap_seconds=.25):
        self.clock = clock
        self.tap_seconds = tap_seconds
        self.taps = []
        self.frames = []

    def screenshot(self):
        self.frames.append(self.clock())
        return str(len(self.frames)).encode()

    def tap(self, x, y):
        self.taps.append((x, y))
        self.clock.advance(self.tap_seconds)


@pytest.mark.parametrize('tap_seconds, expected_frames', [
    (.25, [0, 30, 60]),
    (1, [0, 32, 64]),
])
def test_result_checks_use_thirty_seconds_of_elapsed_adb_time(tap_seconds, expected_frames):
    clock, waits = Clock(), []
    client = Client(clock, tap_seconds)
    def matcher(png, path, roi, threshold):
        assert path.stem == 'result', '战斗期间只识别结束标记'
        # 结果在40秒出现，下一个30秒连点窗口结束后才会发现。
        return ((50, 50), .99) if clock() >= 40 else (None, .2)
    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)
    result = run_battle(client, dict(battle_config(), timeout_seconds=100), template_matcher=matcher,
                        sleep=sleep, clock=clock)
    assert client.frames == expected_frames
    assert result.taps == len(client.taps) and result.taps > 12
    assert len(set(Counter(client.taps).values())) == 1
    assert set(client.taps) == {(1, 1), (2, 2), (3, 3), (4, 4)}
    assert waits == [] and result.end_target == 'result'


@pytest.mark.parametrize('finished_at, expected_frames', [(2, [0, 18]), (19, [0, 18, 36])])
def test_eighteen_second_check_catches_short_result_before_room_clicks(finished_at, expected_frames):
    clock = Clock()
    client = Client(clock)
    original_tap = client.tap

    def tap(x, y):
        assert clock() < finished_at + 20, '结算20秒后自动回房间，不能继续点击战斗坐标'
        original_tap(x, y)

    def matcher(png, path, roi, threshold):
        # 同时覆盖首轮快速打完，以及刚检查完后结束战斗的情况。
        return ((50, 50), .99) if finished_at <= clock() < finished_at + 20 else (None, .2)

    client.tap = tap
    result = run_battle(client, battle_config(), template_matcher=matcher,
                        sleep=clock.advance, clock=clock, check_interval_seconds=18)
    assert client.frames == expected_frames
    assert result.end_target == 'result' and result.taps == 4 * expected_frames[-1]
    assert clock() == expected_frames[-1]


def test_eighteen_second_windows_resume_without_fixed_pause():
    clock, pauses, tap_times = Clock(), [], []
    client = Client(clock)
    original_tap = client.tap

    def tap(x, y):
        tap_times.append(clock())
        original_tap(x, y)

    def sleep(seconds):
        pauses.append((clock(), seconds))
        clock.advance(seconds)

    client.tap = tap
    result = run_battle(client, battle_config(),
                        template_matcher=lambda *args: ((50, 50), .99) if clock() >= 35 else (None, .2),
                        sleep=sleep, clock=clock, check_interval_seconds=18)
    assert client.frames == [0, 18, 36]
    assert pauses == [] and result.taps == 144
    assert min(t for t in tap_times if t >= 18) == 18


@pytest.mark.parametrize('seconds', [0, -1, float('nan'), float('inf'), True, '18'])
def test_invalid_check_interval_fails_before_device_access(seconds):
    clock = Clock()
    client = Client(clock)
    with pytest.raises(ValueError, match='检查间隔'):
        run_battle(client, battle_config(), check_interval_seconds=seconds)
    assert not client.frames and not client.taps


@pytest.mark.parametrize('runner, expected_buttons', [
    (run_battle, [(1, 1), (2, 2), (3, 3), (4, 4)]),
    (run_battle_without_first_skill, [(2, 2), (3, 3), (4, 4)]),
])
def test_selected_buttons_run_concurrently_and_finish_before_result_check(runner, expected_buttons, caplog):
    clock, waits = Clock(), []
    client = Client(clock)
    config = battle_config()
    original = deepcopy(config)
    barrier = Barrier(len(expected_buttons), timeout=3)
    def tap(x, y):
        client.taps.append((x, y))
        if barrier.wait() == 0:
            clock.advance(30)
    client.tap = tap
    def matcher(png, *args):
        return ((50, 50), .99) if int(png) == 2 else (None, .2)
    with caplog.at_level('INFO', logger='naruto_daily'):
        result = runner(client, config, template_matcher=matcher,
                        sleep=waits.append, clock=clock)
    assert result.taps == len(expected_buttons) and sorted(client.taps) == expected_buttons
    assert client.frames == [0, 30] and waits == []
    assert config == original
    assert f'{len(expected_buttons)} 个战斗按钮' in caplog.text


def test_result_visible_on_entry_stops_without_attacks_or_sleep():
    clock, waits = Clock(), []
    client = Client(clock)
    result = run_battle(client, battle_config(), template_matcher=lambda *a: ((50, 50), .99),
                        sleep=waits.append, clock=clock)
    assert result.taps == 0 and result.end_target == 'result'
    assert client.frames == [0] and client.taps == [] and waits == []


def test_timeout_shorter_than_check_interval_stops_without_extra_screenshot():
    clock, waits = Clock(), []
    client = Client(clock)
    action = battle_config()
    action['timeout_seconds'] = 5
    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, action, template_matcher=lambda *a: (None, .2),
                   sleep=waits.append, clock=clock)
    assert clock() == 5 and len(client.taps) == 20
    assert client.frames == [0] and waits == []


def test_slow_tap_crossing_timeout_does_not_start_another_batch():
    clock, waits = Clock(), []
    client = Client(clock, tap_seconds=2)
    action = battle_config()
    action['timeout_seconds'] = 1
    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, action, template_matcher=lambda *a: (None, .2),
                   sleep=waits.append, clock=clock)
    assert 1 <= len(client.taps) <= 4 and client.frames == [0]
    assert waits == []


def test_screenshot_exceeding_battle_timeout_does_not_receive_attack():
    clock = Clock()
    client = Client(clock)
    def matcher(*args):
        clock.advance(66)
        return None, .2
    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, battle_config(), template_matcher=matcher,
                   sleep=lambda _: None, clock=clock)
    assert client.taps == []


def test_configured_batch_delay_keeps_clicking_between_result_checks():
    clock, waits = Clock(), []
    client = Client(clock, tap_seconds=0)
    action = battle_config()
    action['tap_interval_seconds'] = 2
    result = run_battle(client, action,
                        template_matcher=lambda png, *a: ((50, 50), .99) if int(png) == 2 else (None, .2),
                        sleep=waits.append, clock=clock)
    assert result.taps == 60 and len(client.frames) == 2
    assert waits == [2] * 15


def test_one_failed_parallel_tap_stops_after_current_batch_without_retry():
    clock = Clock()
    client = Client(clock)
    barrier = Barrier(4, timeout=3)
    def tap(x, y):
        client.taps.append((x, y))
        barrier.wait()
        if x == 1:
            raise AdbError('设备输入失败')
    client.tap = tap
    with pytest.raises(AdbError, match='设备输入失败'):
        run_battle(client, battle_config(), template_matcher=lambda *a: (None, .2),
                   sleep=lambda _: None, clock=clock)
    assert sorted(client.taps) == [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert client.frames == [0]


class CompletionModel:
    def __init__(self, result):
        self.config = {'max_calls': 3}
        self.result = result
        self.calls = 0
    def complete_json(self, png, prompt, *, timeout_seconds=None, refresh_image=None):
        self.calls += 1
        return self.result


def test_periodic_checks_resume_immediately_at_same_speed():
    clock, pauses, tap_times = Clock(), [], []
    client = Client(clock)
    original_tap = client.tap

    def tap(x, y):
        tap_times.append(clock())
        original_tap(x, y)

    def sleep(seconds):
        pauses.append((clock(), seconds, len(client.taps)))
        clock.advance(seconds)

    client.tap = tap
    result = run_battle(client, dict(battle_config(), timeout_seconds=110),
                        template_matcher=lambda *args: ((50, 50), .99) if clock() >= 90 else (None, .2),
                        sleep=sleep, clock=clock)
    assert pauses == []
    assert client.frames == [0, 30, 60, 90]
    assert result.taps == 360
    assert sum(t < 30 for t in tap_times) == sum(30 <= t < 60 for t in tap_times) == sum(60 <= t < 90 for t in tap_times) == 120
    assert min(t for t in tap_times if t >= 30) == 30
    assert min(t for t in tap_times if t >= 60) == 60


@pytest.mark.parametrize('use_model', [False, True])
def test_result_found_during_check_never_resumes_taps_or_waits_unnecessarily(use_model):
    clock, waits = Clock(), []
    client = Client(clock)

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    class ResultModel:
        config = {'max_calls': 3}

        def complete_json(self, *args, **kwargs):
            finished = clock() >= 30
            return {'finished': finished, 'page': 'finished' if finished else 'battle',
                    'reason': '战后弹窗' if finished else '战斗中'}

    def matcher(*args):
        return ((50, 50), .99) if not use_model and clock() >= 30 else (None, .2)

    result = run_battle(client, battle_config(), template_matcher=matcher,
                        sleep=sleep, clock=clock, model=ResultModel() if use_model else None)
    assert waits == []
    assert client.frames == [0, 30]
    assert len(client.taps) == result.taps == 120
    assert result.end_target == ('multimodal' if use_model else 'result')


def test_screenshot_and_model_check_wait_for_all_parallel_inputs_to_finish():
    clock, active, lock, events = Clock(), [0], Lock(), []
    client = Client(clock)
    barrier = Barrier(4, timeout=3)

    def tap(x, y):
        with lock:
            active[0] += 1
            client.taps.append((x, y))
        if barrier.wait() == 0:
            clock.advance(30)
        with lock:
            active[0] -= 1

    def sleep(seconds):
        raise AssertionError('检查完成后不应额外暂停')

    original_screenshot = client.screenshot
    def screenshot():
        assert active[0] == 0
        events.append(('screenshot', clock(), len(client.taps)))
        return original_screenshot()

    class Model:
        def complete_json(self, *args, **kwargs):
            assert active[0] == 0
            events.append(('model', clock(), len(client.taps)))
            finished = clock() >= 60
            return {'finished': finished, 'page': 'finished' if finished else 'battle', 'reason': '测试'}

    client.tap = tap
    client.screenshot = screenshot
    result = run_battle(client, dict(battle_config(), timeout_seconds=100),
                        template_matcher=lambda *args: (None, .2),
                        sleep=sleep, clock=clock, model=Model())
    assert result.taps == 8 and client.frames == [0, 30, 60]
    assert events == [('screenshot', 0, 0), ('model', 0, 0),
                      ('screenshot', 30, 4), ('model', 30, 4),
                      ('screenshot', 60, 8), ('model', 60, 8)]


@pytest.mark.parametrize('matched', [False, True])
def test_template_check_exhausting_total_timeout_never_resumes_or_accepts_late_result(matched):
    clock, waits = Clock(), []
    client = Client(clock)

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    def matcher(*args):
        if clock() >= 30:
            clock.advance(2)
            return ((50, 50), .99) if matched else (None, .2)
        return None, .2

    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, dict(battle_config(), timeout_seconds=32),
                   template_matcher=matcher, sleep=sleep, clock=clock)
    assert waits == [] and clock() == 32
    assert len(client.taps) == 120 and client.frames == [0, 30]


def test_multimodal_completion_stops_clicking_when_result_template_is_missing():
    clock, waits = Clock(), []
    client = Client(clock)
    model = CompletionModel({'finished': True, 'page': 'finished', 'reason': '奖励弹窗'})
    result = run_battle(client, battle_config(), template_matcher=lambda *a: (None, .2),
                        sleep=waits.append, clock=clock, model=model)
    assert result.end_target == 'multimodal'
    assert model.calls == 1
    assert client.taps == []


def test_multimodal_completion_is_bounded_by_total_timeout_when_pending():
    clock, waits = Clock(), []
    client = Client(clock)
    model = CompletionModel({'finished': False, 'page': 'battle', 'reason': '仍在战斗'})
    action = battle_config()
    action['timeout_seconds'] = 65
    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)
    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, action, template_matcher=lambda *a: (None, .2),
                   sleep=sleep, clock=clock, model=model)
    assert model.calls == 3 and client.frames == [0, 30, 60]
    assert clock() == 65 and len(client.taps) == 260
    assert waits == []


@pytest.mark.parametrize('check_seconds', [0, 2, 8])
@pytest.mark.parametrize('check_interval', [18, 30])
def test_checks_stop_clicks_then_immediately_start_full_window(check_seconds, check_interval):
    clock, waits, tap_times = Clock(), [], []
    client = Client(clock)
    original_tap = client.tap
    first_check = 7 + check_interval
    resumed_at = first_check + check_seconds
    final_check = resumed_at + check_interval
    window_taps = 4 * check_interval

    def tap(x, y):
        tap_times.append(clock())
        original_tap(x, y)

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    class Model:
        calls = 0
        def complete_json(self, *args, **kwargs):
            self.calls += 1
            before = len(client.taps)
            # 入场检查耗时不占第一个连点窗口；周期检查从停止点击时立即开始。
            if self.calls == 1:
                clock.advance(7)
            elif self.calls == 2:
                assert clock() == first_check and before == window_taps
                clock.advance(check_seconds)
            assert len(client.taps) == before
            finished = self.calls == 3
            return {'finished': finished, 'page': 'finished' if finished else 'battle', 'reason': '测试'}

    client.tap = tap
    result = run_battle(client, dict(battle_config(), timeout_seconds=110),
                        template_matcher=lambda *args: (None, .2), sleep=sleep, clock=clock, model=Model(),
                        check_interval_seconds=check_interval)
    assert client.frames == [0, first_check, final_check]
    assert waits == [] and result.taps == 2 * window_taps
    assert not any(first_check <= t < resumed_at for t in tap_times)
    assert min(tap_times) == 7
    assert min(t for t in tap_times if t >= first_check) == resumed_at
    assert sum(7 <= t < first_check for t in tap_times) == sum(resumed_at <= t < final_check for t in tap_times) == window_taps


def test_valid_battle_checks_can_continue_past_six_calls_until_completion():
    clock = Clock()
    client = Client(clock)

    class Model:
        config = {'max_calls': 6}
        calls = 0
        def complete_json(self, *args, **kwargs):
            self.calls += 1
            finished = self.calls == 8
            return {'finished': finished, 'page': 'finished' if finished else 'battle', 'reason': '测试'}

    model = Model()
    result = run_battle(client, dict(battle_config(), timeout_seconds=300),
                        template_matcher=lambda *args: (None, .2), sleep=clock.advance, clock=clock, model=model)
    assert model.calls == 8 and result.end_target == 'multimodal'
    assert client.frames == [0, 30, 60, 90, 120, 150, 180, 210]


@pytest.mark.parametrize('finished', [False, True])
def test_model_response_after_timeout_cannot_resume_clicking_or_report_success(finished):
    clock = Clock()
    client = Client(clock)
    budgets = []

    class Model:
        def complete_json(self, *args, timeout_seconds, refresh_image=None):
            budgets.append(timeout_seconds)
            if clock() == 0:
                return {'finished': False, 'page': 'battle', 'reason': '测试'}
            clock.advance(6)
            return {'finished': finished, 'page': 'finished' if finished else 'battle', 'reason': '测试'}

    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, dict(battle_config(), timeout_seconds=35),
                   template_matcher=lambda *args: (None, .2), sleep=clock.advance, clock=clock, model=Model())
    assert budgets == [35, 5] and client.frames == [0, 30]
    assert len(client.taps) == 120


def test_invalid_model_response_still_stops_all_clicks():
    clock = Clock()
    client = Client(clock)
    model = CompletionModel({'finished': 'false', 'page': 'battle', 'reason': '无效格式'})
    with pytest.raises(VisualError, match='返回格式无效'):
        run_battle(client, battle_config(), template_matcher=lambda *args: (None, .2),
                   sleep=clock.advance, clock=clock, model=model)
    assert client.taps == [] and model.calls == 1
