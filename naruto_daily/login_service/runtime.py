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
import re
import secrets
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING

from naruto_daily.common.execution import DAILY_TIMEOUT_SECONDS
from naruto_daily.common.environment import model_environment_for_child

from .config import ServiceConfig
from .ldplayer import LDPlayerError, LDPlayerPoolFull
from .log_events import is_private_capture_message
from .models import HttpError, ISO_PATTERN, TaskRecord, UUID_PATTERN, equal_secret, parse_submission
from .queue import MockRedisQueue, RedisQueueError, RedisTaskQueue, SAFE_DRIVER_ERRORS, SAFE_TASK_ERROR
from .run_logs import RedisRunReporter, TERMINAL as RUN_TERMINAL_STATUSES
from .user_options import ARENA_WEEKLY_SECONDS

if TYPE_CHECKING:
    from .storage import ObjectStorage
    from .qq_notify import QQGroupNotifier
    from .user_names import UserNameResolver
    from .qq_bindings import QQBindingResolver


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


class _TaskInterrupted(Exception):
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
        qq_notifier: QQGroupNotifier | None = None,
        user_name_resolver: UserNameResolver | None = None,
        qq_binding_resolver: QQBindingResolver | None = None,
    ) -> None:
        if storage is None:
            from .storage import create_storage
            storage = create_storage(config)
        self.config = config
        self.storage = storage
        self._qq_notifier = qq_notifier
        self._user_name_resolver = user_name_resolver
        self._qq_binding_resolver = qq_binding_resolver
        if self._qq_notifier is None and config.qq_bot_app_id:
            from .qq_notify import QQGroupNotifier
            self._qq_notifier = QQGroupNotifier(
                config.qq_bot_app_id, config.qq_bot_app_secret, config.qq_bot_group_openid,
            )
        if self._user_name_resolver is None and config.database_url:
            from .user_names import UserNameResolver
            self._user_name_resolver = UserNameResolver(config.database_url)
        if self._qq_binding_resolver is None and config.qq_bot_app_id and config.database_url:
            from .qq_bindings import QQBindingResolver
            self._qq_binding_resolver = QQBindingResolver(
                config.database_url, config.qq_bot_app_id, config.qq_bot_group_openid,
            )
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._interruption_tasks: set[asyncio.Task[None]] = set()
        self._tasks: dict[str, TaskRecord] = {}
        self._queue: MockRedisQueue[str] = MockRedisQueue(config.max_queue_size)
        self._redis_queue = redis_queue or (RedisTaskQueue(config) if config.redis_url else None)
        self._run_reporter = run_reporter
        self._log_publisher = None
        if config.sls_enabled and self._redis_queue is not None:
            from .sls_logs import SlsLogPublisher
            self._log_publisher = SlsLogPublisher(config)
            self._redis_queue.log_publisher = self._log_publisher
        if self._run_reporter is None and isinstance(self._redis_queue, RedisTaskQueue):
            self._run_reporter = RedisRunReporter(
                config, self._redis_queue, retention_seconds=config.run_log_retention_seconds,
                log_publisher=self._log_publisher,
            )
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        if isinstance(self._run_reporter, RedisRunReporter):
            self._run_reporter.on_interrupted = self._on_run_interrupted
        self._pending_deletes: set[str] = set()
        self._pending_session_failures: dict[str, TaskRecord] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._admission_lock = asyncio.Lock()
        self._manager = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._shutdown_task: asyncio.Task[None] | None = None
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
        if self._log_publisher is not None:
            self._log_publisher.start()
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

    def _on_run_interrupted(self, task_id: str, attempt_id: str | None) -> None:
        record = self._tasks.get(task_id)
        if record is None or record.attempt_id != attempt_id:
            return
        first_interruption = not record._admin_interrupted
        record._admin_interrupted = True
        if record._interrupt_event is not None:
            record._interrupt_event.set()
        self._set_daily_status(record, "interrupted", error_code="admin_interrupted")
        logger.info("Task %s: 管理员已中断任务，停止对应执行进程", task_id)
        if first_interruption and self._run_reporter is not None:
            self._run_reporter.emit(record, status="interrupted", error_code="admin_interrupted",
                                    level="WARNING", message="管理员已中断任务，停止对应执行进程")
        child = self._children.get(task_id)
        if child is not None:
            self._kill(child)
        if first_interruption and record._instance_released and not record._processing:
            # A slow final write may discover cancellation after normal cleanup.
            # Never await this from the reporter's own publication loop.
            cleanup = asyncio.create_task(self._finish_failed_session(record))
            self._interruption_tasks.add(cleanup)
            cleanup.add_done_callback(self._interruption_tasks.discard)

    @staticmethod
    def _check_interrupted(record: TaskRecord) -> None:
        if record._admin_interrupted:
            raise _TaskInterrupted

    async def _set_status(self, record: TaskRecord, status: str) -> None:
        self._check_interrupted(record)
        warned = False
        while self._redis_queue is not None:
            self._check_interrupted(record)
            if record.expires_at_ms <= time.time() * 1000:
                raise _TaskExpired
            try:
                if not await self._redis_queue.set_status(record, status, record.error):
                    raise _TaskExpired
                self._check_interrupted(record)
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

    async def _finish_failed_session(self, record: TaskRecord) -> None:
        """Retry a cleanup-confirmed failure without replaying the emulator task."""
        if record.expires_at_ms <= time.time() * 1000:
            self._pending_session_failures.pop(record.id, None)
            return
        if self._redis_queue is None:
            record.status = 'failed'
            record.error = SAFE_DRIVER_ERRORS.get(record.daily_error_code, SAFE_TASK_ERROR)
            return
        try:
            # A false result means the lease/owner is gone; never recreate it.
            await self._redis_queue.finish_failed(record)
            self._pending_session_failures.pop(record.id, None)
        except RedisQueueError:
            if record.id not in self._pending_session_failures:
                logger.warning('Could not finish failed login session %s; will retry', record.id)
            self._pending_session_failures[record.id] = record

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
        self._check_interrupted(record)
        # Popen plus threaded pipe reads works with both Windows event-loop policies.
        # Public job configuration enters the child; Redis is also needed for model routing.
        if daily:
            self._set_daily_status(record, "starting_emulator")
        child_env = {
            key: value for key, value in os.environ.items()
            if key.upper() in _CHILD_ENV_ALLOWLIST
        }
        child_env["PYTHONIOENCODING"] = "utf-8"
        if daily:
            child_env.update(model_environment_for_child())
            child_env.pop('REDIS_URL', None)
        if daily and self.config.redis_url:
            # 与常驻 worker 使用同一 Redis 轮询计数；不放进日志或 JSON 协议。
            child_env['REDIS_URL'] = self.config.redis_url
        if daily and record.login_type in ('android_qq', 'ios_qq'):
            # These selected-profile values never enter the public task protocol.
            if self.config.database_url:
                child_env['DATABASE_URL'] = self.config.database_url
            if self.config.qq_capture_api_base:
                child_env['QQ_CAPTURE_API_BASE'] = self.config.qq_capture_api_base
            if self.config.qq_apk_release_prefix:
                try:
                    downloads = await asyncio.wait_for(
                        self.storage.apk_download_requests(self.config.qq_apk_release_prefix),
                        timeout=max(0.001, min(5.0, record.expires_at_ms / 1000 - time.time())),
                    )
                    child_env['QQ_APK_DOWNLOADS'] = json.dumps({
                        package: {**downloads[package], 'sha256': digest}
                        for package, digest in (('com.huy.authcapture', self.config.qq_capture_apk_sha256),
                                                ('com.tencent.tim', self.config.qq_login_apk_sha256))
                    })
                except Exception:
                    # Signing errors may contain credentials; keep the legacy
                    # startup path available without logging exception contents.
                    logger.warning('Task %s: 辅助应用下载地址暂不可用，继续原有应用检测和扫码流程', record.id,
                                   extra={'backend_only': True})
                # Signing yields to cancellation/shutdown. Do not launch work
                # that became inactive while its private requests were prepared.
                self._check_interrupted(record)
                if self._stopping:
                    raise _WorkerStopping
                if record.expires_at_ms <= time.time() * 1000:
                    raise _TaskExpired
        # 子进程复用启动时的模型配置，不重新请求 KV，也不读取本地模型 key。
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
        refresh_id: str | None = None
        arena_started = False
        capture_owner = message.get('captureUserId')
        if not (daily and record.login_type in ('android_qq', 'ios_qq')
                and self.config.database_url and self._redis_queue is not None
                and isinstance(capture_owner, str) and UUID_PATTERN.fullmatch(capture_owner)
                and record.attempt_id and message.get('captureAttemptId') == record.attempt_id):
            capture_owner = None
        capture_started = False
        module_monitor = None
        module_stack: list[str] = []
        module_serial = None
        try:
            child.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            child.stdin.close()
            async with asyncio.timeout(max(0.001, record.expires_at_ms / 1000 - time.time())) as timeout:
                while True:
                    line = await asyncio.to_thread(child.stdout.readline, 1_000_001)
                    if not line:
                        self._check_interrupted(record)
                        break
                    if len(line) > 1_000_000:
                        raise ValueError("Emulator output exceeds limit")
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid emulator output")
                    self._check_interrupted(record)
                    if self._stopping:
                        raise _WorkerStopping
                    if event == {'type': 'capture_started'}:
                        # Keep older children's protocol fences, without public
                        # progress or task-end revocation side effects.
                        if capture_owner is None or capture_started or png is not None:
                            raise ValueError('Unexpected QQ capture start')
                        capture_started = True
                        continue
                    if daily and event.get("type") == "log":
                        self._accept_log(record, event)
                    elif daily and event.get('type') == 'module':
                        name, state, serial = event.get('name'), event.get('state'), event.get('serial')
                        if (record.authorized_at is None and record.daily_started_at is None
                                or not isinstance(name, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,79}', name) is None
                                or state not in ('started', 'finished')
                                or not isinstance(serial, str) or serial == 'unknown'
                                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', serial) is None
                                or module_serial is not None and serial != module_serial
                                or self.config.ldplayer_console_path is None):
                            raise ValueError('Invalid module event')
                        if state == 'started':
                            if len(module_stack) >= 32:
                                raise ValueError('Too many nested modules')
                            if module_monitor is None:
                                from .module_timeout import ModuleTimeoutMonitor
                                module_monitor = ModuleTimeoutMonitor(
                                    record.id, self.config.ldplayer_console_path.with_name('adb.exe'), self.storage,
                                )
                                module_serial = serial
                            module_stack.append(name)
                            module_monitor.begin(name, serial)
                        else:
                            if not module_stack or module_stack[-1] != name:
                                raise ValueError('Unexpected module completion')
                            module_stack.pop()
                            module_monitor.end()
                    elif event.get("type") == "stage" and event.get("status") in (
                        "starting_emulator", "opening_game", "preparing_qr",
                    ):
                        if daily and png is not None:
                            raise ValueError("Unexpected login stage after QR publication")
                        if record.expires_at_ms <= time.time() * 1000:
                            self._expire(record)
                        if record.status != "expired" and not self._stopping:
                            await self._set_status(record, event["status"])
                    elif daily and event.get('type') == 'refreshing':
                        request_id = event.get('requestId')
                        if (not record.qr_published or record.scan_captured_at is not None
                                or record.authorized_at is not None or record.daily_started_at is not None or refresh_id is not None
                                or not isinstance(request_id, str) or not UUID_PATTERN.fullmatch(request_id)
                                or self._redis_queue is None
                                or not await self._redis_queue.pending_refresh(record, request_id)):
                            raise ValueError('Unexpected QR refresh command')
                        refresh_id = request_id
                        await self._set_status(record, 'preparing_qr')
                    elif event.get("type") == "qr" and isinstance(event.get("png"), str):
                        if daily and png is not None and (refresh_id is None or event.get('requestId') != refresh_id):
                            raise ValueError("Duplicate QR output")
                        if (record.scan_captured_at is not None or record.authorized_at is not None
                                or record.daily_started_at is not None):
                            raise ValueError('QR output after authorization')
                        candidate = base64.b64decode(event["png"], validate=True)
                        if candidate.startswith(b"\x89PNG\r\n\x1a\n") and len(candidate) <= 700_000:
                            png = candidate
                            if daily:
                                await self._publish_qr(record, png, refresh_request_id=refresh_id)
                                refresh_id = None
                                self._set_daily_status(record, "waiting_scan")
                        elif daily:
                            raise ValueError("Invalid QR image")
                    elif daily and event == {'type': 'scan_captured'}:
                        if (capture_owner is None or not record.qr_published
                                or record.scan_captured_at is not None or record.authorized_at is not None
                                or record.daily_started_at is not None or record.expires_at_ms <= time.time() * 1000):
                            raise ValueError('Unexpected scan completion')
                        record.scan_captured_at = time.time()
                        refresh_id = None
                        # Publish the handoff immediately; game authorization and
                        # its execution budget still require the later event.
                        self._set_daily_status(record, 'opening_game')
                        try:
                            if not await self._redis_queue.mark_scan_captured(record):
                                raise _TaskExpired
                            record.status = 'qr_ready'
                        except RedisQueueError:
                            # The run reporter retries scanCapturedAt separately.
                            # A transient Redis failure must not cancel the login.
                            logger.warning('Task %s: scan completion session update unavailable', record.id)
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
                        if record.login_timeout_seconds and self._redis_queue is not None:
                            # Success wins a race with a refresh click. The uploader
                            # owns command deletion, also when no new QR is needed.
                            try:
                                await self._redis_queue.mark_authorized(record)
                            except RedisQueueError:
                                logger.warning('Task %s: authorization command cleanup unavailable', record.id)
                        refresh_id = None
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
                    elif daily and event.get('type') == 'arena_started':
                        if (record.user_options.get('mainTask') != 'arena_weekly'
                                or record.daily_started_at is None or arena_started):
                            raise ValueError('Unexpected arena battle start')
                        deadline = event.get('executionDeadline')
                        now = time.time()
                        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                                or not now < deadline <= now + ARENA_WEEKLY_SECONDS):
                            raise ValueError('Invalid arena battle deadline')
                        arena_started = True
                        record.execution_deadline = float(deadline)
                        self._set_daily_status(record, 'running_daily')
                        timeout.reschedule(asyncio.get_running_loop().time()
                                           + max(0, deadline - time.time()))
                        if self._run_reporter is not None:
                            self._run_reporter.emit(record, message='已开始决斗场周胜战斗，执行 40 分钟后自动结束')
                    elif daily and event.get("type") == "daily_completed":
                        count = event.get("actionCount")
                        if (record.daily_started_at is None or action_count is not None or module_stack
                                or record.user_options.get('mainTask') == 'arena_weekly' and not arena_started
                                or type(count) is not int or count < 0):
                            raise ValueError("Unexpected daily completion")
                        action_count = count
                    elif event.get("type") == "error" and event.get("code") in SAFE_DRIVER_ERRORS:
                        raise _WorkerError(SAFE_DRIVER_ERRORS[event["code"]], event["code"])
                    elif daily:
                        raise ValueError("Invalid daily worker event")
                exit_code = await asyncio.to_thread(child.wait)
                self._check_interrupted(record)
                if self._stopping:
                    raise _WorkerStopping
                if exit_code != 0 or png is None or daily and action_count is None:
                    raise RuntimeError("Emulator process exited before a QR image was produced")
                if daily:
                    self._set_daily_status(record, "completed", action_count=action_count)
                return png
        except TimeoutError:
            self._check_interrupted(record)
            if not arena_started or not timeout.expired() or self._stopping:
                raise
            # 定时周胜按正常完成结束；finally 立即杀掉执行子进程，不走失败保留现场。
            logger.info('Task %s: 决斗场周胜战斗时限已到，结束执行子进程', record.id)
            self._set_daily_status(record, 'completed')
            assert png is not None
            return png
        finally:
            self._kill(child)
            try:
                if module_monitor is not None:
                    try:
                        await module_monitor.close()
                    except Exception:
                        logger.warning('Task %s: 模块异常截图收尾失败', record.id,
                                       extra={'backend_only': True})
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
        from .log_events import FILE_ONLY_PREFIX, model_log_secrets, sanitize_log_message

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
                file_only = message.startswith(FILE_ONLY_PREFIX)
                if file_only:
                    message = message[len(FILE_ONLY_PREFIX):]
                level = logging.INFO
                for prefix, candidate in (("INFO ", logging.INFO), ("WARNING ", logging.WARNING), ("ERROR ", logging.ERROR)):
                    if message.startswith(prefix):
                        level, message = candidate, message[len(prefix):]
                        break
                indentation = message[:len(message) - len(message.lstrip(" \t"))].replace("\t", "    ")
                message = sanitize_log_message(message, secrets=known_secrets, max_length=None)
                if message:
                    logger.log(level, "Task %s | %s%s", record.id, indentation, message,
                               extra={'file_only': file_only})
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
        # Older children may lack source routing. Block capture diagnostics
        # before any public milestone, Redis refresh, or SLS publication.
        if event.get('backend_only') is True or is_private_capture_message(message):
            return
        milestone = event.get('secretRealmCompletedAt')
        if 'secretRealmCompletedAt' in event:
            if (not isinstance(milestone, str) or milestone != timestamp
                    or record.daily_started_at is None or record.daily_status != 'running_daily'
                    or level != 'INFO'):
                raise ValueError('Invalid secret realm completion event')
            if record.secret_realm_completed_at is not None:
                return
            record.secret_realm_completed_at = milestone
        # Public logs describe stages and action boundaries. Repeated screenshot,
        # similarity, polling and ADB coordinate INFO messages remain internal.
        if level == "INFO" and index is None and milestone is None:
            if event.get("executionCheckpoint") is True and isinstance(self._run_reporter, RedisRunReporter):
                self._run_reporter.refresh(record)
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
            "loginType": record.login_type,
            **({'refreshQueueKey': self.config.redis_queue_key} if record.login_timeout_seconds else {}),
        }
        if record.login_type in ('android_qq', 'ios_qq') and self._redis_queue is not None:
            try:
                user_id = await self._redis_queue.notification_user_id(record)
            except RedisQueueError:
                # Only the child can determine whether both apps are installed.
                # Missing identity fails capture configuration there; legacy QQ
                # remains usable when its optional identity lookup is unavailable.
                user_id = ''
                logger.warning('Task %s: QQ capture identity temporarily unavailable', record.id,
                               extra={'backend_only': True})
            if user_id:
                message['captureUserId'] = user_id
                message['captureAttemptId'] = record.attempt_id
        return await self._run_child(
            record, [sys.executable, "-u", "-m", "naruto_daily.login_service.ldplayer_worker"], message,
            daily=True,
        )

    def _notify_qr_ready(self, record: TaskRecord, refresh_request_id: str | None = None) -> None:
        if self._qq_notifier is None:
            return
        revision = refresh_request_id or 'initial'
        if revision in record._qr_notifications:
            return
        record._qr_notifications.add(revision)
        task = asyncio.create_task(self._send_qr_notification(record, refreshed=refresh_request_id is not None))
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

    async def _notification_username(self, record: TaskRecord) -> str:
        # Share one lookup between QR refreshes and the final notification.
        if record._username_lookup_task is None:
            record._username_lookup_task = asyncio.create_task(self._lookup_notification_username(record))
            # Cancelling a caller must neither repeat the lookup nor leave a
            # late database failure attached to an unobserved background task.
            record._username_lookup_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None,
            )
        return await asyncio.shield(record._username_lookup_task)

    async def _lookup_notification_username(self, record: TaskRecord) -> str:
        if self._redis_queue is None or self._user_name_resolver is None:
            raise ValueError('Username lookup is unavailable')
        user_id = await self._redis_queue.notification_user_id(record)
        if not user_id:
            raise ValueError('Task identity is unavailable')
        user_name = await self._user_name_resolver.resolve(user_id)
        if not user_name:
            raise ValueError('Username is unavailable')
        record.username = user_name
        record._notification_user_id = user_id
        return user_name

    async def _notification_member(self, record: TaskRecord) -> str:
        if self._qq_binding_resolver is None or not record._notification_user_id:
            return ''
        try:
            # Query again for each message so unbinding takes effect even during a run.
            return await self._qq_binding_resolver.resolve(record._notification_user_id)
        except Exception as error:
            logger.warning('QQ binding lookup failed for task %s (%s)', record.id, type(error).__name__,
                           extra={'backend_only': True})
            return ''

    async def _prepare_notification_username(self, record: TaskRecord) -> None:
        # Resolve before the login session expires. The same name is passed
        # through run snapshots and reused when daily execution ends.
        if self._qq_notifier is None:
            return
        try:
            await self._notification_username(record)
        except Exception as error:
            logger.warning('QQ task username lookup failed for task %s (%s)', record.id, type(error).__name__,
                           extra={'backend_only': True})

    def _notify_task_finished(self, record: TaskRecord, status: str) -> None:
        if (self._qq_notifier is None or self._stopping
                or status not in {'completed', 'failed', 'expired'}
                or record.daily_error_code == 'worker_stopped'
                or record._terminal_notification_scheduled
                or status == 'completed' and record.daily_started_at is None):
            return
        record._terminal_notification_scheduled = True
        task = asyncio.create_task(self._send_task_notification(record, status, record.daily_error_code))
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

    async def _send_task_notification(self, record: TaskRecord, status: str, error_code: str | None) -> None:
        try:
            if not record.username:
                raise ValueError('Username is unavailable')
            member = await self._notification_member(record)
            await self._qq_notifier.notify_task_finished(
                record.id, record.username, status=status, error_code=error_code,
                secret_realm_completed=record.secret_realm_completed_at is not None,
                **({'member_openid': member} if member else {}),
                main_task=record.user_options.get('mainTask', 'daily'),
            )
            logger.info('QQ task notification sent for task %s (%s)', record.id, status,
                        extra={'backend_only': True})
        except Exception as error:
            logger.warning('QQ task notification failed for task %s (%s)', record.id, type(error).__name__,
                           extra={'backend_only': True})

    async def _send_qr_notification(self, record: TaskRecord, *, refreshed: bool) -> None:
        try:
            if self._redis_queue is None or self._user_name_resolver is None:
                raise ValueError('Username lookup is unavailable')
            user_id = await self._redis_queue.notification_user_id(record)
            if not user_id:
                raise ValueError('Task identity is unavailable')
            user_name = await self._notification_username(record)
            member = await self._notification_member(record)
            if record.status != 'qr_ready' or record.expires_at_ms <= time.time() * 1000:
                return
            if await self._redis_queue.notification_user_id(record) != user_id:
                return
            await self._qq_notifier.notify_qr_ready(record.id, user_name, refreshed=refreshed,
                                                   expires_at_ms=record.expires_at_ms,
                                                   **({'member_openid': member} if member else {}))
            logger.info('QQ QR notification sent for task %s', record.id, extra={'backend_only': True})
        except Exception as error:
            # Third-party exception messages can contain credentials or database URLs.
            logger.warning('QQ QR notification failed for task %s (%s)', record.id, type(error).__name__,
                           extra={'backend_only': True})

    async def _publish_qr(self, record: TaskRecord, png: bytes, *, refresh_request_id: str | None = None) -> None:
        if record.expires_at_ms <= time.time() * 1000:
            raise _TaskExpired
        try:
            await self.storage.put(record.id, png, record.expires_at)
            if refresh_request_id is None:
                await self._set_status(record, "qr_ready")
            else:
                assert self._redis_queue is not None
                while True:
                    if record.expires_at_ms <= time.time() * 1000:
                        raise _TaskExpired
                    try:
                        if not await self._redis_queue.complete_refresh(record, refresh_request_id):
                            raise _TaskExpired
                        break
                    except RedisQueueError:
                        if self._stopping:
                            raise _WorkerStopping from None
                        await self._retry_pause()
                record.status = 'qr_ready'
        except BaseException:
            self._pending_deletes.add(record.id)
            raise
        record.qr_published = True
        self._notify_qr_ready(record, refresh_request_id)
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
        if record.scan_captured_at is not None:
            result['scanCapturedAt'] = record.scan_captured_at
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
        hold_seconds = FAILED_INSTANCE_HOLD_SECONDS
        if record.authorized_at is None and record.daily_started_at is None:
            hold_seconds = min(hold_seconds, max(0, record.expires_at_ms / 1000 - time.time()))
        if hold_seconds <= 0:
            return
        logger.warning(
            "Task %s: 执行失败，保留模拟器 %g 秒供排查，期间实例保持占用；到时自动关闭",
            record.id, hold_seconds,
        )
        if record._interrupt_event is None:
            record._interrupt_event = asyncio.Event()
        waiters = [asyncio.create_task(self._stop_event.wait()),
                   asyncio.create_task(record._interrupt_event.wait())]
        try:
            done, _ = await asyncio.wait(waiters, timeout=hold_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                logger.info("Task %s: 失败现场保留时间已到，开始关闭模拟器", record.id)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _release_and_requeue(self, record: TaskRecord) -> bool:
        if (record.qr_published or record.authorized_at is not None
                or record.daily_started_at is not None):
            return False
        self._set_daily_status(record, "interrupted", error_code="worker_stopped")
        if self.config.emulator_driver == "ldplayer":
            if not await self._cleanup_ldplayer(record.id):
                self._set_daily_status(record, "failed", error_code="instance_cleanup_failed")
                return False
        if self._run_reporter is not None:
            self._run_reporter.emit(
                record, status="interrupted", error_code="worker_stopped", level="WARNING",
            )
            # Finish this attempt while its session still authorizes the worker.
            # After requeue, another consumer (even with the same owner) may
            # immediately take over; the old finally must not emit or finish again.
            await self._run_reporter.flush(record.id)
            self._run_reporter.finish(record.id)
        await self._requeue(record)
        return self._run_reporter is not None

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
                reported = await self._release_and_requeue(record)
                if self._run_reporter is not None and not reported:
                    self._run_reporter.emit(
                        record, status=record.daily_status or "interrupted",
                        error_code=record.daily_error_code or "worker_stopped", level="WARNING",
                    )
                    self._run_reporter.finish(record.id)
                return
            self._tasks[record.id] = record
            await self._process_record(record)

    async def _process_record(self, record: TaskRecord) -> None:
        task_id = record.id
        record._processing = True
        record._interrupt_event = asyncio.Event()
        uploaded = False
        keep_ldplayer = False
        requeue_on_exit = False
        cancelled = False
        if self._run_reporter is not None:
            self._run_reporter.emit(record, status="starting_emulator")
        try:
            self._check_interrupted(record)
            await self._prepare_notification_username(record)
            self._check_interrupted(record)
            await self._set_status(record, "starting_emulator")
            if self._stopping:
                raise _WorkerStopping
            png = (
                await self._run_mock(record)
                if self.config.emulator_driver == "mock"
                else await self._run_ldplayer(record)
            )
            self._check_interrupted(record)
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
            record.qr_published = True
            self._notify_qr_ready(record)
            keep_ldplayer = self.config.emulator_driver == "ldplayer"
            if self.config.ldplayer_pool_indices:
                try:
                    await asyncio.to_thread(self._mark_ldplayer_ready, task_id)
                except Exception:
                    logger.warning("Could not mark the LDPlayer pool slot ready for task %s", task_id)
        except _TaskInterrupted:
            self._set_daily_status(record, "interrupted", error_code="admin_interrupted")
            self._pending_deletes.add(task_id)
        except asyncio.CancelledError:
            cancelled = True
            self._set_daily_status(record, "interrupted", error_code="worker_stopped")
            requeue_on_exit = not record.qr_published and self._redis_queue is not None
            raise
        except Exception as error:
            # Failures before the child starts have no daily status yet, but
            # still need a safe public reason in the final execution log.
            record.daily_error_code = (
                error.code if isinstance(error, _WorkerError) else
                "worker_stopped" if self._stopping else
                "command_timeout" if isinstance(error, (TimeoutError, _TaskExpired)) else
                "unexpected_error"
            )
            if not record.qr_published and (self._stopping or record.daily_status is not None):
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
                # Keep the account occupied until the failed instance is closed.
                # Finally will publish failure so an explicit new request can run.
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
                    self._set_daily_status(record, "interrupted", error_code="worker_stopped")
                    requeue_on_exit = True
                self._pending_deletes.add(task_id)
        finally:
            child = self._children.pop(task_id, None)
            if child is not None:
                self._kill(child)
            interruption_reported = False
            requeue_on_exit = requeue_on_exit and not (
                record.qr_published or record.authorized_at is not None
                or record.daily_started_at is not None or record._admin_interrupted
            )
            keep_ldplayer = keep_ldplayer and not record._admin_interrupted
            if requeue_on_exit:
                interruption_reported = await self._release_and_requeue(record)
            elif self.config.emulator_driver == "ldplayer" and not keep_ldplayer:
                try:
                    if (not self._stopping and not record._admin_interrupted
                            and (record.daily_status == "failed" or record.status == "failed")):
                        await self._hold_failed_ldplayer(record)
                finally:
                    # The pool binding stays reserved throughout the hold. Also
                    # release it if the consumer is cancelled during inspection.
                    released = await self._cleanup_ldplayer(task_id)
                    record._instance_released = released
                    if not released and (record.qr_published or record.daily_status == "interrupted"):
                        self._set_daily_status(record, "interrupted" if record._admin_interrupted else "failed",
                                               error_code="instance_cleanup_failed")
                    elif (record.qr_published or record._admin_interrupted) and record.daily_status in {'failed', 'interrupted'}:
                        await self._finish_failed_session(record)
            final_status = record.daily_status
            if final_status not in RUN_TERMINAL_STATUSES:
                final_status = ("interrupted" if self._stopping else record.status
                                if record.status in {"failed", "expired"} else
                                "completed" if self.config.emulator_driver == "mock" else "failed")
            if self._run_reporter is not None and not interruption_reported:
                record.daily_status = final_status
                self._run_reporter.emit(
                    record, status=final_status,
                    error_code=record.daily_error_code,
                    level="ERROR" if final_status == "failed" else "WARNING" if final_status in {"interrupted", "expired"} else "INFO",
                    message="模拟二维码已生成" if self.config.emulator_driver == "mock" and final_status == "completed" else None,
                )
                delivered = True
                if isinstance(self._run_reporter, RedisRunReporter):
                    delivered = await self._run_reporter.flush(task_id)
                if record._admin_interrupted:
                    final_status = record.daily_status
                    if record._instance_released and record.status != "failed":
                        await self._finish_failed_session(record)
                self._run_reporter.finish(task_id)
            else:
                delivered = True
            record._processing = False
            if delivered and not cancelled and not requeue_on_exit and not keep_ldplayer:
                self._notify_task_finished(record, final_status)

    async def _sweep_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._stop_event.wait(), self.config.sweep_interval_ms / 1000)
                return
            except TimeoutError:
                await self._sweep()

    async def _sweep(self) -> None:
        now = time.time() * 1000
        for record in list(self._pending_session_failures.values()):
            await self._finish_failed_session(record)
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
        """Stop producers and drain SLS before honoring caller cancellation."""
        if self._shutdown_task is None:
            self._stopping = True
            self._stop_event.set()
            self._queue.close()
            self._shutdown_task = asyncio.create_task(self._finish_stop(), name="login-worker-shutdown")
        cancelled = False
        while not self._shutdown_task.done():
            try:
                await asyncio.shield(self._shutdown_task)
            except asyncio.CancelledError:
                cancelled = True
        self._shutdown_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _finish_stop(self) -> None:
        errors: list[BaseException] = []

        async def cleanup(operation):
            try:
                await operation
            except BaseException as error:
                if not errors:
                    errors.append(error)

        for child in list(self._children.values()):
            try:
                self._kill(child)
            except OSError as error:
                if not errors:
                    errors.append(error)
        await asyncio.gather(*self._workers, return_exceptions=True)
        # An in-progress sweep can still expire a task and emit a final event.
        if self._sweep_task is not None:
            await cleanup(self._sweep_task)
        try:
            if self._notification_tasks:
                # Notifications remain bounded; SLS uploads must fully drain.
                pending = list(self._notification_tasks)
                await asyncio.wait(pending, timeout=25)
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if self._qq_notifier is not None:
                await cleanup(self._qq_notifier.close())
        finally:
            try:
                if self._run_reporter is not None:
                    await cleanup(self._run_reporter.close())
                # Draining the reporter can discover the final interruption.
                # Finish its newly produced cleanup before closing Redis.
                if self._interruption_tasks:
                    await cleanup(asyncio.gather(*list(self._interruption_tasks), return_exceptions=True))
                for record in list(self._pending_session_failures.values()):
                    await cleanup(self._finish_failed_session(record))
            finally:
                if self._log_publisher is not None:
                    await cleanup(self._log_publisher.close())
        # Ready OSS images remain available to the client until their own expiry.
        to_delete = self._pending_deletes if self._redis_queue is not None else set(self._tasks) | self._pending_deletes
        await asyncio.gather(
            *(self.storage.delete(task_id) for task_id in to_delete),
            return_exceptions=True,
        )
        if self._redis_queue is not None:
            await cleanup(self._redis_queue.close())
        self._tasks.clear()
        self._pending_deletes.clear()
        self._pending_session_failures.clear()
        if errors:
            raise errors[0]
