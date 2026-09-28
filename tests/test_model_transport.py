import io
import logging
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal import transport
from naruto_daily.multimodal.diagnostics import model_request_context


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


def test_rate_limit_server_and_network_errors_use_the_same_retry_delay(caplog):
    clock = Clock()
    calls = []
    errors = [failure(429), failure(503), URLError('temporary network error')]
    caplog.set_level(logging.INFO, logger='naruto_daily')

    def request(*args, **kwargs):
        calls.append(clock.now)
        if errors:
            raise errors.pop(0)
        return io.BytesIO(b'success')

    result = transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                          open_request=request, sleep=clock.sleep, clock=clock)
    assert result.body == b'success'
    assert calls == [0, 10, 20, 30] and clock.sleeps == [10, 10, 10]
    succeeded = next(item for item in caplog.messages if '[多模态请求成功]' in item)
    assert 'attempt=4/4' in succeeded and 'retries=3/3' in succeeded
    assert 'elapsed=30.00s' in succeeded and 'attempt_elapsed=0.00s' in succeeded
    assert 'error_type=' not in succeeded
    assert f'request_id={result.request_id}' in succeeded


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


def test_timeout_exhausting_phase_budget_logs_first_attempt_and_real_cause(caplog):
    clock = Clock()
    calls = []
    caplog.set_level(logging.INFO, logger='naruto_daily')

    def request(*args, timeout):
        calls.append(timeout)
        clock.now += timeout
        raise URLError(TimeoutError('private-key https://private.invalid/request'))

    with model_request_context('登录收尾主界面'), pytest.raises(
            transport.MultimodalRequestError, match='剩余时间不足'):
        transport.request_with_retry(
            Request('https://private.invalid', data=b'private-prompt',
                    headers={'Authorization': 'Bearer private-key'}),
            request_timeout=30, timeout_seconds=30, open_request=request,
            sleep=clock.sleep, clock=clock)

    assert calls == [30] and clock.sleeps == []
    messages = [item.getMessage() for item in caplog.records
                if 'request_id=' in item.getMessage()]
    failure_log = next(item for item in messages if 'stop_reason=budget_insufficient' in item)
    for field, value in [('budget', 30), ('remaining', 0), ('elapsed', 30),
                         ('attempt_elapsed', 30), ('request_timeout', 30)]:
        assert float(re.search(rf'\b{field}=([0-9.]+)', failure_log).group(1)) == value
    assert 'scene=登录收尾主界面' in failure_log
    assert 'attempt=1/4' in failure_log
    assert 'retries=0/3' in failure_log
    assert 'URLError' in failure_log and 'TimeoutError' in failure_log
    assert len(set(re.findall(r'\brequest_id=([^\s,;，；]+)', '\n'.join(messages)))) == 1
    assert '进行第1/3次接口重试' not in caplog.text
    for secret in ['private-key', 'private.invalid', 'private-prompt']:
        assert secret not in caplog.text


@pytest.mark.parametrize('status,expected_attempts,stop_reason', [
    (401, 1, 'non_retryable'), (429, 4, 'retries_exhausted'),
])
def test_http_failure_logs_status_reason_and_one_id_per_logical_request(
        caplog, status, expected_attempts, stop_reason):
    clock = Clock()
    calls = []
    caplog.set_level(logging.INFO, logger='naruto_daily')

    def request(*args, **kwargs):
        calls.append(1)
        raise HTTPError('https://private.invalid', status, 'private-key',
                        {'Authorization': 'Bearer private-key'}, io.BytesIO(b'private-response'))

    with model_request_context('主界面二次核对'), pytest.raises(transport.MultimodalRequestError):
        transport.request_with_retry(Request('https://private.invalid'), request_timeout=30,
                                     open_request=request, sleep=clock.sleep, clock=clock)

    assert len(calls) == expected_attempts
    messages = [item.getMessage() for item in caplog.records
                if 'request_id=' in item.getMessage()]
    final_log = next(item for item in messages if f'stop_reason={stop_reason}' in item)
    assert f'attempt={expected_attempts}/4' in final_log
    assert f'status={status}' in final_log or f'http_status={status}' in final_log
    assert 'HTTPError' in final_log
    assert 'scene=主界面二次核对' in final_log
    assert len(set(re.findall(r'\brequest_id=([^\s,;，；]+)', '\n'.join(messages)))) == 1
    for secret in ['private-key', 'private.invalid', 'private-response']:
        assert secret not in caplog.text


def test_response_body_timeout_preserves_http_status_and_read_phase(caplog):
    clock = Clock()
    caplog.set_level(logging.INFO, logger='naruto_daily')

    class Response(io.BytesIO):
        status = 200

        def read(self, _size):
            clock.now += 30
            raise TimeoutError('private-response-body')

    response = Response()
    with pytest.raises(transport.MultimodalRequestError, match='剩余时间不足'):
        transport.request_with_retry(Request('https://example.test'), request_timeout=30,
                                     timeout_seconds=30, open_request=lambda *a, **k: response,
                                     sleep=clock.sleep, clock=clock)

    final_log = next(item for item in caplog.messages if 'stop_reason=budget_insufficient' in item)
    assert 'phase=read_body' in final_log and 'status=200' in final_log
    assert 'error_type=TimeoutError' in final_log
    assert 'private-response-body' not in caplog.text
    assert response.closed and not clock.sleeps
