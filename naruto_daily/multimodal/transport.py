"""所有模型入口共用的 HTTP 重试；SDK 通过临时本机转发复用同一路径。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from naruto_daily.common.visual import VisualError

LOGGER = logging.getLogger('naruto_daily')
MAX_REQUEST_RETRIES = 3
REQUEST_RETRY_SECONDS = 2
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


def request_budget(timeout):
    return float(timeout) * (MAX_REQUEST_RETRIES + 1) + REQUEST_RETRY_SECONDS * MAX_REQUEST_RETRIES


def request_with_retry(request, *, request_timeout, timeout_seconds=None, open_request=None,
                       sleep=None, clock=None, max_response_bytes=1_048_576):
    open_request = open_request or build_opener(NoRedirect).open
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    budget = request_budget(request_timeout) if timeout_seconds is None else float(timeout_seconds)
    deadline = clock() + budget
    for attempt in range(MAX_REQUEST_RETRIES + 1):
        remaining = deadline - clock()
        if request_timeout <= 0 or remaining <= 0:
            raise MultimodalRequestError('多模态调用已超过本阶段剩余时间')
        status_code = None
        try:
            with open_request(request, timeout=min(request_timeout, remaining)) as response:
                body = response.read(max_response_bytes + 1)
                headers = getattr(response, 'headers', None)
                result = ModelResponse(getattr(response, 'status', 200),
                                       headers.get('Content-Type', 'application/json') if headers else 'application/json',
                                       body)
            if clock() >= deadline:
                raise MultimodalRequestError('多模态响应已超过本阶段剩余时间')
            if len(body) > max_response_bytes:
                raise MultimodalRequestError('多模态响应超过大小限制')
            return result
        except HTTPError as exc:
            status_code = exc.code
            retryable = status_code in RETRYABLE_HTTP_STATUS
            message = (f'多模态 API 请求受限（HTTP {status_code}）' if status_code == 429 else
                       f'多模态 API 请求失败（HTTP {status_code}），请检查模型、密钥或接口额度')
            exc.close()
        except (URLError, TimeoutError, OSError, HTTPException):
            retryable = True
            message = '多模态 API 网络异常或请求超时'
        if not retryable:
            raise MultimodalRequestError(message, status_code=status_code) from None
        if attempt == MAX_REQUEST_RETRIES:
            raise MultimodalRequestError(f'{message}；已重试{MAX_REQUEST_RETRIES}次，停止请求',
                                         status_code=status_code) from None
        if deadline - clock() <= REQUEST_RETRY_SECONDS:
            raise MultimodalRequestError(f'{message}；本阶段剩余时间不足，停止重试',
                                         status_code=status_code) from None
        LOGGER.warning('%s；%d秒后进行第%d/%d次接口重试',
                       message, REQUEST_RETRY_SECONDS, attempt + 1, MAX_REQUEST_RETRIES)
        sleep(REQUEST_RETRY_SECONDS)


@contextmanager
def model_api_connection(connection):
    """SDK 只访问带临时令牌的本机端口；上游密钥仅由共用 HTTP 层发送。"""
    upstream = str(connection['base_url']).rstrip('/')
    prefix = urlsplit(upstream).path.rstrip('/')
    token = secrets.token_urlsafe(32)
    timeout = float(connection['timeout_seconds'])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            auth = self.headers.get('Authorization', '')
            key = self.headers.get('x-api-key', '')
            if not (secrets.compare_digest(auth, 'Bearer ' + token) or secrets.compare_digest(key, token)):
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            if parsed.netloc or parsed.scheme or not parsed.path.startswith(prefix + '/'):
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                length = -1
            if not 0 <= length <= 16_777_216:
                self.send_error(413)
                return
            headers = {name: self.headers[name] for name in
                       ('Content-Type', 'Accept', 'anthropic-version', 'anthropic-beta') if name in self.headers}
            # 不转发 SDK 的临时令牌；真实密钥只交给配置指定的上游。
            headers['Authorization'] = 'Bearer ' + connection['api_key']
            request = Request(upstream + self.path[len(prefix):],
                              data=self.rfile.read(length), headers=headers)
            try:
                # 完整读取当前响应再交给 SDK，避免重试半截流导致工具重复执行。
                result = request_with_retry(request, request_timeout=timeout, max_response_bytes=16_777_216)
            except MultimodalRequestError as exc:
                body = json.dumps({'error': {'type': 'api_error', 'message': str(exc)}}).encode()
                result = ModelResponse(exc.status_code or 502, 'application/json', body)
            try:
                self.send_response(result.status)
                self.send_header('Content-Type', result.content_type)
                self.send_header('Content-Length', str(len(result.body)))
                self.end_headers()
                self.wfile.write(result.body)
            except OSError:
                pass  # SDK 已取消，不重试已经返回的响应。

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield {**connection, 'base_url': f'http://127.0.0.1:{server.server_port}{prefix}',
               'api_key': token, 'timeout_seconds': request_budget(timeout)}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
