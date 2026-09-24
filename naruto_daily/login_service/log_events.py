"""Bounded, sanitized task logs for the worker's JSON event protocol."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
import re
from typing import Callable, Iterable, Iterator


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


def sanitize_log_message(message: str, *, secrets: Iterable[str] = ()) -> str:
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
    if len(message) > MAX_LOG_MESSAGE_LENGTH:
        message = message[:MAX_LOG_MESSAGE_LENGTH - 1] + "…"
    return message


class ProtocolLogHandler(logging.Handler):
    """Forward only public log fields; forwarding failures never stop the task."""

    def __init__(
        self, send_event: Callable[[dict[str, object]], None], *, secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(level=logging.INFO)
        self._send_event = send_event
        self._secrets = tuple(secrets) + (os.environ.get("MULTIMODAL_API_KEY", ""),)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # getMessage deliberately excludes exc_info and stack_info. Tracebacks
            # may contain credentials and are not suitable for the user's log pane.
            # DailySession may load its .env after this handler was installed.
            # Keep earlier keys protected too if configuration changes in-process.
            message = sanitize_log_message(
                record.getMessage(),
                secrets=(*self._secrets, os.environ.get("MULTIMODAL_API_KEY", "")),
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
    handler = ProtocolLogHandler(send_event, secrets=secrets)
    try:
        for candidate in namespace:
            candidate.setLevel(logging.INFO)
            candidate.disabled = False
            candidate.handlers = []
            candidate.propagate = candidate is not logger
        logger.addHandler(handler)
        yield
    finally:
        for candidate, level, disabled, propagate, handlers in saved:
            candidate.setLevel(level)
            candidate.disabled = disabled
            candidate.propagate = propagate
            candidate.handlers = handlers
        handler.close()
