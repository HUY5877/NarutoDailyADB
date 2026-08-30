import logging
from pathlib import Path

from naruto_daily.fortune import handle_coin_fortune


class FortuneClient:
    def __init__(self):
        self.taps = []
        self.screenshot_count = 0

    def screenshot(self):
        self.screenshot_count += 1
        return f"招财画面{self.screenshot_count}".encode()

    def tap(self, x, y):
        self.taps.append((x, y))


def fortune_action():
    return {
        "type": "handle_coin_fortune",
        "clicks": 2,
        "timeout_seconds": 20,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "free_button": {
            "name": "免费一次",
            "template": "free_once.png",
            "left": 1000,
            "top": 700,
            "right": 1450,
            "bottom": 1050,
            "threshold": 0.85,
        },
    }


def test_coin_fortune_clicks_confirmed_free_button_twice(caplog):
    client = FortuneClient()
    results = iter([
        (None, 0.31),
        ((1260, 850), 0.92), ((1262, 851), 0.95),
        ((1261, 850), 0.93), ((1263, 852), 0.96),
    ])
    waits = []

    def matcher(png, template_path, roi, threshold):
        assert template_path == Path("assets") / "free_once.png"
        assert roi == (1000, 700, 1450, 1050)
        assert threshold == 0.85
        return next(results)

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = handle_coin_fortune(
            client,
            fortune_action(),
            template_matcher=matcher,
            asset_root=Path("assets"),
            sleep=waits.append,
        )

    assert client.taps == [(1262, 851), (1263, 852)]
    assert waits == [1.0, 3.0, 3.0, 3.0, 3.0]
    assert summary.clicks == 2
    assert "完成第 2/2 次免费招财" in caplog.text


def test_coin_fortune_does_not_click_disappeared_button():
    client = FortuneClient()
    results = iter([
        ((1260, 850), 0.92), (None, 0.25),
        ((1261, 850), 0.93), ((1263, 852), 0.96),
        ((1261, 850), 0.93), ((1264, 853), 0.97),
    ])

    summary = handle_coin_fortune(
        client,
        fortune_action(),
        template_matcher=lambda *args: next(results),
        sleep=lambda _: None,
    )

    assert client.taps == [(1263, 852), (1264, 853)]
    assert summary.clicks == 2
