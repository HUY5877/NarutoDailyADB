import json
from pathlib import Path

import pytest

from naruto_daily.common.navigation import navigate_to_card
from naruto_daily.recruit import handle_free_recruit
from naruto_daily.recruit.handler import _click_confirmed_target
from naruto_daily.return_to_main import return_to_main
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError


ROOT = Path(__file__).parents[1]


def configured(action_type):
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    return next(a for actions in config["tasks"].values() for a in actions if a["type"] == action_type)


class ScreenClient:
    def __init__(self):
        self.frame = 0
        self.taps = []
        self.swipes = []

    def screenshot(self):
        self.frame += 1
        return str(self.frame).encode()

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, *args):
        self.swipes.append(args)


def test_card_confirmation_retries_and_relocates_moving_card():
    client, waits = ScreenClient(), []
    def matcher(png, path, roi, threshold):
        frame = int(png)
        if path.name == "organization_pray_label.png":
            return ((681 if frame < 3 else 981, 492), 0.979)
        if path.name == "go_now_button.png":
            if frame == 2:
                return None, 0.816
            if frame == 3:
                assert roi[0] > 700  # 根据新卡片文字重算区域
                return (981, 650), 0.994
            return (681, 650), 0.994
        return None, 0.2
    navigate_to_card(client, configured("find_card_and_click"), template_matcher=matcher, sleep=waits.append)
    assert client.taps == [(981, 650)] and client.swipes == []
    assert waits == [1, 1, 1]


def test_card_becoming_completed_during_retry_does_not_click():
    client, waits = ScreenClient(), []
    def matcher(png, path, roi, threshold):
        frame = int(png)
        if path.name == "organization_pray_label.png":
            return (681, 492), 0.99
        if path.name == "completed_stamp.png" and frame == 3:
            return (681, 650), 0.99
        if path.name == "go_now_button.png" and frame == 1:
            return (681, 650), 0.99
        return None, 0.4
    result = navigate_to_card(client, configured("find_card_and_click"), template_matcher=matcher, sleep=waits.append)
    assert result.completed and client.taps == []
    assert waits == [1, 1]


def test_main_page_appears_during_close_retry_cancels_click():
    client, waits = ScreenClient(), []
    def matcher(png, path, roi, threshold):
        frame = int(png)
        if path.name == "main_reward_label.png" and frame == 3:
            return (1800, 500), 0.99
        if path.name == "promo_close.png" and frame == 1:
            return (1800, 80), 0.99
        return None, 0.4
    result = return_to_main(client, configured("return_to_main"), template_matcher=matcher, sleep=waits.append)
    assert result.clicks == 0 and client.taps == []
    assert waits == [1, 1]


def test_recruit_retry_never_combines_button_and_guard_from_different_frames(monkeypatch):
    from types import SimpleNamespace
    from naruto_daily.recruit import handler
    client, waits, model_frames = ScreenClient(), [], []

    def matcher(png, path, roi, threshold):
        if path.name == "advanced_recruit_tab.png":
            return (120, 250), .99
        if path.name == "recruit_red_dot.png":
            return ((218, 230), .99) if roi[1] < 400 and int(png) < 9 else (None, .4)
        raise AssertionError("免费按钮不应调用 OpenCV")

    def extract(png, *args, **kwargs):
        model_frames.append(int(png))
        if int(png) == 8:
            return dict(category="高级招募", free_count=0, button_text=None, center=None)
        return dict(category="高级招募", free_count=1, button_text="免费1抽", center=[1200, 850])

    monkeypatch.setattr(handler, "MultimodalClient", lambda *a, **k: SimpleNamespace(complete_json=extract))
    handle_free_recruit(client, configured("handle_free_recruit"), template_matcher=matcher, sleep=waits.append)
    assert client.taps == [(120, 250)]
    assert model_frames == [7, 8]  # 第9帧红点消失，不复用第7帧的免费按钮。


def test_guard_recovery_does_not_reuse_disappeared_button():
    client, waits = ScreenClient(), []
    action = configured("handle_free_recruit")
    def matcher(png, path, roi, threshold):
        frame = int(png)
        if path.name == "advanced_recruit_tab.png":
            return ((1200, 850), 0.99) if frame <= 2 else (None, 0.4)
        return (None, 0.4) if frame == 2 else ((218, 230), 0.99)
    images = ImageRecognition(client, matcher, sleep=waits.append)
    with pytest.raises(VisualError, match="超时"):
        _click_confirmed_target(client, action["advanced_tab"], images=images,
            timeout_seconds=3, poll_seconds=1, pre_click_wait=1, post_click_wait=1,
            guard_target=action["advanced_red_dot"], guard_name="高级招募")
    assert client.taps == [] and sum(waits) == 3
