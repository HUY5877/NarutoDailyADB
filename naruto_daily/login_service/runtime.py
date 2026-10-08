"""Queue consumers, task lifetime management, and isolated mock emulator jobs."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING

from naruto_daily.common.execution import DAILY_TIMEOUT_SECONDS

from .config import ServiceConfig
from .ldplayer import LDPlayerError, LDPlayerPoolFull
from .models import HttpError, ISO_PATTERN, TaskRecord, UUID_PATTERN, equal_secret, parse_submission
from .queue import MockRedisQueue, RedisQueueError, RedisTaskQueue, SAFE_DRIVER_ERRORS, SAFE_TASK_ERROR
from .run_logs import RedisRunReporter, TERMINAL as RUN_TERMINAL_STATUSES

if TYPE_CHECKING:
    from .storage import ObjectStorage


logger = logging.getLogger(__name__)
FAILED_INSTANCE_HOLD_SECONDS = 60.0

_CHILD_ENV_ALLOWLIST = frozenset({
    "ALLUSERSPROFILE", "APPDATA", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "COMSPEC", "HOME", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "LOCALAPPDATA",
    "PATH", "PATHEXT", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PUBLIC",
    "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
})


class _TaskExpired(Exception):
    pass


class _WorkerStopping(Exception):
    pass


class _WorkerError(Exception):
    """A worker failure already reduced to an approved public message."""

    def __init__(self, message: str, code: str = "unexpected_error") -> None:
        super().__init__(message)
        self.code = code


class LoginRuntime:
    def __init__(
        self, config: ServiceConfig, storage: ObjectStorage | None = None,
        *, redis_queue: RedisTaskQueue | None = None,
        run_reporter: RedisRunReporter | None = None,
    ) -> None:
        if storage is None:
            from .storage import create_storage
            storage = create_storage(config)
        self.config = config
        self.storage = storage
        self._tasks: dict[str, TaskRecord] = {}
        self._queue: MockRedisQueue[str] = MockRedisQueue(config.max_queue_size)
        self._redis_queue = redis_queue or (RedisTaskQueue(config) if config.redis_url else None)
        self._run_reporter = run_reporter
        if self._run_reporter is None and isinstance(self._redis_queue, RedisTaskQueue):
            self._run_reporter = RedisRunReporter(
                config, self._redis_queue, retention_seconds=config.run_log_retention_seconds,
                stream_limit=config.run_log_max_entries,
            )
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._pending_deletes: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._admission_lock = asyncio.Lock()
        self._manager = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._stopping = False
        self._initialized = False
        self._next_storage_sweep_at = 0.0

    async def start(self) -> None:
        if self._stopping:
            raise RuntimeError("A stopped LoginRuntime cannot be restarted")
        if self._initialized:
            return
        if self.config.emulator_driver == "ldplayer":
            console = self.config.ldplayer_console_path
            if console is None or not Path(console).is_file():
                raise ValueError("LDPLAYER_CONSOLE_PATH must name an existing file")
            if not self.config.ldplayer_pool_indices and not self.config.ldplayer_template.strip():
                raise ValueError("LDPLAYER_TEMPLATE must name a clean stopped template")
            if self.config.ldplayer_pool_indices and self._redis_queue is None:
                raise ValueError("LDPLAYER_POOL_INDICES requires REDIS_URL worker mode")
        try:
            if self._redis_queue is not None:
                await self._redis_queue.initialize()
            await self.storage.initialize()
        except BaseException:
            if self._redis_queue is not None:
                await self._redis_queue.close()
            raise
        self._next_storage_sweep_at = time.monotonic() + self.config.storage_sweep_interval_ms / 1000
        self._initialized = True
        if self._run_reporter is not None:
            self._run_reporter.start()
        consume = self._consume_redis if self._redis_queue is not None else self._consume
        self._workers = [asyncio.create_task(consume()) for _ in range(self.config.concurrency)]
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    @property
    def stats(self) -> dict[str, int]:
        return {"queued": self._queue.length, "active": len(self._children), "tasks": len(self._tasks)}

    def submit(self, body: object) -> str:
        if self._redis_queue is not None:
            raise HttpError(503, "Submit login requests through the TypeScript client")
        if self._stopping or not self._initialized:
            raise HttpError(503, "Service is unavailable")
        record = parse_submission(body)
        existing = self._tasks.get(record.id)
        if existing is not None:
            if (equal_secret(record.access_token, existing.access_token)
                    and record.created_at == existing.created_at and record.expires_at == existing.expires_at):
                return existing.id
            raise HttpError(409, "Task id is already in use")
        if len(self._tasks) >= self.config.max_tasks:
            raise HttpError(429, "Task capacity reached; retry later")
        self._tasks[record.id] = record
        if not self._queue.lpush(record.id):
            del self._tasks[record.id]
            raise HttpError(429, "Queue is full; retry later")
        return record.id

    async def _retry_pause(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), 1)
        except TimeoutError:
            pass

    async def _set_status(self, record: TaskRecord, status: str) -> None:
        warned = False
        while self._redis_queue is not None:
            if record.expires_at_ms <= time.time() * 1000:
                raise _TaskExpired
            try:
                if not await self._redis_queue.set_status(record, status, record.error):
                    raise _TaskExpired
                break
            except RedisQueueError:
                if self._stopping:
                    raise _WorkerStopping from None
                if not warned:
                    logger.warning("Redis status update unavailable; retrying task %s", record.id)
                    warned = True
                await self._retry_pause()
        record.status = status
        logger.info("Task %s: %s", record.id, status)
        if self._run_reporter is not None and status not in {"failed", "expired"}:
            self._run_reporter.emit(record, status="waiting_scan" if status == "qr_ready" else status,
                                    level="ERROR" if status == "failed" else "INFO")

    async def _requeue(self, record: TaskRecord) -> None:
        assert self._redis_queue is not None
        try:
            if await self._redis_queue.requeue(record):
                logger.info("Task %s: returned to Redis for the next worker", record.id)
        except RedisQueueError:
            logger.warning("Could not return task %s to Redis; create a new request if it expires", record.id)

    @staticmethod
    def _kill(child: subprocess.Popen[bytes]) -> None:
        if child.poll() is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass

    def _expire(self, record: TaskRecord) -> None:
        if record.status == "expired":
            return
        record.status = "expired"
        record.error = None
        if (self._run_reporter is not None and record.id not in self._children
                and record.authorized_at is None and record.daily_started_at is None
                and record.daily_status not in {"failed", "interrupted", "completed"}):
            self._run_reporter.emit(record, status="expired", level="WARNING")
        child = self._children.get(record.id)
        if child is not None and record.authorized_at is None and record.daily_started_at is None:
            self._kill(child)
        self._queue.remove(lambda task_id: task_id == record.id)
        self._pending_deletes.add(record.id)

    def _lookup(self, task_id: str) -> TaskRecord | None:
        if not UUID_PATTERN.fullmatch(task_id):
            return None
        record = self._tasks.get(task_id.lower())
        if record is not None and record.expires_at_ms <= time.time() * 1000:
            self._expire(record)
        return record

    def get_task(self, task_id: str, session_token: str | None) -> dict[str, object] | None:
        record = self._lookup(task_id)
        if record is None or not equal_secret(session_token, record.access_token):
            return None
        return record.to_view()

    def object_available(self, task_id: str) -> bool:
        record = self._lookup(task_id)
        return record is not None and record.status == "qr_ready"

    def _execution_deadline(self, event: dict[str, object], now: float) -> float:
        duration = min(self.config.daily_timeout_seconds, DAILY_TIMEOUT_SECONDS)
        # Older children omit this field; their compatibility budget is still
        # capped at 50 minutes and must not reset after logged_in.
        deadline = event.get("executionDeadline", now + duration)
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or deadline <= 0 or deadline > now + duration):
            raise ValueError("Invalid daily execution deadline")
        return float(deadline)

    async def _run_child(
        self, record: TaskRecord, command: list[str], message: dict[str, object],
        *, daily: bool = False,
    ) -> bytes:
        # Popen plus threaded pipe reads works with both Windows event-loop policies.
        # Public job configuration enters the child; Redis is also needed for model routing.
        if daily:
            self._set_daily_status(record, "starting_emulator")
        child_env = {
            key: value for key, value in os.environ.items()
            if key.upper() in _CHILD_ENV_ALLOWLIST
        }
        child_env["PYTHONIOENCODING"] = "utf-8"
        if daily and self.config.redis_url:
            # 与常驻 worker 使用同一 Redis 轮询计数；不放进日志或 JSON 协议。
            child_env['REDIS_URL'] = self.config.redis_url
        # 模型配置由子进程直接读取项目根目录 .env，服务环境不得覆盖。
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self._children[record.id] = child
        assert child.stdin is not None and child.stdout is not None
        stderr = getattr(child, "stderr", None)
        stderr_task = (
            asyncio.create_task(self._relay_child_stderr(record, stderr), name=f"login-stderr:{record.id}")
            if stderr is not None else None
        )
        png: bytes | None = None
        action_count: int | None = None
        try:
            child.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            child.stdin.close()
            async with asyncio.timeout(max(0.001, record.expires_at_ms / 1000 - time.time())) as timeout:
                while True:
                    line = await asyncio.to_thread(child.stdout.readline, 1_000_001)
                    if not line:
                        break
                    if len(line) > 1_000_000:
                        raise ValueError("Emulator output exceeds limit")
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid emulator output")
                    if self._stopping:
                        raise _WorkerStopping
                    if daily and event.get("type") == "log":
                        self._accept_log(record, event)
                    elif event.get("type") == "stage" and event.get("status") in (
                        "starting_emulator", "opening_game", "preparing_qr",
                    ):
                        if daily and png is not None:
                            raise ValueError("Unexpected login stage after QR publication")
                        if record.expires_at_ms <= time.time() * 1000:
                            self._expire(record)
                        if record.status != "expired" and not self._stopping:
                            await self._set_status(record, event["status"])
                    elif event.get("type") == "qr" and isinstance(event.get("png"), str):
                        if daily and png is not None:
                            raise ValueError("Duplicate QR output")
                        candidate = base64.b64decode(event["png"], validate=True)
                        if candidate.startswith(b"\x89PNG\r\n\x1a\n") and len(candidate) <= 700_000:
                            png = candidate
                            if daily:
                                await self._publish_qr(record, png)
                                self._set_daily_status(record, "waiting_scan")
                        elif daily:
                            raise ValueError("Invalid QR image")
                    elif daily and event.get("type") == "authorized":
                        if (not record.qr_published or record.authorized_at is not None
                                or record.daily_started_at is not None
                                or record.expires_at_ms <= time.time() * 1000):
                            raise ValueError("Unexpected authorization confirmation")
                        now = time.time()
                        record.execution_deadline = self._execution_deadline(event, now)
                        record.authorized_at = record.execution_deadline - self.config.daily_timeout_seconds
                        timeout.reschedule(asyncio.get_running_loop().time()
                                           + max(0, record.execution_deadline - time.time()))
                        self._set_daily_status(record, "finishing_login")
                    elif daily and event.get("type") == "logged_in":
                        if (not record.qr_published or record.daily_started_at is not None
                                or record.authorized_at is None and record.expires_at_ms <= time.time() * 1000):
                            raise ValueError("Unexpected login confirmation")
                        record.daily_started_at = time.time()
                        if record.authorized_at is None:
                            # Older children combine authorization and final login.
                            record.execution_deadline = self._execution_deadline(event, record.daily_started_at)
                            timeout.reschedule(asyncio.get_running_loop().time()
                                               + max(0, record.execution_deadline - time.time()))
                        self._set_daily_status(record, "running_daily")
                    elif daily and event.get("type") == "daily_completed":
                        count = event.get("actionCount")
                        if (record.daily_started_at is None or action_count is not None
                                or type(count) is not int or count < 0):
                            raise ValueError("Unexpected daily completion")
                        action_count = count
                    elif event.get("type") == "error" and event.get("code") in SAFE_DRIVER_ERRORS:
                        raise _WorkerError(SAFE_DRIVER_ERRORS[event["code"]], event["code"])
                    elif daily:
                        raise ValueError("Invalid daily worker event")
                exit_code = await asyncio.to_thread(child.wait)
                if self._stopping:
                    raise _WorkerStopping
                if exit_code != 0 or png is None or daily and action_count is None:
                    raise RuntimeError("Emulator process exited before a QR image was produced")
                if daily:
                    self._set_daily_status(record, "completed", action_count=action_count)
                return png
        finally:
            self._kill(child)
            try:
                await asyncio.to_thread(child.wait)
                if stderr_task is not None:
                    # The child has closed its writer; drain buffered diagnostics
                    # before closing our pipe, but do not hold shutdown indefinitely.
                    try:
                        await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2)
                    except TimeoutError:
                        logger.warning("Task %s: stderr drain timed out during shutdown", record.id)
            finally:
                if stderr_task is not None and not stderr_task.done():
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
                child.stdin.close()
                child.stdout.close()
                if stderr is not None:
                    stderr.close()

    async def _relay_child_stderr(self, record: TaskRecord, stream) -> None:
        """Show full diagnostics locally without publishing them to Redis."""
        from .log_events import model_log_secrets, sanitize_log_message

        known_secrets = (
            *model_log_secrets(), self.config.oss_access_key_id,
            self.config.oss_access_key_secret, self.config.oss_sts_token or "",
            self.config.service_token, record.access_token,
        )
        try:
            while True:
                raw = await asyncio.to_thread(stream.readline)
                if not raw:
                    return
                message = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                level = logging.INFO
                for prefix, candidate in (("INFO ", logging.INFO), ("WARNING ", logging.WARNING), ("ERROR ", logging.ERROR)):
                    if message.startswith(prefix):
                        level, message = candidate, message[len(prefix):]
                        break
                indentation = message[:len(message) - len(message.lstrip(" \t"))].replace("\t", "    ")
                message = sanitize_log_message(message, secrets=known_secrets, max_length=None)
                if message:
                    logger.log(level, "Task %s | %s%s", record.id, indentation, message)
        except (OSError, ValueError):
            # Pipe errors must not hide the worker's actual protocol outcome.
            logger.warning("Task %s: could not read child stderr", record.id)

    def _accept_log(self, record: TaskRecord, event: dict) -> None:
        message, timestamp, level = event.get("message"), event.get("time"), event.get("level")
        if (not isinstance(message, str) or not message or len(message) > 1000
                or not isinstance(timestamp, str) or not ISO_PATTERN.fullmatch(timestamp)
                or level not in {"INFO", "WARNING", "ERROR"}):
            raise ValueError("Invalid task log event")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Invalid task log time") from None
        index, total = event.get("actionIndex"), event.get("actionTotal")
        if (index is not None or total is not None) and (
            type(index) is not int or type(total) is not int or not 0 <= index <= total <= 100_000
        ):
            raise ValueError("Invalid task log progress")
        # Public logs describe stages and action boundaries. Repeated screenshot,
        # similarity, polling and ADB coordinate INFO messages remain internal.
        if level == "INFO" and index is None:
            return
        if self._run_reporter is not None:
            self._run_reporter.emit(record, message=message, level=level, timestamp=timestamp,
                                    action_index=index, action_total=total)

    async def _run_mock(self, record: TaskRecord) -> bytes:
        message = {
            "type": "start", "id": record.id, "nonce": secrets.token_hex(32),
            "stepDelayMs": self.config.step_delay_ms, "expiresAt": record.expires_at,
        }
        return await self._run_child(
            record, [sys.executable, "-u", str(Path(__file__).with_name("mock_emulator.py"))], message,
        )

    async def _run_ldplayer(self, record: TaskRecord) -> bytes:
        assert self.config.ldplayer_console_path is not None
        message = {
            "type": "start", "id": record.id, "expiresAt": record.expires_at,
            "consolePath": str(self.config.ldplayer_console_path),
            "template": self.config.ldplayer_template,
            "poolIndices": list(self.config.ldplayer_pool_indices),
            "stateDir": str(self.config.ldplayer_state_dir),
            "package": self.config.ldplayer_package,
            "commandTimeout": self.config.ldplayer_command_timeout_seconds,
            "dailyConfigPath": str(self.config.daily_config_path),
            "dailyTimeoutSeconds": self.config.daily_timeout_seconds,
            "userOptions": record.user_options,
        }
        return await self._run_child(
            record, [sys.executable, "-u", "-m", "naruto_daily.login_service.ldplayer_worker"], message,
            daily=True,
        )

    async def _publish_qr(self, record: TaskRecord, png: bytes) -> None:
        if record.expires_at_ms <= time.time() * 1000:
            raise _TaskExpired
        try:
            await self.storage.put(record.id, png, record.expires_at)
            await self._set_status(record, "qr_ready")
        except BaseException:
            self._pending_deletes.add(record.id)
            raise
        record.qr_published = True
        if self.config.ldplayer_pool_indices:
            await asyncio.to_thread(self._mark_ldplayer_ready, record.id)

    def _set_daily_status(
        self, record: TaskRecord, status: str, *,
        error_code: str | None = None, action_count: int | None = None,
    ) -> None:
        """Keep execution results beyond the independent Redis/QR session TTL."""
        record.daily_status = status
        record.daily_error_code = error_code
        if self._run_reporter is not None and status not in RUN_TERMINAL_STATUSES:
            self._run_reporter.emit(record, status=status, error_code=error_code,
                                    level="ERROR" if status == "failed" else "WARNING" if status == "interrupted" else "INFO")
        directory = self.config.ldplayer_state_dir / "runs"
        result = {
            "taskId": record.id, "status": status, "updatedAt": time.time(),
            "startedAt": record.daily_started_at, "authorizedAt": record.authorized_at,
            "deadline": (record.execution_deadline if record.execution_deadline is not None else
                         record.authorized_at + self.config.daily_timeout_seconds
                         if record.authorized_at is not None else
                         record.daily_started_at + self.config.daily_timeout_seconds
                         if record.daily_started_at is not None else record.expires_at_ms / 1000),
        }
        if error_code is not None:
            result["errorCode"] = error_code
        if action_count is not None:
            result["actionCount"] = action_count
        temporary = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(result, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, directory / f"{record.id}.json")
        except OSError:
            # An unwritable journal must not bypass emulator cleanup or kill a
            # consumer permanently. Never log filesystem/SDK exception text.
            logger.warning("Could not persist daily execution status for task %s", record.id)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        logger.info("Task %s daily: %s%s", record.id, status, f" ({error_code})" if error_code else "")

    def _ldplayer_manager(self):
        from .ldplayer import LDPlayerManager

        assert self.config.ldplayer_console_path is not None
        if self._manager is None:
            self._manager = LDPlayerManager(
                console_path=self.config.ldplayer_console_path,
                template=self.config.ldplayer_template,
                state_dir=self.config.ldplayer_state_dir,
                command_timeout=self.config.ldplayer_command_timeout_seconds,
                pool_indices=self.config.ldplayer_pool_indices,
            )
        return self._manager

    def _close_ldplayer(self, task_id: str) -> None:
        self._ldplayer_manager().close(task_id)

    def _mark_ldplayer_ready(self, task_id: str) -> None:
        self._ldplayer_manager().mark_ready(task_id)

    def _prepare_ldplayer_retry(self, task_id: str) -> None:
        self._ldplayer_manager().prepare_retry(task_id)

    async def _cleanup_ldplayer(self, task_id: str) -> bool:
        try:
            await asyncio.to_thread(self._close_ldplayer, task_id)
            return True
        except Exception:
            logger.warning("Could not close the LDPlayer login instance for task %s", task_id)
            return False

    async def _hold_failed_ldplayer(self, record: TaskRecord) -> None:
        """Leave the failed emulator visible and reserved until inspection time ends."""
        record.daily_status = "failed"
        if self._run_reporter is not None:
            # Publish the failure now; emulator cleanup must not delay the UI.
            # The reporter deduplicates this unchanged status after cleanup.
            self._run_reporter.emit(
                record, status="failed", error_code=record.daily_error_code, level="ERROR",
            )
        logger.warning(
            "Task %s: 执行失败，保留模拟器 %g 秒供排查，期间实例保持占用；到时自动关闭",
            record.id, FAILED_INSTANCE_HOLD_SECONDS,
        )
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=FAILED_INSTANCE_HOLD_SECONDS)
        except TimeoutError:
            logger.info("Task %s: 失败现场保留时间已到，开始关闭模拟器", record.id)

    async def _release_and_requeue(self, record: TaskRecord) -> None:
        if self.config.emulator_driver == "ldplayer":
            if not await self._cleanup_ldplayer(record.id):
                return
            if self.config.ldplayer_pool_indices:
                try:
                    await asyncio.to_thread(self._prepare_ldplayer_retry, record.id)
                except Exception:
                    logger.warning("Could not prepare LDPlayer pool task %s for retry", record.id)
                    return
        await self._requeue(record)

    async def _consume(self) -> None:
        while not self._stopping:
            task_id = await self._queue.brpop()
            if task_id is None:
                return
            record = self._lookup(task_id)
            if record is None or record.status == "expired" or self._stopping:
                continue
            await self._process_record(record)

    async def _consume_redis(self) -> None:
        assert self._redis_queue is not None
        disconnected = False
        while not self._stopping:
            if len(self._tasks) >= self.config.max_tasks:
                await self._retry_pause()
                continue
            try:
                wait_for_capacity = False
                redis_contacted = False
                if self.config.emulator_driver == "ldplayer" and self.config.ldplayer_pool_indices:
                    async with self._admission_lock:
                        manager = self._ldplayer_manager()
                        try:
                            has_capacity = await asyncio.to_thread(manager.has_capacity)
                        except LDPlayerError:
                            logger.warning("LDPlayer pool capacity is unavailable; retrying")
                            has_capacity = False
                        if not has_capacity:
                            record = None
                            wait_for_capacity = True
                        else:
                            redis_contacted = True
                            record = await self._redis_queue.pop()
                            if record is not None:
                                try:
                                    await asyncio.to_thread(
                                        manager.allocate, record.id, record.expires_at_ms / 1000,
                                    )
                                except LDPlayerPoolFull:
                                    await self._requeue(record)
                                    record = None
                                    wait_for_capacity = True
                                except LDPlayerError:
                                    # The isolated child retries the idempotent allocation and
                                    # converts any persistent driver failure to a safe task error.
                                    logger.warning("LDPlayer pool reservation failed; retrying in task worker")
                else:
                    redis_contacted = True
                    record = await self._redis_queue.pop()
                if disconnected and redis_contacted:
                    logger.info("Redis connection restored")
                    disconnected = False
                if record is None:
                    if wait_for_capacity:
                        await self._retry_pause()
                    continue
            except (HttpError, ValueError):
                logger.warning("Skipped an invalid or expired Redis login task")
                continue
            except RedisQueueError:
                if not disconnected:
                    logger.warning("Redis is unavailable; waiting to reconnect")
                    disconnected = True
                await self._retry_pause()
                continue
            if self._stopping:
                await self._release_and_requeue(record)
                return
            self._tasks[record.id] = record
            await self._process_record(record)

    async def _process_record(self, record: TaskRecord) -> None:
        task_id = record.id
        uploaded = False
        keep_ldplayer = False
        requeue_on_exit = False
        if self._run_reporter is not None:
            self._run_reporter.emit(record, status="starting_emulator")
        try:
            await self._set_status(record, "starting_emulator")
            if self._stopping:
                raise _WorkerStopping
            png = (
                await self._run_mock(record)
                if self.config.emulator_driver == "mock"
                else await self._run_ldplayer(record)
            )
            if record.qr_published:
                # The QR was published while the child was alive. The same
                # child has now finished its daily run; finally releases it.
                return
            if record.expires_at_ms <= time.time() * 1000:
                raise _TaskExpired
            if self._stopping:
                raise _WorkerStopping
            await self.storage.put(task_id, png, record.expires_at)
            uploaded = True
            if record.expires_at_ms <= time.time() * 1000:
                raise _TaskExpired
            if self._stopping and self._redis_queue is None:
                raise _WorkerStopping
            await self._set_status(record, "qr_ready")
            keep_ldplayer = self.config.emulator_driver == "ldplayer"
            if self.config.ldplayer_pool_indices:
                try:
                    await asyncio.to_thread(self._mark_ldplayer_ready, task_id)
                except Exception:
                    logger.warning("Could not mark the LDPlayer pool slot ready for task %s", task_id)
        except Exception as error:
            # Failures before the child starts have no daily status yet, but
            # still need a safe public reason in the final execution log.
            record.daily_error_code = (
                error.code if isinstance(error, _WorkerError) else
                "worker_stopped" if self._stopping else
                "command_timeout" if isinstance(error, (TimeoutError, _TaskExpired)) else
                "unexpected_error"
            )
            if not record.qr_published and record.daily_status is not None:
                code = error.code if isinstance(error, _WorkerError) else "protocol_error"
                if self._stopping:
                    code = "worker_stopped"
                elif isinstance(error, (TimeoutError, _TaskExpired)):
                    code = "command_timeout"
                self._set_daily_status(
                    record, "interrupted" if self._stopping else "failed", error_code=code,
                )
            if record.qr_published:
                if self._stopping:
                    status, code = "interrupted", "worker_stopped"
                elif isinstance(error, TimeoutError) or (
                    record.authorized_at is None and record.daily_started_at is None
                    and record.expires_at_ms <= time.time() * 1000
                ):
                    status = "failed"
                    code = ("daily_timeout" if record.daily_started_at is not None else
                            "login_finish_timeout" if record.authorized_at is not None else "login_timeout")
                else:
                    status = "failed"
                    code = error.code if isinstance(error, _WorkerError) else "protocol_error"
                self._set_daily_status(record, status, error_code=code)
                # qr_ready is terminal in the existing browser/Redis protocol.
                # Record execution failures locally, never rewrite that session
                # or automatically replay an already published login task.
                self._pending_deletes.add(task_id)
                if record.expires_at_ms <= time.time() * 1000:
                    self._expire(record)
            elif isinstance(error, _TaskExpired) or record.expires_at_ms <= time.time() * 1000:
                self._expire(record)
                if uploaded:
                    # The sweeper may have deleted the object before a slow upload finished.
                    self._pending_deletes.add(task_id)
                    try:
                        await self.storage.delete(task_id)
                        self._pending_deletes.discard(task_id)
                    except Exception:
                        pass
            elif self._stopping:
                if self._redis_queue is not None:
                    requeue_on_exit = True
                else:
                    self._expire(record)
            else:
                record.error = (
                    str(error) if isinstance(error, _WorkerError)
                    else SAFE_TASK_ERROR if self.config.emulator_driver == "mock"
                    else SAFE_DRIVER_ERRORS["unexpected_error"]
                )
                try:
                    await self._set_status(record, "failed")
                except _TaskExpired:
                    self._expire(record)
                except _WorkerStopping:
                    requeue_on_exit = True
                self._pending_deletes.add(task_id)
        finally:
            child = self._children.pop(task_id, None)
            if child is not None:
                self._kill(child)
            if requeue_on_exit:
                await self._release_and_requeue(record)
            elif self.config.emulator_driver == "ldplayer" and not keep_ldplayer:
                try:
                    if (not self._stopping
                            and (record.daily_status == "failed" or record.status == "failed")):
                        await self._hold_failed_ldplayer(record)
                finally:
                    # The pool binding stays reserved throughout the hold. Also
                    # release it if the consumer is cancelled during inspection.
                    released = await self._cleanup_ldplayer(task_id)
                    if record.qr_published and not released:
                        self._set_daily_status(record, "failed", error_code="instance_cleanup_failed")
            if self._run_reporter is not None:
                final_status = record.daily_status
                if final_status not in RUN_TERMINAL_STATUSES:
                    final_status = ("interrupted" if self._stopping else record.status
                                    if record.status in {"failed", "expired"} else
                                    "completed" if self.config.emulator_driver == "mock" else "failed")
                if not requeue_on_exit:
                    record.daily_status = final_status
                    self._run_reporter.emit(
                        record, status=final_status,
                        error_code=record.daily_error_code,
                        level="ERROR" if final_status == "failed" else "WARNING" if final_status in {"interrupted", "expired"} else "INFO",
                        message="模拟二维码已生成" if self.config.emulator_driver == "mock" and final_status == "completed" else None,
                    )
                self._run_reporter.finish(task_id)

    async def _sweep_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._stop_event.wait(), self.config.sweep_interval_ms / 1000)
                return
            except TimeoutError:
                await self._sweep()

    async def _sweep(self) -> None:
        now = time.time() * 1000
        for task_id, record in list(self._tasks.items()):
            if record.expires_at_ms <= now:
                self._expire(record)
            if (record.expires_at_ms + self.config.retention_ms <= now
                    and task_id not in self._children and task_id not in self._pending_deletes):
                del self._tasks[task_id]
        for task_id in list(self._pending_deletes):
            try:
                await self.storage.delete(task_id)
                self._pending_deletes.discard(task_id)
            except Exception:
                # Retry on the next pass; SDK errors may contain credentials.
                pass
        if time.monotonic() >= self._next_storage_sweep_at:
            self._next_storage_sweep_at = time.monotonic() + self.config.storage_sweep_interval_ms / 1000
            try:
                await self.storage.sweep()
            except Exception:
                pass

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._stop_event.set()
        self._queue.close()
        for child in list(self._children.values()):
            self._kill(child)
        await asyncio.gather(*self._workers, return_exceptions=True)
        if self._run_reporter is not None:
            await self._run_reporter.close()
        if self._sweep_task is not None:
            await self._sweep_task
        # Ready OSS images remain available to the client until their own expiry.
        to_delete = self._pending_deletes if self._redis_queue is not None else set(self._tasks) | self._pending_deletes
        await asyncio.gather(
            *(self.storage.delete(task_id) for task_id in to_delete),
            return_exceptions=True,
        )
        if self._redis_queue is not None:
            await self._redis_queue.close()
        self._tasks.clear()
        self._pending_deletes.clear()
