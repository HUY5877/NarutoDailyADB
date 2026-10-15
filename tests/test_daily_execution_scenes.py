"""Offline scene regressions for fresh observations under the shared deadline."""
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from naruto_daily.battle import run_battle
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, current_execution, execution_scope,
)
from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.multimodal.client import MultimodalRequestError
from naruto_daily.secret_realm.recognition import wait_for_tickets
from test_battle import Clock, battle_config, target
from test_battle_retry_frames import Device, model_for, request_image, response_for


@pytest.fixture(autouse=True)
def offline_jev_environment(monkeypatch, configured_environment):
    monkeypatch.setenv('JEV_API_KEY', 'offline-jev-key')


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


def temporary_failure():
    return MultimodalRequestError('offline network timeout', retryable=True)


def test_direct_main_match_retries_fresh_preview_and_uses_latest_dimensions(monkeypatch):
    from naruto_daily.common import main_screen
    from test_main_screen import answer, png, target as main_target

    clock, device, seen = Clock(), Frames(), []
    frames = iter([png(960, 540)] * 4 + [png(1920, 1080)])
    device.screenshot = lambda: next(frames)

    def complete(preview, prompt, *, timeout_seconds):
        seen.append(preview)
        if len(seen) <= 4:
            raise temporary_failure()
        return answer()

    monkeypatch.setattr(main_screen, 'MultimodalClient',
                        lambda *a, **kw: SimpleNamespace(complete_json=complete))
    with execution_scope(120, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device)
        images.main_call_limit = 1
        center, _ = images.match(device.screenshot(), main_target(), timeout_seconds=.1)

    assert center == (420, 995)  # Last frame is twice the size of the first.
    assert len(seen) == 5 and clock() == 40
    assert images.main_calls == {'查找': 1, '点击前复查': 0}


def test_secret_entry_retries_rebuild_candidate_map_and_only_clicks_confirmed_frame(monkeypatch):
    from naruto_daily.secret_realm import enter

    clock, device, requests = Clock(), Frames(), []
    monkeypatch.setattr(enter, 'entry_candidates', lambda frame: (frame, {1: (int(frame) * 10, 20)}))

    def complete(frame, prompt, *, timeout_seconds):
        requests.append(frame)
        if len(requests) <= 4:
            raise temporary_failure()
        return dict(page='main', name='忍者挑战', candidate_id=1)

    action = dict(timeout_seconds=.1, multimodal={'max_calls': 2},
                  pre_click_wait_seconds=1, post_click_wait_seconds=1, poll_seconds=1)
    with execution_scope(120, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device, sleep=clock.advance)
        enter._locate_and_tap(images, SimpleNamespace(complete_json=complete), action,
                              'main', '忍者挑战', clock=clock)

    assert requests == device.frames == [b'1', b'2', b'3', b'4', b'5', b'6']
    assert device.taps == [(60, 20)]


def test_ticket_retry_rechecks_guard_and_keeps_latest_deploy_coordinate():
    clock, device, requests, guards = Clock(), Frames(), [], []

    def matcher(frame, *args):
        guards.append(frame)
        return (int(frame) * 10, 20), 1

    def complete(frame, prompt, *, timeout_seconds):
        requests.append(frame)
        if len(requests) <= 4:
            raise temporary_failure()
        return dict(page='room', tickets=3)

    action = dict(timeout_seconds=.1, multimodal={'max_calls': 1},
                  deploy_button=target('deploy'), poll_seconds=1)
    with execution_scope(120, clock=clock, sleep=clock.advance):
        result = wait_for_tickets(ImageRecognition(device, matcher),
                                  SimpleNamespace(complete_json=complete), action, clock=clock)

    assert result.tickets == 3 and result.deploy_center == (50, 20)
    assert requests == guards == device.frames and len(requests) == 5
    assert not device.taps


def test_recruit_retry_cancels_when_new_frame_loses_red_dot():
    from naruto_daily.recruit.free_button import click_free_recruit
    from test_recruit_multimodal import GUARD, TARGET

    clock, device, requests, guards = Clock(), Frames(), [], []

    def matcher(frame, *args):
        guards.append(frame)
        return ((10, 20), 1) if frame == b'1' else (None, 0)

    def complete(frame, *args, **kwargs):
        requests.append(frame)
        raise temporary_failure()

    with execution_scope(100, clock=clock, sleep=clock.advance):
        result = click_free_recruit(
            device, TARGET, category='普通招募', guard=GUARD,
            images=ImageRecognition(device, matcher), model=SimpleNamespace(complete_json=complete),
            timeout_seconds=.1, poll_seconds=1, pre_click_wait=1,
            post_click_wait=1, max_calls=1, clock=clock,
        )
    assert result is False and not device.taps
    assert requests == [b'1'] and guards == device.frames == [b'1', b'2']


def test_abundance_retry_observes_new_page_without_replaying_click():
    from naruto_daily.abundance.handler import finish_abundance_result

    clock, device, requests = Clock(), Frames(), []

    def complete(frame, *args, **kwargs):
        requests.append(frame)
        assert not device.taps
        if len(requests) <= 4:
            raise temporary_failure()
        return {'page': 'service_popup'}

    def matcher(frame, template, *args):
        if frame == b'7' and template.name == 'abundance_completed.png':
            return (950, 800), .99
        return None, .1

    action = dict(timeout_seconds=.1, result_return=target('result'), post_click_wait_seconds=1)
    with execution_scope(120, clock=clock, sleep=clock.advance):
        finish_abundance_result(device, action, template_matcher=matcher,
                                 model=SimpleNamespace(complete_json=complete), clock=clock)
    assert requests == [b'2', b'3', b'4', b'5', b'6']
    assert device.frames == [b'1', *requests, b'7']
    assert device.taps == [(400, 540)]


@pytest.mark.parametrize('close_remains', [False, True])
def test_either_retries_entire_frame_before_matching_second_target(monkeypatch, close_remains):
    from naruto_daily.common import main_screen
    from test_main_screen import target as main_target

    clock, device, requests, guarded = Clock(), Frames(), [], []

    class Model:
        def __init__(self, *args):
            pass

        def match(self, frame, *args):
            requests.append(frame)
            if len(requests) == 1:
                raise temporary_failure()
            return None, 0

    def matcher(frame, *args):
        guarded.append(frame)
        return ((int(frame), 10), 1) if frame == b'1' or close_remains else (None, 0)

    monkeypatch.setattr(main_screen, 'MainScreenRecognition', Model)
    with execution_scope(100, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device, matcher)
        index, results = images.either([main_target('home'), target('popup')])
    if close_remains:
        assert index == 1 and results[1][0] == (2, 10)
        assert requests == device.frames == [b'1', b'2'] and guarded == [b'2']
    else:
        assert index is None and results[1][0] is None
        assert requests == device.frames == [b'1', b'2', b'3'] and guarded == [b'2', b'3']
    assert not device.taps


@pytest.mark.parametrize('late', [False, True])
def test_battle_more_than_three_retries_keep_all_taps_stopped(tmp_path, late):
    clock, requests = Clock(), []
    device = Device(clock, lambda now: 'room')

    def request(req, *, timeout):
        assert timeout <= 30
        requests.append(request_image(req))
        assert not device.taps
        if len(requests) <= 4:
            clock.advance(timeout)
            raise TimeoutError('offline timeout')
        if late:
            clock.advance(101)
        return response_for(requests[-1])

    model = model_for(tmp_path, clock, request, clock.advance)
    with pytest.raises(DailyDeadlineExceeded) if late else nullcontext():
        with execution_scope(200, clock=clock, sleep=clock.advance):
            result = run_battle(device, dict(battle_config(), timeout_seconds=.1), model=model,
                                template_matcher=lambda *a: (None, .1), clock=clock, sleep=clock.advance)
            assert result.taps == 0

    assert not device.taps
    assert requests == [frame for _, frame in device.frames] and len(requests) == 5
    assert [at for at, _ in device.frames] == [0, 40, 80, 120, 160]


def test_battle_tap_threads_inherit_budget_and_resume_only_after_check(tmp_path):
    clock, contexts = Clock(), []
    device = Device(clock, lambda now: 'room' if now >= 2 else 'battle')
    original_tap = device.tap

    def tap(*point):
        contexts.append(current_execution())
        original_tap(*point)

    device.tap = tap
    model = model_for(tmp_path, clock, lambda req, **kw: response_for(request_image(req)), clock.advance)
    with execution_scope(100, clock=clock, sleep=clock.advance) as budget:
        result = run_battle(device, dict(battle_config(), timeout_seconds=.1), model=model,
                            template_matcher=lambda *a: (None, .1), clock=clock,
                            sleep=clock.advance, check_interval_seconds=2)
    assert result.taps == len(contexts) == 8
    assert all(context is budget for context in contexts)
    assert all(at < 2 for at, *_ in device.taps)


def test_workflow_read_ignores_stage_cap_and_retries_new_frames(tmp_path):
    clock, device, requests = Clock(), Frames(), []

    def request(req, *, timeout):
        requests.append(request_image(req))
        if len(requests) <= 4:
            clock.advance(timeout)
            raise TimeoutError('offline timeout')
        return response_for(b'room', decision=False)

    action = dict(timeout_seconds=.1, multimodal=dict(max_calls=1, timeout_seconds=30))
    model = model_for(tmp_path, clock, request, clock.advance)
    with execution_scope(200, clock=clock, sleep=clock.advance):
        workflow = ModelWorkflow(device, action, model=model)
        result = workflow.read('页面', 'offline', lambda data: data['finished'], timeout=.01)
    assert result is True and requests == device.frames and len(requests) == 5
    assert not device.taps


def test_workflow_button_ignores_legacy_twenty_second_limit():
    clock, device = Clock(), Frames()
    action = dict(timeout_seconds=1, button=target('button'), poll_seconds=5)
    with execution_scope(60, clock=clock, sleep=clock.advance):
        workflow = ModelWorkflow(device, action, model=object(),
                                 template_matcher=lambda *a: ((10, 20), 1) if clock() >= 25 else (None, 0))
        workflow.button('button', click=False)
    assert clock() == 25 and not device.taps


def test_ticket_late_response_raises_total_deadline_without_returning_old_center():
    clock, device = Clock(), Frames()

    def complete(*args, **kwargs):
        clock.advance(11)
        return dict(page='room', tickets=3)

    action = dict(timeout_seconds=100, multimodal={'max_calls': 1},
                  deploy_button=target('deploy'), poll_seconds=1)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(10, clock=clock, sleep=clock.advance):
            wait_for_tickets(ImageRecognition(device, lambda *a: ((10, 20), 1)),
                             SimpleNamespace(complete_json=complete), action, clock=clock)
    assert not device.taps


def test_main_screen_permanent_error_is_not_retried(monkeypatch):
    from naruto_daily.common import main_screen
    from test_main_screen import png, target as main_target

    clock, calls = Clock(), []
    failure = MultimodalRequestError('offline unauthorized', status_code=401)

    def complete(*args, **kwargs):
        calls.append(True)
        raise failure

    monkeypatch.setattr(main_screen, 'MultimodalClient',
                        lambda *a, **kw: SimpleNamespace(complete_json=complete))
    with pytest.raises(MultimodalRequestError) as caught:
        with execution_scope(100, clock=clock, sleep=clock.advance):
            ImageRecognition(Frames()).match(png(96, 54), main_target('home'))
    assert caught.value is failure and len(calls) == 1 and clock() == 0


def test_deployment_warning_waits_past_load_delay_then_confirms_and_restarts_loading():
    from naruto_daily.secret_realm.deployment import wait_after_deploy

    clock, device = Clock(), Frames()

    def matcher(frame, path, *args):
        if device.taps:
            return None, 0
        if path.stem == 'secret_deploy_warning_text' or clock() >= 21:
            return (900, 700), 1
        return None, 0

    action = dict(load_wait_seconds=15, pre_click_wait_seconds=3, poll_seconds=1)
    with execution_scope(100, clock=clock, sleep=clock.advance):
        result = wait_after_deploy(ImageRecognition(device, matcher), action,
                                   confirmed_tickets=1, clock=clock)
    assert result is True and device.taps == [(900, 700)]
    assert clock() == 39  # 21s to find confirm + 3s recheck + the normal 15s load.


def test_deployment_without_warning_keeps_fixed_loading_wait():
    from naruto_daily.secret_realm.deployment import wait_after_deploy

    clock, device = Clock(), Frames()
    action = dict(load_wait_seconds=15, pre_click_wait_seconds=3, poll_seconds=1)
    with execution_scope(100, clock=clock, sleep=clock.advance):
        result = wait_after_deploy(ImageRecognition(device, lambda *a: (None, 0)), action,
                                   confirmed_tickets=1, clock=clock)
    assert result is False and not device.taps and clock() == 15


@pytest.mark.parametrize('confirm_available', [False, True])
def test_deployment_stuck_warning_waits_for_total_limit_without_repeat_click(confirm_available):
    from naruto_daily.secret_realm.deployment import wait_after_deploy

    clock, device = Clock(), Frames()

    def matcher(frame, path, *args):
        if path.stem == 'secret_deploy_warning_text' or confirm_available:
            return (900, 700), 1
        return None, 0

    action = dict(load_wait_seconds=15, pre_click_wait_seconds=3, poll_seconds=1)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(40, clock=clock, sleep=clock.advance):
            wait_after_deploy(ImageRecognition(device, matcher), action,
                              confirmed_tickets=1, clock=clock)
    assert clock() == 40 and len(device.taps) == int(confirm_available)


def test_recruit_scrolling_elapsed_uses_actual_budget_clock_once():
    from naruto_daily.recruit.handler import _find_ordinary_targets, ORDINARY_SCROLL

    clock, device, swipes = Clock(), Frames(), []

    def swipe(*args):
        swipes.append(args)
        clock.advance(args[-1] / 1000)

    def matcher(*args):
        return ((200, 700), 1) if swipes else (None, 0)

    device.swipe = swipe
    action = dict(timeout_seconds=100, poll_seconds=1,
                  ordinary_tab=dict(target('ordinary'), bottom=1080),
                  ordinary_red_dot=dict(target('dot'), bottom=1080))
    with execution_scope(100, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device, matcher)
        _find_ordinary_targets(images, action)
        assert images.elapsed == clock() == 2.5  # 1s recheck, .5s swipe, 1s settle.
    assert swipes == [ORDINARY_SCROLL]
