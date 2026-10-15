import asyncio
from io import BytesIO, StringIO
import logging
from types import SimpleNamespace
from urllib.request import Request

import cv2
import httpx2 as httpx
import numpy as np
import pytest

from naruto_daily.common import main_screen
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, execution_scope, retry_observation,
)
from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.common.visual import VisualError
from naruto_daily.login_service.log_events import forward_task_logs
from naruto_daily.multimodal import diagnostics, transport


LOGGER = logging.getLogger("naruto_daily")
URL = "https://example.test/v1/chat/completions"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def logs():
    events, backend = [], StringIO()
    with forward_task_logs(events.append, diagnostic_stream=backend):
        yield SimpleNamespace(events=events, backend=backend)


def public_messages(logs):
    return "\n".join(event["message"] for event in logs.events)


def report_exception(error):
    try:
        raise error
    except Exception:
        LOGGER.exception("执行阶段 daily 失败")


def api_error(*, retryable=False):
    return transport.MultimodalRequestError(
        "多模态 API 网络异常或请求超时", retryable=retryable, error_kind="timeout"
    )


def test_marking_preserves_exception_identity_and_does_not_inherit_from_cause():
    error = TimeoutError("内部 API 故障细节")
    assert diagnostics.mark_model_api_failure(error) is error
    assert diagnostics.mark_model_api_failure(error) is error
    assert diagnostics.is_model_api_failure(error)

    outer = TypeError("本地参数错误")
    outer.__cause__ = error
    assert not diagnostics.is_model_api_failure(outer)
    assert not diagnostics.is_model_api_failure(None)


@pytest.mark.parametrize("kwargs,expected", [
    ({"error_kind": "timeout"}, True),
    ({"error_kind": "http_status"}, True),
    ({"status_code": 503}, True),
    ({}, False),
    ({"retryable": True}, False),
])
def test_request_error_marks_only_explicit_api_failure_metadata(kwargs, expected):
    error = transport.MultimodalRequestError("多模态 API 超时重试截图配置异常", **kwargs)
    assert diagnostics.is_model_api_failure(error) is expected


def test_request_diagnostics_are_complete_in_backend_and_absent_from_public_logs(logs):
    clock = Clock()
    with diagnostics.model_request_context("主界面识别/home"):
        request = diagnostics.RequestDiagnostics(budget=150, clock=clock, max_attempts=4)
    request.begin(1, 30)
    clock.now = 30
    request.failure(TimeoutError("external-detail-not-logged"))
    request.stop("retries_exhausted")

    assert logs.events == []
    text = logs.backend.getvalue()
    assert "[多模态请求开始]" in text
    assert "[多模态请求失败]" in text
    assert "[多模态请求停止]" in text
    assert "scene=主界面识别/home" in text
    assert "error_type=TimeoutError" in text
    assert "error_kind=timeout" in text
    assert "stop_reason=retries_exhausted" in text


def test_sync_request_retry_and_final_api_exception_are_backend_only(logs):
    clock, calls = Clock(), []

    def fail_request(request, *, timeout):
        calls.append(clock())
        raise TimeoutError("external detail")

    with pytest.raises(transport.MultimodalRequestError) as caught:
        transport.request_with_retry(
            Request(URL), request_timeout=30, open_request=fail_request,
            clock=clock, sleep=clock.sleep,
        )
    report_exception(caught.value)

    assert diagnostics.is_model_api_failure(caught.value)
    assert calls == [0, 10, 20, 30]
    assert clock.waits == [10, 10, 10]
    assert logs.events == []
    text = logs.backend.getvalue()
    assert text.count("[多模态请求失败]") == 4
    assert text.count("次接口重试") == 3
    assert "stop_reason=retries_exhausted" in text
    assert "MultimodalRequestError" in text and "执行阶段 daily 失败" in text


@pytest.mark.parametrize("failure_kind", ["timeout", "http_status"])
def test_async_request_retry_and_final_api_exception_are_backend_only(logs, monkeypatch, failure_kind):
    calls, waits = [], []

    async def wait(seconds):
        waits.append(seconds)

    async def request_handler(request):
        calls.append(request)
        if failure_kind == "timeout":
            raise httpx.ReadTimeout("external detail", request=request)
        return httpx.Response(503, content=b"unavailable")

    async def run():
        async with transport.ModelAsyncClient(
            request_timeout=30, transport=httpx.MockTransport(request_handler)
        ) as client:
            expected = httpx.ReadTimeout if failure_kind == "timeout" else transport.MultimodalRequestError
            with pytest.raises(expected) as caught:
                await client.post(URL, json={})
            assert diagnostics.is_model_api_failure(caught.value)
            report_exception(caught.value)

    monkeypatch.setattr(transport.asyncio, "sleep", wait)
    asyncio.run(run())

    assert len(calls) == 4 and waits == [10, 10, 10]
    assert logs.events == []
    text = logs.backend.getvalue()
    assert text.count("[多模态请求失败]") == 4
    assert text.count("次接口重试") == 3
    assert "stop_reason=retries_exhausted" in text
    assert "执行阶段 daily 失败" in text


def test_outer_observation_refresh_warning_is_backend_only_after_transport_failure(logs):
    clock, observed_images, sent_images = Clock(), [], []

    def send(request, *, timeout):
        sent_images.append(request.data)
        if len(sent_images) == 1:
            raise TimeoutError("external detail")
        return BytesIO(b"recognized")

    def observe():
        image = b"first-frame" if not observed_images else b"fresh-frame"
        observed_images.append(image)
        return transport.request_with_retry(
            Request(URL, data=image), request_timeout=30, open_request=send,
        ).body

    with execution_scope(100, clock=clock, sleep=clock.sleep):
        assert retry_observation(observe, name="奖励入口确认") == b"recognized"

    assert observed_images == sent_images == [b"first-frame", b"fresh-frame"]
    assert clock.waits == [10]
    assert logs.events == []
    text = logs.backend.getvalue()
    assert "[多模态请求失败]" in text
    assert "stop_reason=refresh_required" in text
    assert "奖励入口确认接口暂时失败" in text
    assert "10 秒后重新截图重试" in text
    assert "[多模态请求成功]" in text


def test_screenshot_refresh_failure_remains_public_even_after_hidden_api_retry(logs):
    clock, requests, refreshes = Clock(), [], []

    def send(request, *, timeout):
        requests.append(request)
        raise TimeoutError("external detail")

    def refresh():
        refreshes.append(True)
        raise TypeError("截图处理参数错误")

    with pytest.raises(transport.MultimodalRequestError) as caught:
        transport.request_with_retry(
            Request(URL), request_timeout=30, open_request=send,
            refresh_request=refresh, clock=clock, sleep=clock.sleep,
        )
    report_exception(caught.value)

    assert len(requests) == len(refreshes) == 1
    assert not diagnostics.is_model_api_failure(caught.value)
    assert len(logs.events) == 1
    assert "截图或请求刷新失败" in public_messages(logs)
    assert "TypeError" in public_messages(logs)
    assert "次接口重试" not in public_messages(logs)
    assert "次接口重试" in logs.backend.getvalue()
    assert "stop_reason=refresh_failed" in logs.backend.getvalue()


@pytest.mark.parametrize("marked", [False, True])
def test_model_workflow_failure_log_uses_the_exception_marker(logs, marked):
    error = api_error() if marked else transport.MultimodalRequestError("截图刷新失败")

    def complete(*args, **kwargs):
        raise error

    workflow = ModelWorkflow(
        SimpleNamespace(screenshot=lambda: b"offline-frame"),
        {"timeout_seconds": 60, "multimodal": {"timeout_seconds": 30, "max_calls": 1}},
        model=SimpleNamespace(complete_json=complete),
    )
    with pytest.raises(transport.MultimodalRequestError) as caught:
        workflow.read("战斗完成检查", "offline prompt", lambda value: value)

    assert caught.value is error
    assert ("接口请求失败" in public_messages(logs)) is not marked
    assert "接口请求失败" in logs.backend.getvalue()


@pytest.mark.parametrize("error_kind", ["api", "code", "visual", "daily_deadline"])
def test_main_screen_failure_log_only_hides_api_errors(logs, tmp_path, error_kind):
    errors = {
        "api": api_error(),
        "code": TypeError("模型识别参数错误"),
        "visual": VisualError("主界面识别内容未确认"),
        "daily_deadline": DailyDeadlineExceeded("执行总时限已到"),
    }
    error = errors[error_kind]

    def complete(*args, **kwargs):
        raise error

    recognizer = main_screen.MainScreenRecognition.__new__(main_screen.MainScreenRecognition)
    recognizer.asset_root = tmp_path
    recognizer.timeout = 30
    recognizer.model = SimpleNamespace(complete_json=complete)
    png = cv2.imencode(".png", np.full((54, 96, 3), 100, np.uint8))[1].tobytes()

    with pytest.raises(type(error)) as caught:
        recognizer.match(png, {"main_menu": "home", "name": "主界面"})

    assert caught.value is error
    assert ("主界面模型识别失败" in public_messages(logs)) is (error_kind != "api")
    assert "主界面模型识别失败" in logs.backend.getvalue()
    assert f"异常类型={type(error).__name__}" in logs.backend.getvalue()


@pytest.mark.parametrize("error_factory", [
    lambda: diagnostics.mark_model_api_failure(TimeoutError("API 故障细节")),
    lambda: diagnostics.mark_model_api_failure(httpx.ReadTimeout("API 故障细节")),
    lambda: api_error(),
])
def test_final_exception_record_for_marked_api_failure_is_backend_only(logs, error_factory):
    error = error_factory()
    report_exception(error)

    assert logs.events == []
    assert type(error).__name__ in logs.backend.getvalue()
    assert str(error) in logs.backend.getvalue()
    assert "执行阶段 daily 失败" in logs.backend.getvalue()


@pytest.mark.parametrize("error_factory", [
    lambda: TypeError("多模态 API 超时重试参数格式错误"),
    lambda: KeyError("多模态 API 请求超时字段"),
    lambda: VisualError("多模态 API 识别内容未确认"),
    lambda: transport.MultimodalRequestError("多模态 API 截图刷新失败"),
    lambda: DailyDeadlineExceeded("执行总时限已到，停止全部操作"),
    lambda: TimeoutError("本地截图超时"),
])
def test_non_api_errors_remain_public_regardless_of_api_words_in_their_text(logs, error_factory):
    error = error_factory()
    assert not diagnostics.is_model_api_failure(error)
    report_exception(error)

    assert len(logs.events) == 1
    message = public_messages(logs)
    assert type(error).__name__ in message
    assert str(error) in message
    assert "执行阶段 daily 失败" in message
    assert type(error).__name__ in logs.backend.getvalue()


@pytest.mark.parametrize("outer_class", [DailyDeadlineExceeded, TypeError])
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_outer_code_or_daily_timeout_stays_public_but_api_cause_is_cut_off(logs, outer_class, link):
    hidden = diagnostics.mark_model_api_failure(TimeoutError("内部 API 故障细节"))
    hidden.__cause__ = KeyError("更深层 API 原因也不公开")
    outer = outer_class("任务总时限已到" if outer_class is DailyDeadlineExceeded else "本地参数错误")
    setattr(outer, link, hidden)
    report_exception(outer)

    assert not diagnostics.is_model_api_failure(outer)
    assert len(logs.events) == 1
    message = public_messages(logs)
    assert type(outer).__name__ in message and str(outer) in message
    assert "内部 API 故障细节" not in message
    assert "更深层 API 原因也不公开" not in message
    assert "原因：" not in message
    assert "内部 API 故障细节" in logs.backend.getvalue()
    assert "更深层 API 原因也不公开" in logs.backend.getvalue()


def test_retry_budget_expiry_still_produces_public_daily_deadline_error(logs):
    clock = Clock()

    def observe():
        raise api_error(retryable=True)

    with pytest.raises(DailyDeadlineExceeded) as caught:
        with execution_scope(5, clock=clock, sleep=clock.sleep):
            retry_observation(observe, name="主界面识别")
    report_exception(caught.value)

    assert clock.waits == [5]
    assert not diagnostics.is_model_api_failure(caught.value)
    assert len(logs.events) == 1
    assert "DailyDeadlineExceeded" in public_messages(logs)
    assert "执行总时限已到" in public_messages(logs)
    assert "接口暂时失败" not in public_messages(logs)
    assert "网络异常或请求超时" not in public_messages(logs)
    assert "接口暂时失败" in logs.backend.getvalue()
    assert "DailyDeadlineExceeded" in logs.backend.getvalue()


def _with_cause(error, cause):
    error.__cause__ = cause
    return error


def _failing_navigation(monkeypatch, backend, error):
    class FailingSDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise error

        async def __aexit__(self, *args):
            pass

    from naruto_daily.multimodal.agent import compat_runner

    monkeypatch.setattr(
        backend, "ModelAsyncClient" if backend is compat_runner else "ClaudeSDKClient", FailingSDK,
    )
    device = SimpleNamespace(
        screenshot=lambda: b"offline-frame",
        swipe=lambda *_: pytest.fail("failed model requests must not move the character"),
    )
    model = SimpleNamespace(agent_connection=lambda: {
        "base_url": "https://example.test/v1", "api_key": "offline-key",
        "model": "offline", "timeout_seconds": 30,
    })
    with pytest.raises((backend.AgentError, transport.MultimodalRequestError, DailyDeadlineExceeded)) as caught:
        backend.run_agent(
            device, model, "offline goal", joystick=compat_runner.Joystick(200, 800, 80),
            evidence_type=backend.GoalEvidence,
        )
    return caught.value


@pytest.mark.parametrize("kind,hidden", [
    ("status", True), ("connection", True), ("transport_cause", True),
    ("legacy_transport_cause", True), ("marked_cause", True), ("model_http", True),
    ("local_type_error", False), ("local_runtime_error", False),
    ("tool_validation", False), ("usage_limit", False), ("usage_limit_with_api_cause", False),
    ("refresh_failure", False), ("daily_deadline", False),
])
def test_compat_navigation_only_hides_confirmed_api_failures(logs, monkeypatch, kind, hidden):
    import httpx as legacy_httpx
    from openai import APIConnectionError, APIStatusError, OpenAIError
    from pydantic_ai.exceptions import AgentRunError, ModelHTTPError, UsageLimitExceeded
    from naruto_daily.multimodal.agent import compat_runner

    request = httpx.Request("POST", URL)
    factories = {
        "status": lambda: APIStatusError("API response failed", response=httpx.Response(503, request=request), body=None),
        "connection": lambda: APIConnectionError(request=request),
        "transport_cause": lambda: _with_cause(APIConnectionError(request=request), httpx.ReadTimeout("offline transport")),
        "legacy_transport_cause": lambda: _with_cause(APIConnectionError(request=request), legacy_httpx.ReadTimeout("offline transport")),
        "marked_cause": lambda: _with_cause(OpenAIError("SDK wrapper"), api_error()),
        "model_http": lambda: ModelHTTPError(429, "offline"),
        "local_type_error": lambda: _with_cause(APIConnectionError(request=request), TypeError("local parameter bug")),
        "local_runtime_error": lambda: _with_cause(APIConnectionError(request=request), RuntimeError("local screenshot bug")),
        "tool_validation": lambda: AgentRunError("tool argument validation failed"),
        "usage_limit": lambda: UsageLimitExceeded("local SDK usage limit"),
        "usage_limit_with_api_cause": lambda: _with_cause(UsageLimitExceeded("local SDK usage limit"), APIConnectionError(request=request)),
        "refresh_failure": lambda: transport.MultimodalRequestError("screenshot refresh failed"),
        "daily_deadline": lambda: _with_cause(OpenAIError("SDK wrapper"), DailyDeadlineExceeded("执行总时限已到")),
    }
    original = factories[kind]()
    raised = _failing_navigation(monkeypatch, compat_runner, original)
    assert diagnostics.is_model_api_failure(raised) is hidden
    if kind in {"marked_cause", "daily_deadline"}:
        assert raised is original.__cause__
    report_exception(raised)

    assert bool(logs.events) is not hidden
    assert "执行阶段 daily 失败" in logs.backend.getvalue()
    if kind == "daily_deadline":
        assert "DailyDeadlineExceeded" in public_messages(logs)
        assert "执行总时限已到" in public_messages(logs)
    elif not hidden:
        assert "导航模型请求或工具参数异常" in public_messages(logs)


@pytest.mark.parametrize("kind,hidden", [
    ("api_reason", True), ("api_status", True), ("marked", True),
    ("max_turns", False), ("api_words_only", False), ("invalid_status", False),
    ("process", False), ("cli_connection", False), ("type_error", False),
    ("runtime_with_api_cause", False), ("local_timeout", False), ("daily_deadline", False),
])
def test_claude_navigation_uses_structured_api_failure_fields(logs, monkeypatch, kind, hidden):
    from claude_agent_sdk import CLIConnectionError, ProcessError, ResultError
    from naruto_daily.multimodal.agent import claude_runner

    factories = {
        "api_reason": lambda: ResultError("offline failure", {"terminal_reason": "api_error"}),
        "api_status": lambda: ResultError("offline failure", {"api_error_status": 503}),
        "marked": api_error,
        "max_turns": lambda: ResultError("offline failure", {"terminal_reason": "max_turns"}),
        "api_words_only": lambda: ResultError("API request timeout retry failed"),
        "invalid_status": lambda: ResultError("offline failure", {"api_error_status": True}),
        "process": lambda: ProcessError("local CLI process failed"),
        "cli_connection": lambda: CLIConnectionError("local CLI connection failed"),
        "type_error": lambda: TypeError("local parameter bug"),
        "runtime_with_api_cause": lambda: _with_cause(RuntimeError("local wrapper"), api_error()),
        "local_timeout": lambda: TimeoutError("local CLI wait timed out"),
        "daily_deadline": lambda: DailyDeadlineExceeded("执行总时限已到"),
    }
    original = factories[kind]()
    raised = _failing_navigation(monkeypatch, claude_runner, original)
    assert diagnostics.is_model_api_failure(raised) is hidden
    report_exception(raised)

    assert bool(logs.events) is not hidden
    assert "执行阶段 daily 失败" in logs.backend.getvalue()
    if kind == "daily_deadline":
        assert raised is original
        assert "DailyDeadlineExceeded" in public_messages(logs)
    elif not hidden:
        assert "导航模型请求或工具参数异常" in public_messages(logs)
