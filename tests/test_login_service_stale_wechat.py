"""A restored WeChat request must finish cancellation before selecting QQ.

Screenshots contain real UI templates and fixed, public synthetic QR payloads.
Only the device and clock boundaries are simulated; navigation is exercised.
"""

import numpy as np
import pytest
from pathlib import Path
from types import SimpleNamespace

from naruto_daily.login_service import qr_navigation as navigation
from test_login_service_channels import login_frame, wechat_frame
from test_login_service_web_qr import assert_test_qr, frame as qq_frame
from test_login_service_qr_navigation import frame_with


class Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    result = Clock()
    monkeypatch.setattr(navigation.time, 'time', result.time)
    monkeypatch.setattr(navigation.time, 'sleep', result.sleep)
    return result


class RestoredWeChatDevice:
    def __init__(self, clock, channel, *, image=None, cancellation_stuck=False):
        self.clock = clock
        self.deadline = clock.time() + 14
        self.login_type = channel
        self.game_package = 'com.tencent.KiHan'
        self.old_page = wechat_frame() if image is None else image
        self.selector = login_frame()
        self.qq_page = qq_frame()
        self.state = 'wechat'
        self.cancellation_stuck = cancellation_stuck
        self.cancelled_at = None
        self.events = []

    def screenshot(self):
        return {'wechat': self.old_page, 'selector': self.selector, 'qq': self.qq_page}[self.state].copy()

    def hierarchy(self):
        return ''

    def back(self):
        pytest.fail('The restored WeChat SDK request needs its Cancel control, not Android Back')

    def tap(self, x, y):
        assert self.clock.time() < self.deadline, 'No input is permitted after the task deadline'
        if self.state == 'wechat':
            assert 0 <= x < 180 and 36 <= y < 111, 'Only the observed WeChat Cancel control is safe'
            assert self.cancelled_at is None, 'Cancellation must not be sent twice'
            self.cancelled_at = self.clock.time()
            self.events.append('wechat_cancel')
            if not self.cancellation_stuck:
                # The selector appears before the SDK has finished its callback.
                self.state = 'selector'
            return
        assert self.state == 'selector', 'Do not tap the newly returned QQ authorization page'
        assert self.cancelled_at is not None
        assert self.clock.time() - self.cancelled_at >= 3, 'Wait for the old SDK cancellation callback'
        left, right = (1400, 1850) if self.login_type == 'android_qq' else (950, 1400)
        assert left < x < right and 720 < y < 900, 'Select only this task\'s Android/iOS QQ channel'
        self.events.append(self.login_type)
        self.state = 'qq'


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_initial_qq_navigation_cancels_restored_wechat_before_selecting_channel(clock, channel):
    device = RestoredWeChatDevice(clock, channel)

    png = navigation.fetch_login_qr(device, poll_seconds=.5)

    assert_test_qr(png)
    assert device.events == ['wechat_cancel', channel]


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_unknown_portrait_page_never_triggers_a_cancel_or_channel_click(clock, channel):
    unknown = np.full((1920, 1080, 3), 255, np.uint8)
    device = RestoredWeChatDevice(clock, channel, image=unknown)

    with pytest.raises(navigation.QRNavigationError):
        navigation.fetch_login_qr(device, poll_seconds=.5)

    assert device.events == []
    assert clock.time() <= device.deadline


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_recognized_wechat_without_cancel_control_fails_without_guessing_input(clock, channel):
    missing_cancel = wechat_frame()
    missing_cancel[36:111, :180] = 255
    device = RestoredWeChatDevice(clock, channel, image=missing_cancel)

    with pytest.raises(navigation.QRNavigationError, match='WeChat|微信'):
        navigation.fetch_login_qr(device, poll_seconds=.5)

    assert device.events == []


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_wechat_cancellation_that_does_not_leave_is_sent_once_then_fails(clock, channel):
    device = RestoredWeChatDevice(clock, channel, cancellation_stuck=True)

    with pytest.raises(navigation.QRNavigationError):
        navigation.fetch_login_qr(device, poll_seconds=.5)

    assert device.events == ['wechat_cancel']
    assert clock.time() <= device.deadline


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_refresh_uses_observed_wechat_cancel_even_when_requested_channel_is_qq(clock, channel):
    device = RestoredWeChatDevice(clock, channel)

    png = navigation.refresh_login_qr(device, is_authorized=lambda: False, poll_seconds=.5)

    assert_test_qr(png)
    assert device.events == ['wechat_cancel', channel]


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
@pytest.mark.parametrize('authorized_after_checks', [0, 1])
def test_authorization_wins_before_any_restored_wechat_cancellation(clock, channel, authorized_after_checks):
    device = RestoredWeChatDevice(clock, channel)
    observations = iter([False] * authorized_after_checks + [True])

    result = navigation.refresh_login_qr(device, is_authorized=lambda: next(observations), poll_seconds=.5)

    assert result is None
    assert device.events == []


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_refresh_does_not_cancel_again_if_old_wechat_returns_during_fetch(clock, channel):
    class ReopenedWeChatDevice(RestoredWeChatDevice):
        screenshot_count = 0

        def screenshot(self):
            self.screenshot_count += 1
            if self.screenshot_count == 3:
                # Refresh saw the selector; its delegated fetch sees the old
                # request again before this task has clicked the QQ channel.
                self.state = 'wechat'
            return super().screenshot()

    device = ReopenedWeChatDevice(clock, channel)

    with pytest.raises(navigation.QRNavigationError):
        navigation.refresh_login_qr(device, is_authorized=lambda: False, poll_seconds=.5)

    assert device.events == ['wechat_cancel']
    assert clock.time() <= device.deadline


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_expired_wechat_recognized_only_by_xml_is_cancelled_before_reading_qr(clock, channel):
    class XmlWeChatDevice(RestoredWeChatDevice):
        def hierarchy(self):
            if self.state != 'wechat':
                return ''
            return ('<hierarchy>'
                    '<node package="com.tencent.KiHan" resource-id="com.tencent.KiHan:id/qrcode_iv" />'
                    '<node package="com.tencent.KiHan" resource-id="com.tencent.KiHan:id/qrcode_status_tv" '
                    'text="二维码已过期" />'
                    '</hierarchy>')

    old_page = wechat_frame()
    old_page[1100:1360, 250:830] = 255
    device = XmlWeChatDevice(clock, channel, image=old_page)

    png = navigation.fetch_login_qr(device, poll_seconds=.5)

    assert_test_qr(png)
    assert device.events == ['wechat_cancel', channel]


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
@pytest.mark.parametrize('refresh', [False, True])
def test_deadline_during_wechat_cancellation_wait_never_clicks_qq_afterwards(clock, channel, refresh):
    device = RestoredWeChatDevice(clock, channel)
    device.deadline = clock.time() + 2

    with pytest.raises(navigation.QRNavigationError):
        if refresh:
            navigation.refresh_login_qr(device, is_authorized=lambda: False, poll_seconds=.5)
        else:
            navigation.fetch_login_qr(device, poll_seconds=.5)

    assert device.events == ['wechat_cancel']
    assert clock.time() == device.deadline


class StrictNavigationDevice:
    def __init__(self, clock, channel, old_kind, *, stuck=False, qq_layers=(), delayed_selector=0):
        self.clock, self.login_type = clock, channel
        self.deadline = clock.time() + 20
        self.game_package = 'com.tencent.KiHan'
        self.old_kind, self.state = old_kind, 'old'
        self.events = []
        self.stuck = stuck
        self.qq_layers = list(qq_layers)
        self.delayed_selector = delayed_selector
        self.cancelled_at = None
        self.selector = login_frame()

    def is_huy_login_activity(self):
        return self.state == 'old' and self.old_kind == 'tim'

    def screenshot(self):
        if self.state == 'old':
            if self.old_kind == 'wechat':
                return wechat_frame()
            if self.old_kind in ('web', 'native'):
                return qq_frame(page=self.old_kind)
            if self.old_kind == 'start_game':
                value = frame_with('start_game', 800, 850)
                back = frame_with('game_back', 1700, 40)
                value[10:170, 1650:1850] = back[10:170, 1650:1850]
                return value
            return np.full((1080, 1920, 3), 255, np.uint8)
        if self.state == 'layer':
            name = self.qq_layers[0]
            x, y = {'scan_login': (677, 940), 'other_login': (1059, 1001),
                    'switch_account': (820, 900), 'add_account': (1700, 750)}[name]
            return frame_with(name, x, y)
        if self.state == 'selector' or self.delayed_selector:
            if self.state != 'selector':
                self.delayed_selector -= 1
            return self.selector.copy()
        if self.state == 'scan':
            return frame_with('scan_login', 677, 940)
        return wechat_frame() if self.login_type.endswith('_wechat') else qq_frame()

    def hierarchy(self):
        return ''

    def back(self):
        assert self.clock.time() < self.deadline
        if self.state == 'layer':
            self.events.append('back_' + self.qq_layers.pop(0))
            self.state = 'layer' if self.qq_layers else 'selector'
        else:
            assert self.state == 'old' and self.old_kind in ('tim', 'web', 'native')
            assert 'back_' + self.old_kind not in self.events, 'Do not repeat cancellation on the same page'
            self.events.append('back_' + self.old_kind)
            if not self.stuck:
                self.state = 'layer' if self.qq_layers else 'selector'

    def tap(self, x, y):
        assert self.clock.time() < self.deadline
        if self.state == 'old' and self.old_kind == 'wechat':
            assert 0 <= x < 180 and 36 <= y < 111
            assert self.cancelled_at is None
            self.cancelled_at = self.clock.time()
            self.events.append('wechat_cancel')
            self.state = 'selector'
            return
        if self.state == 'old' and self.old_kind == 'start_game':
            assert 1650 < x < 1850 and 10 < y < 170
            self.events.append('game_back')
            self.state = 'selector'
            return
        if self.state == 'scan':
            assert 540 < x < 1375 and 840 < y < 1080
            self.events.append('scan_login')
            self.state = 'fresh'
            return
        assert self.state == 'selector', 'The game channel must not be clicked again on a transition frame'
        if self.cancelled_at is not None:
            assert self.clock.time() - self.cancelled_at >= 3
        left, right = {'android_qq': (1400, 1850), 'ios_qq': (950, 1400),
                       'android_wechat': (550, 1000), 'ios_wechat': (150, 600)}[self.login_type]
        assert left < x < right and 720 < y < 900
        self.events.append(self.login_type)
        self.state = 'scan' if self.old_kind == 'native' and self.login_type.endswith('_qq') else 'fresh'


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq', 'android_wechat', 'ios_wechat'])
@pytest.mark.parametrize('old_kind', ['web', 'native', 'wechat', 'tim'])
def test_strict_initial_navigation_reselects_channel_before_accepting_old_authorization(clock, channel, old_kind):
    device = StrictNavigationDevice(clock, channel, old_kind)

    result = navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)

    assert isinstance(result, bytes) and result.startswith(b'\x89PNG')
    cancellation = 'wechat_cancel' if old_kind == 'wechat' else 'back_' + old_kind
    assert device.events == [cancellation, channel] + (['scan_login'] if old_kind == 'native' and channel.endswith('_qq') else [])


@pytest.mark.parametrize('old_kind', ['web', 'native', 'tim'])
def test_strict_cancel_that_never_leaves_is_bounded_and_never_publishes_old_qr(clock, old_kind):
    device = StrictNavigationDevice(clock, 'android_qq', old_kind, stuck=True)

    with pytest.raises(navigation.QRNavigationError):
        navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)

    assert device.events == ['back_' + old_kind]
    assert clock.time() <= 110


def test_strict_native_qq_backtracks_known_layers_without_clicking_old_scan_login(clock):
    device = StrictNavigationDevice(clock, 'ios_qq', 'native', qq_layers=['scan_login', 'other_login'])

    assert_test_qr(navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True))

    assert device.events == ['back_native', 'back_scan_login', 'back_other_login', 'ios_qq', 'scan_login']


def test_strict_wechat_reselection_backtracks_confirmed_old_qq_layers(clock):
    device = StrictNavigationDevice(clock, 'ios_wechat', 'native', qq_layers=['scan_login', 'other_login'])

    assert navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True).startswith(b'\x89PNG')

    assert device.events == ['back_native', 'back_scan_login', 'back_other_login', 'ios_wechat']


def test_strict_qq_backtracking_stops_at_four_known_layers(clock):
    device = StrictNavigationDevice(clock, 'ios_qq', 'native',
                                    qq_layers=['scan_login', 'other_login', 'switch_account', 'add_account'])

    with pytest.raises(navigation.QRNavigationError):
        navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)

    assert device.events == ['back_native', 'back_scan_login', 'back_other_login', 'back_switch_account']


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_strict_reselects_qq_when_late_old_wechat_appears_after_first_channel_tap(clock, channel):
    class LateWeChatDevice(StrictNavigationDevice):
        restored = False

        def screenshot(self):
            if self.state == 'fresh' and not self.restored:
                self.old_kind, self.state, self.restored = 'wechat', 'old', True
            return super().screenshot()

    device = LateWeChatDevice(clock, channel, 'web')
    device.state = 'selector'

    assert_test_qr(navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True))

    assert device.events == [channel, 'wechat_cancel', channel]


def test_strict_unknown_qq_intermediate_page_is_not_treated_as_selected_channel(clock):
    device = StrictNavigationDevice(clock, 'android_qq', 'web', qq_layers=['scan_login'])
    device.state = 'layer'

    with pytest.raises(navigation.QRNavigationError):
        navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)

    assert device.events == []


def test_strict_selected_qq_waits_through_stale_selector_frames_without_reclicking(clock):
    device = StrictNavigationDevice(clock, 'android_qq', 'web', delayed_selector=2)

    assert_test_qr(navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True))

    assert device.events == ['back_web', 'android_qq']


@pytest.mark.parametrize('channel', ['android_qq', 'ios_wechat'])
def test_strict_existing_start_game_page_returns_to_selector_before_current_channel(clock, channel):
    device = StrictNavigationDevice(clock, channel, 'start_game')

    assert navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True).startswith(b'\x89PNG')

    assert device.events == ['game_back', channel]


@pytest.mark.parametrize('activity,expected', [
    ('mResumedActivity: ActivityRecord{a u0 com.tencent.tim/com.tencent.open.agent.AgentActivity t1}', True),
    ('topResumedActivity: ActivityRecord{a u0 com.tencent.tim/com.tencent.open.agent.AgentActivity t1}', True),
    ('mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivityFake', False),
    ('mResumedActivity: com.tencent.other/com.tencent.open.agent.AgentActivity', False),
    ('mResumedActivity: com.tencent.tim/.MainActivity\nother: com.tencent.tim/com.tencent.open.agent.AgentActivity', False),
])
def test_huy_activity_detection_requires_exact_resumed_package_and_activity(monkeypatch, clock, activity, expected):
    device = navigation.LoginDevice(Path('unused-adb'), 'emulator-5554', clock.time() + 10)
    monkeypatch.setattr(device, '_run', lambda *args: activity.encode())

    assert device.is_huy_login_activity() is expected


def test_strict_tim_reappearing_after_channel_selection_fails_with_partial_install_diagnostic(clock):
    class PartialInstallDevice(StrictNavigationDevice):
        def is_huy_login_activity(self):
            return self.state == 'fresh'

    device = PartialInstallDevice(clock, 'android_qq', 'web')
    with pytest.raises(navigation.QRNavigationError, match='两款辅助应用|完整安装'):
        navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)
    assert device.events == ['back_web', 'android_qq']


def test_strict_rechecks_tim_before_counting_a_channel_tap(clock):
    class RacingDevice(StrictNavigationDevice):
        checks_on_selector = 0
        restored = False

        def is_huy_login_activity(self):
            if self.state == 'selector' and not self.restored:
                self.checks_on_selector += 1
                if self.checks_on_selector == 2:
                    self.old_kind, self.state, self.restored = 'tim', 'old', True
            return self.state == 'old' and self.old_kind == 'tim'

    device = RacingDevice(clock, 'ios_qq', 'web')

    assert_test_qr(navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True))

    assert device.events == ['back_web', 'back_tim', 'ios_qq']


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq', 'android_wechat', 'ios_wechat'])
def test_qq_authorization_detection_does_not_dump_ui_on_known_game_selector(channel):
    device = SimpleNamespace(login_type=channel, hierarchy=lambda: pytest.fail('Known game pages do not need an XML dump'))

    assert navigation.qq_authorization_page(device, login_frame()) is False


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq', 'android_wechat', 'ios_wechat'])
def test_strict_game_back_after_first_selection_requires_a_fresh_channel_tap(clock, channel):
    class LateStartGameDevice(StrictNavigationDevice):
        restored = False

        def screenshot(self):
            if self.state == 'fresh' and not self.restored:
                self.old_kind, self.state, self.restored = 'start_game', 'old', True
            return super().screenshot()

    device = LateStartGameDevice(clock, channel, 'web')
    device.state = 'selector'

    result = navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)
    if channel.endswith('_qq'):
        assert_test_qr(result)
    else:
        assert result.startswith(b'\x89PNG')

    assert device.events == [channel, 'game_back', channel]


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
@pytest.mark.parametrize('late_kind', ['wechat', 'start_game'])
def test_completed_cancel_wait_does_not_expire_a_later_return_to_selector(clock, channel, late_kind):
    class DelayedRestoreDevice(StrictNavigationDevice):
        restored = False

        def screenshot(self):
            if self.state == 'fresh' and not self.restored:
                self.clock.now += 11
                self.old_kind, self.state, self.restored = late_kind, 'old', True
            return super().screenshot()

    device = DelayedRestoreDevice(clock, channel, 'web')
    device.deadline = clock.time() + 60

    assert_test_qr(navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True))

    late_action = 'wechat_cancel' if late_kind == 'wechat' else 'game_back'
    assert device.events == ['back_web', channel, late_action, channel]


def test_later_selector_wait_keeps_previous_wechat_cancellation_deduplication(clock):
    class RepeatedWeChatDevice(StrictNavigationDevice):
        restored_at = None

        def screenshot(self):
            if self.state == 'fresh' and self.restored_at is None:
                self.clock.now += 11
                self.restored_at = self.clock.time()
                self.state = 'old'
            return super().screenshot()

    device = RepeatedWeChatDevice(clock, 'android_qq', 'wechat')
    device.deadline = clock.time() + 60

    with pytest.raises(navigation.QRNavigationError):
        navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)

    assert device.events == ['wechat_cancel', 'android_qq']
    assert device.restored_at <= clock.time() <= device.restored_at + 10


@pytest.mark.parametrize('channel', ['android_wechat', 'ios_wechat'])
def test_strict_late_tim_after_wechat_selection_is_cancelled_before_reselecting_wechat(clock, channel):
    class LateTimDevice(StrictNavigationDevice):
        restored = False

        def is_huy_login_activity(self):
            if self.state == 'fresh' and not self.restored:
                self.old_kind, self.state, self.restored = 'tim', 'old', True
            return self.state == 'old' and self.old_kind == 'tim'

    device = LateTimDevice(clock, channel, 'web')
    device.state = 'selector'

    result = navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)

    assert isinstance(result, bytes) and result.startswith(b'\x89PNG')
    assert device.events == [channel, 'back_tim', channel]


@pytest.mark.parametrize('channel', ['android_wechat', 'ios_wechat'])
@pytest.mark.parametrize('qq_kind', ['web', 'native'])
def test_strict_late_qq_qr_after_wechat_selection_is_cancelled_before_reselecting_wechat(clock, channel, qq_kind):
    class LateQQDevice(StrictNavigationDevice):
        restored = False

        def screenshot(self):
            if self.state == 'fresh' and not self.restored:
                self.old_kind, self.state, self.restored = qq_kind, 'old', True
                if qq_kind == 'native':
                    self.qq_layers = ['scan_login', 'other_login']
            return super().screenshot()

    device = LateQQDevice(clock, channel, 'web')
    device.state = 'selector'

    result = navigation.fetch_login_qr(device, poll_seconds=.5, require_channel_selection=True)

    assert isinstance(result, bytes) and result.startswith(b'\x89PNG')
    layers = ['back_scan_login', 'back_other_login'] if qq_kind == 'native' else []
    assert device.events == [channel, 'back_' + qq_kind, *layers, channel]
