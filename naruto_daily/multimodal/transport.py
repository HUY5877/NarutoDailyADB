"""模型直连 HTTP 客户端及重试策略，不启动本地转发服务。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http.client import HTTPException
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import httpx2 as httpx

from naruto_daily.common.visual import VisualError
from .diagnostics import RequestDiagnostics

LOGGER = logging.getLogger('naruto_daily')
MAX_REQUEST_RETRIES = 3
REQUEST_RETRY_SECONDS = 10
RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class MultimodalError(VisualError):
    """模型调用失败，不得当作已经识别成功。"""


class MultimodalRequestError(MultimodalError):
    """请求已结束重试，调用方不得重跑识别或已执行的工具。"""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不把带密钥的请求自动转发到另一个地址。


@dataclass(frozen=True)
class ModelResponse:
    status: int
    content_type: str
    body: bytes
    request_id: str = ''


def request_budget(timeout):
    # 默认预算和 SDK 超时需容纳全部重试等待；调用方的阶段截止时间仍优先。
    return float(timeout) * (MAX_REQUEST_RETRIES + 1) + REQUEST_RETRY_SECONDS * MAX_REQUEST_RETRIES


def request_with_retry(request, *, request_timeout, timeout_seconds=None, open_request=None,
                       sleep=None, clock=None, max_response_bytes=1_048_576, cancel_event=None):
    open_request = open_request or build_opener(NoRedirect).open
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    budget = request_budget(request_timeout) if timeout_seconds is None else float(timeout_seconds)
    diagnostic = RequestDiagnostics(budget=budget, clock=clock, max_attempts=MAX_REQUEST_RETRIES + 1)
    deadline = diagnostic.deadline
    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            diagnostic.stop('cancelled')
            raise MultimodalRequestError('多模态请求已取消')

    for attempt in range(MAX_REQUEST_RETRIES + 1):
        check_cancelled()
        remaining = deadline - clock()
        if request_timeout <= 0 or remaining <= 0:
            diagnostic.stop('budget_exhausted')
            raise MultimodalRequestError('多模态调用已超过本阶段剩余时间')
        status_code = None
        diagnostic.begin(attempt + 1, min(request_timeout, remaining))
        try:
            with open_request(request, timeout=min(request_timeout, remaining)) as response:
                diagnostic.phase = 'read_body'
                diagnostic.status = getattr(response, 'status', 200)
                body = response.read(max_response_bytes + 1)
                headers = getattr(response, 'headers', None)
                result = ModelResponse(getattr(response, 'status', 200),
                                       headers.get('Content-Type', 'application/json') if headers else 'application/json',
                                       body, diagnostic.request_id)
            check_cancelled()
            if clock() >= deadline:
                diagnostic.stop('late_response')
                raise MultimodalRequestError('多模态响应已超过本阶段剩余时间')
            if len(body) > max_response_bytes:
                diagnostic.stop('response_too_large')
                raise MultimodalRequestError('多模态响应超过大小限制')
            diagnostic.log(logging.INFO, '成功', f'response_bytes={len(body)}')
            return result
        except HTTPError as exc:
            status_code = exc.code
            retryable = status_code in RETRYABLE_HTTP_STATUS
            message = (f'多模态 API 请求受限（HTTP {status_code}）' if status_code == 429 else
                       f'多模态 API 请求失败（HTTP {status_code}），请检查模型、密钥或接口额度')
            diagnostic.failure(exc, status=status_code)
            exc.close()
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            retryable = True
            message = '多模态 API 网络异常或请求超时'
            diagnostic.failure(exc, status=diagnostic.status)
        check_cancelled()
        if not retryable:
            diagnostic.stop('non_retryable')
            raise MultimodalRequestError(f'{message}；{diagnostic.summary()}', status_code=status_code) from None
        if attempt == MAX_REQUEST_RETRIES:
            diagnostic.stop('retries_exhausted')
            raise MultimodalRequestError(f'{message}；已重试{MAX_REQUEST_RETRIES}次，停止请求；{diagnostic.summary()}',
                                         status_code=status_code) from None
        retry_seconds = REQUEST_RETRY_SECONDS
        if deadline - clock() <= retry_seconds:
            diagnostic.stop('budget_insufficient')
            raise MultimodalRequestError(f'{message}；本阶段剩余时间不足，停止重试；{diagnostic.summary()}',
                                         status_code=status_code) from None
        LOGGER.warning('%s；%d秒后进行第%d/%d次接口重试；request_id=%s scene=%s remaining=%.2fs',
                       message, retry_seconds, attempt + 1, MAX_REQUEST_RETRIES,
                       diagnostic.request_id, diagnostic.scene, max(0, deadline - clock()))
        if cancel_event is None:
            sleep(retry_seconds)
        else:
            cancel_event.wait(retry_seconds)
            check_cancelled()


class ModelAsyncClient(httpx.AsyncClient):
    """SDK 直接请求配置的网关；仅在当前 HTTP 请求内部重试。"""

    def __init__(self, *, request_timeout, **kwargs):
        super().__init__(timeout=request_timeout, follow_redirects=False, trust_env=False, **kwargs)
        self.request_timeout = float(request_timeout)

    async def send(self, request, **kwargs):
        await request.aread()  # JSON 请求体可重发，不重跑 SDK 工具或设备动作。
        diagnostic = RequestDiagnostics(budget=request_budget(self.request_timeout), clock=time.monotonic,
                                        max_attempts=MAX_REQUEST_RETRIES + 1)
        deadline = diagnostic.deadline
        for attempt in range(MAX_REQUEST_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostic.stop('budget_exhausted')
                raise httpx.ReadTimeout('多模态请求已超过重试预算', request=request)
            response = None
            diagnostic.begin(attempt + 1, min(self.request_timeout, remaining))
            try:
                async with asyncio.timeout(min(self.request_timeout, remaining)):
                    # SDK send 可能包含读体，不能据此断言耗时全部发生在连接阶段。
                    diagnostic.phase = 'http_send'
                    response = await super().send(request, **kwargs)
                    diagnostic.phase = 'read_body'
                    diagnostic.status = response.status_code
                    await response.aread()
                if 200 <= response.status_code < 300:
                    diagnostic.log(logging.INFO, '成功', f'response_bytes={len(response.content)}')
                    return response
                diagnostic.failure(status=response.status_code)
                if response.status_code not in RETRYABLE_HTTP_STATUS:
                    diagnostic.stop('non_retryable')
                    return response
                if attempt == MAX_REQUEST_RETRIES:
                    diagnostic.stop('retries_exhausted')
                    return response
                message = f'多模态 API 请求失败（HTTP {response.status_code}）'
            except asyncio.CancelledError:
                diagnostic.log(logging.INFO, '取消', 'stop_reason=cancelled')
                raise
            except (httpx.TransportError, TimeoutError) as exc:
                diagnostic.failure(exc, status=diagnostic.status)
                if attempt == MAX_REQUEST_RETRIES:
                    diagnostic.stop('retries_exhausted')
                    raise httpx.ReadTimeout('多模态 API 网络异常或请求超时；已重试3次；' + diagnostic.summary(),
                                            request=request) from None
                message = '多模态 API 网络异常或请求超时'
            finally:
                if response is not None:
                    await response.aclose()
            if deadline - time.monotonic() <= REQUEST_RETRY_SECONDS:
                diagnostic.stop('budget_insufficient')
                raise httpx.ReadTimeout('多模态请求剩余时间不足，停止重试；' + diagnostic.summary(), request=request)
            LOGGER.warning('%s；%d秒后进行第%d/%d次接口重试；request_id=%s scene=%s remaining=%.2fs',
                           message, REQUEST_RETRY_SECONDS, attempt + 1, MAX_REQUEST_RETRIES,
                           diagnostic.request_id, diagnostic.scene, max(0, deadline - time.monotonic()))
            await asyncio.sleep(REQUEST_RETRY_SECONDS)
