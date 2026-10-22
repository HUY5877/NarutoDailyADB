"""丰饶恢复普通点击及配置节奏，其他战斗继续使用高速通道。"""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from naruto_daily.battle import run_battle, run_battle_without_first_skill, run_duel_battle
from test_battle import Clock, Client, battle_config


class TickClock(Clock):
    def advance(self, seconds):
        with self.lock:
            self.now = round(self.now + seconds, 9)


@pytest.mark.parametrize('runner,configured_interval,expected_starts', [
    (run_battle, 0, [i / 20 for i in range(20)]),
    (run_battle, .5, [0, .5]),
    (run_duel_battle, 0, [i / 20 for i in range(20)]),
    (run_duel_battle, .5, [0, .5]),
])
def test_other_battles_keep_fast_channel_and_configured_interval(
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


@pytest.mark.parametrize('supports_fast_channel', [False, True])
@pytest.mark.parametrize('configured_interval,expected_taps,expected_waits,expected_check', [
    (0, 102, [], 1.02),
    (.5, 6, [.5, .44], 1),
])
def test_abundance_uses_ordinary_taps_without_extra_rate_limit(
        supports_fast_channel, configured_interval, expected_taps, expected_waits, expected_check):
    clock = TickClock()
    client = Client(clock, tap_seconds=.01)
    waits = []

    def forbidden_channel(points):
        pytest.fail('丰饶不应尝试打开高速点击通道')

    if supports_fast_channel:
        client.battle_taps = client.duel_taps = forbidden_channel

    def sleep(seconds):
        waits.append(seconds)
        clock.advance(seconds)

    config = dict(battle_config(), tap_interval_seconds=configured_interval)
    original = deepcopy(config)
    summary = run_battle_without_first_skill(
        client, config, clock=clock, sleep=sleep, check_interval_seconds=1,
        template_matcher=lambda png, *a: ((50, 50), .99) if png == b'2' else (None, .2),
    )
    assert summary.taps == len(client.taps) == expected_taps
    assert client.frames == pytest.approx([0, expected_check])
    assert waits == pytest.approx(expected_waits)
    assert set(client.taps) == {(2, 2), (3, 3), (4, 4)}
    assert config == original
