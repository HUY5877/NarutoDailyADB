"""Unconfirmed read-only observations keep the original execution deadline."""
from types import SimpleNamespace

import cv2
import numpy as np

import pytest

from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, current_execution, execution_scope, retry_observation,
)
from naruto_daily.common.navigation import navigate_to_card, required_template_tap
from naruto_daily.common.environment import EnvironmentConfigError
from naruto_daily.multimodal.client import MultimodalError
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.return_to_main import return_to_main
from test_execution import Clock
from test_login_service_daily import session_for
from test_main_screen import action as main_action, answer, fake_model, png, target
from test_navigation import card_action


class Frames:
    def __init__(self):
        self.frames = []
        self.taps = []

    def screenshot(self):
        frame = str(len(self.frames) + 1).encode()
        self.frames.append(frame)
        return frame

    def tap(self, *point):
        self.taps.append(point)


def test_visual_misses_retry_fresh_observations_past_three_attempts():
    clock, device = Clock(), Frames()

    def observe():
        frame = device.screenshot()
        if len(device.frames) <= 4:
            raise VisualError('结果动画尚未确认结束')
        return frame

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        assert retry_observation(observe, name='结果核验') == b'5'

    assert device.frames == [b'1', b'2', b'3', b'4', b'5']
    assert not device.taps


def test_visual_misses_stop_at_original_deadline_without_another_frame():
    clock, device = Clock(), Frames()

    def observe():
        device.screenshot()
        raise VisualError('体力数值不明确')

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(3, clock=clock, sleep=clock.sleep):
            retry_observation(observe)

    assert clock() == 3
    assert not device.taps
    assert current_execution() is None


@pytest.mark.parametrize('failure', [AdbError('设备离线'), ValueError('无效配置')])
def test_hard_device_or_configuration_error_does_not_retry(failure):
    clock, device = Clock(), Frames()

    def observe():
        device.screenshot()
        raise failure

    with pytest.raises(type(failure)) as caught:
        with execution_scope(30, clock=clock, sleep=clock.sleep):
            retry_observation(observe)

    assert caught.value is failure
    assert device.frames == [b'1'] and clock() == 0


def test_visual_error_without_execution_scope_preserves_legacy_failure():
    device = Frames()
    failure = VisualError('按钮编号未确认')

    def observe():
        device.screenshot()
        raise failure

    with pytest.raises(VisualError) as caught:
        retry_observation(observe)

    assert caught.value is failure and device.frames == [b'1']


def test_main_menu_can_confirm_after_call_cap_and_clicks_latest_position(monkeypatch):
    clock, device, replies = Clock(), Frames(), []

    def complete(*args, **kwargs):
        replies.append(True)
        if len(replies) <= 4:
            return answer(found=False)
        return answer(candidate_id=3 if len(replies) == 5 else 4)

    fake_model(monkeypatch, complete)
    device.screenshot = lambda: png(96, 54)
    action = main_action()
    action['multimodal']['max_calls'] = 1
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        required_template_tap(device, action, sleep=clock.sleep)

    assert device.taps == [(29, 50)]
    assert len(replies) == 6


def test_invalid_main_model_candidates_reobserve_instead_of_clicking(monkeypatch):
    clock, device, frames = Clock(), Frames(), []

    def screenshot():
        frame = png(96, 54, color=len(frames) * 20)
        frames.append(frame)
        return frame

    responses = iter([answer(candidate_id=999)] * 4 + [answer(candidate_id=4)])
    fake_model(monkeypatch, lambda *args, **kwargs: next(responses))
    device.screenshot = screenshot
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        center, _ = ImageRecognition(device).find(target())

    assert center == (29, 50)
    assert len(frames) == len(set(frames)) == 5
    assert not device.taps


def test_main_menu_unknown_until_deadline_never_clicks(monkeypatch):
    clock, device = Clock(), Frames()
    fake_model(monkeypatch, lambda *args, **kwargs: answer(found=False))
    device.screenshot = lambda: png(96, 54)
    action = main_action()
    action['multimodal']['max_calls'] = 1
    action['timeout_seconds'] = .1

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(3, clock=clock, sleep=clock.sleep):
            required_template_tap(device, action, sleep=clock.sleep)

    assert clock() == 3 and not device.taps


def test_extra_login_home_check_observes_until_confirmed_without_replaying_actions(tmp_path, monkeypatch):
    session, clock, calls = session_for(tmp_path)
    session._authorized = True
    start = clock()
    cleanup_runs = []

    def cleanup(actions):
        cleanup_runs.append(actions)

    def match(*args):
        return ((1800, 500), 1) if clock() >= start + 5 else (None, 0)

    monkeypatch.setattr(session, '_runner', lambda: SimpleNamespace(run=cleanup))
    session._template_matcher = match
    session.finish_login(start + 20)

    assert session._logged_in
    assert clock() >= start + 5
    assert len(cleanup_runs) == 1
    assert not any('tap' in command for command, _ in calls)


def test_extra_login_home_check_uses_original_deadline_when_never_confirmed(tmp_path, monkeypatch):
    session, clock, calls = session_for(tmp_path, matcher=lambda *args: (None, 0))
    session._authorized = True
    monkeypatch.setattr(session, '_runner', lambda: SimpleNamespace(run=lambda actions: None))
    deadline = clock() + 3

    with pytest.raises(DailyDeadlineExceeded):
        session.finish_login(deadline)

    assert clock() == deadline
    assert not session._logged_in
    assert not any('tap' in command for command, _ in calls)


def return_action():
    def marker(name):
        return dict(name=name, template=name + '.png', left=0, top=0,
                    right=1920, bottom=1080, threshold=.9)

    return dict(max_rounds=1, idle_rounds=1, poll_seconds=1,
                pre_click_wait_seconds=1, post_click_wait_seconds=1,
                main_page_marker=marker('home'), close_target=marker('close'))


def test_return_home_observes_past_idle_and_round_caps_without_clicking_unknown_page():
    clock, device = Clock(), Frames()

    def match(frame, template, *args):
        return ((900, 500), 1) if template.stem == 'home' and clock() >= 5 else (None, 0)

    with execution_scope(20, clock=clock, sleep=clock.sleep):
        result = return_to_main(device, return_action(), template_matcher=match, sleep=clock.sleep)

    assert result.rounds > 1 and result.clicks == 0
    assert clock() >= 5 and not device.taps


def test_return_home_rechecks_latest_close_and_continues_after_round_cap():
    clock, device = Clock(), Frames()

    def match(frame, template, *args):
        if device.taps:
            return ((900, 500), 1) if template.stem == 'home' and clock() >= 5 else (None, 0)
        if template.stem == 'close':
            return (int(frame) * 10, 20), 1
        return None, 0

    with execution_scope(20, clock=clock, sleep=clock.sleep):
        result = return_to_main(device, return_action(), template_matcher=match, sleep=clock.sleep)

    assert result.clicks == 1 and result.rounds > 1
    assert device.taps == [(20, 20)]


def test_return_home_never_confirmed_stops_at_shared_deadline():
    clock, device = Clock(), Frames()
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(3, clock=clock, sleep=clock.sleep):
            return_to_main(device, return_action(), template_matcher=lambda *args: (None, 0),
                           sleep=clock.sleep)
    assert clock() == 3 and not device.taps


@pytest.mark.parametrize('problem', ['missing', 'corrupt', 'transparent', 'region'])
def test_invalid_template_remains_immediate_configuration_error(tmp_path, problem):
    clock, frames = Clock(), []
    template = tmp_path / 'target.png'
    region = (0, 0, 96, 54)
    if problem == 'corrupt':
        template.write_bytes(b'not a PNG')
    elif problem == 'transparent':
        template.write_bytes(cv2.imencode('.png', np.zeros((10, 10, 4), dtype=np.uint8))[1].tobytes())
    elif problem == 'region':
        template.write_bytes(png(10, 10))
        region = (0, 0, 5, 5)

    def observe():
        frames.append(True)
        return find_template_in_png(png(96, 54), template, region, .9)

    with pytest.raises(VisualError):
        with execution_scope(20, clock=clock, sleep=clock.sleep):
            retry_observation(observe)
    assert frames == [True] and clock() == 0


def test_lazy_main_model_configuration_error_does_not_retry(monkeypatch):
    from naruto_daily.multimodal import client as model_client

    clock, device, loads = Clock(), Frames(), []
    device.screenshot = lambda: png(96, 54)

    def load_environment():
        loads.append(True)
        raise EnvironmentConfigError('缺少模型环境配置')

    monkeypatch.setattr(model_client, 'read_environment', load_environment)
    with pytest.raises(MultimodalError):
        with execution_scope(3, clock=clock, sleep=clock.sleep):
            ImageRecognition(device).find(target())

    assert loads == [True] and clock() == 0


class CardFrames(Frames):
    def __init__(self):
        super().__init__()
        self.swipes = []

    def swipe(self, *movement):
        self.swipes.append(movement)


def navigation_action(max_swipes):
    return dict(card_action(), max_swipes=max_swipes,
                pre_click_wait_seconds=1, post_click_wait_seconds=1)


def card_matcher(device, *, available):
    def match(frame, template, *args):
        if not available(frame):
            return None, 0
        if template.name == 'organization_pray_label.png':
            return (562, 394), 1
        if template.name == 'go_now_button.png':
            return (int(frame) * 10, 580), 1
        return None, 0
    return match


def test_reward_card_at_swipe_cap_waits_without_more_swipes_then_clicks_latest_frame():
    clock, device = Clock(), CardFrames()
    matcher = card_matcher(device, available=lambda frame: clock() >= 8)
    with execution_scope(20, clock=clock, sleep=clock.sleep):
        result = navigate_to_card(device, navigation_action(2), template_matcher=matcher, sleep=clock.sleep)

    assert result.swipes == len(device.swipes) == 2 and not result.completed
    assert device.taps == [(80, 580)]


def test_reward_card_never_confirmed_stops_at_deadline_without_swiping_past_cap():
    clock, device = Clock(), CardFrames()
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(8, clock=clock, sleep=clock.sleep):
            navigate_to_card(device, navigation_action(1), template_matcher=lambda *args: (None, 0),
                             sleep=clock.sleep)
    assert clock() == 8 and len(device.swipes) == 1 and not device.taps


@pytest.mark.parametrize('missing', ['button', 'card'])
def test_reward_card_after_alignment_observes_missing_target_without_another_swipe(missing):
    clock, device = Clock(), CardFrames()
    latest = card_matcher(device, available=lambda frame: clock() >= 8)

    def match(frame, template, *args):
        if template.name == 'organization_pray_label.png' and (
                missing == 'button' or int(frame) <= 2):
            return (562, 394), 1
        return latest(frame, template, *args)

    with execution_scope(20, clock=clock, sleep=clock.sleep):
        result = navigate_to_card(device, navigation_action(1), template_matcher=match, sleep=clock.sleep)

    assert result.swipes == len(device.swipes) == 1 and not result.completed
    assert device.taps == [(90, 580)]


def test_reward_card_button_missing_during_click_check_waits_for_fresh_position():
    clock, device = Clock(), CardFrames()
    matcher = card_matcher(device, available=lambda frame: frame == b'1' or clock() >= 6)
    with execution_scope(20, clock=clock, sleep=clock.sleep):
        navigate_to_card(device, navigation_action(3), template_matcher=matcher, sleep=clock.sleep)

    assert not device.swipes
    assert device.taps == [(80, 580)]


def test_reward_card_at_zero_swipe_cap_does_not_use_an_alignment_swipe():
    clock, device = Clock(), CardFrames()

    def match(frame, template, *args):
        if template.name == 'organization_pray_label.png':
            return (562, 394), 1
        if template.name == 'go_now_button.png' and clock() >= 5:
            return (int(frame) * 10, 580), 1
        return None, 0

    with execution_scope(20, clock=clock, sleep=clock.sleep):
        navigate_to_card(device, navigation_action(0), template_matcher=match, sleep=clock.sleep)

    assert not device.swipes
    assert device.taps == [(70, 580)]
