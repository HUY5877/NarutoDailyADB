"""Queue consumers, task lifetime management, and isolated mock emulator jobs."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from .config import ServiceConfig
from .models import HttpError, TaskRecord, UUID_PATTERN, equal_secret, parse_submission
from .queue import MockRedisQueue, RedisQueueError, RedisTaskQueue

if TYPE_CHECKING:
    from .storage import ObjectStorage


logger = logging.getLogger(__name__)


class _TaskExpired(Exception):
    pass


class _WorkerStopping(Exception):
    pass


class LoginRuntime:
    def __init__(
        self, config: ServiceConfig, storage: ObjectStorage | None = None,
        *, redis_queue: RedisTaskQueue | None = None,
    ) -> None:
        if storage is None:
            from .storage import create_storage
            storage = create_storage(config)
        self.config = config
        self.storage = storage
        self._tasks: dict[str, TaskRecord] = {}
        self._queue: MockRedisQueue[str] = MockRedisQueue(config.max_queue_size)
        self._redis_queue = redis_queue or (RedisTaskQueue(config) if config.redis_url else None)
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._pending_deletes: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
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
        child = self._children.get(record.id)
        if child is not None:
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

    async def _run_mock(self, record: TaskRecord) -> bytes:
        # Popen plus threaded pipe reads works with both Windows event-loop policies.
        # Only the job id and a fresh nonce enter the child; service/OSS secrets do not.
        allowed_env = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL"}
        child_env = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
        child = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).with_name("mock_emulator.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self._children[record.id] = child
        assert child.stdin is not None and child.stdout is not None
        message = {
            "type": "start", "id": record.id, "nonce": secrets.token_hex(32),
            "stepDelayMs": self.config.step_delay_ms, "expiresAt": record.expires_at,
        }
        png: bytes | None = None
        try:
            child.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            child.stdin.close()
            async with asyncio.timeout(max(0.001, record.expires_at_ms / 1000 - time.time())):
                while True:
                    line = await asyncio.to_thread(child.stdout.readline, 1_000_001)
                    if not line:
                        break
                    if len(line) > 1_000_000:
                        raise ValueError("Emulator output exceeds limit")
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid emulator output")
                    if event.get("type") == "stage" and event.get("status") in (
                        "starting_emulator", "opening_game", "preparing_qr",
                    ):
                        if record.expires_at_ms <= time.time() * 1000:
                            self._expire(record)
                        if record.status != "expired" and not self._stopping:
                            await self._set_status(record, event["status"])
                    elif event.get("type") == "qr" and isinstance(event.get("png"), str):
                        candidate = base64.b64decode(event["png"], validate=True)
                        if candidate.startswith(b"\x89PNG\r\n\x1a\n"):
                            png = candidate
                exit_code = await asyncio.to_thread(child.wait)
                if exit_code != 0 or png is None:
                    raise RuntimeError("Emulator process exited before a QR image was produced")
                return png
        finally:
            self._kill(child)
            await asyncio.to_thread(child.wait)
            child.stdin.close()
            child.stdout.close()

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
                record = await self._redis_queue.pop()
                if disconnected:
                    logger.info("Redis connection restored")
                    disconnected = False
                if record is None:
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
                await self._requeue(record)
                return
            self._tasks[record.id] = record
            await self._process_record(record)

    async def _process_record(self, record: TaskRecord) -> None:
        task_id = record.id
        uploaded = False
        try:
            if self.config.emulator_driver != "mock":
                raise RuntimeError("LDPlayer adapter is not implemented")
            await self._set_status(record, "starting_emulator")
            if self._stopping:
                raise _WorkerStopping
            png = await self._run_mock(record)
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
        except Exception as error:
            if isinstance(error, _TaskExpired) or record.expires_at_ms <= time.time() * 1000:
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
                    await self._requeue(record)
                else:
                    self._expire(record)
            else:
                record.error = (
                    "LDPlayer adapter is not implemented. Set EMULATOR_DRIVER=mock."
                    if self.config.emulator_driver != "mock"
                    else "Could not prepare the simulated login QR code. Please create a new login request."
                )
                try:
                    await self._set_status(record, "failed")
                except _TaskExpired:
                    self._expire(record)
                except _WorkerStopping:
                    await self._requeue(record)
                self._pending_deletes.add(task_id)
        finally:
            child = self._children.pop(task_id, None)
            if child is not None:
                self._kill(child)

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
