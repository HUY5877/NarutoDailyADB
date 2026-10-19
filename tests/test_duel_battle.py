"""固定六区 ADB 战斗的尺寸预检、并发批次与停止条件；不连接真实设备。"""

from collections import Counter
from copy import deepcopy
from threading import Barrier, Lock

import pytest

from naruto_daily.battle import BattleSummary, run_duel_battle
from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.visual import VisualError
from test_battle import Clock, Client, battle_config


EXPECTED_BUTTONS = [
    dict(name="秘卷", x=1730, y=445),
    dict(name="通灵", x=1730, y=270),
    dict(name="二技能", x=1530, y=750),
    dict(name="普攻", x=1715, y=885),
    dict(name="替身", x=1300, y=965),
    dict(name="大招", x=1730, y=650),
]
EXPECTED_POINTS = {(1730, 445), (1730, 270), (1530, 750), (1715, 885), (1300, 965), (1730, 650)}


class ScreenClient(Client):
    def __init__(self, clock, *, size=(1920, 1080)):
        super().__init__(clock)
        self.size = size
        self.screen_queries = 0

    def screen_size(self):
        self.screen_queries += 1
        return self.size


class SixWayClient(ScreenClient):
    def __init__(self, clock, *, batch_seconds=10, fail_at=None):
        super().__init__(clock)
        self.barrier = Barrier(6, timeout=3)
        self.state_lock = Lock()
        self.active = 0
        self.batch_seconds = batch_seconds
        self.fail_at = fail_at

    def tap(self, x, y):
        with self.state_lock:
            self.active += 1
            self.taps.append((x, y))
        try:
            # 串行、线程不足或漏掉任何固定区都会使这道屏障失败。
            if self.barrier.wait() == 0:
                self.clock.advance(self.batch_seconds)
            if (x, y) == self.fail_at:
                raise AdbError("固定区输入失败")
        finally:
            with self.state_lock:
                self.active -= 1

    def screenshot(self):
        with self.state_lock:
            assert self.active == 0, "截图前必须等待本批全部 ADB 点击完成"
        return super().screenshot()


@pytest.mark.parametrize("size", [(1280, 720), (1080, 1920), (1920, 1200), (1919, 1080)])
def test_wrong_resolution_stops_before_screenshots_or_taps(size):
    clock = Clock()
    client = ScreenClient(clock, size=size)

    with pytest.raises(VisualError, match="1920.*1080|分辨率"):
        run_duel_battle(client, battle_config(), clock=clock)

    assert client.screen_queries == 1
    assert client.frames == []
    assert client.taps == []


def test_duel_builds_a_separate_six_button_battle_and_forwards_all_loop_options(monkeypatch, tmp_path):
    import naruto_daily.battle.handler as handler

    clock = Clock()
    client = ScreenClient(clock)
    battle = battle_config()
    original = deepcopy(battle)
    kwargs = dict(
        template_matcher=lambda *args: (None, .2),
        asset_root=tmp_path,
        sleep=clock.advance,
        clock=clock,
        model=object(),
        check_interval_seconds=18,
        end_detector=lambda png: None,
        before_attack=lambda png: None,
    )
    returned = BattleSummary(0, "自定义结束")

    def shared_loop(actual_client, prepared, **options):
        assert actual_client is client
        assert client.screen_queries == 1
        assert prepared is not battle
        assert prepared["buttons"] == EXPECTED_BUTTONS
        assert options == kwargs
        assert prepared["timeout_seconds"] == original["timeout_seconds"]
        return returned

    monkeypatch.setattr(handler, "run_battle", shared_loop)

    assert run_duel_battle(client, battle, **kwargs) is returned
    assert battle == original
    assert client.frames == []
    assert client.taps == []


def test_all_six_fixed_regions_repeat_concurrently_and_finish_before_each_result_check():
    clock = Clock()
    client = SixWayClient(clock)
    battle = battle_config()
    original = deepcopy(battle)

    result = run_duel_battle(
        client, battle, clock=clock, sleep=clock.advance, check_interval_seconds=10,
        template_matcher=lambda png, *args: ((50, 50), .99) if png == b"4" else (None, .2),
    )

    assert result == BattleSummary(18, "result")
    assert client.screen_queries == 1
    assert client.frames == [0, 10, 20, 30]
    assert client.active == 0
    assert Counter(client.taps) == Counter({point: 3 for point in EXPECTED_POINTS})
    for start in (0, 6, 12):
        assert set(client.taps[start:start + 6]) == EXPECTED_POINTS
    assert battle == original


def test_result_visible_on_entry_returns_without_attacks_or_sleep():
    clock = Clock()
    client = ScreenClient(clock)
    waits = []

    result = run_duel_battle(
        client, battle_config(), clock=clock, sleep=waits.append,
        template_matcher=lambda *args: ((50, 50), .99),
    )

    assert result == BattleSummary(0, "result")
    assert client.screen_queries == 1
    assert client.frames == [0]
    assert client.taps == []
    assert waits == []


def test_entry_callback_is_forwarded_and_the_new_frame_is_checked_by_end_detector():
    clock = Clock()
    client = ScreenClient(clock)
    detected, prepared = [], []

    def detect(png):
        detected.append(png)
        return "准备确认后结束" if png == b"2" else None

    def before_attack(png):
        assert client.taps == []
        prepared.append(png)

    result = run_duel_battle(
        client, battle_config(), clock=clock, sleep=clock.advance,
        template_matcher=lambda *args: (None, .2),
        end_detector=detect, before_attack=before_attack,
    )

    assert result == BattleSummary(0, "准备确认后结束")
    assert prepared == [b"1"]
    assert detected == [b"1", b"2"]
    assert client.frames == [0, 0]
    assert client.taps == []


def test_before_attack_can_finish_the_duel_without_sending_any_input():
    clock = Clock()
    client = ScreenClient(clock)
    summary = BattleSummary(0, "入场已完成")

    result = run_duel_battle(
        client, battle_config(), clock=clock,
        template_matcher=lambda *args: (None, .2), before_attack=lambda png: summary,
    )

    assert result is summary
    assert client.frames == [0]
    assert client.taps == []


@pytest.mark.parametrize("finished", [False, True])
def test_unknown_model_state_keeps_all_six_regions_stopped_until_recheck(finished):
    clock = Clock()
    client = ScreenClient(clock)
    waits = []

    class Model:
        calls = 0

        def complete_json(self, png, *args, **kwargs):
            assert client.taps == []
            self.calls += 1
            if self.calls == 1:
                return dict(finished=finished, page="unknown", reason="尚未确认页面")
            return dict(finished=True, page="finished", reason="确认结算")

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    model = Model()
    result = run_duel_battle(
        client, battle_config(), clock=clock, sleep=sleep, model=model,
        template_matcher=lambda *args: (None, .2),
    )

    assert result == BattleSummary(0, "multimodal")
    assert model.calls == 2
    assert client.frames == [0, 1]
    assert client.taps == []
    assert waits == [1]


def test_timeout_during_one_six_way_batch_finishes_that_batch_without_starting_another():
    clock = Clock()
    client = SixWayClient(clock, batch_seconds=10)

    with pytest.raises(VisualError, match="战斗.*超时"):
        run_duel_battle(
            client, dict(battle_config(), timeout_seconds=5), clock=clock, sleep=clock.advance,
            template_matcher=lambda *args: (None, .2),
        )

    assert client.active == 0
    assert Counter(client.taps) == Counter(EXPECTED_POINTS)
    assert client.frames == [0]
    assert clock() == 10


def test_one_failed_parallel_region_stops_after_the_current_batch_without_retry():
    clock = Clock()
    client = SixWayClient(clock, fail_at=(1730, 445))

    with pytest.raises(AdbError, match="固定区输入失败"):
        run_duel_battle(
            client, battle_config(), clock=clock, sleep=clock.advance,
            template_matcher=lambda *args: (None, .2),
        )

    assert client.active == 0
    assert Counter(client.taps) == Counter(EXPECTED_POINTS)
    assert client.frames == [0]
