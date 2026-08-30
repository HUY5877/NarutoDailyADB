import logging
from pathlib import Path

import pytest

from naruto_daily.navigation import navigate_to_card
from naruto_daily.visual import VisualError


class NavigationClient:
    def __init__(self):
        self.taps = []
        self.swipes = []

    def screenshot(self):
        return b"screen"

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, x1, y1, x2, y2, duration_ms):
        self.swipes.append((x1, y1, x2, y2, duration_ms))


def card_action():
    return {
        "type": "find_card_and_click",
        "name": "组织祈福",
        "template": "organization_pray_label.png",
        "left": 0, "top": 300, "right": 1920, "bottom": 480,
        "threshold": 0.85,
        "button_template": "go_now_button.png",
        "button_threshold": 0.85,
        "button_left_offset": -170, "button_top_offset": 80,
        "button_right_offset": 170, "button_bottom_offset": 260,
        "max_swipes": 6,
        "swipe_x1": 1650, "swipe_y1": 550,
        "swipe_x2": 350, "swipe_y2": 550,
        "swipe_duration_ms": 500,
        "wait_seconds": 1,
    }


def test_card_visible_now_clicks_corresponding_button_without_swiping(caplog):
    client = NavigationClient()
    results = iter([((562, 394), 0.99), ((548, 585), 0.98)])
    regions = []

    def matcher(png, path, roi, threshold):
        regions.append(roi)
        return next(results)

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = navigate_to_card(
            client,
            card_action(),
            template_matcher=matcher,
            asset_root=Path("assets"),
            sleep=lambda _: None,
        )

    assert client.taps == [(548, 585)]
    assert client.swipes == []
    assert regions[1] == (392, 474, 732, 654)
    assert summary.swipes == 0
    assert "发现组织祈福" in caplog.text
    assert "发现对应的立刻前往" in caplog.text


def test_card_after_two_pages_swipes_then_clicks():
    client = NavigationClient()
    results = iter([(None, 0.20), (None, 0.30), ((562, 394), 0.97), ((560, 580), 0.96)])
    waits = []

    summary = navigate_to_card(
        client,
        card_action(),
        template_matcher=lambda *args: next(results),
        sleep=waits.append,
    )

    assert client.swipes == [(1650, 550, 350, 550, 500)] * 2
    assert waits == [1.0, 1.0]
    assert client.taps == [(560, 580)]
    assert summary.swipes == 2


def test_card_missing_after_max_swipes_raises_without_clicking():
    client = NavigationClient()

    with pytest.raises(VisualError, match="滑动 6 次后仍未找到组织祈福"):
        navigate_to_card(
            client,
            card_action(),
            template_matcher=lambda *args: (None, 0.20),
            sleep=lambda _: None,
        )

    assert client.taps == []
    assert len(client.swipes) == 6


def test_card_label_without_corresponding_button_raises_without_clicking():
    client = NavigationClient()
    results = iter([((562, 394), 0.99), (None, 0.40)])

    with pytest.raises(VisualError, match="找到组织祈福，但未找到它下方的立刻前往"):
        navigate_to_card(
            client,
            card_action(),
            template_matcher=lambda *args: next(results),
            sleep=lambda _: None,
        )

    assert client.taps == []
    assert client.swipes == []
