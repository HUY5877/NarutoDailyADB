from collections import Counter
from threading import Barrier, Lock

import pytest

from naruto_daily.battle import run_battle
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
    (.25, [0, 20, 40]),
    (.75, [0, 21, 42]),
])
def test_result_checks_use_twenty_seconds_of_elapsed_adb_time(tap_seconds, expected_frames):
    clock, waits = Clock(), []
    client = Client(clock, tap_seconds)
    def matcher(png, path, roi, threshold):
        assert path.stem == 'result', '战斗期间只识别结束标记'
        # 结果在25秒出现，但只能在下一次到期检查时发现。
        return ((50, 50), .99) if clock() >= 25 else (None, .2)
    result = run_battle(client, battle_config(), template_matcher=matcher,
                        sleep=waits.append, clock=clock)
    assert client.frames == expected_frames
    assert result.taps == len(client.taps) and result.taps > 12
    assert len(set(Counter(client.taps).values())) == 1
    assert set(client.taps) == {(1, 1), (2, 2), (3, 3), (4, 4)}
    assert waits == [] and result.end_target == 'result'


def test_four_buttons_run_concurrently_and_finish_before_result_check():
    clock, waits = Clock(), []
    client = Client(clock)
    barrier = Barrier(4, timeout=3)
    def tap(x, y):
        client.taps.append((x, y))
        if barrier.wait() == 0:
            clock.advance(20)
    client.tap = tap
    def matcher(png, *args):
        return ((50, 50), .99) if int(png) == 2 else (None, .2)
    result = run_battle(client, battle_config(), template_matcher=matcher,
                        sleep=waits.append, clock=clock)
    assert result.taps == 4 and sorted(client.taps) == [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert client.frames == [0, 20] and waits == []


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
    assert result.taps == 40 and len(client.frames) == 2
    assert waits == [2] * 10


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
    def complete_json(self, png, prompt, *, timeout_seconds=None):
        self.calls += 1
        return self.result


def test_periodic_pauses_stop_all_taps_for_five_seconds_and_resume_at_same_speed():
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
    result = run_battle(client, dict(battle_config(), timeout_seconds=100),
                        template_matcher=lambda *args: ((50, 50), .99) if clock() >= 70 else (None, .2),
                        sleep=sleep, clock=clock, pause_every_seconds=30, pause_seconds=5)
    assert pauses == [(30, 5, 120), (65, 5, 240)]
    assert client.frames == [0, 20, 35, 55, 70]
    assert result.taps == 240
    assert sum(t < 30 for t in tap_times) == sum(35 <= t < 65 for t in tap_times) == 120
    assert not any(30 <= t < 35 or 65 <= t < 70 for t in tap_times)


@pytest.mark.parametrize('use_model', [False, True])
def test_result_found_after_pause_never_resumes_taps(use_model):
    clock, waits = Clock(), []
    client = Client(clock)

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    class ResultModel:
        config = {'max_calls': 3}

        def complete_json(self, *args, **kwargs):
            finished = clock() >= 35
            return {'finished': finished, 'page': 'finished' if finished else 'battle',
                    'reason': '战后弹窗' if finished else '战斗中'}

    def matcher(*args):
        return ((50, 50), .99) if not use_model and clock() >= 35 else (None, .2)

    result = run_battle(client, battle_config(), template_matcher=matcher,
                        sleep=sleep, clock=clock, model=ResultModel() if use_model else None,
                        pause_every_seconds=30, pause_seconds=5)
    assert waits == [5]
    assert client.frames == [0, 20, 35]
    assert len(client.taps) == result.taps == 120
    assert result.end_target == ('multimodal' if use_model else 'result')


def test_pause_waits_for_all_parallel_inputs_to_finish():
    clock, active, lock = Clock(), [0], Lock()
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
        assert active[0] == 0 and len(client.taps) == 4
        assert seconds == 5
        clock.advance(seconds)

    client.tap = tap
    result = run_battle(client, battle_config(),
                        template_matcher=lambda *args: ((50, 50), .99) if clock() >= 35 else (None, .2),
                        sleep=sleep, clock=clock, pause_every_seconds=30, pause_seconds=5)
    assert result.taps == 4 and client.frames == [0, 35]


def test_pause_exhausting_total_timeout_never_resumes_or_checks_late_frame():
    clock, waits = Clock(), []
    client = Client(clock)

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, dict(battle_config(), timeout_seconds=32),
                   template_matcher=lambda *a: (None, .2), sleep=sleep, clock=clock,
                   pause_every_seconds=30, pause_seconds=5)
    assert waits == [2] and clock() == 32
    assert len(client.taps) == 120 and client.frames == [0, 20]


def test_multimodal_completion_stops_clicking_when_result_template_is_missing():
    clock, waits = Clock(), []
    client = Client(clock)
    model = CompletionModel({'finished': True, 'page': 'finished', 'reason': '奖励弹窗'})
    result = run_battle(client, battle_config(), template_matcher=lambda *a: (None, .2),
                        sleep=waits.append, clock=clock, model=model)
    assert result.end_target == 'multimodal'
    assert model.calls == 1
    assert client.taps == []


def test_multimodal_completion_keeps_battle_clicking_when_pending():
    clock, waits = Clock(), []
    client = Client(clock)
    model = CompletionModel({'finished': False, 'page': 'battle', 'reason': '仍在战斗'})
    action = battle_config()
    action['timeout_seconds'] = 65
    with pytest.raises(VisualError, match='战斗完成识别达到模型调用上限'):
        run_battle(client, action, template_matcher=lambda *a: (None, .2),
                   sleep=waits.append, clock=clock, model=model)
    assert model.calls == 3
    assert len(client.taps) > 0
