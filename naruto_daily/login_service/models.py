"""Task data and validation shared by the login HTTP server and worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hmac
import re
import time


UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
ISO_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z")
MAX_TTL_MS = 10 * 60 * 1000
STATUSES = ("queued", "starting_emulator", "opening_game", "preparing_qr", "qr_ready", "failed", "expired")


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def equal_secret(actual: str | None, expected: str) -> bool:
    return isinstance(actual, str) and hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


def timestamp_ms(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000


@dataclass
class TaskRecord:
    id: str
    access_token: str
    created_at: str
    expires_at: str
    expires_at_ms: float
    status: str = "queued"
    error: str | None = None
    _queue_payload: str | None = field(default=None, repr=False, compare=False)
    qr_published: bool = False
    daily_status: str | None = None
    daily_started_at: float | None = None

    def to_view(self) -> dict[str, object]:
        view: dict[str, object] = {
            "id": self.id, "status": self.status, "createdAt": self.created_at,
            "expiresAt": self.expires_at, "simulation": True,
        }
        if self.error:
            view["error"] = self.error
        return view


def _validate_timestamps(created_at: object, expires_at: object) -> float:
    invalid_timestamps = "Task timestamps must be recent; expiry must be in the future and within 10 minutes"
    if (not isinstance(created_at, str) or not isinstance(expires_at, str)
            or not ISO_PATTERN.fullmatch(created_at) or not ISO_PATTERN.fullmatch(expires_at)):
        raise HttpError(400, invalid_timestamps)
    try:
        created, expires = timestamp_ms(created_at), timestamp_ms(expires_at)
    except (ValueError, OverflowError, OSError):
        raise HttpError(400, invalid_timestamps) from None
    now = time.time() * 1000
    if (created > now + 5000 or created < now - MAX_TTL_MS or expires <= now
            or expires <= created or expires - created > MAX_TTL_MS or expires - now > MAX_TTL_MS):
        raise HttpError(400, invalid_timestamps)
    return expires


def parse_submission(body: object) -> TaskRecord:
    if not isinstance(body, dict):
        raise HttpError(400, "Invalid task body")
    task_id, token = body.get("id"), body.get("accessToken")
    created_at, expires_at = body.get("createdAt"), body.get("expiresAt")
    if (not isinstance(task_id, str) or not UUID_PATTERN.fullmatch(task_id)
            or not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token)
            or not isinstance(created_at, str) or not isinstance(expires_at, str)):
        raise HttpError(400, "Expected UUID id, 64-character hex accessToken and ISO timestamps")
    expires = _validate_timestamps(created_at, expires_at)
    return TaskRecord(task_id.lower(), token, created_at, expires_at, expires)


def parse_queue_task(body: object, key_prefix: str) -> TaskRecord:
    """Validate the shared TypeScript queue envelope without a bearer token."""
    if not isinstance(body, dict):
        raise HttpError(400, "Invalid queue task")
    task_id = body.get("id")
    if (type(body.get("version")) is not int or body["version"] != 1
            or not isinstance(task_id, str) or not UUID_PATTERN.fullmatch(task_id)
            or task_id != task_id.lower()
            or not isinstance(key_prefix, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", key_prefix)
            or body.get("objectKey") != f"{key_prefix}/{task_id}/qr.png"):
        raise HttpError(400, "Invalid queue task")
    created_at, expires_at = body.get("createdAt"), body.get("expiresAt")
    expires = _validate_timestamps(created_at, expires_at)
    return TaskRecord(task_id, "", created_at, expires_at, expires)
