"""Bounded, sanitized task logs for the worker's JSON event protocol."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import re
from typing import Callable, Iterable, Iterator, TextIO

from naruto_daily.common.environment import read_multimodal_environment
from naruto_daily.multimodal.channels import API_KEY_ENV_NAMES
from naruto_daily.multimodal.diagnostics import is_model_api_failure


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
    """保护启动时注入环境变量的模型密钥。"""
    values = read_multimodal_environment()
    return tuple((values.get(name) or '').strip() for name in API_KEY_ENV_NAMES)


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
    return _truncate_message(message, max_length)


def _truncate_message(message: str, max_length: int | None) -> str:
    if max_length is not None and len(message) > max_length:
        message = message[:max_length - 1] + "…"
    return message


def _exception_summary(error: BaseException, *, secrets: Iterable[str], max_length: int) -> str:
    """Keep the outer failure and underlying cause, without traceback frames."""
    root = error
    seen = {id(error)}
    for _ in range(8):
        cause = root.__cause__
        if cause is None and not root.__suppress_context__:
            cause = root.__context__
        if cause is None or id(cause) in seen:
            break
        if is_model_api_failure(cause):
            # Preserve an outer code error/overall deadline, but do not append
            # private API diagnostics from its exception chain to the web log.
            break
        seen.add(id(cause))
        root = cause

    def describe(value: BaseException) -> str:
        try:
            detail = str(value)
        except Exception:
            detail = "异常信息无法读取"
        message = type(value).__name__ + (f": {detail}" if detail else "")
        return sanitize_log_message(message, secrets=secrets, max_length=None)

    outer = describe(error)
    if root is error:
        return _truncate_message(outer, max_length)
    cause = describe(root)
    separator = "；原因："
    # Reserve room for the cause even when the wrapping error is very long.
    cause_budget = min(len(cause), (max_length - len(separator)) // 2)
    outer_budget = max_length - len(separator) - cause_budget
    return (_truncate_message(outer, outer_budget) + separator
            + _truncate_message(cause, cause_budget))


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
        if getattr(record, 'backend_only', False):
            return
        try:
            # DailySession may load its .env after this handler was installed.
            # Keep earlier keys protected too if configuration changes in-process.
            secrets = (*self._secrets, *model_log_secrets())
            error = record.exc_info[1] if record.exc_info else None
            if is_model_api_failure(error):
                return
            message = sanitize_log_message(
                record.getMessage(),
                secrets=secrets,
                max_length=200 if error is not None else MAX_LOG_MESSAGE_LENGTH,
            )
            if error is not None:
                # Share only sanitized exception summaries. Tracebacks, source
                # frames and stack_info remain in the local diagnostic stream.
                prefix = "具体错误："
                suffix = f"；{message}" if message else ""
                message = prefix + _exception_summary(
                    error, secrets=secrets,
                    max_length=MAX_LOG_MESSAGE_LENGTH - len(prefix) - len(suffix),
                ) + suffix
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
            if getattr(record, 'secret_realm_completed', None) is True and error is None and event['level'] == 'INFO':
                event['secretRealmCompletedAt'] = event['time']
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
