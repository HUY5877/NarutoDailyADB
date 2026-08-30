import logging
from pathlib import Path

import pytest

from naruto_daily.adb_client import AdbError
from naruto_daily.runner import ActionLimitError, ActionRunner
from naruto_daily.visual import VisualError


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


def test_runner_accepts_combined_daily_flow_under_six_hundred_seconds():
    summary = ActionRunner(RecordingClient(), sleep=lambda _: None, dry_run=True).run([
        {"type": "wait", "seconds": 250},
        {"type": "wait", "seconds": 251},
    ])

    assert summary.wait_seconds == 501


def test_runner_rejects_more_than_six_hundred_seconds():
    with pytest.raises(ActionLimitError, match="600 秒"):
        ActionRunner(RecordingClient(), sleep=lambda _: None).run([
            {"type": "wait", "seconds": 300},
            {"type": "wait", "seconds": 300},
            {"type": "wait", "seconds": 1},
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


def required_action():
    return {
        "type": "required_template_tap",
        "name": "开始游戏按钮",
        "template": "start.png",
        "left": 650, "top": 780, "right": 1270, "bottom": 1020,
        "threshold": 0.85,
        "timeout_seconds": 3,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
    }


def test_required_template_tap_waits_until_button_appears(caplog):
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    results = iter([
        (None, 0.20),
        (None, 0.40),
        ((961, 910), 0.96),
        ((961, 910), 0.97),
    ])
    waits = []
    runner = ActionRunner(
        client,
        sleep=waits.append,
        template_matcher=lambda *args: next(results),
        asset_root=Path("assets"),
    )

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        runner.run([required_action()])

    assert client.calls == [("tap", 961, 910)]
    assert waits == [1.0, 1.0, 3.0, 3.0]
    assert "等待开始游戏按钮" in caplog.text
    assert "发现开始游戏按钮" in caplog.text
    assert "点击前等待 3.00 秒" in caplog.text
    assert "点击完成，等待 3.00 秒" in caplog.text


def test_required_template_tap_times_out_and_stops_following_actions():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    runner = ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: (None, 0.20),
    )

    with pytest.raises(VisualError, match="等待开始游戏按钮超时"):
        runner.run([required_action(), {"type": "tap", "x": 1, "y": 2}])

    assert client.calls == []


def test_old_required_action_uses_three_second_defaults():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    action = required_action()
    action.pop("pre_click_wait_seconds")
    action.pop("post_click_wait_seconds")
    waits = []

    ActionRunner(
        client,
        sleep=waits.append,
        template_matcher=lambda *args: ((961, 910), 0.99),
    ).run([action])

    assert client.calls == [("tap", 961, 910)]
    assert waits == [3.0, 3.0]


def test_repeated_unstable_required_button_respects_timeout():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    checks = 0
    waits = []

    def matcher(*args):
        nonlocal checks
        checks += 1
        if checks % 2 == 1:
            return (961, 910), 0.99
        return None, 0.20

    with pytest.raises(VisualError, match="超时"):
        ActionRunner(client, sleep=waits.append, template_matcher=matcher).run([required_action()])

    assert client.calls == []
    assert waits == [3.0]


def test_action_runner_dispatches_optional_second_authorization():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    action = {
        "type": "finish_login_authorization",
        "timeout_seconds": 4,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "second_authorization": {
            "name": "第二次授权同意按钮", "template": "second.png",
            "left": 700, "top": 700, "right": 1220, "bottom": 930,
            "threshold": 0.85,
        },
        "start_game": {
            "name": "开始游戏按钮", "template": "start.png",
            "left": 650, "top": 780, "right": 1270, "bottom": 1020,
            "threshold": 0.85,
        },
    }

    ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: ((956, 911), 0.99),
    ).run([action])

    assert client.calls == [("tap", 956, 911)]


def test_action_runner_dispatches_card_navigation():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    action = {
        "type": "find_card_and_click", "name": "组织祈福", "template": "organization.png",
        "left": 0, "top": 0, "right": 1920, "bottom": 1080, "threshold": 0.85,
        "button_template": "go_now.png", "button_threshold": 0.85,
        "completed_template": "completed.png", "completed_threshold": 0.90,
        "button_left_offset": -140, "button_top_offset": 80,
        "button_right_offset": 140, "button_bottom_offset": 260, "max_swipes": 6,
        "swipe_x1": 1650, "swipe_y1": 550, "swipe_x2": 350, "swipe_y2": 550,
        "swipe_duration_ms": 500, "wait_seconds": 1,
    }
    iter_results = iter([
        ((562, 394), 0.99), (None, 0.20),
        ((548, 585), 0.98), ((549, 586), 0.99),
    ])

    ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: next(iter_results),
    ).run([action])

    assert client.calls == [("tap", 549, 586)]


def test_completed_card_skips_only_prayer_handler_and_keeps_later_tasks(caplog):
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    action = {
        "type": "find_card_and_click", "name": "组织祈福", "template": "organization.png",
        "left": 0, "top": 0, "right": 1920, "bottom": 1080, "threshold": 0.85,
        "button_template": "go_now.png", "button_threshold": 0.85,
        "completed_template": "completed.png", "completed_threshold": 0.90,
        "button_left_offset": -140, "button_top_offset": 80,
        "button_right_offset": 140, "button_bottom_offset": 260, "max_swipes": 6,
        "swipe_x1": 1650, "swipe_y1": 550, "swipe_x2": 350, "swipe_y2": 550,
        "swipe_duration_ms": 500, "wait_seconds": 1,
    }
    results = iter([((1185, 424), 0.99), ((1185, 590), 0.98)])

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=lambda _: None,
            template_matcher=lambda *args: next(results),
        ).run([
            action,
            {"type": "handle_organization_prayer"},
            {"type": "tap", "x": 1, "y": 2},
        ])

    assert client.calls == [("tap", 1, 2)]
    assert "组织祈福今日已完成，跳过对应页面处理" in caplog.text


def test_action_runner_dispatches_organization_prayer():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    action = {
        "type": "handle_organization_prayer",
        "timeout_seconds": 20, "poll_seconds": 1, "wait_seconds": 1,
        "max_claims": 3, "dismiss_x": 1500, "dismiss_y": 200,
        "yesterday_reward": {
            "name": "昨日奖励", "template": "yesterday.png",
            "left": 500, "top": 150, "right": 850, "bottom": 380, "threshold": 0.85,
        },
        "panel_marker": {
            "name": "昨日祈福奖励面板", "template": "panel.png",
            "left": 400, "top": 50, "right": 1550, "bottom": 350, "threshold": 0.95,
        },
        "claim_button": {
            "name": "可领取奖励", "template": "claim.png",
            "left": 400, "top": 100, "right": 1550, "bottom": 980, "threshold": 0.85,
        },
        "coin_prayer": {
            "name": "6000铜币焚香祈福", "template": "coin.png",
            "left": 650, "top": 750, "right": 1100, "bottom": 1020, "threshold": 0.85,
        },
    }
    results = iter([((866, 875), 0.99), (None, 0.20), ((866, 875), 0.99)])

    ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: next(results),
    ).run([action])

    assert client.calls == [("tap", 866, 875)]


def test_action_runner_dispatches_return_to_main():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    action = {
        "type": "return_to_main",
        "max_rounds": 10, "idle_rounds": 2, "poll_seconds": 1,
        "pre_click_wait_seconds": 3, "post_click_wait_seconds": 3,
        "close_target": {
            "name": "通用关闭按钮", "template": "close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.78,
        },
    }
    results = iter([
        ((1834, 67), 0.92), ((1835, 68), 0.94),
        (None, 0.30), (None, 0.25),
    ])

    ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: next(results),
    ).run([action])

    assert client.calls == [("tap", 1835, 68)]


def test_action_runner_dispatches_two_free_fortune_clicks():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    action = {
        "type": "handle_coin_fortune",
        "clicks": 2, "timeout_seconds": 20, "poll_seconds": 1,
        "pre_click_wait_seconds": 3, "post_click_wait_seconds": 3,
        "free_button": {
            "name": "免费一次", "template": "free.png",
            "left": 1000, "top": 700, "right": 1450, "bottom": 1050,
            "threshold": 0.85,
        },
    }
    results = iter([
        ((1260, 850), 0.92), ((1262, 851), 0.95),
        ((1261, 850), 0.93), ((1263, 852), 0.96),
    ])

    ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: next(results),
    ).run([action])

    assert client.calls == [("tap", 1262, 851), ("tap", 1263, 852)]


def test_completed_fortune_card_skips_only_fortune_handler():
    client = RecordingClient()
    client.screenshot = lambda: b"screen"
    card = {
        "type": "find_card_and_click", "name": "进行1次金币招财", "template": "fortune.png",
        "left": 0, "top": 0, "right": 1920, "bottom": 1080, "threshold": 0.85,
        "button_template": "go_now.png", "button_threshold": 0.85,
        "completed_template": "completed.png", "completed_threshold": 0.90,
        "button_left_offset": -140, "button_top_offset": 80,
        "button_right_offset": 140, "button_bottom_offset": 260, "max_swipes": 6,
        "swipe_x1": 1650, "swipe_y1": 550, "swipe_x2": 350, "swipe_y2": 550,
        "swipe_duration_ms": 500, "wait_seconds": 1,
        "completed_skip_type": "handle_coin_fortune",
    }
    results = iter([((1185, 424), 0.99), ((1185, 590), 0.98)])

    ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: next(results),
    ).run([
        card,
        {"type": "handle_coin_fortune"},
        {"type": "tap", "x": 1, "y": 2},
    ])

    assert client.calls == [("tap", 1, 2)]
