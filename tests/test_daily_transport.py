"""Shared daily deadlines override stage caps without reusing stale observations."""
import asyncio
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request

import httpx2 as httpx
import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal import transport


URL = 'https://example.test/v1/chat/completions'


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    async def asleep(self, seconds):
        self.sleep(seconds)


def http_failure(status):
    return HTTPError(URL, status, 'private-error', {}, io.BytesIO(b'private-body'))


@pytest.mark.parametrize('error', ['timeout', 429, 503])
def test_daily_sync_retries_more_than_three_times_with_fresh_frames(error, caplog):
    clock, requests, timeouts = Clock(), [], []
    caplog.set_level('INFO', logger='naruto_daily')

    def send(request, timeout):
        requests.append(request.data)
        timeouts.append(timeout)
        clock.now += 1
        if len(requests) < 6:
            raise TimeoutError('private-error') if error == 'timeout' else http_failure(error)
        return io.BytesIO(b'success')

    with execution_scope(200, clock=clock, sleep=clock.sleep):
        result = transport.request_with_retry(
            Request(URL, data=b'initial'), request_timeout=60, timeout_seconds=.01,
            open_request=send,
            refresh_request=lambda: Request(URL, data=f'fresh-{len(requests)}'.encode()))

    assert result.body == b'success'
    assert requests == [b'initial', b'fresh-1', b'fresh-2', b'fresh-3', b'fresh-4', b'fresh-5']
    assert timeouts == [30] * 6 and clock.waits == [10] * 5
    assert 'attempt=6/unlimited' in caplog.text
    assert 'remaining=144.00s' in caplog.text
    assert 'private-error' not in caplog.text and 'private-body' not in caplog.text


@pytest.mark.parametrize('error,kind,status', [
    (TimeoutError, 'timeout', None),
    (lambda: URLError(TimeoutError()), 'timeout', None),
    (lambda: http_failure(503), 'http_status', 503),
])
def test_daily_sync_without_refresh_yields_retryable_error_to_observation(error, kind, status):
    clock, requests = Clock(), []

    def send(request, timeout):
        requests.append(request)
        raise error()

    with execution_scope(200, clock=clock, sleep=clock.sleep):
        with pytest.raises(transport.MultimodalRequestError) as caught:
            transport.request_with_retry(Request(URL), request_timeout=30, open_request=send)

    assert len(requests) == 1 and clock.waits == []
    assert caught.value.retryable is True
    assert caught.value.error_kind == kind and caught.value.status_code == status


@pytest.mark.parametrize('status', [401, 403])
def test_daily_sync_authentication_failure_stops_without_retry(status):
    clock, requests = Clock(), []

    def send(request, timeout):
        requests.append(request)
        raise http_failure(status)

    with execution_scope(200, clock=clock, sleep=clock.sleep):
        with pytest.raises(transport.MultimodalRequestError) as caught:
            transport.request_with_retry(
                Request(URL), request_timeout=30, open_request=send,
                refresh_request=lambda: pytest.fail('Non-retryable failures must not capture again'))

    assert len(requests) == 1 and not clock.waits
    assert caught.value.retryable is False and caught.value.status_code == status
    assert caught.value.error_kind == 'http_status'


@pytest.mark.parametrize('phase', ['wait', 'refresh', 'response_headers', 'response_body', 'timeout'])
def test_daily_sync_deadline_stops_without_sending_or_accepting_late_frame(phase):
    clock, requests, refreshes = Clock(), [], []
    deadline = 4 if phase == 'wait' else 15

    class LateBody(io.BytesIO):
        def read(self, size):
            clock.now = deadline
            return super().read(size)

    def send(request, timeout):
        requests.append(request.data)
        assert timeout == min(30, deadline)
        if phase == 'response_headers':
            clock.now = deadline
            return io.BytesIO(b'late-success')
        if phase == 'response_body':
            return LateBody(b'late-success')
        if phase == 'timeout':
            clock.now = deadline
        raise TimeoutError()

    def refresh():
        refreshes.append(clock.now)
        clock.now = deadline
        return Request(URL, data=b'fresh')

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(deadline, clock=clock, sleep=clock.sleep):
            transport.request_with_retry(Request(URL, data=b'initial'), request_timeout=30,
                                         open_request=send, refresh_request=refresh)

    assert requests == [b'initial']
    assert refreshes == ([10] if phase == 'refresh' else [])
    assert clock.waits == ([4] if phase == 'wait' else [10] if phase == 'refresh' else [])


@pytest.mark.parametrize('failure_type', [TimeoutError, DailyDeadlineExceeded])
def test_daily_sync_refresh_failure_is_not_retried_or_replaced_with_old_frame(failure_type):
    clock, requests, refreshes = Clock(), [], []

    def send(request, timeout):
        requests.append(request)
        raise TimeoutError()

    def refresh():
        refreshes.append(1)
        raise failure_type('private-screenshot-error')

    expected = DailyDeadlineExceeded if failure_type is DailyDeadlineExceeded else transport.MultimodalRequestError
    with pytest.raises(expected) as caught:
        with execution_scope(200, clock=clock, sleep=clock.sleep):
            transport.request_with_retry(Request(URL), request_timeout=30,
                                         open_request=send, refresh_request=refresh)

    assert len(requests) == 1 and refreshes == [1] and clock.waits == [10]
    if isinstance(caught.value, transport.MultimodalRequestError):
        assert caught.value.retryable is False
        assert 'private-screenshot-error' not in str(caught.value)


def test_daily_client_caps_connection_and_ignores_old_stage_timeout():
    clock, calls = Clock(), []
    config = dict(base_url=URL.removesuffix('/chat/completions'), model='test-model', timeout_seconds=60)

    def send(request, timeout):
        calls.append((timeout, json.loads(request.data)))
        return io.BytesIO(json.dumps({'choices': [
            {'finish_reason': 'stop', 'message': {'content': '{"ok": true}'}}
        ]}).encode())

    client = MultimodalClient(config, open_request=send)
    assert client.agent_connection()['timeout_seconds'] == 60
    with execution_scope(200, clock=clock, sleep=clock.sleep):
        assert client.agent_connection()['timeout_seconds'] == 30
        assert MultimodalClient(config).config['timeout_seconds'] == 30
        assert client.complete_json(b'png', 'prompt', timeout_seconds=.001) == {'ok': True}

    assert calls[0][0] == 30
    assert base64.b64decode(calls[0][1]['messages'][0]['content'][1]['image_url']['url'].split(',')[1]) == b'png'


@pytest.mark.parametrize('refresh_async', [False, True])
@pytest.mark.parametrize('failure', ['timeout', 503])
def test_daily_async_retries_more_than_three_times_preserving_history(monkeypatch, refresh_async, failure, caplog):
    clock, requests, refreshes = Clock(), [], []
    caplog.set_level('INFO', logger='naruto_daily')
    monkeypatch.setattr(transport.asyncio, 'sleep', clock.asleep)
    history = [{'role': 'assistant', 'tool_calls': [{'id': 'already-executed'}]},
               {'role': 'tool', 'tool_call_id': 'already-executed', 'content': 'finished'}]

    async def send(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers['Authorization'] == 'Bearer private-key'
        clock.now += 1
        if len(requests) < 6:
            if failure == 'timeout':
                raise httpx.ReadTimeout('private-error', request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={'ok': True})

    def refresh(request):
        refreshes.append(clock.now)
        payload = json.loads(request.content)
        payload['frame'] = f'fresh-{len(requests)}'
        return httpx.Request(request.method, request.url, headers=request.headers, json=payload)

    async def arefresh(request):
        return refresh(request)

    async def run():
        async with transport.ModelAsyncClient(request_timeout=60, transport=httpx.MockTransport(send),
                                             refresh_request=arefresh if refresh_async else refresh) as client:
            assert client.request_timeout == 30
            response = await client.post(URL, json={'history': history, 'frame': 'initial'},
                                         headers={'Authorization': 'Bearer private-key'})
            assert response.json() == {'ok': True}

    with execution_scope(200, clock=clock, sleep=clock.sleep):
        asyncio.run(run())

    assert [item['frame'] for item in requests] == ['initial'] + [f'fresh-{number}' for number in range(1, 6)]
    assert all(item['history'] == history for item in requests)
    assert refreshes == [11, 22, 33, 44, 55] and clock.waits == [10] * 5
    assert 'attempt=6/unlimited' in caplog.text and 'remaining=144.00s' in caplog.text
    assert 'private-key' not in caplog.text and 'private-error' not in caplog.text


@pytest.mark.parametrize('configured,remaining,expected', [(60, 200, 30), (60, 12, 12), (5, 200, 5)])
def test_daily_async_request_timeout_obeys_cap_and_remaining_budget(monkeypatch, configured, remaining, expected):
    clock, timeouts = Clock(), []
    original_timeout = asyncio.timeout

    def timeout(seconds):
        timeouts.append(seconds)
        return original_timeout(seconds)

    async def send(request):
        return httpx.Response(200, json={'ok': True})

    async def run():
        async with transport.ModelAsyncClient(request_timeout=configured, transport=httpx.MockTransport(send)) as client:
            assert (await client.post(URL, json={})).json() == {'ok': True}

    monkeypatch.setattr(transport.asyncio, 'timeout', timeout)
    with execution_scope(remaining, clock=clock, sleep=clock.sleep):
        asyncio.run(run())

    assert timeouts == [expected]


@pytest.mark.parametrize('status', ['timeout', 503, 401, 403])
def test_daily_async_no_refresh_sends_once_and_reports_structured_error(monkeypatch, status):
    clock, requests = Clock(), []
    monkeypatch.setattr(transport.asyncio, 'sleep', clock.asleep)

    async def send(request):
        requests.append(request)
        if status == 'timeout':
            raise httpx.ReadTimeout('private-error', request=request)
        return httpx.Response(status)

    async def run():
        async with transport.ModelAsyncClient(request_timeout=30, transport=httpx.MockTransport(send)) as client:
            with pytest.raises(transport.MultimodalRequestError) as caught:
                await client.post(URL, json={})
            return caught.value

    with execution_scope(200, clock=clock, sleep=clock.sleep):
        error = asyncio.run(run())

    assert len(requests) == 1 and not clock.waits
    assert error.retryable is (status in ('timeout', 503))
    assert error.status_code == (None if status == 'timeout' else status)
    assert error.error_kind == ('read_timeout' if status == 'timeout' else 'http_status')


@pytest.mark.parametrize('phase', ['wait', 'refresh', 'response', 'timeout'])
def test_daily_async_expiry_propagates_special_deadline_exception(monkeypatch, phase):
    clock, requests, refreshes = Clock(), [], []
    deadline = 4 if phase == 'wait' else 15
    monkeypatch.setattr(transport.asyncio, 'sleep', clock.asleep)

    async def send(request):
        requests.append(request)
        if phase in ('response', 'timeout'):
            clock.now = deadline
        if phase == 'response':
            return httpx.Response(200, json={'late': True})
        raise httpx.ReadTimeout('private-error', request=request)

    async def refresh(request):
        refreshes.append(clock.now)
        clock.now = deadline
        return httpx.Request(request.method, request.url, json={'fresh': True})

    async def run():
        async with transport.ModelAsyncClient(request_timeout=30, refresh_request=refresh,
                                             transport=httpx.MockTransport(send)) as client:
            await client.post(URL, json={})

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(deadline, clock=clock, sleep=clock.sleep):
            asyncio.run(run())

    assert len(requests) == 1
    assert refreshes == ([10] if phase == 'refresh' else [])
    assert clock.waits == ([4] if phase == 'wait' else [10] if phase == 'refresh' else [])


def test_daily_async_refresh_guard_exception_propagates_without_resending(monkeypatch):
    clock, requests = Clock(), []
    monkeypatch.setattr(transport.asyncio, 'sleep', clock.asleep)

    class GuardFinished(Exception):
        pass

    async def send(request):
        requests.append(request)
        raise httpx.ReadTimeout('private-error', request=request)

    def refresh(request):
        raise GuardFinished()

    async def run():
        async with transport.ModelAsyncClient(request_timeout=30, refresh_request=refresh,
                                             transport=httpx.MockTransport(send)) as client:
            await client.post(URL, json={})

    with pytest.raises(GuardFinished):
        with execution_scope(200, clock=clock, sleep=clock.sleep):
            asyncio.run(run())

    assert len(requests) == 1 and clock.waits == [10]


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_injected_sync_request_rejects_attempt_late_success_before_daily_deadline(phase):
    clock = Clock()

    class Response(io.BytesIO):
        def read(self, size):
            if phase == 'body':
                clock.now += 29
            else:
                pytest.fail('Headers already used the request deadline; do not read the body')
            return super().read(size)

    def send(request, timeout):
        assert timeout == 30
        clock.now += 31 if phase == 'headers' else 29
        return Response(b'late-success')

    with execution_scope(200, clock=clock, sleep=clock.sleep):
        with pytest.raises(transport.MultimodalRequestError) as caught:
            transport.request_with_retry(Request(URL), request_timeout=30, open_request=send)

    assert caught.value.retryable and caught.value.error_kind == 'timeout'
    assert clock.now == (31 if phase == 'headers' else 58)
    assert not clock.waits


@pytest.fixture
def model_http_server():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        servers.append((server, thread))
        return f'http://127.0.0.1:{server.server_port}/chat/completions'

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def read_request(self):
        self.close_connection = True
        return self.rfile.read(int(self.headers.get('Content-Length', '0')))


@pytest.mark.parametrize('mode', ['headers_then_body', 'drip_body'])
def test_production_sync_request_wall_timeout_closes_real_slow_connection(model_http_server, mode, caplog):
    closed = threading.Event()
    calls = []
    timeout = .6

    class Handler(QuietHandler):
        def do_POST(self):
            calls.append(self.read_request())
            try:
                # Each individual wait is shorter than .6s, but their sum exceeds it.
                if mode == 'headers_then_body':
                    time.sleep(.2)
                self.send_response(200)
                self.send_header('Content-Length', '1000')
                self.end_headers()
                if mode == 'headers_then_body':
                    time.sleep(.5)
                    self.wfile.write(b'a' * 1000)
                    self.wfile.flush()
                else:
                    for _ in range(20):
                        self.wfile.write(b'a')
                        self.wfile.flush()
                        time.sleep(.08)
                self.connection.settimeout(2)
                if not self.connection.recv(1):
                    closed.set()
            except OSError:
                closed.set()

    url = model_http_server(Handler)
    began = time.monotonic()
    with execution_scope(began + 5):
        with pytest.raises(transport.MultimodalRequestError) as caught:
            transport.request_with_retry(Request(url, data=b'{}'), request_timeout=timeout)
    elapsed = time.monotonic() - began

    assert caught.value.retryable and caught.value.error_kind == 'timeout'
    assert len(calls) == 1
    assert timeout <= elapsed < timeout + .3
    assert closed.wait(2), 'The timed-out request must close its connection rather than run in the background'
    assert 'phase=read_body' in caplog.text and 'status=200' in caplog.text


@pytest.mark.parametrize('status,retryable', [(401, False), (403, False), (503, True), (307, False)])
def test_production_sync_request_preserves_status_and_never_redirects(model_http_server, status, retryable):
    paths = []

    class Handler(QuietHandler):
        def do_POST(self):
            self.read_request()
            paths.append(self.path)
            self.send_response(status)
            self.send_header('Location', '/private-redirect')
            self.send_header('Content-Length', '0')
            self.end_headers()

    url = model_http_server(Handler)
    with execution_scope(time.monotonic() + 5):
        with pytest.raises(transport.MultimodalRequestError) as caught:
            transport.request_with_retry(Request(url, data=b'{}'), request_timeout=1)

    assert paths == ['/chat/completions']
    assert caught.value.status_code == status and caught.value.retryable is retryable
    assert caught.value.error_kind == 'http_status'
    assert '/private-redirect' not in str(caught.value)


def test_production_sync_request_reads_success_and_enforces_response_limit(model_http_server):
    calls = []

    class Handler(QuietHandler):
        def do_POST(self):
            calls.append((self.read_request(), self.headers.get('Authorization')))
            self.send_response(200)
            self.send_header('Content-Length', '24')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'a' * 24)
            self.wfile.flush()

    url = model_http_server(Handler)
    request = Request(url, data=b'{"prompt":"test"}', headers={'Authorization': 'Bearer test-only'})
    with execution_scope(time.monotonic() + 5):
        response = transport.request_with_retry(request, request_timeout=1)
        with pytest.raises(transport.MultimodalRequestError, match='大小限制') as caught:
            transport.request_with_retry(request, request_timeout=1, max_response_bytes=8)

    assert response.body == b'a' * 24 and response.content_type == 'application/json'
    assert caught.value.retryable is False
    assert calls == [(b'{"prompt":"test"}', 'Bearer test-only')] * 2
