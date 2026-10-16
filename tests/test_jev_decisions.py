import base64
from copy import deepcopy
from http.client import BadStatusLine
import io
import json
import re
from urllib.error import HTTPError, URLError

import cv2
import numpy as np
import pytest

from naruto_daily.common.environment import EnvironmentConfigError
from naruto_daily.common.execution import execution_scope
from naruto_daily.multimodal.decisions import (
    DEFAULT_JEV_MODEL, JEV_ENDPOINT, JevDecisionClient,
)
from naruto_daily.multimodal.prompt_jev import ChoicePrompt
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.multimodal.transport import MultimodalError, MultimodalRequestError


def png_frame(width=640, height=360, value=0):
    return cv2.imencode('.png', np.full((height, width, 3), value, np.uint8))[1].tobytes()


PNG = png_frame()
FRESH_PNG = png_frame(value=255)


def response(choice='奖励可领取'):
    return io.BytesIO(json.dumps({'answers': {'decision': {'choice': choice}}}).encode())


def prompt():
    return ChoicePrompt('判断奖励状态，原 JSON 示例仅描述事实。', {
        '奖励可领取': {'available': True, 'button': {'x': 123, 'y': 456}},
        '奖励不可领取': {'available': False, 'button': None},
    })


def client(send, **kwargs):
    return JevDecisionClient({'JEV_API_KEY': 'private-key'}, open_request=send, **kwargs)


def test_choice_prompt_is_string_and_copies_local_candidates():
    candidates = {'完成': {'finished': True, 'buttons': [{'x': 10, 'y': 20}]}}
    text = ChoicePrompt('判断是否完成', candidates)
    candidates['完成']['buttons'][0]['x'] = 999
    assert isinstance(text, str) and text == '判断是否完成'
    assert text.choices['完成']['buttons'][0]['x'] == 10
    assert deepcopy(text) == text


@pytest.mark.parametrize('choices', [{}, [], {'': {}}, {'选项': []}, {123: {}}])
def test_invalid_candidates_fail_before_a_request(choices):
    with pytest.raises(ValueError):
        ChoicePrompt('选择', choices)


def test_sends_only_candidate_labels_and_original_frame_to_fixed_decision_endpoint():
    frame = PNG
    text = prompt()

    def send(request, timeout):
        payload = json.loads(request.data)
        assert request.full_url == JEV_ENDPOINT
        assert request.get_header('Authorization') == 'Bearer private-key'
        assert request.get_header('Typesafe-danger-zone-images') == 'true'
        assert request.get_header('Content-type') == 'application/json'
        assert timeout == 7
        assert payload['model'] == DEFAULT_JEV_MODEL
        assert set(payload) == {'model', 'state', 'questions'}
        assert set(payload['questions']) == {'decision'}
        question = payload['questions']['decision']
        assert question['type'] == 'choice'
        assert question['criteria'] == {'奖励可领取': None, '奖励不可领取': None}
        assert '不生成 JSON' in question['instructions']
        assert '仅供判断截图事实' in payload['state'][0]
        assert str(text) in payload['state'][0]
        url = payload['state'][1]['image_url']['url']
        assert url.startswith('data:image/png;base64,')
        assert base64.b64decode(url.split(',')[1]) == frame
        assert 'button' not in question['criteria']
        return response()

    result = client(send, clock=lambda: 0).complete_json(frame, text, request_timeout=20, timeout_seconds=7)
    assert result == text.choices['奖励可领取']
    result['button']['x'] = 999
    assert text.choices['奖励可领取']['button']['x'] == 123


def test_explicit_model_is_read_from_values_and_environment_repr_is_safe():
    def send(request, timeout):
        assert json.loads(request.data)['model'] == 'typesafe/custom'
        assert request.get_header('Authorization') == 'Bearer private-key'
        return response()

    decision = JevDecisionClient({'JEV_API_KEY': ' private-key ', 'JEV_MODEL': ' typesafe/custom '},
                                 open_request=send)
    assert 'private-key' not in repr(decision)
    assert decision.complete_json(PNG, prompt(), request_timeout=20)['available']


@pytest.mark.parametrize('width,height,expected', [
    (1920, 1080, (1568, 882)),
    (1080, 1920, (882, 1568)),
    (2048, 2048, (1568, 1568)),
    (1568, 882, (1568, 882)),
    (640, 360, (640, 360)),
])
def test_jev_image_longest_edge_is_capped_without_changing_local_candidates(width, height, expected):
    original = png_frame(width, height)
    text = prompt()
    candidates = deepcopy(text.choices)

    def send(request, timeout):
        payload = json.loads(request.data)
        encoded = base64.b64decode(payload['state'][1]['image_url']['url'].split(',')[1])
        frame = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_UNCHANGED)
        assert encoded.startswith(b'\x89PNG\r\n\x1a\n')
        assert (frame.shape[1], frame.shape[0]) == expected
        if max(width, height) <= 1568:
            assert encoded == original
        return response()

    assert client(send).complete_json(original, text, request_timeout=20) == candidates['奖励可领取']
    assert text.choices == candidates


def test_every_retry_resizes_its_fresh_image():
    original = png_frame(1920, 1080)
    fresh = png_frame(1080, 1920, value=255)
    received = []

    def send(request, timeout):
        payload = json.loads(request.data)
        encoded = base64.b64decode(payload['state'][1]['image_url']['url'].split(',')[1])
        frame = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_UNCHANGED)
        received.append(((frame.shape[1], frame.shape[0]), int(frame[0, 0, 0])))
        if len(received) == 1:
            raise TimeoutError()
        return response()

    assert client(send, sleep=lambda _: None, clock=lambda: 0).complete_json(
        original, prompt(), request_timeout=20, refresh_image=lambda: fresh)['available']
    assert received == [((1568, 882), 0), ((882, 1568), 255)]


@pytest.mark.parametrize('image', [b'', b'invalid-png'])
def test_invalid_images_fail_before_sending_a_jev_request(image):
    decision = client(lambda *a, **k: pytest.fail('图片无效时不得发送请求'))
    with pytest.raises(MultimodalError, match='图片无法解析'):
        decision.complete_json(image, prompt(), request_timeout=20)


@pytest.mark.parametrize('value', [None, '', '   '])
def test_missing_key_is_configuration_error(value):
    with pytest.raises(EnvironmentConfigError, match='JEV_API_KEY'):
        JevDecisionClient({'JEV_API_KEY': value})


def test_plain_prompt_cannot_silently_generate_content():
    decision = client(lambda *a, **k: pytest.fail('没有候选时不得发送请求'))
    with pytest.raises(TypeError, match='ChoicePrompt'):
        decision.complete_json(b'png', '返回坐标 JSON', request_timeout=20)


@pytest.mark.parametrize('body,reason', [
    (b'private-response', 'invalid_json_or_response_structure'),
    (b'\xff', 'invalid_json_or_response_structure'),
    (b'[]', 'invalid_json_or_response_structure'),
    (b'{}', 'invalid_json_or_response_structure'),
    (b'{"answers":{"decision":null}}', 'invalid_json_or_response_structure'),
    (b'{"answers":{"decision":{"choice":null}}}', 'invalid_choice'),
    (b'{"answers":{"decision":{"choice":{}}}}', 'invalid_choice'),
    (b'{"answers":{"decision":{"choice":"private-response"}}}', 'invalid_choice'),
])
def test_invalid_responses_preserve_diagnostics_without_private_body(body, reason, caplog):
    caplog.set_level('INFO', logger='naruto_daily')
    decision = client(lambda *a, **k: io.BytesIO(body))
    with model_request_context('主界面奖励/jev'):
        with pytest.raises(MultimodalError) as caught:
            decision.complete_json(PNG, ChoicePrompt('private-prompt', {'有效': {}}),
                                   request_timeout=20)
    assert f'reason={reason}' in caplog.text
    assert 'scene=主界面奖励/jev' in caplog.text and 'response_bytes=' in caplog.text
    assert len(set(re.findall(r'request_id=(\w+)', caplog.text))) == 1
    for private in ('private-key', 'private-response', 'private-image', 'private-prompt'):
        assert private not in caplog.text + str(caught.value)


@pytest.mark.parametrize('error', [
    HTTPError('https://private.invalid', 401, 'private-key', {}, None),
    URLError('private-key'), TimeoutError('private-key'), BadStatusLine('private-key'),
])
def test_transport_errors_never_expose_credentials(error, caplog):
    def send(*args, **kwargs):
        raise error

    decision = client(send, sleep=lambda _: None, clock=lambda: 0)
    with pytest.raises(MultimodalRequestError) as caught:
        decision.complete_json(PNG, prompt(), request_timeout=20)
    assert 'private-key' not in caplog.text + str(caught.value)


def test_retries_capture_new_frame_after_wait_and_keep_single_request_id(caplog):
    now = [0]
    events = []
    payloads = []
    caplog.set_level('INFO', logger='naruto_daily')

    def send(request, timeout):
        payload = json.loads(request.data)
        payloads.append(payload)
        frame = base64.b64decode(payload['state'][1]['image_url']['url'].split(',')[1])
        events.append(('send', now[0], frame, timeout))
        if len(payloads) == 1:
            raise TimeoutError('private-key')
        return response()

    def sleep(seconds):
        events.append(('wait', seconds))
        now[0] += seconds

    def refresh():
        events.append(('screenshot', now[0]))
        return FRESH_PNG

    decision = client(send, sleep=sleep, clock=lambda: now[0])
    with model_request_context('秘境完成/jev'):
        assert decision.complete_json(PNG, prompt(), request_timeout=30,
                                      timeout_seconds=15, refresh_image=refresh)['available']
    assert events == [('send', 0, PNG, 15), ('wait', 10), ('screenshot', 10),
                      ('send', 10, FRESH_PNG, 5)]
    payloads[1]['state'][1] = payloads[0]['state'][1]
    assert payloads[0] == payloads[1]
    assert len(set(re.findall(r'request_id=(\w+)', caplog.text))) == 1
    assert 'scene=秘境完成/jev' in caplog.text


def test_refresh_failure_stops_without_resending_previous_frame():
    calls, waits = [], []

    def send(request, timeout):
        calls.append(request)
        raise TimeoutError()

    def refresh():
        raise OSError('private-screenshot-error')

    decision = client(send, sleep=waits.append, clock=lambda: 0)
    with pytest.raises(MultimodalRequestError, match='刷新失败') as caught:
        decision.complete_json(PNG, prompt(), request_timeout=20, refresh_image=refresh)
    assert len(calls) == 1 and waits == [10]
    assert 'private-screenshot-error' not in str(caught.value)


def test_execution_request_cap_and_fresh_observation_requirement():
    timeouts = []

    def send(request, timeout):
        timeouts.append(timeout)
        raise TimeoutError()

    decision = client(send, clock=lambda: 0)
    with execution_scope(100, clock=lambda: 0):
        with pytest.raises(MultimodalRequestError) as caught:
            decision.complete_json(PNG, prompt(), request_timeout=90)
    assert timeouts == [30]
    assert caught.value.retryable is True
