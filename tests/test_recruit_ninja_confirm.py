import json
from pathlib import Path

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.config import ConfigError, load_settings
from naruto_daily.recruit import handler


ROOT = Path(__file__).parents[1]


@pytest.fixture
def recruit_action():
    action = next(
        action for action in load_settings(ROOT / "config.example.json").tasks["recruit"]
        if action["type"] == "handle_free_recruit"
    )
    return action


@pytest.fixture
def result_confirm(recruit_action):
    return recruit_action["ninja_result_confirm"]


class ScreenshotClient:
    def __init__(self, png):
        self.png = png
        self.taps = []
        self.screenshot_count = 0

    def screenshot(self):
        self.screenshot_count += 1
        return self.png

    def tap(self, x, y):
        self.taps.append((x, y))


class SequenceClient(ScreenshotClient):
    def __init__(self, *fixtures):
        super().__init__(None)
        self.frames = [(ROOT / "tests" / "fixtures" / fixture).read_bytes() for fixture in fixtures]

    def screenshot(self):
        frame = self.frames[min(self.screenshot_count, len(self.frames) - 1)]
        self.screenshot_count += 1
        return frame


@pytest.fixture
def no_model(monkeypatch):
    def unexpected_model(*args, **kwargs):
        raise AssertionError("招募结果确定按钮应只使用模板匹配")

    monkeypatch.setattr(handler, "MultimodalClient", unexpected_model)


def confirm_result(client, action, *, timeout=10, pre_wait=.1):
    images = ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None)
    return handler._confirm_recruit_result(
        client, action, images=images, timeout_seconds=timeout, poll_seconds=.2,
        pre_click_wait=pre_wait, post_click_wait=.1,
    )


@pytest.mark.parametrize("fixture, target_key, expected_center", [
    ("recruit_result.png", "result_confirm", (598, 843)),
    ("recruit_ninja_result.png", "ninja_result_confirm", (956, 985)),
])
def test_example_result_confirm_matches_both_result_pages(recruit_action, fixture, target_key, expected_center):
    target = recruit_action[target_key]
    png = (ROOT / "tests" / "fixtures" / fixture).read_bytes()

    center, score = ImageRecognition(None, asset_root=ROOT).match(png, target)

    assert center == expected_center
    assert score >= target["threshold"]


def test_ninja_result_requires_matching_font_even_with_expanded_roi(result_confirm):
    png = (ROOT / "tests" / "fixtures" / "recruit_ninja_result.png").read_bytes()
    roi = tuple(result_confirm[edge] for edge in ("left", "top", "right", "bottom"))

    old_center, old_score = find_template_in_png(
        png, ROOT / "assets" / "recruit_confirm.png", roi, result_confirm["threshold"],
    )
    new_center, new_score = find_template_in_png(
        png, ROOT / "assets" / "recruit_ninja_confirm.png", roi, result_confirm["threshold"],
    )

    assert old_center is None
    assert old_score < result_confirm["threshold"]
    assert new_center == (956, 985)
    assert new_score >= result_confirm["threshold"]
    # 实机截图中的底部确定按钮边界，中心必须落在按钮内。
    assert 805 < new_center[0] < 1107
    assert 929 < new_center[1] < 1042


def test_ninja_result_is_rechecked_and_confirmed_once_without_model(result_confirm, monkeypatch):
    def unexpected_model(*args, **kwargs):
        raise AssertionError("招募结果确定按钮应只使用模板匹配")

    monkeypatch.setattr(handler, "MultimodalClient", unexpected_model)
    png = (ROOT / "tests" / "fixtures" / "recruit_ninja_result.png").read_bytes()
    client, waits = ScreenshotClient(png), []
    images = ImageRecognition(client, asset_root=ROOT, sleep=waits.append)

    assert handler._click_confirmed_target(
        client, result_confirm, images=images, timeout_seconds=3, poll_seconds=1,
        pre_click_wait=1, post_click_wait=1,
    )

    assert client.taps == [(956, 985)]
    assert client.screenshot_count == 2
    assert waits == [1, 1]


@pytest.mark.parametrize("fixture", ["advanced_recruit_page.png", "recruit_sidebar_shifted.png"])
def test_paid_recruit_buttons_are_not_result_confirm(result_confirm, fixture):
    png = (ROOT / "tests" / "fixtures" / fixture).read_bytes()
    client = ScreenshotClient(png)
    images = ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None)

    center, score = images.match(png, result_confirm)
    assert center is None
    assert score < result_confirm["threshold"]

    with pytest.raises(VisualError, match="等待获得忍者展示确定按钮超时"):
        handler._click_confirmed_target(
            client, result_confirm, images=images, timeout_seconds=3, poll_seconds=1,
            pre_click_wait=1, post_click_wait=1,
        )

    assert client.taps == []


def test_ninja_popup_then_animation_requires_standard_confirmation(recruit_action, no_model):
    client = SequenceClient(
        "recruit_ninja_result.png", "recruit_ninja_result.png",
        "advanced_recruit_page.png", "advanced_recruit_page.png",
        "recruit_result.png", "recruit_result.png",
    )

    assert confirm_result(client, recruit_action)

    assert client.taps == [(956, 985), (598, 843)]
    assert client.screenshot_count == 6


def test_standard_result_without_ninja_popup_is_confirmed_once(recruit_action, no_model):
    client = SequenceClient("recruit_result.png", "recruit_result.png")

    assert confirm_result(client, recruit_action)

    assert client.taps == [(598, 843)]
    assert client.screenshot_count == 2


@pytest.mark.parametrize("first, recheck, expected_taps", [
    ("recruit_ninja_result.png", "recruit_result.png", [(598, 843)]),
    ("recruit_result.png", "recruit_ninja_result.png", [(956, 985), (598, 843)]),
])
def test_confirmation_uses_latest_page_type(recruit_action, no_model, first, recheck, expected_taps):
    client = SequenceClient(first, recheck, "recruit_result.png", "recruit_result.png")

    assert confirm_result(client, recruit_action)

    assert client.taps == expected_taps


def test_missing_final_result_after_ninja_confirmation_is_not_success(recruit_action, no_model):
    client = SequenceClient(
        "recruit_ninja_result.png", "recruit_ninja_result.png", "advanced_recruit_page.png",
    )

    with pytest.raises(VisualError, match="未完成结果确认"):
        confirm_result(client, recruit_action, timeout=3)

    assert client.taps == [(956, 985)]


def test_disappearing_confirmation_is_not_clicked(recruit_action, no_model):
    client = SequenceClient("recruit_ninja_result.png", "advanced_recruit_page.png")

    with pytest.raises(VisualError, match="未完成结果确认"):
        confirm_result(client, recruit_action, timeout=3)

    assert client.taps == []


def test_expired_wait_does_not_click_visible_confirmation(recruit_action, no_model):
    client = SequenceClient("recruit_ninja_result.png")

    with pytest.raises(VisualError, match="未完成结果确认"):
        confirm_result(client, recruit_action, timeout=3, pre_wait=3)

    assert client.taps == []


def test_recheck_at_deadline_does_not_click_visible_confirmation(recruit_action, no_model):
    client = SequenceClient("recruit_ninja_result.png", "advanced_recruit_page.png", "recruit_result.png")

    with pytest.raises(VisualError, match="未完成结果确认"):
        confirm_result(client, recruit_action, timeout=1.1)

    assert client.taps == []
    assert client.screenshot_count == 3


def test_both_layers_share_daily_deadline(recruit_action, no_model):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    client = SequenceClient(
        "recruit_ninja_result.png", "recruit_ninja_result.png", "advanced_recruit_page.png",
    )
    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=lambda: now[0], sleep=sleep):
        confirm_result(client, recruit_action, timeout=.1)

    assert now[0] == pytest.approx(3)
    assert client.taps == [(956, 985)]


def test_legacy_configuration_keeps_single_standard_confirmation(recruit_action, no_model):
    action = dict(recruit_action)
    action.pop("ninja_result_confirm")
    client = SequenceClient("recruit_result.png", "recruit_result.png")

    assert confirm_result(client, action)

    assert client.taps == [(598, 843)]
    assert client.screenshot_count == 2


@pytest.mark.parametrize("update, message", [
    ({"left": 0}, "底部中间安全区域"),
    ({"top": 700}, "底部中间安全区域"),
    ({"right": 1500}, "底部中间安全区域"),
    ({"threshold": .5}, "阈值不能低于"),
])
def test_ninja_confirmation_rejects_unsafe_configuration(tmp_path, update, message):
    payload = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    action = next(action for action in payload["tasks"]["recruit"] if action["type"] == "handle_free_recruit")
    action["ninja_result_confirm"].update(update)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_settings(path)
