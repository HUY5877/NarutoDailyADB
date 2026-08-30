import logging
from pathlib import Path

from naruto_daily.prayer import handle_organization_prayer


class PrayerClient:
    def __init__(self):
        self.taps = []
        self.screenshot_count = 0

    def screenshot(self):
        self.screenshot_count += 1
        return f"画面{self.screenshot_count}".encode()

    def tap(self, x, y):
        self.taps.append((x, y))


def prayer_action():
    return {
        "type": "handle_organization_prayer",
        "timeout_seconds": 20,
        "poll_seconds": 1,
        "wait_seconds": 1,
        "max_claims": 3,
        "dismiss_x": 1500,
        "dismiss_y": 200,
        "yesterday_reward": {
            "name": "昨日奖励",
            "template": "yesterday_reward.png",
            "left": 500, "top": 150, "right": 850, "bottom": 380,
            "threshold": 0.85,
        },
        "panel_marker": {
            "name": "昨日祈福奖励面板",
            "template": "yesterday_panel.png",
            "left": 400, "top": 50, "right": 1550, "bottom": 350,
            "threshold": 0.95,
        },
        "claim_button": {
            "name": "可领取奖励",
            "template": "prayer_claim.png",
            "left": 400, "top": 100, "right": 1550, "bottom": 980,
            "threshold": 0.85,
        },
        "coin_prayer": {
            "name": "6000铜币焚香祈福",
            "template": "coin_prayer.png",
            "left": 650, "top": 750, "right": 1100, "bottom": 1020,
            "threshold": 0.85,
        },
    }


def test_without_yesterday_reward_clicks_coin_prayer_directly(caplog):
    client = PrayerClient()
    results = iter([((866, 875), 0.99), (None, 0.20), ((866, 875), 0.99)])

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = handle_organization_prayer(
            client,
            prayer_action(),
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
            sleep=lambda _: None,
        )

    assert client.taps == [(866, 875)]
    assert summary.yesterday_opened is False
    assert summary.claims == 0
    assert "未发现昨日奖励" in caplog.text


def test_claims_every_available_yesterday_reward_then_dismisses_and_prays():
    client = PrayerClient()
    results = iter([
        ((866, 875), 0.99),
        ((670, 285), 0.98),
        ((950, 160), 0.99),
        ((1130, 360), 0.99),
        ((1130, 585), 0.98),
        (None, 0.30),
        ((866, 875), 0.99),
    ])
    waits = []

    summary = handle_organization_prayer(
        client,
        prayer_action(),
        template_matcher=lambda *args: next(results),
        sleep=waits.append,
    )

    assert client.taps == [
        (670, 285),
        (1130, 360),
        (1130, 585),
        (1500, 200),
        (866, 875),
    ]
    assert waits == [1.0, 1.0, 1.0, 1.0]
    assert summary.yesterday_opened is True
    assert summary.claims == 2


def test_yesterday_panel_with_no_available_reward_dismisses_without_false_click():
    client = PrayerClient()
    results = iter([
        ((866, 875), 0.99),
        ((670, 285), 0.98),
        ((950, 160), 0.99),
        (None, 0.30),
        ((866, 875), 0.99),
    ])

    summary = handle_organization_prayer(
        client,
        prayer_action(),
        template_matcher=lambda *args: next(results),
        sleep=lambda _: None,
    )

    assert client.taps == [(670, 285), (1500, 200), (866, 875)]
    assert summary.claims == 0


def test_all_three_rewards_can_be_claimed_without_hitting_safety_error():
    client = PrayerClient()
    results = iter([
        ((866, 875), 0.99),
        ((670, 285), 0.98),
        ((950, 160), 0.99),
        ((1130, 360), 0.99),
        ((1130, 585), 0.98),
        ((1130, 810), 0.97),
        ((866, 875), 0.99),
    ])

    summary = handle_organization_prayer(
        client,
        prayer_action(),
        template_matcher=lambda *args: next(results),
        sleep=lambda _: None,
    )

    assert client.taps == [
        (670, 285),
        (1130, 360),
        (1130, 585),
        (1130, 810),
        (1500, 200),
        (866, 875),
    ]
    assert summary.claims == 3
