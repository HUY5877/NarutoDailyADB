import io
import json
import logging
import threading
import time
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
def test_transient_http_errors_retry_every_ten_seconds(status, caplog):
    delay = 10
    clock = Clock()
    calls = []
    caplog.set_level(logging.WARNING, logger='naruto_daily')

    def request(*args, **kwargs):
        calls.append(clock.now)
        raise failure(status)

    with pytest.raises(transport.MultimodalRequestError, match='已重试3次') as caught:
        transport.request_with_retry(Request('https://private.invalid'), request_timeout=30,
                                     open_request=request, sleep=clock.sleep, clock=clock)
    assert calls == [0, delay, delay * 2, delay * 3]
    assert clock.sleeps == [delay] * 3
    assert f'{delay}秒后进行第1/3次接口重试' in caplog.text
    assert caught.value.status_code == status
    assert 'private-key' not in caplog.text + str(caught.value)
    assert 'private.invalid' not in caplog.text + str(caught.value)


def test_rate_limit_server_and_network_errors_use_the_same_retry_delay():
    clock = Clock()
    calls = []
    errors = [failure(429), failure(503), URLError('temporary network error')]

    def request(*args, **kwargs):
        calls.append(clock.now)
        if errors:
            raise errors.pop(0)
        return io.BytesIO(b'success')

    result = transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                          open_request=request, sleep=clock.sleep, clock=clock)
    assert result.body == b'success'
    assert calls == [0, 10, 20, 30] and clock.sleeps == [10, 10, 10]


@pytest.mark.parametrize('budget,expected_calls', [(5, [0]), (10, [0]), (25, [0, 10, 20])])
@pytest.mark.parametrize('error', [429, 503, 'timeout'])
def test_retry_wait_respects_remaining_phase_budget(budget, expected_calls, error):
    clock = Clock()
    calls = []

    def request(*args, **kwargs):
        calls.append(clock.now)
        raise TimeoutError() if error == 'timeout' else failure(error)

    with pytest.raises(transport.MultimodalRequestError, match='剩余时间不足'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30, timeout_seconds=budget,
                                     open_request=request, sleep=clock.sleep, clock=clock)
    assert calls == expected_calls
    assert clock.sleeps == [10] * (len(expected_calls) - 1)


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
    assert calls == [0, 10, 20, 30] and clock.sleeps == [10, 10, 10]


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
    assert timeouts == [30, 20] and clock.sleeps == [10]


def test_sleep_overrunning_deadline_does_not_send_another_request():
    clock = Clock()
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        raise failure()

    def sleep(seconds):
        clock.sleep(seconds + 10)

    with pytest.raises(transport.MultimodalRequestError, match='剩余时间'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30, timeout_seconds=15,
                                     open_request=request, sleep=sleep, clock=clock)
    assert calls == [1]
    assert clock.sleeps == [20]


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
    assert clock.sleeps == [10, 10, 10]


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
        assert sdk['timeout_seconds'] == 150
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
    assert kwargs['cancel_event'].is_set()


def test_cancelled_request_does_not_start_upstream():
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(transport.MultimodalRequestError, match='已取消'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                     cancel_event=cancelled,
                                     open_request=lambda *a, **k: pytest.fail('取消后不得请求'))


@pytest.mark.parametrize('late_result', ['success', 'timeout', 'http_error'])
def test_cancellation_discards_late_response_and_never_retries(late_result):
    cancelled = threading.Event()
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        cancelled.set()
        if late_result == 'timeout':
            raise TimeoutError()
        if late_result == 'http_error':
            raise failure(503)
        return io.BytesIO(b'late success')

    with pytest.raises(transport.MultimodalRequestError, match='已取消'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                     cancel_event=cancelled, open_request=request,
                                     sleep=lambda _: pytest.fail('取消后不得等待'))
    assert calls == [1]


def test_cancellation_interrupts_ten_second_retry_wait():
    waiting = threading.Event()

    class CancelEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    cancelled = CancelEvent()
    errors = []
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    def run():
        try:
            transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                         cancel_event=cancelled, open_request=request)
        except transport.MultimodalRequestError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert waiting.wait(2)
        cancelled.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert errors == ['多模态请求已取消'] and calls == [1]
    finally:
        cancelled.set()
        thread.join(timeout=2)


@pytest.mark.parametrize('late_result', ['success', 'timeout'])
def test_relay_shutdown_does_not_wait_for_upstream_or_send_late_response(monkeypatch, late_result):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    responses = []
    client_errors = []

    def upstream(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        if late_result == 'timeout':
            raise TimeoutError()
        return io.BytesIO(b'late success')

    class Opener:
        open = staticmethod(upstream)

    monkeypatch.setattr(transport, 'build_opener', lambda *a: Opener())
    connection = dict(base_url='https://example.test/v1', api_key='offline-key',
                      model='offline', timeout_seconds=30)
    client = None
    try:
        with transport.model_api_connection(connection) as sdk:
            req = Request(sdk['base_url'] + '/chat/completions', data=b'{}',
                          headers={'Authorization': 'Bearer ' + sdk['api_key']})

            def read():
                try:
                    with urlopen(req, timeout=4) as response:
                        responses.append(response.read())
                except Exception as exc:
                    client_errors.append(exc)

            client = threading.Thread(target=read, daemon=True)
            client.start()
            assert entered.wait(2)
            shutdown_started = time.monotonic()
        assert time.monotonic() - shutdown_started < 1
        assert not release.is_set()  # 上游仍然阻塞，但会话已经退出。
        release.set()
        client.join(timeout=2)
        assert not client.is_alive()
        assert calls == [1] and responses == [] and len(client_errors) == 1
    finally:
        release.set()
        if client is not None:
            client.join(timeout=2)
