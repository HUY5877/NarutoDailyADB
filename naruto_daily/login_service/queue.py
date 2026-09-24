"""FIFO queues and the shared Redis session ownership protocol."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
import secrets
from typing import TYPE_CHECKING, Generic, TypeVar

from . import models
from .models import HttpError, ISO_PATTERN, STATUSES, TaskRecord, UUID_PATTERN, parse_queue_task

if TYPE_CHECKING:
    from .config import ServiceConfig


T = TypeVar("T")
logger = logging.getLogger(__name__)
MAX_QUEUE_PAYLOAD_BYTES = 8192
TERMINAL_STATUSES = frozenset({"qr_ready", "failed", "expired"})
SAFE_TASK_ERROR = "Could not prepare the simulated login QR code. Please create a new login request."
UNSUPPORTED_DRIVER_ERROR = "LDPlayer adapter is not implemented. Set EMULATOR_DRIVER=mock."
SAFE_DRIVER_ERRORS = {
    "configuration_error": "The LDPlayer login service is not configured correctly.",
    "emulator_start_failed": "Could not start the login emulator. Please create a new login request.",
    "game_launch_failed": "Could not open the game for login. Please create a new login request.",
    "qr_not_found": "Could not find the login QR code. Please create a new login request.",
    "command_timeout": "Login QR code preparation timed out. Please create a new login request.",
    "unexpected_error": "Could not prepare the login QR code. Please create a new login request.",
    "login_timeout": "Timed out waiting for QR login confirmation.",
    "login_failed": "Could not confirm the game login.",
    "daily_timeout": "The daily task execution timed out.",
    "daily_failed": "Could not complete the daily tasks.",
}
SAFE_REJECTION_ERRORS = {
    "worker_clock_skew": "执行服务器时间与任务服务器不同步，请同步执行服务器系统时间后重新申请。",
    "invalid_task_payload": "登录任务数据无效，无法开始执行，请重新申请。",
    "invalid_task_timestamps": "登录任务时间信息无效，请重新申请。",
}


class RedisQueueError(RuntimeError):
    """A transport error that cannot expose credentials or Redis payloads."""


# Every write first validates the existing session. In particular, GET followed
# by SET must never recreate a missing/expired session or replace its token hash.
_SESSION_GUARD = """
local kind = redis.call('TYPE', KEYS[1]).ok
if kind ~= 'string' then return 0 end
local raw = redis.call('GET', KEYS[1])
if not raw or #raw > 8192 then return 0 end
local ok, session = pcall(cjson.decode, raw)
if not ok or type(session) ~= 'table' then return 0 end
if session.id ~= ARGV[1] or session.createdAt ~= ARGV[2]
    or session.expiresAt ~= ARGV[3] or session.simulation ~= true then return 0 end
if type(session.tokenHash) ~= 'string' or #session.tokenHash ~= 64
    or not string.match(session.tokenHash, '^[0-9a-fA-F]+$') then return 0 end
local ttl = redis.call('PTTL', KEYS[1])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + tonumber(clock[2]) / 1000
local expires = tonumber(ARGV[4])
if ttl <= 0 or not expires or expires <= now then return 0 end
"""

_CLAIM_SCRIPT = _SESSION_GUARD + """
if session.status ~= 'queued' then return 0 end
session.status = 'starting_emulator'
session.workerId = ARGV[5]
session.error = nil
redis.call('SET', KEYS[1], cjson.encode(session), 'KEEPTTL')
return 1
"""

_OWNER_GUARD = """
if session.workerId ~= ARGV[5] then return 0 end
"""

_ACTIVE_GUARD = """
if session.status ~= 'starting_emulator' and session.status ~= 'opening_game'
    and session.status ~= 'preparing_qr' then return 0 end
"""

_STATUS_SCRIPT = _SESSION_GUARD + _OWNER_GUARD + """
-- A completed write whose response was lost is safe to acknowledge again.
-- Do not rewrite a terminal session or refresh its original expiry.
if session.status == ARGV[6] and (session.status == 'qr_ready'
    or session.status == 'failed' or session.status == 'expired') then return 1 end
""" + _ACTIVE_GUARD + """
session.status = ARGV[6]
session.error = nil
if ARGV[7] ~= '' then session.error = ARGV[7] end
redis.call('SET', KEYS[1], cjson.encode(session), 'KEEPTTL')
return 1
"""

_REQUEUE_SCRIPT = _SESSION_GUARD + _OWNER_GUARD + _ACTIVE_GUARD + """
local kind = redis.call('TYPE', KEYS[2]).ok
if kind ~= 'none' and kind ~= 'list' then return 0 end
session.status = 'queued'
session.workerId = nil
session.error = nil
local encoded = cjson.encode(session)
-- Validate and encode before either write. LPUSH comes first so a wrong-type
-- or allocation error cannot strand a queued session with no list entry.
redis.call('LPUSH', KEYS[2], ARGV[6])
redis.call('SET', KEYS[1], encoded, 'KEEPTTL')
return 1
"""


# A popped envelope has not acquired an owner. Only an exact, still queued
# session can authorize a rejection; neither a claimed session nor an expired
# bearer credential can be recreated or overwritten by malformed queue data.
_REJECT_SCRIPT = """
if redis.call('TYPE', KEYS[1]).ok ~= 'string' then return 0 end
local raw = redis.call('GET', KEYS[1])
if not raw or #raw > 8192 then return 0 end
local ok, session = pcall(cjson.decode, raw)
if not ok or type(session) ~= 'table' then return 0 end
if session.id ~= ARGV[1] or session.createdAt ~= ARGV[2]
    or session.expiresAt ~= ARGV[3] or session.simulation ~= true
    or session.status ~= 'queued' then return 0 end
if session.workerId ~= nil and session.workerId ~= cjson.null then return 0 end
if type(session.tokenHash) ~= 'string' or #session.tokenHash ~= 64
    or not string.match(session.tokenHash, '^[0-9a-fA-F]+$') then return 0 end
local ttl = redis.call('PTTL', KEYS[1])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + tonumber(clock[2]) / 1000
if ttl <= 0 or tonumber(ARGV[4]) <= now then return 0 end
local runKind = redis.call('TYPE', KEYS[2]).ok
if runKind ~= 'none' then
    if runKind ~= 'string' then return 0 end
    local valid, previous = pcall(cjson.decode, redis.call('GET', KEYS[2]))
    if not valid or type(previous) ~= 'table' or previous.taskId ~= ARGV[1] then return 0 end
    if previous.status == 'completed' or previous.status == 'failed'
        or previous.status == 'expired' or previous.status == 'interrupted' then return 0 end
end
local logKind = redis.call('TYPE', KEYS[3]).ok
if logKind ~= 'none' and logKind ~= 'stream' then return 0 end
session.status = 'failed'
session.errorCode = ARGV[5]
session.error = ARGV[6]
local encoded = cjson.encode(session)
redis.call('XADD', KEYS[3], 'MAXLEN', ARGV[10], '*', 'json', ARGV[8])
redis.call('SET', KEYS[2], ARGV[7], 'EX', ARGV[9])
redis.call('EXPIRE', KEYS[3], ARGV[9])
redis.call('SET', KEYS[1], encoded, 'KEEPTTL')
return 1
"""


class RedisTaskQueue:
    """Consume TS task envelopes and update their existing, expiring sessions."""

    def __init__(self, config: ServiceConfig, *, client=None) -> None:
        self.config = config
        self.owner = secrets.token_hex(32)
        try:
            if client is None:
                from redis.asyncio import from_url
                from redis.asyncio.retry import Retry
                from redis.backoff import NoBackoff

                client = from_url(
                    config.redis_url, decode_responses=True,
                    socket_connect_timeout=5, socket_timeout=5,
                    # BRPOP can consume a job even if its response is lost.
                    # Let the runtime reconnect without retrying that command.
                    retry=Retry(NoBackoff(), 0),
                )
            self._client = client
        except Exception:
            raise RedisQueueError("Could not configure Redis transport") from None

    def _session_key(self, record: TaskRecord) -> str:
        return f"{self.config.redis_queue_key}:sessions:{record.id}"

    def _session_args(self, record: TaskRecord) -> tuple[str, ...]:
        return (
            record.id, record.created_at, record.expires_at,
            str(record.expires_at_ms), self.owner,
        )

    async def _call(self, method: str, *args, **kwargs):
        try:
            return await getattr(self._client, method)(*args, **kwargs)
        except Exception:
            # SDK exceptions may include URLs, credentials, or server data.
            raise RedisQueueError("Redis operation failed") from None

    async def initialize(self) -> None:
        await self._call("ping")

    async def _reject_invalid_task(self, body: object, code: str) -> None:
        """Publish a safely attributable rejection, without running an emulator."""
        if not isinstance(body, dict):
            logger.warning("Skipped invalid login queue message: invalid_task_payload (unattributable)")
            return
        task_id, created_at, expires_at = body.get("id"), body.get("createdAt"), body.get("expiresAt")
        if (not isinstance(task_id, str) or not UUID_PATTERN.fullmatch(task_id) or task_id != task_id.lower()
                or not isinstance(created_at, str) or not ISO_PATTERN.fullmatch(created_at)
                or not isinstance(expires_at, str) or not ISO_PATTERN.fullmatch(expires_at)):
            logger.warning("Skipped invalid login queue message: invalid_task_payload (unattributable)")
            return
        try:
            models.timestamp_ms(created_at)
            expires_ms = models.timestamp_ms(expires_at)
        except (ValueError, OverflowError, OSError):
            logger.warning("Skipped invalid login queue message: invalid_task_timestamps (unattributable)")
            return
        if code not in SAFE_REJECTION_ERRORS:
            code = "invalid_task_payload"
        try:
            local_before = models.time.time() * 1000
            seconds, micros = await self._call("time")
            redis_ms = int(seconds) * 1000 + int(micros) / 1000
            local_after = models.time.time() * 1000
            if code == "invalid_task_timestamps":
                try:
                    models._validate_timestamps(created_at, expires_at, now_ms=redis_ms)
                except HttpError:
                    pass  # Bad producer timestamps must not be blamed on this worker's clock.
                else:
                    if redis_ms < min(local_before, local_after) - 5000 or redis_ms > max(local_before, local_after) + 5000:
                        code = "worker_clock_skew"
            timestamp = datetime.fromtimestamp(redis_ms / 1000, timezone.utc).isoformat(
                timespec="milliseconds",
            ).replace("+00:00", "Z")
            message = SAFE_REJECTION_ERRORS[code]
            run = {
                "taskId": task_id, "status": "failed", "startedAt": None,
                "finishedAt": timestamp, "updatedAt": timestamp, "workerId": self.owner,
                "sequence": 0, "errorCode": code,
            }
            event = {"time": timestamp, "level": "ERROR", "message": message}
            prefix = self.config.redis_queue_key
            accepted = await self._call(
                "eval", _REJECT_SCRIPT, 3, f"{prefix}:sessions:{task_id}",
                f"{prefix}:runs:{task_id}", f"{prefix}:logs:{task_id}",
                task_id, created_at, expires_at, str(expires_ms), code, message,
                json.dumps(run, ensure_ascii=False), json.dumps(event, ensure_ascii=False),
                str(getattr(self.config, "run_log_retention_seconds", 86_400)),
                str(getattr(self.config, "run_log_max_entries", 2_000)),
            )
            logger.warning("Rejected login queue task %s: %s (failure report %s)",
                           task_id, code, "stored" if accepted else "not authorized")
        except (RedisQueueError, ValueError, TypeError, OverflowError, OSError):
            logger.warning("Rejected login queue task %s: %s; Redis failure report unavailable", task_id, code)

    async def pop(self) -> TaskRecord | None:
        item = await self._call("brpop", self.config.redis_queue_key, timeout=1)
        if item is None:
            return None
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            logger.warning("Skipped invalid login queue response")
            return None
        payload = item[1]
        body = None
        try:
            if isinstance(payload, bytes):
                if len(payload) > MAX_QUEUE_PAYLOAD_BYTES:
                    logger.warning("Skipped oversized login queue message")
                    return None
                payload = payload.decode("utf-8")
            if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_QUEUE_PAYLOAD_BYTES:
                logger.warning("Skipped invalid or oversized login queue message")
                return None
            body = json.loads(payload)
            record = parse_queue_task(body, self.config.key_prefix)
        except HttpError as error:
            await self._reject_invalid_task(body, getattr(error, "code", "invalid_task_payload"))
            return None
        except (ValueError, UnicodeError, RecursionError):
            logger.warning("Skipped malformed login queue message")
            return None
        claimed = await self._call("eval", _CLAIM_SCRIPT, 1, self._session_key(record), *self._session_args(record))
        if not claimed:
            return None
        record.status = "starting_emulator"
        record._queue_payload = payload
        return record

    async def set_status(self, record: TaskRecord, status: str, error: str | None = None) -> bool:
        if status not in STATUSES or status == "queued":
            raise ValueError("Invalid worker status")
        safe_error = ""
        if status == "failed" and error is not None:
            allowed_errors = {SAFE_TASK_ERROR, UNSUPPORTED_DRIVER_ERROR, *SAFE_DRIVER_ERRORS.values()}
            safe_error = error if error in allowed_errors else SAFE_TASK_ERROR
        changed = await self._call(
            "eval", _STATUS_SCRIPT, 1, self._session_key(record),
            *self._session_args(record), status, safe_error,
        )
        if changed:
            record.status = status
            record.error = safe_error or None
            if status in TERMINAL_STATUSES:
                record._queue_payload = None
        return bool(changed)

    async def requeue(self, record: TaskRecord) -> bool:
        if record._queue_payload is None:
            return False
        changed = await self._call(
            "eval", _REQUEUE_SCRIPT, 2, self._session_key(record), self.config.redis_queue_key,
            *self._session_args(record), record._queue_payload,
        )
        if changed:
            record.status = "queued"
            record.error = None
            record._queue_payload = None
        return bool(changed)

    async def close(self) -> None:
        await self._call("aclose")


class MockRedisQueue(Generic[T]):
    """Use from a single asyncio event loop, as Redis would be used by this service."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._items: deque[T] = deque()
        self._waiters: deque[asyncio.Future[T | None]] = deque()
        self._closed = False

    @property
    def length(self) -> int:
        return len(self._items)

    def lpush(self, item: T) -> bool:
        if self._closed:
            return False
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(item)
                return True
        if len(self._items) >= self._capacity:
            return False
        self._items.appendleft(item)
        return True

    async def brpop(self) -> T | None:
        if self._closed:
            return None
        if self._items:
            return self._items.pop()
        future: asyncio.Future[T | None] = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        try:
            return await future
        finally:
            if future in self._waiters:
                self._waiters.remove(future)

    def remove(self, predicate: Callable[[T], bool]) -> None:
        self._items = deque(item for item in self._items if not predicate(item))

    def close(self) -> None:
        self._closed = True
        self._items.clear()
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
