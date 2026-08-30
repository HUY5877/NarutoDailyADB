import logging
from pathlib import Path

import pytest

from naruto_daily.authorization import finish_login_authorization
from naruto_daily.visual import VisualError


class AuthorizationClient:
    def __init__(self):
        self.taps = []

    def screenshot(self):
        return b"screen"

    def tap(self, x, y):
        self.taps.append((x, y))


def authorization_action():
    return {
        "type": "finish_login_authorization",
        "timeout_seconds": 20,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "second_authorization": {
            "name": "第二次授权同意按钮",
            "template": "second.png",
            "left": 700, "top": 700, "right": 1220, "bottom": 930,
            "threshold": 0.85,
        },
        "start_game": {
            "name": "开始游戏按钮",
            "template": "start.png",
            "left": 650, "top": 780, "right": 1270, "bottom": 1020,
            "threshold": 0.85,
        },
    }


def test_direct_start_game_branch_clicks_once(caplog):
    client = AuthorizationClient()
    waits = []
    start_checks = 0

    def matcher(png, path, roi, threshold):
        nonlocal start_checks
        if path.name == "start.png":
            start_checks += 1
            return (956, 911), 0.99
        return None, 0.10

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = finish_login_authorization(
            client,
            authorization_action(),
            template_matcher=matcher,
            asset_root=Path("assets"),
            sleep=waits.append,
        )

    assert client.taps == [(956, 911)]
    assert waits == [3.0, 3.0]
    assert start_checks == 2
    assert summary.used_second_authorization is False
    assert "无需第二次授权" in caplog.text


def test_second_authorization_branch_then_clicks_start_game(caplog):
    client = AuthorizationClient()
    start_results = iter([(None, 0.20), ((956, 911), 0.98), ((956, 911), 0.99)])
    second_checks = 0

    def matcher(png, path, roi, threshold):
        nonlocal second_checks
        if path.name == "start.png":
            return next(start_results)
        second_checks += 1
        return ((400, 300), 0.97)

    waits = []
    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = finish_login_authorization(
            client,
            authorization_action(),
            template_matcher=matcher,
            asset_root=Path("assets"),
            sleep=waits.append,
        )

    assert client.taps == [(400, 300), (956, 911)]
    assert waits == [3.0, 3.0, 3.0, 3.0]
    assert second_checks == 2
    assert summary.used_second_authorization is True
    assert "发现第二次授权页面" in caplog.text


def test_button_disappearing_during_pre_wait_is_not_clicked():
    client = AuthorizationClient()
    start_results = iter([
        ((956, 911), 0.99),
        (None, 0.20),
        ((956, 911), 0.98),
        ((956, 911), 0.99),
    ])
    waits = []

    def matcher(png, path, roi, threshold):
        if path.name == "start.png":
            return next(start_results)
        return None, 0.10

    summary = finish_login_authorization(
        client,
        authorization_action(),
        template_matcher=matcher,
        sleep=waits.append,
    )

    assert client.taps == [(956, 911)]
    assert waits == [3.0, 1.0, 3.0, 3.0]
    assert summary.used_second_authorization is False


def test_old_authorization_action_uses_three_second_defaults():
    client = AuthorizationClient()
    action = authorization_action()
    action.pop("pre_click_wait_seconds")
    action.pop("post_click_wait_seconds")
    waits = []

    summary = finish_login_authorization(
        client,
        action,
        template_matcher=lambda *args: ((956, 911), 0.99),
        sleep=waits.append,
    )

    assert client.taps == [(956, 911)]
    assert waits == [3.0, 3.0]
    assert summary.used_second_authorization is False


def test_repeated_unstable_start_button_respects_timeout():
    client = AuthorizationClient()
    checks = 0
    waits = []

    def matcher(png, path, roi, threshold):
        nonlocal checks
        if path.name != "start.png":
            return None, 0.10
        checks += 1
        if checks % 2 == 1:
            return (956, 911), 0.99
        return None, 0.20

    with pytest.raises(VisualError, match="超时"):
        action = authorization_action()
        action["timeout_seconds"] = 4
        finish_login_authorization(
            client,
            action,
            template_matcher=matcher,
            sleep=waits.append,
        )

    assert client.taps == []
    assert waits == [3.0, 1.0]


def test_authorization_timeout_stops_without_clicking():
    client = AuthorizationClient()

    with pytest.raises(VisualError, match="等待授权完成页面超时"):
        finish_login_authorization(
            client,
            authorization_action(),
            template_matcher=lambda *args: (None, 0.10),
            sleep=lambda _: None,
        )

    assert client.taps == []
