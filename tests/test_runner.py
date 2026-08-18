import logging
from pathlib import Path

import pytest

from naruto_daily.adb_client import AdbError
from naruto_daily.runner import ActionLimitError, ActionRunner


class RecordingClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def _record(self, name, *args):
        if name == self.fail_on:
            raise AdbError("lost device")
        self.calls.append((name, *args))

    def tap(self, x, y): self._record("tap", x, y)
    def swipe(self, x1, y1, x2, y2, duration_ms): self._record("swipe", x1, y1, x2, y2, duration_ms)
    def back(self): self._record("back")


def test_runner_executes_actions_in_order_and_reports_summary():
    client = RecordingClient()
    sleeps = []
    runner = ActionRunner(client, sleep=sleeps.append)
    actions = [
        {"type": "tap", "x": 10, "y": 20},
        {"type": "wait", "seconds": 1.25},
        {"type": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 500},
        {"type": "back"},
    ]

    summary = runner.run(actions)

    assert client.calls == [("tap", 10, 20), ("swipe", 1, 2, 3, 4, 500), ("back",)]
    assert sleeps == [1.25]
    assert (summary.action_count, summary.wait_seconds) == (4, 1.25)


def test_dry_run_suppresses_input_and_sleep():
    client = RecordingClient()
    sleeps = []
    summary = ActionRunner(client, sleep=sleeps.append, dry_run=True).run([
        {"type": "tap", "x": 10, "y": 20},
        {"type": "wait", "seconds": 2},
        {"type": "back"},
    ])

    assert client.calls == []
    assert sleeps == []
    assert (summary.action_count, summary.wait_seconds) == (3, 2.0)


def test_adb_failure_aborts_remaining_actions():
    client = RecordingClient(fail_on="tap")
    runner = ActionRunner(client, sleep=lambda _: None)

    with pytest.raises(AdbError, match="lost device"):
        runner.run([{"type": "tap", "x": 1, "y": 2}, {"type": "back"}])

    assert client.calls == []


def test_runner_rejects_more_than_three_hundred_seconds():
    with pytest.raises(ActionLimitError, match="300 秒"):
        ActionRunner(RecordingClient(), sleep=lambda _: None).run([
            {"type": "wait", "seconds": 200},
            {"type": "wait", "seconds": 101},
        ])


def test_optional_template_tap_clicks_only_when_match_is_found(caplog):
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    matches = iter([((957, 906), 0.94), (None, 0.42)])

    def matcher(png, template_path, roi, threshold):
        assert png == b"screen"
        assert template_path == Path("assets") / "claim_button.png"
        assert roi == (780, 820, 1140, 1000)
        assert threshold == 0.85
        return next(matches)

    action = {
        "type": "optional_template_tap",
        "template": "claim_button.png",
        "left": 780, "top": 820, "right": 1140, "bottom": 1000,
        "threshold": 0.85,
    }
    runner = ActionRunner(client, sleep=lambda _: None, template_matcher=matcher, asset_root=Path("assets"))

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        runner.run([action, action])

    assert client.calls == [("tap", 957, 906)]
    assert "0.940" in caplog.text
    assert "0.420" in caplog.text
    assert "跳过" in caplog.text
