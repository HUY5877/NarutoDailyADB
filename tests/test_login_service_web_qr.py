"""QQ-installed and game-web authorization share the same safe QR handoff.

Every QR in this file is generated from a fixed public unit-test string.
No real authorization image or token is stored or decoded by these tests.
"""
from pathlib import Path
import time

import cv2
import numpy as np
import pytest
import qrcode

from naruto_daily.login_service.qr_navigation import (
    LoginDevice,
    QRNavigationError,
    choose_action,
    crop_authorization_qr,
    fetch_login_qr,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "naruto_daily/login_service/templates"
PAYLOAD = "naruto-web-qr-unit-test"
GAME_PACKAGE = "com.tencent.KiHan"


def paste(frame, name, x, y, *, shared=False):
    path = ROOT / "assets" / name if shared else TEMPLATES / f"{name}.png"
    template = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    height, width = template.shape[:2]
    frame[y:y + height, x:x + width] = template


def qr_pixels():
    code = qrcode.QRCode(version=2, box_size=10, border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(PAYLOAD)
    code.make(fit=False)
    return np.array(code.make_image(fill_color="black", back_color="white").convert("RGB"))


def frame(*, page="web", code=True, heading=True, instruction=True):
    result = np.full((1080, 1920, 3), 255, dtype=np.uint8)
    if page == "web":
        if heading:
            paste(result, "web_qr_heading", 780, 8)
        if instruction:
            paste(result, "web_qr_instruction", 790, 605)
        top = 300
    else:
        if heading:
            paste(result, "qr_heading", 780, 220)
        top = 380
    if code:
        pixels = qr_pixels()
        height, width = pixels.shape[:2]
        result[top:top + height, 820:820 + width] = pixels
    return result


def web_xml(package=GAME_PACKAGE, *, extra="", heading=True, instruction=True):
    labels = (["QQ授权登录"] if heading else []) + (["使用QQ手机版扫码授权登录"] if instruction else [])
    return "<hierarchy>" + "".join(
        f'<node package="{package}" text="{label}" />' for label in [*labels, extra] if label
    ) + "</hierarchy>"


def assert_test_qr(png):
    assert isinstance(png, bytes) and png.startswith(b"\x89PNG")
    crop = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert 200 <= min(crop.shape[:2]) <= max(crop.shape[:2]) < 600
    assert np.all(crop[:24] == 255)
    assert np.all(crop[-24:] == 255)
    assert np.all(crop[:, :24] == 255)
    assert np.all(crop[:, -24:] == 255)
    # Decoding is intentionally limited to this file's known, synthetic QR.
    assert cv2.QRCodeDetector().detectAndDecode(crop)[0] == PAYLOAD


@pytest.mark.parametrize("page", ["web", "native"])
def test_both_authorization_layouts_return_complete_synthetic_qr(page):
    assert_test_qr(crop_authorization_qr(frame(page=page), ""))


@pytest.mark.parametrize("heading,instruction", [(False, False), (True, False), (False, True)])
def test_web_qr_requires_both_authorization_markers(heading, instruction):
    assert crop_authorization_qr(frame(heading=heading, instruction=instruction), "") is None


def test_confirmed_web_authorization_without_loaded_qr_is_not_ready():
    assert crop_authorization_qr(frame(code=False), "") is None


@pytest.mark.parametrize("package", [GAME_PACKAGE, "com.example.configuredgame"])
def test_only_configured_game_package_can_confirm_web_authorization_from_xml(package):
    image = frame(heading=False, instruction=False)
    options = {} if package == GAME_PACKAGE else {"game_package": package}
    assert_test_qr(crop_authorization_qr(image, web_xml(package), **options))
    assert crop_authorization_qr(image, web_xml("com.example.otherapp"), **options) is None


@pytest.mark.parametrize("heading,instruction", [(True, False), (False, True)])
def test_game_xml_requires_both_authorization_texts(heading, instruction):
    assert crop_authorization_qr(frame(heading=False, instruction=False),
                                 web_xml(heading=heading, instruction=instruction)) is None


def test_authorization_text_from_different_packages_is_not_combined():
    xml = ('<hierarchy><node package="com.tencent.KiHan" text="QQ授权登录" />'
           '<node package="com.example.otherapp" text="使用QQ手机版扫码授权登录" /></hierarchy>')
    assert crop_authorization_qr(frame(heading=False, instruction=False), xml) is None


@pytest.mark.parametrize("text", ["二维码失效", "二维码已失效", "二维码已过期", "已失效", "已过期"])
def test_expired_game_authorization_text_never_publishes_qr(text):
    with pytest.raises(QRNavigationError, match="expired"):
        crop_authorization_qr(frame(), web_xml(extra=text))


@pytest.mark.parametrize("background", [0, 130])
def test_expired_web_overlay_is_rejected_without_accessibility_text(background):
    image = frame()
    # Mimic the SDK's white expiry message over a dimmed QR. The template only
    # contains public UI glyphs; the underlying QR is still the synthetic fixture.
    image[300:550, 820:1070] = (image[300:550, 820:1070].astype(np.float32) * 0.4).astype(np.uint8)
    mask = cv2.imdecode(np.frombuffer((TEMPLATES / "web_qr_expired.png").read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
    height, width = mask.shape
    overlay = np.full((height, width, 3), background, dtype=np.uint8)
    overlay[mask > 220] = (255, 255, 255)
    top, left = 400, 860
    image[top:top + height, left:left + width] = overlay
    with pytest.raises(QRNavigationError, match="expired"):
        crop_authorization_qr(image, "")


def test_game_text_support_does_not_widen_native_qq_button_clicks():
    image = np.full((1080, 1920, 3), 255, dtype=np.uint8)
    xml = '<hierarchy><node package="com.tencent.KiHan" text="扫码登录" bounds="[10,10][100,100]" /></hierarchy>'
    assert choose_action(image, xml) is None


@pytest.mark.parametrize("package", [GAME_PACKAGE, "com.example.configuredgame"])
def test_login_device_retains_configured_game_package(package):
    device = LoginDevice(Path("adb.exe"), "emulator-5554", time.time() + 20, game_package=package)
    assert device.game_package == package


class ObservedDevice:
    def __init__(self, images, *, xml="", package=None):
        self.deadline = time.time() + 20
        self.images = iter(images)
        self.xml = xml
        self.clicks = []
        if package is not None:
            self.game_package = package

    def screenshot(self):
        return next(self.images)

    def hierarchy(self):
        return self.xml

    def tap(self, x, y):
        self.clicks.append((x, y))


def test_direct_web_authorization_waits_for_qr_without_extra_taps():
    device = ObservedDevice([frame(code=False), frame()])
    assert_test_qr(fetch_login_qr(device, poll_seconds=0))
    assert device.clicks == []


def test_clicking_qq_android_can_open_web_authorization_directly():
    game = np.full((1080, 1920, 3), 255, dtype=np.uint8)
    paste(game, "qq_android_button.png", 1400, 750, shared=True)
    game[910:942, 142:176] = (0, 255, 0)  # Existing game's consent tick is already checked.
    device = ObservedDevice([game, frame()])
    assert_test_qr(fetch_login_qr(device, poll_seconds=0))
    assert len(device.clicks) == 1
    x, y = device.clicks[0]
    assert 1350 <= x <= 1850 and 720 <= y <= 900


@pytest.mark.parametrize("package", [None, "com.example.configuredgame"])
def test_fetch_passes_custom_game_package_and_defaults_for_old_fake_devices(package):
    device = ObservedDevice([frame(heading=False, instruction=False)],
                            xml=web_xml(package or GAME_PACKAGE), package=package)
    assert_test_qr(fetch_login_qr(device, poll_seconds=0))
    assert device.clicks == []
