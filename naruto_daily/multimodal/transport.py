"""模型直连 HTTP 客户端及重试策略，不启动本地转发服务。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http.client import HTTPException
import inspect
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request

import httpx2 as httpx

from naruto_daily.common.visual import VisualError
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, MODEL_REQUEST_TIMEOUT_SECONDS, current_execution,
)
from .diagnostics import RequestDiagnostics, exception_kind

LOGGER = logging.getLogger('naruto_daily')
MAX_REQUEST_RETRIES = 3
REQUEST_RETRY_SECONDS = 10


class MultimodalError(VisualError):
    """模型调用失败，不得当作已经识别成功。"""


class MultimodalRequestError(MultimodalError):
    """HTTP 请求失败；仅 retryable=True 时可重新截图、重做当前观察。"""

    def __init__(self, message, *, status_code=None, retryable=False, error_kind=None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.error_kind = error_kind


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


async def _request_once(request, *, timeout, max_response_bytes, diagnostic):
    """可取消的单次 HTTP 请求，整个连接、响应头及读体共用一个墙钟时限。"""
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            async with client.stream(request.get_method(), request.full_url,
                                     headers=dict(request.header_items()), content=request.data) as response:
                diagnostic.phase = 'read_body'
                diagnostic.status = response.status_code
                if response.status_code != 200:
                    # 只有 200 才算成功；响应正文和请求 URL 都不进入失败日志。
                    raise HTTPError(request.full_url, response.status_code, response.reason_phrase,
                                    response.headers, None)
                chunks, received = [], 0
                async for chunk in response.aiter_bytes(chunk_size=min(65_536, max_response_bytes + 1)):
                    chunk = chunk[:max_response_bytes + 1 - received]
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > max_response_bytes:
                        break
                return ModelResponse(response.status_code, response.headers.get('Content-Type', 'application/json'),
                                     b''.join(chunks), diagnostic.request_id)


def request_with_retry(request, *, request_timeout, timeout_seconds=None, open_request=None,
                       sleep=None, clock=None, max_response_bytes=1_048_576, cancel_event=None,
                       refresh_request=None):
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    execution = current_execution()
    if execution is not None:
        request_timeout = min(float(request_timeout), MODEL_REQUEST_TIMEOUT_SECONDS)
        budget = execution.remaining()
        clock = execution.clock
    else:
        budget = request_budget(request_timeout) if timeout_seconds is None else float(timeout_seconds)
    diagnostic = RequestDiagnostics(budget=budget, clock=clock,
                                    max_attempts=None if execution else MAX_REQUEST_RETRIES + 1)
    deadline = diagnostic.deadline

    def remaining_time():
        if execution is None:
            return deadline - clock()
        try:
            return execution.remaining()
        except DailyDeadlineExceeded:
            diagnostic.stop('daily_deadline_exhausted')
            raise

    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            diagnostic.stop('cancelled')
            raise MultimodalRequestError('多模态请求已取消')

    attempt = 0
    while True:
        check_cancelled()
        remaining = remaining_time()
        if request_timeout <= 0 or remaining <= 0:
            diagnostic.stop('budget_exhausted')
            raise MultimodalRequestError('多模态调用已超过本阶段剩余时间')
        status_code = None
        diagnostic.begin(attempt + 1, min(request_timeout, remaining))
        if attempt > 0 and refresh_request is not None:
            diagnostic.phase = 'refresh_image'
            refreshed_at = clock()
            diagnostic.log(logging.INFO, '刷新截图', '重试前重新截图并重建请求')
            # 与网络异常处理分开：截图失败必须停止，不能回退到旧请求或继续接口重试。
            try:
                request = refresh_request()
            except DailyDeadlineExceeded:
                diagnostic.stop('daily_deadline_exhausted')
                raise
            except Exception as exc:
                diagnostic.failure(exc)
                diagnostic.stop('refresh_failed')
                raise MultimodalRequestError('多模态重试截图或请求刷新失败，已停止；' + diagnostic.summary()) from None
            check_cancelled()
            remaining = remaining_time()
            if remaining <= 0:
                diagnostic.stop('budget_exhausted')
                raise MultimodalRequestError('多模态重试截图后已超过本阶段剩余时间，未发送请求；' + diagnostic.summary())
            diagnostic.request_timeout = min(request_timeout, remaining)
            diagnostic.log(logging.INFO, '截图已刷新', f'refresh_elapsed={max(0, clock() - refreshed_at):.2f}s')
            diagnostic.phase = 'send_or_wait_headers'
        attempt_timeout = min(request_timeout, remaining)
        attempt_deadline = clock() + attempt_timeout

        def check_response_deadline():
            check_cancelled()
            if remaining_time() <= 0:
                diagnostic.stop('late_response')
                raise MultimodalRequestError('多模态响应已超过本阶段剩余时间')
            if clock() >= attempt_deadline:
                raise TimeoutError('单次多模态请求时限已到')

        try:
            if open_request is None:
                # 同步场景使用当前线程的短生命周期事件循环。超时取消并关闭连接，
                # 不启动后台请求线程，也不会把仍在运行的请求留到下一次重试。
                result = asyncio.run(_request_once(request, timeout=attempt_timeout,
                                                  max_response_bytes=max_response_bytes, diagnostic=diagnostic))
                body = result.body
            else:
                with open_request(request, timeout=attempt_timeout) as response:
                    diagnostic.phase = 'read_body'
                    diagnostic.status = getattr(response, 'status', 200)
                    check_response_deadline()
                    if diagnostic.status != 200:
                        raise HTTPError(request.full_url, diagnostic.status, 'Unexpected HTTP status',
                                        getattr(response, 'headers', None), None)
                    body = response.read(max_response_bytes + 1)
                    headers = getattr(response, 'headers', None)
                    result = ModelResponse(getattr(response, 'status', 200),
                                           headers.get('Content-Type', 'application/json') if headers else 'application/json',
                                           body, diagnostic.request_id)
            check_response_deadline()
            if len(body) > max_response_bytes:
                diagnostic.stop('response_too_large')
                raise MultimodalRequestError('多模态响应超过大小限制')
            diagnostic.log(logging.INFO, '成功', f'response_bytes={len(body)}')
            return result
        except DailyDeadlineExceeded:
            raise
        except HTTPError as exc:
            status_code = exc.code
            message = (f'多模态 API 请求受限（HTTP {status_code}）' if status_code == 429 else
                       f'多模态 API 请求失败（HTTP {status_code}），请检查模型、密钥或接口额度')
            diagnostic.failure(exc, status=status_code)
            error_kind = 'http_status'
            exc.close()
        except (URLError, TimeoutError, OSError, HTTPException, httpx.TransportError) as exc:
            message = '多模态 API 网络异常或请求超时'
            diagnostic.failure(exc, status=diagnostic.status)
            error_kind = exception_kind(exc)
        check_cancelled()
        if execution is not None:
            remaining_time()
        if execution is not None and refresh_request is None:
            # 场景层重新截图并运行识别前守卫；此处不能重复发送旧截图。
            diagnostic.stop('refresh_required')
            raise MultimodalRequestError(f'{message}；需重新截图后重试当前观察；{diagnostic.summary()}',
                                         status_code=status_code, retryable=True, error_kind=error_kind) from None
        if execution is None and attempt == MAX_REQUEST_RETRIES:
            diagnostic.stop('retries_exhausted')
            raise MultimodalRequestError(f'{message}；已重试{MAX_REQUEST_RETRIES}次，停止请求；{diagnostic.summary()}',
                                         status_code=status_code, error_kind=error_kind) from None
        retry_seconds = REQUEST_RETRY_SECONDS
        if execution is None and remaining_time() <= retry_seconds:
            diagnostic.stop('budget_insufficient')
            raise MultimodalRequestError(f'{message}；本阶段剩余时间不足，停止重试；{diagnostic.summary()}',
                                         status_code=status_code, error_kind=error_kind) from None
        retry_count = str(attempt + 1) if execution else f'{attempt + 1}/{MAX_REQUEST_RETRIES}'
        retry_seconds = min(retry_seconds, remaining_time())
        LOGGER.warning('%s；%g秒后进行第%s次接口重试；request_id=%s scene=%s remaining=%.2fs',
                       message, retry_seconds, retry_count,
                       diagnostic.request_id, diagnostic.scene, max(0, remaining_time()))
        if cancel_event is None:
            if execution is None:
                sleep(retry_seconds)
            else:
                try:
                    execution.sleep(retry_seconds)
                except DailyDeadlineExceeded:
                    diagnostic.stop('daily_deadline_exhausted')
                    raise
        else:
            cancel_event.wait(retry_seconds)
            check_cancelled()
        attempt += 1


class ModelAsyncClient(httpx.AsyncClient):
    """SDK 直连网关；日常任务在刷新当前截图后重试，不重跑已执行的工具。"""

    def __init__(self, *, request_timeout, refresh_request=None, **kwargs):
        if current_execution() is not None:
            request_timeout = min(float(request_timeout), MODEL_REQUEST_TIMEOUT_SECONDS)
        super().__init__(timeout=request_timeout, follow_redirects=False, trust_env=False, **kwargs)
        self.request_timeout = float(request_timeout)
        self.refresh_request = refresh_request

    async def send(self, request, **kwargs):
        execution = current_execution()
        request_timeout = (min(self.request_timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
                           if execution is not None else self.request_timeout)
        budget = execution.remaining() if execution else request_budget(request_timeout)
        clock = execution.clock if execution else time.monotonic
        diagnostic = RequestDiagnostics(budget=budget, clock=clock,
                                        max_attempts=None if execution else MAX_REQUEST_RETRIES + 1)
        deadline = diagnostic.deadline

        def remaining_time():
            if execution is None:
                return deadline - clock()
            try:
                return execution.remaining()
            except DailyDeadlineExceeded:
                diagnostic.stop('daily_deadline_exhausted')
                raise

        await request.aread()  # 保留 SDK 对话和工具历史；只刷新当前 HTTP 请求里的截图。
        attempt = 0
        while True:
            remaining = remaining_time()
            if remaining <= 0:
                diagnostic.stop('budget_exhausted')
                raise httpx.ReadTimeout('多模态请求已超过重试预算', request=request)
            diagnostic.begin(attempt + 1, min(request_timeout, remaining))
            if attempt > 0 and self.refresh_request is not None:
                diagnostic.phase = 'refresh_image'
                refreshed_at = clock()
                diagnostic.log(logging.INFO, '刷新截图', '重试前重新截图并重建当前请求')
                # 回调也可以终止导航；其异常不可误当作网络故障，更不能退回旧截图。
                request = self.refresh_request(request)
                if inspect.isawaitable(request):
                    request = await request
                await request.aread()
                remaining = remaining_time()
                if remaining <= 0:
                    diagnostic.stop('budget_exhausted')
                    raise httpx.ReadTimeout('多模态重试截图后已超过重试预算', request=request)
                diagnostic.request_timeout = min(request_timeout, remaining)
                diagnostic.log(logging.INFO, '截图已刷新',
                               f'refresh_elapsed={max(0, clock() - refreshed_at):.2f}s')
            response = None
            status_code = None
            try:
                async with asyncio.timeout(min(request_timeout, remaining)):
                    # SDK send 可能包含读体，不能据此断言耗时全部发生在连接阶段。
                    diagnostic.phase = 'http_send'
                    response = await super().send(request, **kwargs)
                    diagnostic.phase = 'read_body'
                    diagnostic.status = response.status_code
                    await response.aread()
                if remaining_time() <= 0:
                    diagnostic.stop('late_response')
                    raise httpx.ReadTimeout('多模态响应已超过重试预算', request=request)
                if response.status_code == 200:
                    diagnostic.log(logging.INFO, '成功', f'response_bytes={len(response.content)}')
                    return response
                diagnostic.failure(status=response.status_code)
                if execution is None and attempt == MAX_REQUEST_RETRIES:
                    diagnostic.stop('retries_exhausted')
                    raise MultimodalRequestError(
                        f'多模态 API 请求失败（HTTP {response.status_code}）；'
                        f'已重试{MAX_REQUEST_RETRIES}次，停止请求；{diagnostic.summary()}',
                        status_code=response.status_code, error_kind='http_status')
                message = f'多模态 API 请求失败（HTTP {response.status_code}）'
                status_code = response.status_code
                error_kind = 'http_status'
            except asyncio.CancelledError:
                diagnostic.log(logging.INFO, '取消', 'stop_reason=cancelled')
                raise
            except DailyDeadlineExceeded:
                raise
            except (httpx.TransportError, TimeoutError) as exc:
                diagnostic.failure(exc, status=diagnostic.status)
                error_kind = exception_kind(exc)
                if execution is None and attempt == MAX_REQUEST_RETRIES:
                    diagnostic.stop('retries_exhausted')
                    raise httpx.ReadTimeout('多模态 API 网络异常或请求超时；已重试3次；' + diagnostic.summary(),
                                            request=request) from None
                message = '多模态 API 网络异常或请求超时'
            finally:
                if response is not None:
                    await response.aclose()
            remaining = remaining_time()
            if execution is not None and self.refresh_request is None:
                diagnostic.stop('refresh_required')
                raise MultimodalRequestError(f'{message}；需重新截图后重试当前观察；{diagnostic.summary()}',
                                             status_code=status_code, retryable=True, error_kind=error_kind)
            if execution is None and remaining <= REQUEST_RETRY_SECONDS:
                diagnostic.stop('budget_insufficient')
                raise httpx.ReadTimeout('多模态请求剩余时间不足，停止重试；' + diagnostic.summary(), request=request)
            retry_seconds = min(REQUEST_RETRY_SECONDS, remaining)
            retry_count = str(attempt + 1) if execution else f'{attempt + 1}/{MAX_REQUEST_RETRIES}'
            LOGGER.warning('%s；%g秒后进行第%s次接口重试；request_id=%s scene=%s remaining=%.2fs',
                           message, retry_seconds, retry_count,
                           diagnostic.request_id, diagnostic.scene, max(0, remaining))
            await asyncio.sleep(retry_seconds)
            attempt += 1
