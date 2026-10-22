"""Battle observations notify the parent without polling Redis or changing taps."""

import pytest

from naruto_daily.battle import run_battle
from naruto_daily.login_service.log_events import forward_task_logs
from test_battle import Client, Clock, battle_config


@pytest.mark.parametrize("check_interval", [30, 18])
def test_battle_emits_one_checkpoint_per_existing_check_before_its_screenshot(check_interval):
    clock = Clock()
    client = Client(clock)
    events, checkpoints = [], []

    def accept(event):
        events.append(event)
        if event.get("executionCheckpoint") is True:
            checkpoints.append((clock(), len(client.frames), len(client.taps)))

    def matcher(png, path, roi, threshold):
        return ((50, 50), 0.99) if clock() >= 2 * check_interval else (None, 0.2)

    with forward_task_logs(accept):
        summary = run_battle(
            client, dict(battle_config(), timeout_seconds=100),
            template_matcher=matcher, sleep=clock.advance, clock=clock,
            check_interval_seconds=check_interval,
        )

    assert client.frames == [0, check_interval, 2 * check_interval]
    assert checkpoints == [
        (0, 0, 0),
        (check_interval, 1, 4 * check_interval),
        (2 * check_interval, 2, 8 * check_interval),
    ]
    assert summary.taps == len(client.taps) == 8 * check_interval
    assert summary.end_target == "result"
    assert len(events) == 10
    assert all(event["type"] == "log" for event in events)
    checkpoint_positions = [i for i, event in enumerate(events) if event.get("executionCheckpoint") is True]
    assert len(checkpoint_positions) == 3
    for position in checkpoint_positions:
        assert "检查结算" in events[position]["message"]
    assert all("executionCheckpoint" not in event for event in events if "检查结算" not in event["message"])


def test_fast_tap_batches_do_not_emit_extra_checkpoints_or_take_extra_screenshots():
    clock = Clock()
    client = Client(clock)
    events, batch_times = [], []

    def batch(should_continue):
        assert should_continue()
        batch_times.append(clock())
        clock.advance(1)
        return True

    def detector(png):
        return "结算" if clock() >= 60 else None

    with forward_task_logs(events.append):
        summary = run_battle(
            client, dict(battle_config(), end_targets=[]),
            sleep=clock.advance, clock=clock, end_detector=detector,
            _tap_batch=batch,
        )

    assert len(events) == 7
    assert all(event["type"] == "log" for event in events)
    checkpoints = [event for event in events if event.get("executionCheckpoint") is True]
    assert len(checkpoints) == 3
    assert all("检查结算" in event["message"] for event in checkpoints)
    assert client.frames == [0, 30, 60]
    assert batch_times == list(range(60))
    assert summary.taps == 240 and summary.end_target == "结算"
    assert client.taps == []
