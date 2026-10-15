from pathlib import Path

import cv2
import numpy as np
import pytest
import qrcode

from naruto_daily.login_service import qr_navigation


ASSETS = Path(__file__).parents[1] / "assets"


def blank_frame(color=(255, 255, 255)):
    frame = np.empty((1080, 1920, 3), np.uint8)
    frame[:] = color
    return frame


def paste_template(frame, filename, x, y, *, scale=1.0):
    template = cv2.imdecode(
        np.frombuffer((ASSETS / filename).read_bytes(), np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert template is not None
    if scale != 1.0:
        template = cv2.resize(
            template, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR
        )
    height, width = template.shape[:2]
    frame[y:y + height, x:x + width] = template
    return x + width // 2, y + height // 2


def ad_frame(x=1660, y=65, *, scale=1.0, color=(30, 90, 170)):
    frame = blank_frame(color)
    center = paste_template(frame, "login_startup_ad_close.png", x, y, scale=scale)
    return frame, center


def login_frame(name="scan_login"):
    x, y = {"scan_login": (677, 940), "other_login": (1040, 970)}[name]
    frame = blank_frame()
    center = paste_template(frame, f"login_{name}.png", x, y)
    return frame, center


def qr_frame(*, ad=False):
    frame = blank_frame()
    paste_template(frame, "login_qr_heading.png", 780, 220)
    code = np.array(qrcode.make("startup-ad-offline-test").convert("RGB"))
    height, width = code.shape[:2]
    frame[380:380 + height, 780:780 + width] = code
    if ad:
        paste_template(frame, "login_startup_ad_close.png", 1660, 65)
    return frame


class Clock:
    now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(qr_navigation.time, "time", value.time)
    monkeypatch.setattr(qr_navigation.time, "sleep", value.sleep)
    return value


class ScriptedDevice:
    def __init__(self, frames, clock, *, xml_by_frame=None, after_capture=None):
        self.frames = frames
        self.clock = clock
        self.deadline = clock.time() + 60
        self.screenshot_count = 0
        self.clicks = []
        self.xml_by_frame = xml_by_frame or {}
        self.after_capture = after_capture

    def screenshot(self):
        assert self.screenshot_count < len(self.frames), "unexpected extra screenshot"
        frame = self.frames[self.screenshot_count]
        self.screenshot_count += 1
        if self.after_capture is not None:
            self.after_capture(self)
        return frame.copy()

    def hierarchy(self):
        return self.xml_by_frame.get(self.screenshot_count, "")

    def tap(self, x, y):
        assert self.clock.time() < self.deadline, "tap after login deadline"
        self.clicks.append((self.screenshot_count, x, y))


def assert_returned_qr(png):
    assert png is not None and png.startswith(b"\x89PNG")
    crop = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert cv2.QRCodeDetector().detectAndDecode(crop)[0] == "startup-ad-offline-test"


@pytest.mark.parametrize("scale", [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5])
def test_real_ad_template_matches_all_supported_scales_and_locations(scale):
    index = round((scale - 0.7) * 10)
    colors = [(255, 255, 255), (15, 15, 15), (36, 110, 205)]
    positions = [(80, 95), (1680, 70), (950, 780)]
    frame, center = ad_frame(
        *positions[index % 3], scale=scale, color=colors[index % 3]
    )
    action = qr_navigation.find_startup_ad_close(frame)
    assert action == qr_navigation.LoginAction("startup_ad_close", *center)


def test_real_ad_template_matches_over_busy_advertisement_background():
    frame = np.random.default_rng(42).integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
    center = paste_template(frame, "login_startup_ad_close.png", 1780, 730, scale=1.3)
    assert qr_navigation.find_startup_ad_close(frame) == qr_navigation.LoginAction(
        "startup_ad_close", *center
    )


@pytest.mark.parametrize("color", [(255, 255, 255), (0, 0, 0), (90, 90, 90)])
def test_blank_frames_do_not_supply_an_ad_close(color):
    assert qr_navigation.find_startup_ad_close(blank_frame(color)) is None


@pytest.mark.parametrize("filename", ["promo_close.png", "announcement_close.png"])
def test_normal_orange_game_close_is_not_a_startup_ad(filename):
    frame = blank_frame((40, 80, 150))
    paste_template(frame, filename, 1680, 65)
    assert qr_navigation.find_startup_ad_close(frame) is None


def test_authorization_heading_rejects_ad_close_even_before_qr_has_loaded():
    frame, _ = ad_frame()
    paste_template(frame, "login_qr_heading.png", 780, 220)
    assert qr_navigation.authorization_page(frame)
    assert qr_navigation.find_startup_ad_close(frame) is None


def test_real_authorization_qr_rejects_ad_close():
    assert qr_navigation.find_startup_ad_close(qr_frame(ad=True)) is None


@pytest.mark.parametrize("shape", [(1920, 1080, 3), (540, 960, 3)])
def test_ad_detection_rejects_unsupported_frame_sizes(shape):
    frame = np.full(shape, 90, np.uint8)
    paste_template(frame, "login_startup_ad_close.png", 100, 100)
    assert qr_navigation.find_startup_ad_close(frame) is None


def test_ad_is_reconfirmed_on_new_frame_then_normal_login_reaches_real_qr(clock):
    old_ad, old_center = ad_frame(1660, 65)
    new_ad, new_center = ad_frame(1050, 140, scale=1.2)
    scan, scan_center = login_frame()
    device = ScriptedDevice([old_ad, new_ad, scan, qr_frame()], clock)

    assert_returned_qr(qr_navigation.fetch_login_qr(device, poll_seconds=0.5))

    assert old_center != new_center
    assert device.clicks == [(2, *new_center), (3, *scan_center)]
    assert device.screenshot_count == 4


def test_ad_disappearing_on_confirmation_frame_cancels_the_click(clock):
    ad, _ = ad_frame()
    scan, scan_center = login_frame()
    device = ScriptedDevice([ad, blank_frame(), scan, qr_frame()], clock)

    assert_returned_qr(qr_navigation.fetch_login_qr(device, poll_seconds=0.5))

    assert device.clicks == [(3, *scan_center)]


def test_interrupted_ad_observations_are_not_counted_as_confirmation(clock):
    ad, _ = ad_frame()
    device = ScriptedDevice([ad, blank_frame(), ad, qr_frame()], clock)

    assert_returned_qr(qr_navigation.fetch_login_qr(device, poll_seconds=0.5))

    assert device.clicks == []


def test_qr_appearing_during_ad_confirmation_wins_without_a_click(clock):
    ad, _ = ad_frame()
    device = ScriptedDevice([ad, qr_frame(ad=True)], clock)

    assert_returned_qr(qr_navigation.fetch_login_qr(device, poll_seconds=0.5))

    assert device.clicks == []
    assert device.screenshot_count == 2


def test_ad_obscuring_a_normal_login_action_is_closed_before_that_action(clock):
    ad, ad_center = ad_frame()
    scan_center = paste_template(ad, "login_scan_login.png", 677, 940)
    scan, _ = login_frame()
    device = ScriptedDevice([ad, ad, scan, qr_frame()], clock)

    assert_returned_qr(qr_navigation.fetch_login_qr(device, poll_seconds=0.5))

    assert device.clicks == [(2, *ad_center), (3, *scan_center)]


def test_ad_close_attempts_are_bounded_at_three_actual_clicks(clock):
    ad, center = ad_frame()
    device = ScriptedDevice([ad] * 8, clock)

    with pytest.raises(qr_navigation.QRNavigationError, match="广告") as caught:
        qr_navigation.fetch_login_qr(device, poll_seconds=0.5)

    assert "3" in str(caught.value)
    assert device.clicks == [(2, *center), (4, *center), (6, *center)]
    assert device.screenshot_count <= 8


@pytest.mark.parametrize("name", ["other_login", "scan_login"])
def test_normal_login_action_disables_further_ad_closing(clock, name):
    initial, center = login_frame(name)
    ad, _ = ad_frame()
    device = ScriptedDevice([initial, ad, ad, qr_frame()], clock)

    assert_returned_qr(qr_navigation.fetch_login_qr(device, poll_seconds=0.5))

    assert device.clicks == [(1, *center)]


@pytest.mark.parametrize("ad_already_observed", [False, True])
def test_game_wait_has_priority_over_ad_and_preserves_later_ad_detection(clock, ad_already_observed):
    ad, ad_center = ad_frame()
    xml = (
        '<hierarchy><node package="android" resource-id="android:id/alertTitle" '
        'text="火影忍者没有响应"/><node package="android" '
        'resource-id="android:id/aerr_wait" text="等待" '
        'bounds="[460,606][1459,690]"/></hierarchy>'
    )
    prefix = [ad] if ad_already_observed else []
    wait_frame = len(prefix) + 1
    device = ScriptedDevice(
        prefix + [ad, ad, ad, qr_frame()], clock, xml_by_frame={wait_frame: xml}
    )

    assert_returned_qr(qr_navigation.fetch_login_qr(device, poll_seconds=0.5))

    assert device.clicks == [(wait_frame, 959, 648), (wait_frame + 2, *ad_center)]


def test_explicitly_disabled_startup_handling_never_searches_or_clicks_ads(clock, monkeypatch):
    ad, _ = ad_frame()
    scan, center = login_frame()
    device = ScriptedDevice([ad, ad, scan, qr_frame()], clock)
    monkeypatch.setattr(
        qr_navigation, "find_startup_ad_close",
        lambda frame: pytest.fail("startup ads must be disabled for this navigation"),
    )

    assert_returned_qr(qr_navigation.fetch_login_qr(
        device, poll_seconds=0.5, allow_startup_ads=False
    ))

    assert device.clicks == [(3, *center)]


def test_refresh_leaves_qr_then_delegates_with_startup_ads_disabled(clock, monkeypatch):
    scan, _ = login_frame()
    device = ScriptedDevice([qr_frame(), scan], clock)
    back_frames = []
    monkeypatch.setattr(device, "back", lambda: back_frames.append(device.screenshot_count), raising=False)
    authorized = lambda: False
    calls = []

    def fetch_again(actual_device, **kwargs):
        calls.append((actual_device, kwargs))
        return b"refreshed-qr"

    monkeypatch.setattr(qr_navigation, "fetch_login_qr", fetch_again)

    assert qr_navigation.refresh_login_qr(
        device, is_authorized=authorized, poll_seconds=0.5
    ) == b"refreshed-qr"

    assert calls == [(device, {
        "poll_seconds": 0.5,
        "is_authorized": authorized,
        "allow_startup_ads": False,
    })]
    assert back_frames == [1]
    assert device.clicks == []


@pytest.mark.parametrize("authorized_after_frames", [0, 1, 2])
def test_authorization_before_or_during_confirmation_prevents_ad_click(clock, authorized_after_frames):
    ad, _ = ad_frame()
    device = ScriptedDevice([ad, ad], clock)

    result = qr_navigation.fetch_login_qr(
        device, poll_seconds=0.5,
        is_authorized=lambda: device.screenshot_count >= authorized_after_frames,
    )

    assert result is None
    assert device.screenshot_count == authorized_after_frames
    assert device.clicks == []


@pytest.mark.parametrize("expires_after_frames", [0, 2])
def test_deadline_before_or_during_confirmation_prevents_ad_click(clock, expires_after_frames):
    ad, _ = ad_frame()

    def expire_after_capture(device):
        if device.screenshot_count == expires_after_frames:
            clock.now = device.deadline

    device = ScriptedDevice([ad, ad], clock, after_capture=expire_after_capture)
    if expires_after_frames == 0:
        device.deadline = clock.time()

    with pytest.raises(qr_navigation.QRNavigationError, match="deadline"):
        qr_navigation.fetch_login_qr(device, poll_seconds=0.5)

    assert device.screenshot_count == expires_after_frames
    assert device.clicks == []
