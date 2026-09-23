from io import BytesIO
from pathlib import Path
import time

import cv2
import numpy as np
import pytest
import qrcode

from naruto_daily.login_service.qr_navigation import (
    LoginDevice, QRNavigationError, crop_authorization_qr, choose_action, fetch_login_qr,
)


ASSETS = Path(__file__).parents[1] / "naruto_daily/login_service/templates"


def frame_with(name, x, y):
    frame = np.full((1080, 1920, 3), 255, np.uint8)
    template = cv2.imdecode(np.frombuffer((ASSETS / f"{name}.png").read_bytes(), np.uint8), 1)
    h, w = template.shape[:2]
    frame[y:y+h, x:x+w] = template
    return frame


def qr_frame(heading=True):
    frame = (frame_with("qr_heading", 780, 220) if heading
             else np.full((1080, 1920, 3), 255, np.uint8))
    qr = qrcode.make("naruto-login-unit-test").convert("RGB")
    qr = np.array(qr)
    h, w = qr.shape[:2]
    frame[380:380+h, 780:780+w] = qr
    return frame


def test_crops_real_pixels_with_quiet_zone_and_rejects_unrelated_qr():
    assert crop_authorization_qr(qr_frame(False), "") is None
    png = crop_authorization_qr(qr_frame(), "")
    crop = cv2.imdecode(np.frombuffer(png, np.uint8), 1)
    assert crop.shape[0] < 600 and crop.shape[1] < 600
    assert (crop[:12] == 255).all()
    assert cv2.QRCodeDetector().detectAndDecode(crop)[0] == "naruto-login-unit-test"


def test_qr_heading_without_loaded_code_is_not_ready():
    assert crop_authorization_qr(frame_with("qr_heading", 780, 220), "") is None


@pytest.mark.parametrize("label,expected", [
    ("切换账号", "switch_account"), ("添加账号", "add_account"),
    ("其他登录方式", "other_login"), ("扫码登录", "scan_login"),
])
def test_qq_accessibility_nodes_drive_clicks(label, expected):
    xml = f'<hierarchy><node package="com.tencent.mobileqq" text="{label}" enabled="true" bounds="[20,30][120,90]" /></hierarchy>'
    action = choose_action(np.full((1080, 1920, 3), 255, np.uint8), xml)
    assert (action.name, action.x, action.y) == (expected, 70, 60)


def test_qq_modal_has_priority_over_background_links():
    xml = '<hierarchy><node package="com.tencent.mobileqq" text="其他登录方式" bounds="[1,1][100,100]"/><node package="com.tencent.mobileqq" text="扫码登录" bounds="[200,300][600,400]"/></hierarchy>'
    assert choose_action(np.full((1080, 1920, 3), 255, np.uint8), xml).name == "scan_login"


def test_other_apps_cannot_trigger_qq_clicks_and_password_is_never_selected():
    xml = '<hierarchy><node package="evil.app" text="扫码登录" bounds="[1,1][100,100]"/><node package="com.tencent.mobileqq" text="登录" bounds="[1,1][100,100]"/></hierarchy>'
    assert choose_action(np.full((1080, 1920, 3), 255, np.uint8), xml) is None


def test_template_fallback_and_unknown_pages():
    assert choose_action(frame_with("add_account", 1770, 780), "").name == "add_account"
    assert choose_action(np.zeros((1080, 1920, 3), np.uint8), "") is None


def test_checked_game_agreement_is_green_and_must_not_be_toggled_off():
    frame = frame_with("qq_android", 1400, 770)
    frame[910:942, 142:176] = (45, 65, 90)
    assert choose_action(frame, "").name == "game_agreement"
    cv2.polylines(frame, [np.array([(143,916),(156,932),(179,905)])], False, (100,235,45), 6)
    assert choose_action(frame, "").name == "qq_android"


def test_android_game_anr_chooses_wait_only_for_confirmed_game_dialog():
    frame = np.full((1080, 1920, 3), 255, np.uint8)
    xml = '<hierarchy><node package="android" resource-id="android:id/alertTitle" text="火影忍者没有响应"/><node package="android" resource-id="android:id/aerr_wait" text="等待" bounds="[460,606][1459,690]"/></hierarchy>'
    assert choose_action(frame, xml).name == "game_wait"
    assert choose_action(frame, xml.replace("火影忍者", "其他应用")) is None


def test_qq_main_page_stops_before_screenshots_or_clicks(monkeypatch):
    device = LoginDevice(Path("adb.exe"), "emulator-5554", time.time()+10)
    monkeypatch.setattr(device, "_run", lambda *args: b' mResumedActivity: ActivityRecord{123 u0 com.tencent.mobileqq/.activity.SplashActivity t4}')
    monkeypatch.setattr(device, "screenshot", lambda: pytest.fail("QQ message page must not be captured"))
    with pytest.raises(QRNavigationError, match="QQ main"):
        fetch_login_qr(device)


def test_navigation_waits_for_page_changes_and_returns_only_real_qr():
    class Device:
        deadline = time.time() + 10
        clicks = []
        def screenshot(self):
            return qr_frame() if self.clicks else frame_with("scan_login", 677, 940)
        def hierarchy(self):
            return ""
        def tap(self, x, y):
            self.clicks.append((x, y))
    device = Device()
    assert fetch_login_qr(device, poll_seconds=0).startswith(b"\x89PNG")
    assert len(device.clicks) == 1


def test_scan_login_is_clicked_once_while_waiting_for_delayed_qr():
    class Device:
        deadline = time.time() + 10
        clicks = []
        frames = iter([
            frame_with("scan_login", 677, 940),
            frame_with("other_login", 1059, 1001),
            frame_with("scan_login", 677, 940),
            frame_with("qr_heading", 780, 220),
            qr_frame(),
        ])
        def screenshot(self):
            return next(self.frames)
        def hierarchy(self):
            return ""
        def tap(self, x, y):
            self.clicks.append((x,y))
    device = Device()
    assert fetch_login_qr(device, poll_seconds=0).startswith(b"\x89PNG")
    assert len(device.clicks) == 1


def test_navigation_deadline_does_not_tap_unknown_screen():
    class Device:
        deadline = time.time() - 1
        def screenshot(self):
            pytest.fail("expired job must not touch device")
    with pytest.raises(QRNavigationError):
        fetch_login_qr(Device(), poll_seconds=0)


def test_adb_commands_target_only_given_serial_and_hide_errors(monkeypatch):
    from naruto_daily.login_service import qr_navigation
    import subprocess
    commands = []
    def fail(args, **kwargs):
        commands.append((args, kwargs))
        raise subprocess.CalledProcessError(1, args, output=b"private-data")
    monkeypatch.setattr(qr_navigation.subprocess, "run", fail)
    with pytest.raises(QRNavigationError, match="ADB") as error:
        LoginDevice(Path("adb.exe"), "emulator-5560", time.time()+5).tap(20, 30)
    assert "private-data" not in str(error.value)
    assert commands[0][0][1:3] == ["-s", "emulator-5560"]
    assert 0 < commands[0][1]["timeout"] <= 5
