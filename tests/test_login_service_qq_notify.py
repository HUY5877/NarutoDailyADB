"""Verify official QQ requests at the external HTTP boundary without credentials."""

import asyncio
from collections import deque
import importlib
from io import BytesIO
import json
import threading
import time
from urllib.error import HTTPError, URLError

import pytest


APP_ID = "test-app-id"
APP_SECRET = "private-app-secret"
TOKEN = "private-access-token"
GROUP = "test-group-openid"
TASK_ID = "12345678-1234-1234-1234-123456789012"


def notifier_module():
    try:
        return importlib.import_module("naruto_daily.login_service.qq_notify")
    except ModuleNotFoundError:
        pytest.fail("The official QQ notifier module has not been implemented")


class HTTPResponse(BytesIO):
    def __init__(self, body):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        super().__init__(body)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class QQTransport:
    """Replace only the network operation; keep request construction and parsing real."""

    def __init__(self, *responses, token_delay=0):
        self.responses = deque(responses)
        self.requests = []
        self.token_delay = token_delay
        self.lock = threading.Lock()

    def __call__(self, request, *, timeout):
        with self.lock:
            self.requests.append((request, timeout))
            response = self.responses.popleft()
        if request.full_url.endswith("/app/getAppAccessToken") and self.token_delay:
            time.sleep(self.token_delay)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, HTTPResponse) else HTTPResponse(response)


def make_notifier(monkeypatch, transport):
    module = notifier_module()
    monkeypatch.setattr(module.urllib.request, "urlopen", transport)
    return module, module.QQGroupNotifier(APP_ID, APP_SECRET, GROUP)


def request_json(request):
    return json.loads(request.data.decode("utf-8"))


def token_response(token=TOKEN, expires_in="7200"):
    return {"access_token": token, "expires_in": expires_in}


def test_posts_credentials_and_named_active_group_message(monkeypatch):
    # Catches wrong official endpoints, missing credentials, and passive-only payloads.
    transport = QQTransport(token_response(), {"id": "message-1", "timestamp": "2026-10-11T00:00:00Z"})
    _, notifier = make_notifier(monkeypatch, transport)
    asyncio.run(notifier.notify_qr_ready(TASK_ID, "小鸣人"))

    (token_request, token_timeout), (message_request, message_timeout) = transport.requests
    assert token_request.full_url == "https://api.bot.qq.com/app/getAppAccessToken"
    assert token_request.get_method() == "POST"
    assert request_json(token_request) == {"appId": APP_ID, "clientSecret": APP_SECRET}
    assert message_request.full_url == "https://api.bot.qq.com/v2/groups/test-group-openid/messages"
    assert message_request.get_method() == "POST"
    assert message_request.get_header("Authorization") == "QQBot private-access-token"
    assert message_request.get_header("Content-type") == "application/json"
    assert token_timeout == message_timeout == 5
    payload = request_json(message_request)
    assert set(payload) == {"msg_type", "content", "msg_seq"}
    assert payload["msg_type"] == 0
    assert isinstance(payload["msg_seq"], int) and payload["msg_seq"] > 0
    assert "小鸣人" in payload["content"]
    assert "二维码已生成" in payload["content"]
    assert "12345678" in payload["content"]
    assert TASK_ID not in payload["content"]
    assert TOKEN not in payload["content"]
    assert "http" not in payload["content"]


def test_expiry_during_token_fetch_prevents_group_message(monkeypatch):
    clock = [1.0]
    transport = QQTransport(token_response())
    module, notifier = make_notifier(monkeypatch, transport)
    monkeypatch.setattr(module, 'time', lambda: clock[0], raising=False)
    def delayed_token(request, *, timeout):
        result = transport(request, timeout=timeout)
        clock[0] = 2.0
        return result
    monkeypatch.setattr(module.urllib.request, 'urlopen', delayed_token)
    with pytest.raises(module.QQNotifyError):
        asyncio.run(notifier.notify_qr_ready(TASK_ID, '小鸣人', expires_at_ms=1500))
    assert len(transport.requests) == 1


def test_refresh_message_supports_missing_name(monkeypatch):
    # Catches treating old submissions as a made-up real person or losing refresh state.
    transport = QQTransport(token_response(), {"id": "message-1"})
    _, notifier = make_notifier(monkeypatch, transport)
    asyncio.run(notifier.notify_qr_ready(TASK_ID, "", refreshed=True))
    content = request_json(transport.requests[1][0])["content"]
    assert "未提供姓名" in content
    assert "二维码已刷新" in content


def test_user_name_cannot_insert_lines_or_unbounded_content(monkeypatch):
    # Catches including unsanitized newline/control characters or an unlimited name.
    transport = QQTransport(token_response(), {"id": "message-1"})
    _, notifier = make_notifier(monkeypatch, transport)
    asyncio.run(notifier.notify_qr_ready(TASK_ID, "小鸣人\r\n管理员\x00" + "界" * 1000))
    content = request_json(transport.requests[1][0])["content"]
    assert "小鸣人 管理员" in content
    assert "\r" not in content and "\x00" not in content
    assert content.count("\n") <= 1
    assert len(content) < 200


def test_reuses_token_and_uses_distinct_message_sequences(monkeypatch):
    # Catches fetching a token per notification or reusing a message sequence.
    transport = QQTransport(token_response(), {"id": "message-1"}, {"id": "message-2"})
    _, notifier = make_notifier(monkeypatch, transport)

    async def notify_twice():
        await notifier.notify_qr_ready(TASK_ID, "小鸣人")
        await notifier.notify_qr_ready(TASK_ID, "小佐助", refreshed=True)

    asyncio.run(notify_twice())
    assert len(transport.requests) == 3
    first, second = (request_json(item[0]) for item in transport.requests[1:])
    assert first["msg_seq"] != second["msg_seq"]
    assert transport.requests[2][0].get_header("Authorization") == "QQBot private-access-token"


@pytest.mark.parametrize("status,error_code,expected", [
    ("completed", None, "日常任务已全部完成"),
    ("expired", None, "任务已超时"),
    ("failed", "daily_failed", "任务执行失败"),
    ("failed", "daily_timeout", "任务执行失败（超时）"),
    ("failed", "command_timeout", "任务执行失败（超时）"),
    ("failed", "login_timeout", "任务执行失败（超时）"),
    ("failed", "login_finish_timeout", "任务执行失败（超时）"),
])
def test_finished_tasks_send_named_active_group_messages(monkeypatch, status, error_code, expected):
    transport = QQTransport(token_response(), {"id": "message-1"})
    module, notifier = make_notifier(monkeypatch, transport)
    asyncio.run(notifier.notify_task_finished(TASK_ID, "小鸣人", status=status, error_code=error_code))

    (token_request, token_timeout), (message_request, message_timeout) = transport.requests
    assert request_json(token_request) == {"appId": APP_ID, "clientSecret": APP_SECRET}
    assert message_request.full_url == "https://api.bot.qq.com/v2/groups/test-group-openid/messages"
    assert message_request.get_header("Authorization") == "QQBot private-access-token"
    assert token_timeout == message_timeout == 5
    payload = request_json(message_request)
    assert set(payload) == {"msg_type", "content", "msg_seq"}
    assert payload["msg_type"] == 0
    assert payload["msg_seq"] == 1
    assert "小鸣人" in payload["content"]
    assert expected in payload["content"]
    assert "任务：12345678" in payload["content"]
    if status == "failed":
        assert module.SAFE_RUN_ERRORS[error_code] in payload["content"]
    for private_value in (TASK_ID, APP_SECRET, TOKEN, GROUP):
        assert private_value not in payload["content"]


@pytest.mark.parametrize("status", ["interrupted", "running_daily", "unknown", ""])
def test_finished_task_ignores_non_notifiable_status_without_network(monkeypatch, status):
    transport = QQTransport()
    _, notifier = make_notifier(monkeypatch, transport)
    asyncio.run(notifier.notify_task_finished(TASK_ID, "小鸣人", status=status))
    assert transport.requests == []


@pytest.mark.parametrize("error_code", [None, "unknown", APP_SECRET + TOKEN, "private-timeout_timeout\n" + APP_SECRET])
def test_finished_task_uses_safe_reason_for_unknown_errors(monkeypatch, error_code):
    transport = QQTransport(token_response(), {"id": "message-1"})
    module, notifier = make_notifier(monkeypatch, transport)
    asyncio.run(notifier.notify_task_finished(TASK_ID, "小鸣人", status="failed", error_code=error_code))
    content = request_json(transport.requests[1][0])["content"]
    assert "任务执行失败" in content
    assert module.SAFE_RUN_ERRORS["unexpected_error"] in content
    assert "超时" not in content
    assert APP_SECRET not in content and TOKEN not in content
    if error_code:
        assert error_code not in content


@pytest.mark.parametrize("user_name,task_id,expected_name,expected_id", [
    ("小鸣人\r\n管理员\x00" + "界" * 1000, "\r\n/12345678/?" + TASK_ID, "小鸣人 管理员", "12345678"),
    ("", "\r\n/?", "未提供姓名", "未知"),
], ids=["controls-and-long-name", "missing-name-and-id"])
def test_finished_task_sanitizes_name_and_short_task_id(monkeypatch, user_name, task_id, expected_name, expected_id):
    transport = QQTransport(token_response(), {"id": "message-1"})
    _, notifier = make_notifier(monkeypatch, transport)
    asyncio.run(notifier.notify_task_finished(task_id, user_name, status="completed"))
    content = request_json(transport.requests[1][0])["content"]
    assert expected_name in content
    assert f"任务：{expected_id}" in content
    assert "\r" not in content and "\x00" not in content
    assert content.count("\n") == 1
    assert len(content) < 200


def test_qr_and_finished_messages_reuse_token_and_increasing_sequences(monkeypatch):
    transport = QQTransport(token_response(), {"id": "message-1"}, {"id": "message-2"}, {"id": "message-3"})
    _, notifier = make_notifier(monkeypatch, transport)

    async def notify_all():
        await notifier.notify_qr_ready(TASK_ID, "小鸣人")
        await notifier.notify_task_finished(TASK_ID, "小鸣人", status="completed")
        await notifier.notify_task_finished("abcdefgh-1234", "小佐助", status="failed", error_code="daily_timeout")

    asyncio.run(notify_all())
    assert len(transport.requests) == 4
    assert sum(item[0].full_url.endswith("/app/getAppAccessToken") for item in transport.requests) == 1
    messages = [request_json(item[0]) for item in transport.requests[1:]]
    assert [item["msg_seq"] for item in messages] == [1, 2, 3]
    assert all(set(item) == {"msg_type", "content", "msg_seq"} for item in messages)
    assert all(item[0].get_header("Authorization") == "QQBot private-access-token" for item in transport.requests[1:])


@pytest.mark.parametrize("stage", ["token", "message"])
@pytest.mark.parametrize("failure_kind", ["business", "network"])
def test_finished_notification_failures_are_safe_and_never_retried(monkeypatch, stage, failure_kind):
    failure = (
        {"code": 40001, "message": APP_SECRET + TOKEN}
        if failure_kind == "business" else URLError(APP_SECRET + TOKEN)
    )
    responses = [failure] if stage == "token" else [token_response(), failure]
    transport = QQTransport(*responses)
    module, notifier = make_notifier(monkeypatch, transport)
    with pytest.raises(module.QQNotifyError) as caught:
        asyncio.run(notifier.notify_task_finished(TASK_ID, "小鸣人", status="completed"))
    assert APP_SECRET not in str(caught.value) and TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(transport.requests) == (1 if stage == "token" else 2)


def test_refreshes_token_before_monotonic_expiration(monkeypatch):
    # Catches trusting a token right up to expiry or retaining the stale token header.
    transport = QQTransport(token_response(expires_in="100"), {"id": "message-1"}, {"id": "message-2"}, token_response("new-token", "100"), {"id": "message-3"})
    module, notifier = make_notifier(monkeypatch, transport)
    clock = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    async def notify_over_time():
        await notifier.notify_qr_ready(TASK_ID, "小鸣人")
        clock[0] = 1080.0
        await notifier.notify_qr_ready(TASK_ID, "小鸣人")
        clock[0] = 1099.0
        await notifier.notify_qr_ready(TASK_ID, "小鸣人")

    asyncio.run(notify_over_time())
    assert len(transport.requests) == 5
    assert transport.requests[3][0].full_url.endswith("/app/getAppAccessToken")
    assert transport.requests[4][0].get_header("Authorization") == "QQBot new-token"


def test_concurrent_notifications_share_one_token_request(monkeypatch):
    # Catches concurrent cache misses acquiring duplicate tokens.
    transport = QQTransport(token_response(), {"id": "message-1"}, {"id": "message-2"}, token_delay=0.03)
    _, notifier = make_notifier(monkeypatch, transport)

    async def notify_together():
        await asyncio.gather(notifier.notify_qr_ready(TASK_ID, "小鸣人"), notifier.notify_qr_ready(TASK_ID, "小佐助"))

    asyncio.run(notify_together())
    assert len(transport.requests) == 3
    assert sum(item[0].full_url.endswith("/app/getAppAccessToken") for item in transport.requests) == 1


def test_cancelled_caller_cannot_start_a_parallel_token_fetch(monkeypatch):
    # Catches releasing the token lock while a cancelled caller's HTTP thread runs.
    first_fetch_started = threading.Event()
    release_first_fetch = threading.Event()
    duplicate_fetch_started = threading.Event()
    lock = threading.Lock()
    token_requests = []
    message_requests = []

    def transport(request, *, timeout):
        if request.full_url.endswith("/app/getAppAccessToken"):
            with lock:
                token_requests.append(request)
                first = len(token_requests) == 1
            if first:
                first_fetch_started.set()
                release_first_fetch.wait(2)
            else:
                duplicate_fetch_started.set()
            return HTTPResponse(token_response())
        message_requests.append(request)
        return HTTPResponse({"id": "message-1"})

    _, notifier = make_notifier(monkeypatch, transport)

    async def cancel_then_notify():
        first = asyncio.create_task(notifier.notify_qr_ready(TASK_ID, "小鸣人"))
        assert await asyncio.to_thread(first_fetch_started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(notifier.notify_qr_ready(TASK_ID, "小佐助"))
        try:
            duplicate = await asyncio.to_thread(duplicate_fetch_started.wait, 0.1)
        finally:
            release_first_fetch.set()
            await second
        assert not duplicate

    asyncio.run(cancel_then_notify())
    assert len(token_requests) == 1
    assert len(message_requests) == 1


@pytest.mark.parametrize("stage", ["token", "message"])
def test_http_200_business_failure_is_safe_and_never_retried(monkeypatch, stage):
    # Catches assuming HTTP 200 means success, exposing API errors, or duplicate sends.
    failure = {"code": 40001, "message": APP_SECRET + TOKEN, "access_token": TOKEN, "expires_in": "7200"}
    responses = [failure] if stage == "token" else [token_response(), failure]
    transport = QQTransport(*responses)
    module, notifier = make_notifier(monkeypatch, transport)
    with pytest.raises(module.QQNotifyError) as caught:
        asyncio.run(notifier.notify_qr_ready(TASK_ID, "小鸣人"))
    assert APP_SECRET not in str(caught.value) and TOKEN not in str(caught.value)
    assert "40001" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(transport.requests) == (1 if stage == "token" else 2)


@pytest.mark.parametrize("stage", ["token", "message"])
@pytest.mark.parametrize("failure_kind", ["bad_json", "http_error", "timeout", "network"])
def test_external_failures_expose_only_safe_errors(monkeypatch, stage, failure_kind):
    # Catches raw server bodies, URL/credential details, or automatic HTTP retries.
    failures = {
        "bad_json": HTTPResponse((APP_SECRET + TOKEN).encode()),
        "http_error": HTTPError("https://example.invalid/" + APP_SECRET, 401, TOKEN, {}, BytesIO(APP_SECRET.encode())),
        "timeout": TimeoutError(APP_SECRET + TOKEN),
        "network": URLError(APP_SECRET + TOKEN),
    }
    responses = [failures[failure_kind]] if stage == "token" else [token_response(), failures[failure_kind]]
    transport = QQTransport(*responses)
    module, notifier = make_notifier(monkeypatch, transport)
    with pytest.raises(module.QQNotifyError) as caught:
        asyncio.run(notifier.notify_qr_ready(TASK_ID, "小鸣人"))
    assert APP_SECRET not in str(caught.value) and TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(transport.requests) == (1 if stage == "token" else 2)


@pytest.mark.parametrize("response", [[], {"access_token": TOKEN}, {"access_token": TOKEN, "expires_in": "NaN"}, {"access_token": TOKEN, "expires_in": 0}, {"access_token": "bad\n" + TOKEN, "expires_in": "100"}])
def test_invalid_token_response_cannot_reach_group_send(monkeypatch, response):
    # Catches accepting non-object data, missing/invalid expiry, or a header injection token.
    transport = QQTransport(response)
    module, notifier = make_notifier(monkeypatch, transport)
    with pytest.raises(module.QQNotifyError) as caught:
        asyncio.run(notifier.notify_qr_ready(TASK_ID, "小鸣人"))
    assert TOKEN not in str(caught.value)
    assert len(transport.requests) == 1


def test_oversized_response_is_read_with_a_limit(monkeypatch):
    # Catches reading an unbounded API body or accepting a truncated oversized response.
    response = HTTPResponse(b" " * 1000000)
    transport = QQTransport(response)
    module, notifier = make_notifier(monkeypatch, transport)
    with pytest.raises(module.QQNotifyError):
        asyncio.run(notifier.notify_qr_ready(TASK_ID, "小鸣人"))
    assert len(response.read_sizes) == 1
    assert 0 < response.read_sizes[0] < 1000000


def test_close_prevents_new_notifications(monkeypatch):
    # Catches using a notifier after its owning runtime has closed it.
    transport = QQTransport()
    module, notifier = make_notifier(monkeypatch, transport)

    async def notify_after_close():
        await notifier.close()
        await notifier.close()
        with pytest.raises(module.QQNotifyError):
            await notifier.notify_qr_ready(TASK_ID, "小鸣人")

    asyncio.run(notify_after_close())
    assert transport.requests == []


def test_close_does_not_wait_for_cancelled_callers_token_request(monkeypatch):
    started, release = threading.Event(), threading.Event()
    requests = []
    def transport(request, *, timeout):
        requests.append(request)
        started.set()
        release.wait(2)
        return HTTPResponse(token_response())
    module, notifier = make_notifier(monkeypatch, transport)
    async def scenario():
        pending = asyncio.create_task(notifier.notify_qr_ready(TASK_ID, '小鸣人'))
        assert await asyncio.to_thread(started.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        try:
            await asyncio.wait_for(notifier.close(), .2)
        finally:
            release.set()
        with pytest.raises(module.QQNotifyError):
            await notifier._token_task
        with pytest.raises(module.QQNotifyError):
            await notifier.notify_qr_ready(TASK_ID, '小鸣人')
    asyncio.run(scenario())
    assert len(requests) == 1
