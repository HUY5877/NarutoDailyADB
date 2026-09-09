from pathlib import Path

import pytest

from naruto_daily.battle import run_battle
from naruto_daily.common.visual import VisualError


def target(name):
    return dict(name=name, template=name + '.png', left=0, top=0, right=100, bottom=100, threshold=0.85)


def battle_config():
    return dict(start_timeout_seconds=3, timeout_seconds=10, tap_interval_seconds=0.2,
                battle_marker=target('battle'), end_targets=[target('result')],
                buttons=[dict(name=name, x=i, y=i) for i, name in enumerate(('一技能', '二技能', '大招', '普攻'), 1)])


class Client:
    def __init__(self):
        self.taps = []
        self.frames = 0

    def screenshot(self):
        self.frames += 1
        return str(self.frames).encode()

    def tap(self, x, y):
        self.taps.append((x, y))


def test_battle_clicks_twelve_buttons_per_screenshot_and_stops_on_result():
    client, waits = Client(), []
    def matcher(png, path, roi, threshold):
        visible = 'result' if len(client.taps) >= 12 else 'battle'
        return ((50, 50), 0.99) if path.stem == visible else (None, 0.2)
    summary = run_battle(client, battle_config(), template_matcher=matcher, sleep=waits.append)
    assert client.taps == [(1, 1), (2, 2), (3, 3), (4, 4)] * 3
    assert summary.taps == 12 and summary.end_target == 'result'
    assert waits == [0.2] * 12
    assert client.frames == 2


def test_missing_pause_button_does_not_delay_attacks_or_retry_each_frame():
    client, waits = Client(), []
    def matcher(png, path, roi, threshold):
        frame = int(png)
        visible = 'result' if frame >= 5 else None
        return ((50, 50), 0.99) if path.stem == visible else (None, 0.2)
    result = run_battle(client, battle_config(), template_matcher=matcher, sleep=waits.append)
    assert client.taps == [(1, 1), (2, 2), (3, 3), (4, 4)] * 12
    assert result.end_target == 'result'
    assert waits == [0.2] * 48
    assert client.frames == 5


def test_result_visible_on_entry_stops_without_waiting_for_pause_button():
    client, waits = Client(), []
    def matcher(png, path, roi, threshold):
        return ((50, 50), 0.99) if path.stem == 'result' else (None, 0.2)
    result = run_battle(client, battle_config(), template_matcher=matcher, sleep=waits.append)
    assert result.end_target == 'result'
    assert client.taps == []
    assert waits == []


def test_battle_timeout_stops_inputs_without_claiming_completion():
    client, waits = Client(), []
    action = battle_config()
    action['timeout_seconds'] = 1
    def matcher(png, path, roi, threshold):
        return ((50, 50), 0.99) if path.stem == 'battle' else (None, 0.2)
    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, action, template_matcher=matcher, sleep=waits.append)
    assert len(client.taps) == 5 and sum(waits) == pytest.approx(1)


def test_deadline_crossing_during_sleep_calculation_uses_normal_timeout():
    client, waits = Client(), []
    action = battle_config()
    action['timeout_seconds'] = 1
    now = [0.0]
    original_tap = client.tap
    def slow_tap(x, y):
        original_tap(x, y)
        now[0] = 1.1
    client.tap = slow_tap

    def sleep(seconds):
        if seconds < 0:
            raise ValueError('sleep length must be non-negative')
        waits.append(seconds)

    def matcher(png, path, roi, threshold):
        return ((50, 50), 0.99) if path.stem == 'battle' else (None, 0.2)

    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, action, template_matcher=matcher, sleep=sleep,
                   clock=lambda: now[0])
    assert client.taps == [(1, 1)]
    assert all(seconds >= 0 for seconds in waits)


def test_screenshot_exceeding_battle_timeout_does_not_receive_attack():
    client = Client()
    now = [0.0]
    def matcher(*args):
        now[0] = 11.0
        return None, 0.2
    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, battle_config(), template_matcher=matcher,
                   sleep=lambda _: None, clock=lambda: now[0])
    assert client.taps == []
