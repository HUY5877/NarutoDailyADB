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


def test_battle_repeats_four_buttons_and_stops_before_any_result_page_click():
    client, waits = Client(), []
    def matcher(png, path, roi, threshold):
        visible = 'result' if len(client.taps) == 9 else 'battle'
        return ((50, 50), 0.99) if path.stem == visible else (None, 0.2)
    summary = run_battle(client, battle_config(), template_matcher=matcher, sleep=waits.append)
    assert client.taps == [(1, 1), (2, 2), (3, 3), (4, 4)] * 2 + [(1, 1)]
    assert summary.taps == 9 and summary.end_target == 'result'
    assert waits == [0.2] * 9


def test_unknown_loading_screen_does_not_receive_attack_input():
    client, waits = Client(), []
    def matcher(png, path, roi, threshold):
        frame = int(png)
        visible = 'battle' if frame in (1, 4) else ('result' if frame >= 5 else None)
        return ((50, 50), 0.99) if path.stem == visible else (None, 0.2)
    result = run_battle(client, battle_config(), template_matcher=matcher, sleep=waits.append)
    assert client.taps == [(1, 1)]
    assert result.end_target == 'result'
    assert waits[:2] == [1, 1]


def test_battle_must_be_seen_before_accepting_result():
    client = Client()
    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, battle_config(), template_matcher=lambda *args: (None, 0.2), sleep=lambda _: None)
    assert client.taps == []


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
    ticks = iter([0, 0, 0, 0, 0.9, 1.1])

    def sleep(seconds):
        if seconds < 0:
            raise ValueError('sleep length must be non-negative')
        waits.append(seconds)

    def matcher(png, path, roi, threshold):
        return ((50, 50), 0.99) if path.stem == 'battle' else (None, 0.2)

    with pytest.raises(VisualError, match='战斗.*超时'):
        run_battle(client, action, template_matcher=matcher, sleep=sleep,
                   clock=lambda: next(ticks, 1.1))
    assert client.taps == [(1, 1)]
    assert all(seconds >= 0 for seconds in waits)
