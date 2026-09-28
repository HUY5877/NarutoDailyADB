"""模型请求诊断：保留类型和时序，不记录请求内容或外部异常原文。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from http.client import HTTPException
import logging
import socket
import ssl
from urllib.error import HTTPError, URLError
from uuid import uuid4


LOGGER = logging.getLogger('naruto_daily')
_SCENE = ContextVar('multimodal_request_scene', default='多模态识别')


def current_model_scene():
    return _SCENE.get()


@contextmanager
def model_request_context(scene):
    token = _SCENE.set(' '.join(str(scene).split())[:160])
    try:
        yield
    finally:
        _SCENE.reset(token)


def safe_exception_details(exc):
    """只取异常类型链及数值错误码；reason 字符串不进入日志。"""
    chain, seen = [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(chain) < 6:
        chain.append(exc)
        seen.add(id(exc))
        reason = getattr(exc, 'reason', None) if isinstance(exc, URLError) else None
        exc = reason if isinstance(reason, BaseException) else (exc.__cause__ or exc.__context__)
    kinds = []
    for error in chain:
        name = type(error).__name__
        if name in {'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout'}:
            kinds.append(name.removesuffix('Timeout').lower() + '_timeout')
        elif isinstance(error, TimeoutError):
            kinds.append('timeout')
        elif isinstance(error, ssl.SSLCertVerificationError):
            kinds.append('tls_certificate')
        elif isinstance(error, ssl.SSLError):
            kinds.append('tls')
        elif isinstance(error, socket.gaierror):
            kinds.append('dns')
        elif isinstance(error, ConnectionResetError):
            kinds.append('connection_reset')
        elif isinstance(error, ConnectionRefusedError):
            kinds.append('connection_refused')
        elif isinstance(error, HTTPError):
            kinds.append('http_status')
        elif isinstance(error, HTTPException):
            kinds.append('http_protocol')
    fields = ['error_type=' + '->'.join(type(error).__name__ for error in chain),
              'error_kind=' + (kinds[0] if kinds else 'transport_or_client_error')]
    for field in ('errno', 'winerror', 'verify_code'):
        values = [getattr(error, field, None) for error in chain]
        values = list(dict.fromkeys(value for value in values if type(value) is int))
        if values:
            fields.append(field + '=' + ','.join(map(str, values)))
    return ' '.join(fields)


class RequestDiagnostics:
    """同一 HTTP 请求的所有尝试共用编号和总预算。"""

    def __init__(self, *, budget, clock, max_attempts):
        self.request_id = uuid4().hex[:12]
        self.scene = current_model_scene()
        self.clock = clock
        self.started = clock()
        self.budget = budget
        self.deadline = self.started + budget
        self.max_attempts = max_attempts
        self.attempt = 0
        self.attempt_started = self.started
        self.request_timeout = 0.0
        self.phase = 'before_request'
        self.error = ''
        self.status = None

    def begin(self, attempt, timeout):
        self.attempt = attempt
        self.attempt_started = self.clock()
        self.request_timeout = timeout
        self.phase = 'send_or_wait_headers'
        self.error = ''
        self.status = None
        self.log(logging.INFO, '开始')

    def summary(self):
        now = self.clock()
        return (f'request_id={self.request_id} scene={self.scene} '
                f'attempt={self.attempt}/{self.max_attempts} retries={max(0, self.attempt - 1)}/{self.max_attempts - 1} '
                f'phase={self.phase} request_timeout={self.request_timeout:.2f}s '
                f'budget={self.budget:.2f}s elapsed={max(0, now - self.started):.2f}s '
                f'attempt_elapsed={max(0, now - self.attempt_started):.2f}s '
                f'remaining={max(0, self.deadline - now):.2f}s '
                f'status={self.status if self.status is not None else "none"}'
                + (f' {self.error}' if self.error else ''))

    def log(self, level, event, detail=''):
        LOGGER.log(level, '[多模态请求%s] %s%s', event, self.summary(), f' {detail}' if detail else '')

    def failure(self, exc=None, *, status=None):
        self.status = status
        self.error = safe_exception_details(exc) if exc is not None else 'error_type=HTTPStatusError error_kind=http_status'
        self.log(logging.WARNING, '失败')

    def stop(self, reason):
        self.log(logging.ERROR, '停止', f'stop_reason={reason}')
