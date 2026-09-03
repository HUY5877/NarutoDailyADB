import logging
from pathlib import Path

from naruto_daily.runner import ActionRunner


class RecordingClient:
    def __init__(self):
        self.taps = []
        self.screenshot_count = 0

    def screenshot(self):
        self.screenshot_count += 1
        return f"招募画面{self.screenshot_count}".encode()

    def tap(self, x, y):
        self.taps.append((x, y))


def recruit_action():
    return {
        "type": "handle_free_recruit",
        "timeout_seconds": 40,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "advanced_tab": {
            "name": "高级招募标签", "template": "advanced_tab.png",
            "left": 0, "top": 150, "right": 300, "bottom": 350, "threshold": 0.90,
        },
        "advanced_red_dot": {
            "name": "高级招募红点", "template": "recruit_red_dot.png",
            "left": 170, "top": 180, "right": 270, "bottom": 300, "threshold": 0.95,
        },
        "ordinary_tab": {
            "name": "普通招募标签", "template": "ordinary_tab.png",
            "left": 0, "top": 780, "right": 300, "bottom": 1030, "threshold": 0.90,
        },
        "ordinary_red_dot": {
            "name": "普通招募红点", "template": "recruit_red_dot.png",
            "left": 170, "top": 800, "right": 270, "bottom": 980, "threshold": 0.95,
        },
        "free_button": {
            "name": "免费招募按钮", "template": "free_recruit.png",
            "left": 900, "top": 700, "right": 1500, "bottom": 1020, "threshold": 0.85,
        },
        "result_confirm": {
            "name": "招募结果确定按钮", "template": "recruit_confirm.png",
            "left": 350, "top": 700, "right": 800, "bottom": 1000, "threshold": 0.90,
        },
    }


def test_recruit_processes_advanced_then_ordinary_free_draw(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99), ((121, 251), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((1280, 880), 0.99), ((1281, 881), 0.99),
        ((218, 230), 0.99),
        ((600, 870), 0.99), ((601, 871), 0.99),
        ((218, 880), 0.99),
        ((218, 880), 0.99),
        ((120, 900), 0.99), ((121, 901), 0.99),
        ((218, 880), 0.99),
        ((218, 880), 0.99),
        ((1200, 850), 0.99), ((1201, 851), 0.99),
        ((218, 880), 0.99),
        ((600, 870), 0.99), ((601, 871), 0.99),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == [
        (121, 251),
        (1281, 881),
        (601, 871),
        (121, 901),
        (1201, 851),
        (601, 871),
    ]
    assert waits == [3.0] * 14
    assert "高级招募免费次数处理完成" in caplog.text
    assert "普通招募免费次数处理完成" in caplog.text


def test_recruit_skips_each_category_without_red_dot(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        (None, 0.30),
        (None, 0.25),
    ])

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=lambda _: None,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == []
    assert "高级招募没有红点，跳过" in caplog.text
    assert "普通招募没有红点，跳过" in caplog.text


def test_recruit_skips_category_when_red_dot_disappears_during_wait(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        (None, 0.40),
        (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == []
    assert waits == [3.0]
    assert "等待后高级招募红点已消失，跳过高级招募" in caplog.text


def test_recruit_does_not_click_free_button_when_red_dot_disappears_after_tab(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99), ((121, 251), 0.99),
        ((218, 230), 0.99),
        (None, 0.40),
        (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == [(121, 251)]
    assert waits == [3.0, 3.0, 3.0]
    assert "点击高级招募标签后红点已消失，不点击免费按钮" in caplog.text


def test_recruit_does_not_click_tab_when_red_dot_disappears_during_tab_wait(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99),
        ((121, 251), 0.99),
        (None, 0.40),
        (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == []
    assert waits == [3.0, 3.0]
    assert "点击高级招募标签前红点已消失，取消点击" in caplog.text


def test_recruit_does_not_click_free_button_when_red_dot_disappears_during_button_wait(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99),
        ((121, 251), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((1280, 880), 0.99),
        ((1281, 881), 0.99),
        (None, 0.40),
        (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == [(121, 251)]
    assert waits == [3.0, 3.0, 3.0, 3.0]
    assert "点击免费招募按钮前高级招募红点已消失，取消点击" in caplog.text
