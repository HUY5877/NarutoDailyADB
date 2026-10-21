"""丰饶独立限速，检查暂停、已有慢速配置及其他战斗速度保持不变。"""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from naruto_daily.battle import run_battle, run_battle_without_first_skill, run_duel_battle
from naruto_daily.common.visual import VisualError
from test_battle import Clock, Client, battle_config


class TickClock(Clock):
    def advance(self, seconds):
        with self.lock:
            self.now = round(self.now + seconds, 9)


@pytest.mark.parametrize('runner,configured_interval,expected_starts', [
    (run_battle_without_first_skill, 0, [0, .2, .4, .6, .8]),
    (run_battle_without_first_skill, .5, [0, .5]),
    (run_battle, 0, [i / 20 for i in range(20)]),
    (run_duel_battle, 0, [i / 20 for i in range(20)]),
])
def test_abundance_limits_each_button_to_five_batches_per_second(
        runner, configured_interval, expected_starts):
    clock = TickClock()
    client = Client(clock)
    client.screen_size = lambda: (1920, 1080)
    starts, connections, closed = [], [], []

    @contextmanager
    def battle_taps(points):
        connections.append(tuple(points))

        def tap(can_send):
            assert can_send()
            starts.append(clock())
            clock.advance(.01)
            return True

        try:
            yield tap
        finally:
            closed.append(True)

    client.battle_taps = battle_taps
    config = dict(battle_config(), tap_interval_seconds=configured_interval)
    original = deepcopy(config)
    options = {'prefer_fast_taps': True} if runner is run_battle else {}
    summary = runner(client, config, clock=clock, sleep=clock.advance,
                     check_interval_seconds=1,
                     template_matcher=lambda png, *a: ((50, 50), .99) if png == b'3' else (None, .2),
                     **options)

    # 连续两个检查窗口各自限速；暂停后复用同一连接，结束后仅关闭一次。
    assert starts == pytest.approx(expected_starts + [1 + t for t in expected_starts])
    assert client.frames == pytest.approx([0, 1, 2])
    assert len(connections) == len(closed) == 1
    assert client.taps == [] and summary.taps == len(starts) * len(connections[0])
    assert config == original
    if runner is run_battle_without_first_skill:
        assert connections == [((2, 2), (3, 3), (4, 4))]


def test_abundance_fallback_is_slowed_without_delaying_the_next_check():
    clock = TickClock()
    client = Client(clock, tap_seconds=.01)
    waits = []

    @contextmanager
    def unsupported(points):
        yield None

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    client.battle_taps = unsupported
    summary = run_battle_without_first_skill(
        client, battle_config(), clock=clock, sleep=sleep, check_interval_seconds=1,
        template_matcher=lambda png, *a: ((50, 50), .99) if png == b'2' else (None, .2),
    )
    assert summary.taps == 15
    assert client.frames == pytest.approx([0, 1])
    assert waits == pytest.approx([.2, .2, .2, .2, .05])
    assert set(client.taps) == {(2, 2), (3, 3), (4, 4)}


def test_abundance_rate_wait_stops_at_timeout_and_closes_connection():
    clock = TickClock()
    client = Client(clock)
    starts, closed = [], []

    @contextmanager
    def battle_taps(points):
        def tap(can_send):
            assert can_send()
            starts.append(clock())
            clock.advance(.01)
            return True
        try:
            yield tap
        finally:
            closed.append(True)

    client.battle_taps = battle_taps
    with pytest.raises(VisualError, match='超时'):
        run_battle_without_first_skill(
            client, dict(battle_config(), timeout_seconds=.13),
            clock=clock, sleep=clock.advance, template_matcher=lambda *a: (None, .2),
        )
    assert starts == [0] and closed == [True]
    assert clock() == pytest.approx(.13)
