"""使用用户未标注截图的真实游戏像素回放活动弹窗。

原图去掉模拟器窗口，裁切 (1,34,641,394) 得到 640×360 游戏画面，
使用 INTER_CUBIC 缩放到运行分辨率 1920×1080。标题模板取其中
(780,285,1605,500)，不包含按钮或红框；点击点取另一张附件红框内部。
"""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.common import main_screen
from naruto_daily.common.visual import find_template_in_png
from naruto_daily.return_to_main import return_to_main


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


def login_return_action():
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    return next(a for a in config["tasks"]["login"] if a["type"] == "return_to_main")


def match(png):
    target = login_return_action()["dismiss_popup"]["marker"]
    return find_template_in_png(
        png, ROOT / target["template"],
        tuple(target[k] for k in ("left", "top", "right", "bottom")),
        target["threshold"],
    )


def test_real_popup_title_is_recognized_at_runtime_resolution():
    png = (FIXTURES / "organization_tournament_popup.png").read_bytes()
    frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)

    center, score = match(png)

    assert frame.shape == (1080, 1920, 3)
    assert center == (1192, 392)
    assert score > .98


@pytest.mark.parametrize("fixture", [
    "daily_home.png", "main_page_activity_evening.png", "squad_assault_rewards.png",
    "squad_assault_quadruple_dialog.png", "secret_exit_dialog.png",
])
def test_unrelated_screens_do_not_trigger_the_blank_click(fixture):
    center, score = match((FIXTURES / fixture).read_bytes())

    assert center is None and score < .9


def test_real_popup_replay_dismisses_then_returns_to_main(monkeypatch):
    popup = (FIXTURES / "organization_tournament_popup.png").read_bytes()
    home = (FIXTURES / "daily_home.png").read_bytes()

    class HomeRecognition:
        def __init__(self, *args):
            pass

        def match(self, png, target, timeout):
            return ((0, 0), 1) if png == home else (None, 0)

    class Device:
        current = popup

        def __init__(self):
            self.taps = []

        def screenshot(self):
            return self.current

        def tap(self, x, y):
            self.taps.append((x, y))
            self.current = home

    monkeypatch.setattr(main_screen, "MainScreenRecognition", HomeRecognition)
    device = Device()

    summary = return_to_main(device, login_return_action(), asset_root=ROOT, sleep=lambda _: None)

    assert device.taps == [(1220, 975)]
    assert summary.clicks == 1 and summary.rounds == 2
