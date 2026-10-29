import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto_daily.common import main_screen
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.visual import VisualError
from naruto_daily.runner import ActionRunner


ROOT = Path(__file__).parents[1]


def run_entry(monkeypatch, after_click):
    main = (ROOT / 'tests/fixtures/main_rewards_right_hud.png').read_bytes()
    rewards = (ROOT / 'tests/fixtures/reward_cards_stuck.png').read_bytes()
    response = dict(page='main', target='rewards', found=True, candidate_id=7)
    monkeypatch.setattr(main_screen, 'MultimodalClient',
                        lambda *a, **kw: SimpleNamespace(complete_json=lambda *a, **kw: response))
    frames, taps, delays = [], [], []

    def screenshot():
        if not taps:
            return main
        frames.append(1)
        return rewards if after_click(len(frames)) else main

    client = SimpleNamespace(screenshot=screenshot, tap=lambda *xy: taps.append(xy))
    monkeypatch.setattr('naruto_daily.runner.handle_free_recruit',
                        lambda device, action, **kwargs: device.tap(100, 200))
    entry = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))['tasks']['recruit'][1]
    runner = ActionRunner(client, asset_root=ROOT, sleep=delays.append)
    return runner, [entry, dict(type='handle_free_recruit')], taps, frames, delays


def test_does_not_advance_when_entry_click_never_opens_reward_cards(monkeypatch):
    runner, actions, taps, frames, delays = run_entry(monkeypatch, lambda _: False)
    with pytest.raises(VisualError, match='未确认奖励任务页'):
        runner.run(actions)
    assert taps == [(1845, 537)]
    assert len(frames) > 1
    assert sum(delays) < 20


def test_waits_for_loading_before_advancing_to_next_action(monkeypatch):
    runner, actions, taps, frames, _ = run_entry(monkeypatch, lambda count: count >= 4)
    runner.run(actions)
    assert len(frames) == 4
    assert taps == [(1845, 537), (100, 200)]


def test_ready_reward_cards_advance_without_extra_input(monkeypatch):
    runner, actions, taps, frames, _ = run_entry(monkeypatch, lambda _: True)
    runner.run(actions)
    assert len(frames) == 1
    assert taps == [(1845, 537), (100, 200)]


def test_late_destination_screenshot_does_not_advance(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main_screen.time, 'monotonic', lambda: now[0])

    def slow_screenshot(_):
        now[0] = 100
        return True

    runner, actions, taps, _, _ = run_entry(monkeypatch, slow_screenshot)
    with pytest.raises(VisualError, match='未确认奖励任务页'):
        runner.run(actions)
    assert taps == [(1845, 537)]


def test_wrong_destination_keeps_local_cap_under_daily_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main_screen.time, 'monotonic', lambda: now[0])
    runner, actions, taps, frames, _ = run_entry(monkeypatch, lambda _: False)
    with execution_scope(3000, clock=lambda: now[0],
                         sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)):
        with pytest.raises(VisualError, match='未确认奖励任务页'):
            runner.run(actions)
    assert taps == [(1845, 537)] and frames
    assert now[0] < 20


def test_daily_deadline_stops_destination_capture_and_later_actions(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main_screen.time, 'monotonic', lambda: now[0])

    def expired_screenshot(_):
        now[0] = 6
        return True

    runner, actions, taps, _, _ = run_entry(monkeypatch, expired_screenshot)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(5, clock=lambda: now[0],
                             sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            runner.run(actions)
    assert taps == [(1845, 537)]
