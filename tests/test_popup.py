import logging
from pathlib import Path

import pytest

from naruto_daily.popup import cleanup_popups, return_to_main
from naruto_daily.visual import VisualError


class ScreenshotClient:
    def __init__(self):
        self.screenshot_count = 0
        self.taps = []

    def screenshot(self):
        self.screenshot_count += 1
        return f"画面{self.screenshot_count}".encode()

    def tap(self, x, y):
        self.taps.append((x, y))


def popup_action():
    return {
        "type": "popup_cleanup",
        "rounds": 4,
        "idle_rounds": 1,
        "wait_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "targets": [
            {
                "name": "登录奖励领取按钮", "template": "claim.png",
                "left": 780, "top": 820, "right": 1140, "bottom": 1000, "threshold": 0.85,
            },
            {
                "name": "推广弹窗关闭按钮", "template": "close.png",
                "left": 1700, "top": 0, "right": 1920, "bottom": 180, "threshold": 0.85,
            },
        ],
    }


def test_cleanup_handles_changing_popup_order_and_stops_after_idle_round(caplog):
    client = ScreenshotClient()
    results = iter([
        (None, 0.20), ((1842, 60), 0.96), ((1843, 61), 0.97),
        ((957, 907), 0.99), ((958, 908), 0.98), (None, 0.30),
        (None, 0.25), (None, 0.22),
    ])

    def matcher(png, path, roi, threshold):
        return next(results)

    waits = []
    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = cleanup_popups(
            client,
            popup_action(),
            template_matcher=matcher,
            asset_root=Path("assets"),
            sleep=waits.append,
        )

    assert client.taps == [(1843, 61), (958, 908)]
    assert summary.rounds == 3
    assert summary.clicks == 2
    assert waits == [3.0, 3.0, 3.0, 3.0]
    assert "第 1 轮" in caplog.text
    assert "推广弹窗关闭按钮" in caplog.text
    assert "连续 1 轮没有发现可处理弹窗，结束清理" in caplog.text


def test_action_runner_dispatches_popup_cleanup():
    from naruto_daily.runner import ActionRunner

    client = ScreenshotClient()
    results = iter([
        ((957, 907), 0.99), ((958, 908), 0.98), (None, 0.10),
        (None, 0.10), (None, 0.10),
    ])
    runner = ActionRunner(
        client,
        sleep=lambda _: None,
        template_matcher=lambda *args: next(results),
        asset_root=Path("assets"),
    )

    runner.run([popup_action()])

    assert client.taps == [(958, 908)]


def test_return_to_main_rejects_unknown_page_after_closing_nested_pages(caplog):
    client = ScreenshotClient()
    results = iter([
        (None, 0.20), ((1834, 67), 0.97), (None, 0.21), ((1835, 68), 0.98),
        (None, 0.22), ((1828, 72), 0.97), (None, 0.23), ((1829, 71), 0.98),
        (None, 0.24), (None, 0.31),
        (None, 0.25), (None, 0.28),
    ])
    waits = []
    action = {
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "main_page_marker": {
            "name": "主界面奖励入口", "template": "main.png",
            "left": 1700, "top": 400, "right": 1920, "bottom": 700,
            "threshold": 0.90,
        },
        "close_target": {
            "name": "通用关闭按钮", "template": "close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.95,
        },
    }

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        with pytest.raises(VisualError, match="未能确认已经返回主界面"):
            return_to_main(
                client,
                action,
                template_matcher=lambda *args: next(results),
                asset_root=Path("assets"),
                sleep=waits.append,
            )

    assert client.taps == [(1835, 68), (1829, 71)]
    assert waits == [3.0, 3.0, 3.0, 3.0, 1.0]
    assert "连续 2 轮既未发现主界面标记，也未发现关闭按钮" in caplog.text


def test_return_to_main_stops_immediately_when_real_main_page_is_visible(caplog):
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "main_page_activity_icon.png").read_bytes()

    class MainPageClient:
        def __init__(self):
            self.taps = []

        def screenshot(self):
            return screenshot

        def tap(self, x, y):
            self.taps.append((x, y))

    client = MainPageClient()
    action = {
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "main_page_marker": {
            "name": "主界面奖励入口", "template": "assets/reward_entry.png",
            "left": 1700, "top": 400, "right": 1920, "bottom": 700,
            "threshold": 0.90,
        },
        "close_target": {
            "name": "通用关闭按钮", "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.95,
        },
    }

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = return_to_main(client, action, asset_root=project_root, sleep=lambda _: None)

    assert client.taps == []
    assert summary.rounds == 1
    assert summary.clicks == 0
    assert "确认已处于主界面" in caplog.text


def test_return_to_main_closes_activity_page_once_then_stops_on_main_page(caplog):
    project_root = Path(__file__).parents[1]
    activity_page = (project_root / "tests" / "fixtures" / "activity_page_close.png").read_bytes()
    main_page = (project_root / "tests" / "fixtures" / "main_page_activity_icon.png").read_bytes()

    class ActivityPageClient:
        def __init__(self):
            self.current = activity_page
            self.taps = []

        def screenshot(self):
            return self.current

        def tap(self, x, y):
            self.taps.append((x, y))
            self.current = main_page

    client = ActivityPageClient()
    action = {
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "main_page_marker": {
            "name": "主界面奖励入口", "template": "assets/reward_entry.png",
            "left": 1700, "top": 400, "right": 1920, "bottom": 700,
            "threshold": 0.90,
        },
        "close_target": {
            "name": "通用关闭按钮", "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.95,
        },
    }

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = return_to_main(client, action, asset_root=project_root, sleep=lambda _: None)

    assert client.taps == [(1828, 70)]
    assert summary.rounds == 2
    assert summary.clicks == 1
    assert "确认已处于主界面" in caplog.text


def test_return_to_main_cancels_stale_close_click_when_main_page_appears_during_wait(caplog):
    project_root = Path(__file__).parents[1]
    activity_page = (project_root / "tests" / "fixtures" / "activity_page_close.png").read_bytes()
    main_page = (project_root / "tests" / "fixtures" / "main_page_activity_icon.png").read_bytes()

    class LoadingClient:
        def __init__(self):
            self.current = activity_page
            self.taps = []

        def screenshot(self):
            return self.current

        def tap(self, x, y):
            self.taps.append((x, y))

    client = LoadingClient()
    action = {
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "main_page_marker": {
            "name": "主界面奖励入口", "template": "assets/reward_entry.png",
            "left": 1700, "top": 400, "right": 1920, "bottom": 700,
            "threshold": 0.90,
        },
        "close_target": {
            "name": "通用关闭按钮", "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.95,
        },
    }

    def finish_loading(seconds):
        if seconds == 3:
            client.current = main_page

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = return_to_main(client, action, asset_root=project_root, sleep=finish_loading)

    assert client.taps == []
    assert summary.rounds == 1
    assert summary.clicks == 0
    assert "确认已处于主界面，取消点击" in caplog.text
