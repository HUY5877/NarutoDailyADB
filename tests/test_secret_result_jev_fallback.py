"""秘境结算每十个外层轮次使用 JEV 兜底，动作仍以最新页面为准。"""
import io
import json
import logging
import re
from contextlib import nullcontext
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.multimodal.client import MultimodalClient, MultimodalRequestError
from naruto_daily.multimodal.decisions import JEV_ENDPOINT
from naruto_daily.multimodal.prompt_jev import ChoicePrompt, SECRET_RESULT_FALLBACK
from naruto_daily.secret_realm import handler
from naruto_daily.secret_realm.result_fallback import observe_result_fallback


RETURN = (209, 1010)
NINJA_RETURN = (150, 1000)
BLANK = (960, 900)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def config(**overrides):
    action = dict(timeout_seconds=60, poll_seconds=.1,
                  pre_click_wait_seconds=.1, post_click_wait_seconds=.1)
    for name in ('deploy_button', 'create_room', 'result_marker', 'result_return', 'reward_overlay'):
        action[name] = dict(name=name, template=name + '.png', left=0, top=0,
                            right=1920, bottom=1080, threshold=.9)
    return dict(action, **overrides)


def page_of(frame):
    return frame.decode().split(':')[0]


def latest_round(caplog):
    rounds = [re.match(r'\[秘境结算 第(\d+)轮\]', record.getMessage())
              for record in caplog.records]
    return next((int(match[1]) for match in reversed(rounds) if match), 0)


class Device:
    def __init__(self, page='unknown', page_for_frame=None, return_on_tap=True):
        self.page = page
        self.page_for_frame = page_for_frame
        self.return_on_tap = return_on_tap
        self.frames, self.taps = [], []

    def screenshot(self):
        page = self.page_for_frame(self) if self.page_for_frame else self.page
        frame = f'{page}:{len(self.frames) + 1}'.encode()
        self.frames.append(frame)
        return frame

    def tap(self, *point):
        self.taps.append(point)
        if self.return_on_tap:
            self.page = 'room'


def matcher(frame, path, *args):
    visible = {
        'room': {'deploy_button'}, 'lobby': {'create_room'},
        'result': {'result_marker', 'result_return'},
        'reward': {'reward_overlay'},
    }.get(page_of(frame), set())
    points = {'result_return': RETURN, 'create_room': (1443, 791)}
    return (points.get(path.stem, (1700, 900)), 1) if path.stem in visible else (None, .1)


def local_probe(frame):
    page = page_of(frame)
    if page in {'room', 'lobby', 'reward', 'failure'}:
        return page, None
    if page == 'ninja_selection':
        return page, NINJA_RETURN
    if page == 'result':
        return page, RETURN
    return None


@pytest.fixture(autouse=True)
def local_scene_detection(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='naruto_daily')
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda frame: NINJA_RETURN if page_of(frame) == 'ninja_selection' else None)
    monkeypatch.setattr(handler, 'secret_failure_visible', lambda frame: page_of(frame) == 'failure')


class Model:
    def __init__(self, caplog, response=None):
        self.caplog = caplog
        self.response = response or (lambda *_: {'page': 'unknown'})
        self.calls = []

    def complete_json(self, frame, prompt, **kwargs):
        self.calls.append(dict(frame=frame, prompt=prompt, round=latest_round(self.caplog), kwargs=kwargs))
        return self.response(frame, kwargs)


def finish(device, model, clock, **kwargs):
    images = ImageRecognition(device, matcher, sleep=clock.sleep)
    handler.finish_secret_result(device, images, config(), model=model, clock=clock, **kwargs)
    return images


def test_first_fallback_is_round_ten_and_local_rechecks_do_not_count(caplog):
    clock, device = Clock(), Device()
    model = Model(caplog, lambda *_: {'page': 'room'})

    finish(device, model, clock)

    assert [call['round'] for call in model.calls] == [10]
    assert len(device.frames) > 10
    assert device.taps == []
    assert model.calls[0]['prompt'] is SECRET_RESULT_FALLBACK


def test_unknown_returns_to_local_polling_until_round_twenty(caplog):
    clock = Clock()
    device = Device(page_for_frame=lambda device: 'room' if latest_round(caplog) > 20 else 'unknown')
    model = Model(caplog)

    finish(device, model, clock)

    assert [call['round'] for call in model.calls] == [10, 20]
    assert device.taps == []


@pytest.mark.parametrize('page, success_count', [('reward', 1), ('failure', 0)])
def test_fallback_closes_confirmed_overlay_and_reports_only_reward_success(caplog, page, success_count):
    clock, device, successes = Clock(), Device(), []
    model = Model(caplog, lambda *_: {'page': page})

    finish(device, model, clock, on_success=lambda: successes.append(True))

    assert device.taps == [BLANK]
    assert len(successes) == success_count
    assert [call['round'] for call in model.calls] == [10]
    assert page_of(device.frames[-1]) == 'room'


def test_reward_fallback_after_active_exit_does_not_report_success(caplog):
    clock, device, successes = Clock(), Device(), []
    model = Model(caplog, lambda *_: {'page': 'reward'})

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        finish(device, model, clock, exited_battle=True, on_success=lambda: successes.append(True))

    assert [call['round'] for call in model.calls] == [10]
    assert device.taps == [BLANK]
    assert successes == []


def test_room_recognized_on_round_ten_skips_fallback_entirely(caplog):
    clock = Clock()
    device = Device(page_for_frame=lambda device: 'room' if latest_round(caplog) >= 10 else 'unknown')
    model = Model(caplog, lambda *_: pytest.fail('第10轮已确认房间，不应调用模型'))

    finish(device, model, clock)

    assert latest_round(caplog) == 10
    assert model.calls == []
    assert device.taps == []


def test_unknown_fallback_does_not_replay_standalone_blank_taps(caplog):
    clock = Clock()
    device = Device(return_on_tap=False,
                    page_for_frame=lambda device: 'room' if latest_round(caplog) > 10 else 'unknown')
    taps_at_model = []

    def unknown(*_):
        taps_at_model.append(list(device.taps))
        return {'page': 'unknown'}

    finish(device, Model(caplog, unknown), clock, result_confirmed=True)

    assert taps_at_model == [[BLANK] * 9]
    assert device.taps == [BLANK] * 9


@pytest.mark.parametrize('new_page, expected_taps', [('room', []), ('ninja_selection', [NINJA_RETURN])])
def test_slow_reward_response_obeys_latest_local_room_or_ninja_page(caplog, new_page, expected_taps):
    clock, device, successes = Clock(), Device(), []

    def reward(*_):
        clock.sleep(2)
        device.page = new_page
        return {'page': 'reward'}

    model = Model(caplog, reward)
    finish(device, model, clock, on_success=lambda: successes.append(True))

    assert device.taps == expected_taps
    # 新页面控制动作；模型已经确认的成功证据仍然保留。
    assert successes == [True]
    assert len(model.calls) == 1
    assert page_of(device.frames[-1]) == 'room'


def test_reward_confirmed_by_model_and_local_recheck_reports_success_once(caplog):
    clock, device, successes = Clock(), Device(), []

    def reward(*_):
        device.page = 'reward'
        return {'page': 'reward'}

    finish(device, Model(caplog, reward), clock, on_success=lambda: successes.append(True))

    assert device.taps == [BLANK]
    assert successes == [True]


@pytest.mark.parametrize('page', ['room', 'lobby', 'ninja_selection'])
def test_fresh_local_priority_page_skips_jev(page, caplog):
    clock, device = Clock(), Device(page)
    model = Model(caplog, lambda *_: pytest.fail('本地优先页面不应调用 JEV'))
    images = ImageRecognition(device, matcher, sleep=clock.sleep)

    state = observe_result_fallback(device, images, model, probe=local_probe, deadline=10, clock=clock)

    assert state == (page, NINJA_RETURN if page == 'ninja_selection' else None)
    assert model.calls == []
    assert device.taps == []


def test_model_error_retries_whole_observation_and_local_room_skips_second_request(caplog):
    clock, device, observed = Clock(), Device(), []

    def fail_then_room(*_):
        device.page = 'room'
        raise MultimodalRequestError('暂时网络失败', retryable=True)

    def probe(frame):
        observed.append(frame)
        return local_probe(frame)

    model = Model(caplog, fail_then_room)
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        images = ImageRecognition(device, matcher)
        state = observe_result_fallback(device, images, model, probe=probe, deadline=30, clock=clock)

    assert state == ('room', None)
    assert [page_of(frame) for frame in observed] == ['unknown', 'room']
    assert observed == device.frames
    assert len(model.calls) == 1
    assert clock() == 10
    assert device.taps == []


def test_model_error_on_unchanged_page_uses_new_screenshot_for_retry(caplog):
    clock, device = Clock(), Device()
    attempts = []

    def retry(frame, kwargs):
        attempts.append(frame)
        if len(attempts) == 1:
            raise MultimodalRequestError('暂时网络失败', retryable=True)
        return {'page': 'failure'}

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        images = ImageRecognition(device, matcher)
        state = observe_result_fallback(device, images, Model(caplog, retry),
                                        probe=local_probe, deadline=30, clock=clock)

    assert state == ('failure', None)
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert device.taps == []


@pytest.mark.parametrize('scoped', [False, True])
def test_late_fallback_result_cannot_trigger_tap(scoped, caplog):
    clock, device, successes = Clock(), Device(), []

    def late_reward(*_):
        clock.sleep(60)
        return {'page': 'reward'}

    model = Model(caplog, late_reward)
    scope = execution_scope(15, clock=clock, sleep=clock.sleep) if scoped else nullcontext()
    expected = DailyDeadlineExceeded if scoped else VisualError
    with pytest.raises(expected), scope:
        images = ImageRecognition(device, matcher, sleep=clock.sleep)
        handler.finish_secret_result(device, images, config(timeout_seconds=15),
                                     model=model, clock=clock, on_success=lambda: successes.append(True))

    assert len(model.calls) == 1
    assert device.taps == []
    assert successes == []


def test_api_retries_share_original_daily_budget(caplog):
    clock, device = Clock(), Device()

    def unavailable(*_):
        raise MultimodalRequestError('暂时网络失败', retryable=True)

    model = Model(caplog, unavailable)
    with pytest.raises(DailyDeadlineExceeded), execution_scope(25, clock=clock, sleep=clock.sleep):
        images = ImageRecognition(device, matcher)
        handler.finish_secret_result(device, images, config(timeout_seconds=.01), model=model, clock=clock)

    assert clock() == pytest.approx(25)
    assert len(model.calls) == 2
    assert all(call['round'] == 10 for call in model.calls)
    assert device.taps == []


def test_standalone_fallback_supplies_fresh_image_and_accounts_for_model_time(caplog):
    clock, device = Clock(), Device()
    refreshed = []

    def request(frame, kwargs):
        refreshed.append(kwargs['refresh_image']())
        assert refreshed[-1] != frame
        assert kwargs['timeout_seconds'] == pytest.approx(10)
        clock.sleep(2)
        return {'page': 'unknown'}

    images = ImageRecognition(device, matcher, sleep=clock.sleep)
    state = observe_result_fallback(device, images, Model(caplog, request),
                                    probe=local_probe, deadline=10, clock=clock)

    assert state == ('unknown', None)
    assert images.elapsed == pytest.approx(2)
    assert len(refreshed) == 1
    assert device.taps == []


def test_result_return_button_is_not_replayed_after_jev_fallback(caplog):
    clock = Clock()

    def page(device):
        if latest_round(caplog) > 10:
            return 'room'
        return 'result'

    device = Device(page_for_frame=page, return_on_tap=False)
    model = Model(caplog)

    finish(device, model, clock, result_confirmed=True)

    assert device.taps.count(RETURN) == 1
    assert [call['round'] for call in model.calls] == [10]


def test_fallback_choice_prompt_routes_to_jev_without_generation(monkeypatch, configured_environment):
    monkeypatch.setenv('JEV_API_KEY', 'fake-jev-key')
    frame = cv2.imencode('.png', np.zeros((20, 30, 3), np.uint8))[1].tobytes()
    sent = []
    assert isinstance(SECRET_RESULT_FALLBACK, ChoicePrompt)
    assert {state['page'] for state in SECRET_RESULT_FALLBACK.choices.values()} == {
        'room', 'reward', 'failure', 'unknown',
    }
    reward_label = next(label for label, state in SECRET_RESULT_FALLBACK.choices.items()
                        if state['page'] == 'reward')

    def open_request(request, timeout):
        sent.append(request.full_url)
        assert request.full_url == JEV_ENDPOINT
        body = json.loads(request.data)
        assert body['questions']['decision']['type'] == 'choice'
        assert set(body['questions']['decision']['criteria']) == set(SECRET_RESULT_FALLBACK.choices)
        assert 'messages' not in body
        return io.BytesIO(json.dumps({'answers': {'decision': {'choice': reward_label}}}).encode())

    model = MultimodalClient({'timeout_seconds': 10}, open_request=open_request, clock=lambda: 0)
    device = SimpleNamespace(screenshot=lambda: frame)
    images = ImageRecognition(device, sleep=lambda _: None)

    state = observe_result_fallback(device, images, model, probe=lambda frame: None,
                                    deadline=10, clock=lambda: 0)

    assert state == ('reward', None)
    assert sent == [JEV_ENDPOINT]
    assert configured_environment.value == 0
