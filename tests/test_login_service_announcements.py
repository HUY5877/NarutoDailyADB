from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.login import popups
from naruto_daily.login_service import qr_navigation as navigation
from naruto_daily.login_service.qq_capture import QQCaptureSession
from test_login_service_channels import login_frame
from test_login_service_qr_navigation import frame_with
from test_login_service_startup_ads import paste_template, qr_frame


ROOT = Path(__file__).resolve().parents[1]


def announcement_frame(x=1530, y=90):
    frame = np.full((1080, 1920, 3), (90, 60, 30), np.uint8)
    center = paste_template(frame, 'announcement_close.png', x, y)
    return frame, center


def png(frame):
    success, encoded = cv2.imencode('.png', frame)
    assert success
    return encoded.tobytes()


def dismiss(*args, **kwargs):
    helper = getattr(popups, 'dismiss_announcement', None)
    assert callable(helper), '登录阶段必须复用已有公告清理函数'
    return helper(*args, **kwargs)


class PopupClient:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.taps = []

    def screenshot(self):
        return next(self.frames)

    def tap(self, x, y):
        self.taps.append((x, y))


def test_announcement_cleanup_rechecks_and_uses_latest_position():
    first, _ = announcement_frame()
    moved, center = announcement_frame(1560, 110)
    client = PopupClient([png(first), png(moved)])
    waits = []
    assert dismiss(client, png(first), asset_root=ROOT, sleep=waits.append)
    assert client.taps == [center]
    assert waits == [1.0, 1.0]


def test_announcement_that_disappears_is_not_clicked():
    frame, _ = announcement_frame()
    blank = png(np.zeros_like(frame))
    client = PopupClient([blank, blank])
    assert dismiss(client, png(frame), asset_root=ROOT, sleep=lambda _: None)
    assert client.taps == []


def test_without_announcement_no_extra_screenshot_sleep_or_click():
    client = PopupClient([])
    waits = []
    assert not dismiss(client, png(login_frame()), asset_root=ROOT, sleep=waits.append)
    assert client.taps == [] and waits == []


def test_announcement_recheck_that_turns_portrait_returns_without_click():
    frame, _ = announcement_frame()
    portrait = png(np.zeros((1920, 1080, 3), np.uint8))
    client = PopupClient([portrait, portrait])
    assert dismiss(client, png(frame), asset_root=ROOT, sleep=lambda _: None)
    assert client.taps == []


class Clock:
    now = 100.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class LoginDevice:
    def __init__(self, clock, channel):
        self.clock, self.login_type = clock, channel
        self.deadline = clock.time() + 12
        self.stage = 0
        self.taps = []
        self.announcement, self.close_center = announcement_frame()

    def screenshot(self):
        if self.stage == 0:
            return self.announcement.copy()
        return login_frame() if self.stage == 1 else qr_frame()

    def hierarchy(self):
        return ''

    def _run(self, *_):
        return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity'
                if self.stage == 2 else b'mResumedActivity: com.tencent.KiHan/.GameActivity')

    def tap(self, x, y):
        assert self.clock.time() < self.deadline
        if self.stage == 0:
            assert (x, y) == self.close_center
        else:
            expected = navigation.choose_action(login_frame(), '', self.login_type)
            assert (x, y) == (expected.x, expected.y)
        self.taps.append((x, y))
        self.stage += 1


class RestoredLoginDevice(LoginDevice):
    returned = False

    def screenshot(self):
        if self.stage == 1 and not self.returned:
            frame = frame_with('start_game', 780, 820)
            back = frame_with('game_back', 1680, 30)
            frame[:170, 1650:1850] = back[:170, 1650:1850]
            return frame
        return super().screenshot()

    def tap(self, x, y):
        if self.stage == 1 and not self.returned:
            action = navigation.choose_action(self.screenshot(), '', self.login_type)
            assert action.name == 'game_back' and (x, y) == (action.x, action.y)
            self.taps.append((x, y))
            self.returned = True
            return
        super().tap(x, y)


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
@pytest.mark.parametrize('device_type', [LoginDevice, RestoredLoginDevice])
def test_capture_closes_announcement_before_selecting_requested_qq(channel, device_type):
    clock = Clock()
    device = device_type(clock, channel)
    session = object.__new__(QQCaptureSession)
    session.device, session._clock, session._sleep = device, clock.time, clock.sleep
    session._open_login_channel()
    assert device.stage == 2
    assert device.taps[0] == device.close_center


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
@pytest.mark.parametrize('device_type', [LoginDevice, RestoredLoginDevice])
def test_original_qr_flow_closes_announcement_even_when_ads_disabled(monkeypatch, channel, device_type):
    clock = Clock()
    monkeypatch.setattr(navigation.time, 'time', clock.time)
    monkeypatch.setattr(navigation.time, 'sleep', clock.sleep)
    device = device_type(clock, channel)
    result = navigation.fetch_login_qr(device, require_channel_selection=True,
                                       allow_startup_ads=False)
    assert result and result.startswith(b'\x89PNG')
    assert device.stage == 2
    assert device.taps[0] == device.close_center


def test_announcement_recheck_keeps_original_login_deadline():
    clock = Clock()
    device = LoginDevice(clock, 'android_qq')
    device.deadline = clock.time() + .25
    with pytest.raises(navigation.QRNavigationError, match='deadline'):
        navigation.dismiss_login_announcement(device, device.screenshot(),
                                              clock=clock.time, sleep=clock.sleep)
    assert clock.time() == device.deadline
    assert device.taps == []


def test_announcement_recheck_does_not_close_a_new_authorization_page():
    clock = Clock()
    device = LoginDevice(clock, 'android_qq')
    original = device.screenshot()
    authorization = qr_frame()
    paste_template(authorization, 'announcement_close.png', 1530, 90)
    device.screenshot = lambda: authorization.copy()
    assert navigation.dismiss_login_announcement(device, original,
                                                 clock=clock.time, sleep=clock.sleep)
    assert device.taps == []


@pytest.mark.parametrize('restore', [False, True])
def test_recognized_channel_or_start_game_does_not_probe_announcement(monkeypatch, restore):
    clock = Clock()
    device = RestoredLoginDevice(clock, 'android_qq') if restore else LoginDevice(clock, 'android_qq')
    device.stage = 1
    monkeypatch.setattr(navigation, 'dismiss_login_announcement',
                        lambda *_args, **_kwargs: pytest.fail('已识别到登录步骤时不能检测公告'))
    session = object.__new__(QQCaptureSession)
    session.device, session._clock, session._sleep = device, clock.time, clock.sleep
    session._open_login_channel()
    assert device.stage == 2
