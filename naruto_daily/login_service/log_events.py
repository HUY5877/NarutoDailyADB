"""Bounded, sanitized task logs for the worker's JSON event protocol."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
import re
from typing import Callable, Iterable, Iterator, TextIO

from naruto_daily.common.environment import EnvironmentConfigError, read_multimodal_environment
from naruto_daily.multimodal.channels import API_KEY_ENV_NAMES


MAX_LOG_MESSAGE_LENGTH = 1_000
_REDACTED = "[REDACTED]"
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@")
_AUTH_HEADER = re.compile(r"(?im)\b(authorization\s*[:=]\s*)[^\r\n]+")
_COOKIE_HEADER = re.compile(r"(?im)\b((?:set-)?cookie\s*[:=]\s*)[^\r\n]+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]+")
_SECRET_FIELD = re.compile(
    r"(?i)(\b(?:[a-z0-9]+[-_])*(?:api[-_ ]?key|access[-_]?key(?:[-_]?id|[-_]?secret)?|"
    r"secret(?:[-_]?key)?|password|passwd|pwd|access[-_]?token|"
    r"refresh[-_]?token|auth[-_]?token|id[-_]?token|token|signature|credential|密码|密钥|令牌)"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]\)]+)"
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def model_log_secrets() -> tuple[str, ...]:
    """保护实际使用的根目录密钥，同时继续遮蔽进程中残留的旧密钥。"""
    try:
        values = read_multimodal_environment()
    except EnvironmentConfigError:
        # 配置错误由模型调用方报告，日志处理不得掩盖原始异常。
        values = {}
    return tuple((values.get(name) or '').strip() for name in API_KEY_ENV_NAMES) + tuple(
        os.environ.get(name, '') for name in API_KEY_ENV_NAMES)


def sanitize_log_message(message: str, *, secrets: Iterable[str] = (),
                         max_length: int | None = MAX_LOG_MESSAGE_LENGTH) -> str:
    """Remove known credentials before truncation or whitespace normalization."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            message = message.replace(secret, _REDACTED)
    message = _URL_CREDENTIALS.sub(r"\1" + _REDACTED + "@", message)
    message = _AUTH_HEADER.sub(r"\1" + _REDACTED, message)
    message = _COOKIE_HEADER.sub(r"\1" + _REDACTED, message)
    message = _BEARER.sub(r"\1" + _REDACTED, message)
    message = _SECRET_FIELD.sub(r"\1" + _REDACTED, message)
    message = _CONTROL_CHARACTERS.sub("", message)
    message = " ".join(message.split())
    if max_length is not None and len(message) > max_length:
        message = message[:max_length - 1] + "…"
    return message


class DiagnosticLogHandler(logging.StreamHandler):
    """Keep full local diagnostics and traceback lines outside the web protocol."""

    def __init__(self, stream: TextIO, *, secrets: Iterable[str] = ()) -> None:
        super().__init__(stream)
        self.setLevel(logging.INFO)
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self._secrets = tuple(secrets) + model_log_secrets()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            secrets = (*self._secrets, *model_log_secrets())
            for line in self.format(record).splitlines():
                indent = line[:len(line) - len(line.lstrip())]
                message = sanitize_log_message(line, secrets=secrets, max_length=None)
                self.stream.write(indent + message + "\n")
            self.flush()
        except Exception:
            # A closed terminal must not replace the original game error.
            pass


class ProtocolLogHandler(logging.Handler):
    """Forward only public log fields; forwarding failures never stop the task."""

    def __init__(
        self, send_event: Callable[[dict[str, object]], None], *, secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(level=logging.INFO)
        self._send_event = send_event
        self._secrets = tuple(secrets) + model_log_secrets()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # getMessage deliberately excludes exc_info and stack_info. Tracebacks
            # may contain credentials and are not suitable for the user's log pane.
            # DailySession may load its .env after this handler was installed.
            # Keep earlier keys protected too if configuration changes in-process.
            message = sanitize_log_message(
                record.getMessage(),
                secrets=(*self._secrets, *model_log_secrets()),
            )
            if not message:
                return
            event: dict[str, object] = {
                "type": "log",
                "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                    timespec="milliseconds",
                ).replace("+00:00", "Z"),
                "level": (
                    "ERROR" if record.levelno >= logging.ERROR else
                    "WARNING" if record.levelno >= logging.WARNING else "INFO"
                ),
                "message": message,
            }
            index = getattr(record, "action_index", None)
            total = getattr(record, "action_total", None)
            if type(index) is int and type(total) is int and 0 <= index <= total:
                event["actionIndex"] = index
                event["actionTotal"] = total
            self._send_event(event)
        except Exception:
            # Logging is best effort, including formatting and broken output pipes.
            # Do not call handleError: that can dump the raw, unredacted LogRecord.
            return


@contextmanager
def forward_task_logs(
    send_event: Callable[[dict[str, object]], None], *, secrets: Iterable[str] = (),
    diagnostic_stream: TextIO | None = None,
) -> Iterator[None]:
    """Temporarily route this task's namespace to one isolated protocol handler."""
    logger = logging.getLogger("naruto_daily")
    namespace = [logger] + [
        candidate for name, candidate in list(logging.Logger.manager.loggerDict.items())
        if name.startswith("naruto_daily.") and isinstance(candidate, logging.Logger)
    ]
    saved = [
        (candidate, candidate.level, candidate.disabled, candidate.propagate, candidate.handlers[:])
        for candidate in namespace
    ]
    secrets = tuple(secrets)
    handler = ProtocolLogHandler(send_event, secrets=secrets)
    diagnostic_handler = DiagnosticLogHandler(diagnostic_stream, secrets=secrets) if diagnostic_stream is not None else None
    try:
        for candidate in namespace:
            candidate.setLevel(logging.INFO)
            candidate.disabled = False
            candidate.handlers = []
            candidate.propagate = candidate is not logger
        logger.addHandler(handler)
        if diagnostic_handler is not None:
            logger.addHandler(diagnostic_handler)
        yield
    finally:
        for candidate, level, disabled, propagate, handlers in saved:
            candidate.setLevel(level)
            candidate.disabled = disabled
            candidate.propagate = propagate
            candidate.handlers = handlers
        handler.close()
        if diagnostic_handler is not None:
            diagnostic_handler.close()
