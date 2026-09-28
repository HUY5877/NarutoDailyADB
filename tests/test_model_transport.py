import io
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal import transport


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def failure(status=429):
    return HTTPError('https://private.invalid/v1', status, 'private-key', {}, io.BytesIO(b'private-key'))


@pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504])
def test_transient_http_errors_retry_three_times_at_three_seconds(status, caplog):
    clock = Clock()
    calls = []
    caplog.set_level(logging.WARNING, logger='naruto_daily')

    def request(*args, **kwargs):
        calls.append(clock.now)
        raise failure(status)

    with pytest.raises(transport.MultimodalRequestError, match='已重试3次') as caught:
        transport.request_with_retry(Request('https://private.invalid'), request_timeout=30,
                                     open_request=request, sleep=clock.sleep, clock=clock)
    assert calls == [0, 3, 6, 9]
    assert clock.sleeps == [3, 3, 3]
    assert caught.value.status_code == status
    assert 'private-key' not in caplog.text + str(caught.value)
    assert 'private.invalid' not in caplog.text + str(caught.value)


def test_last_retry_can_succeed_without_another_wait():
    clock = Clock()
    calls = []

    def request(*args, **kwargs):
        calls.append(clock.now)
        if len(calls) < 4:
            raise URLError('private-key')
        return io.BytesIO(b'success')

    result = transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                          open_request=request, sleep=clock.sleep, clock=clock)
    assert result.body == b'success'
    assert calls == [0, 3, 6, 9] and clock.sleeps == [3, 3, 3]


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_permanent_http_errors_do_not_retry(status):
    clock = Clock()
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        raise failure(status)

    with pytest.raises(transport.MultimodalRequestError):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                     open_request=request, sleep=clock.sleep, clock=clock)
    assert calls == [1] and not clock.sleeps


def test_attempts_and_waits_share_the_phase_deadline():
    clock = Clock()
    timeouts = []

    def request(*args, timeout):
        timeouts.append(timeout)
        clock.now += timeout
        raise TimeoutError('private-key')

    with pytest.raises(transport.MultimodalRequestError, match='剩余时间不足'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30, timeout_seconds=60,
                                     open_request=request, sleep=clock.sleep, clock=clock)
    assert timeouts == [30, 27] and clock.sleeps == [3]


def test_sleep_overrunning_deadline_does_not_send_another_request():
    clock = Clock()
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        raise failure()

    def sleep(seconds):
        clock.sleep(seconds + 10)

    with pytest.raises(transport.MultimodalRequestError, match='剩余时间'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30, timeout_seconds=5,
                                     open_request=request, sleep=sleep, clock=clock)
    assert calls == [1]


def test_late_response_is_rejected():
    clock = Clock()

    def request(*args, **kwargs):
        clock.now += 5
        return io.BytesIO(b'late')

    with pytest.raises(transport.MultimodalRequestError, match='响应已超过'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30, timeout_seconds=5,
                                     open_request=request, sleep=clock.sleep, clock=clock)
    assert not clock.sleeps


def test_workflow_does_not_multiply_exhausted_http_retries(tmp_path):
    (tmp_path / '.env').write_text('MULTIMODAL_API_KEY=offline-key', encoding='utf-8')
    clock = Clock()
    requests = []
    screenshots = []

    def request(*args, **kwargs):
        requests.append(1)
        raise failure()

    class Device:
        def screenshot(self):
            screenshots.append(1)
            return b'png'

    settings = dict(base_url='https://example.test/v1', model='offline', timeout_seconds=30, max_calls=3)
    model = MultimodalClient(settings, asset_root=tmp_path, open_request=request,
                             sleep=clock.sleep, clock=clock)
    flow = ModelWorkflow(Device(), dict(timeout_seconds=240, multimodal=settings), model=model,
                         sleep=lambda _: pytest.fail('外层不得再重试接口错误'))
    with pytest.raises(transport.MultimodalRequestError, match='已重试3次'):
        flow.read('装备材料详情', 'prompt', lambda value: value)
    assert len(requests) == 4 and len(screenshots) == 1
    assert clock.sleeps == [3, 3, 3]


@pytest.mark.parametrize('base_path,route', [('/v1', '/v1/chat/completions'), ('', '/v1/messages')])
def test_sdk_relay_uses_shared_transport_and_replaces_only_authorization(monkeypatch, base_path, route):
    calls = []

    def request(request, **kwargs):
        calls.append((request, kwargs))
        return transport.ModelResponse(200, 'application/json', b'{"ok":true}')

    monkeypatch.setattr(transport, 'request_with_retry', request)
    connection = dict(base_url='https://example.test' + base_path, api_key='upstream-private-key',
                      model='offline', timeout_seconds=30)
    with transport.model_api_connection(connection) as sdk:
        assert sdk['timeout_seconds'] == 129
        assert sdk['api_key'] != connection['api_key']
        address = sdk['base_url'] + route[len(base_path):]
        payload = b'{"model":"offline","stream":true}'
        req = Request(address, data=payload, headers={
            'Authorization': 'Bearer ' + sdk['api_key'], 'anthropic-version': '2023-06-01',
        })
        with urlopen(req, timeout=2) as result:
            assert json.load(result) == {'ok': True}
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(address, data=payload), timeout=2)
        assert caught.value.code == 403
    assert len(calls) == 1
    upstream, kwargs = calls[0]
    assert upstream.full_url == 'https://example.test' + route
    assert upstream.data == payload
    assert upstream.get_header('Authorization') == 'Bearer upstream-private-key'
    assert upstream.get_header('Anthropic-version') == '2023-06-01'
    assert kwargs['request_timeout'] == 30
