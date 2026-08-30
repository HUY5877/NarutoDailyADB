import logging
from pathlib import Path

from naruto_daily.popup import cleanup_popups, return_to_main


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


def test_return_to_main_closes_nested_pages_and_stops_after_two_idle_rounds(caplog):
    client = ScreenshotClient()
    results = iter([
        ((1834, 67), 0.92), ((1835, 68), 0.94),
        ((1828, 72), 0.91), ((1829, 71), 0.93),
        (None, 0.31),
        (None, 0.28),
    ])
    waits = []
    action = {
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "close_target": {
            "name": "通用关闭按钮", "template": "close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.82,
        },
    }

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = return_to_main(
            client,
            action,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
            sleep=waits.append,
        )

    assert client.taps == [(1835, 68), (1829, 71)]
    assert waits == [3.0, 3.0, 3.0, 3.0, 1.0]
    assert summary.rounds == 4
    assert summary.clicks == 2
    assert "连续 2 轮没有发现关闭按钮，已回到主界面" in caplog.text
