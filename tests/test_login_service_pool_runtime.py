"""Pool admission behavior without Redis, OSS, or real LDPlayer processes."""

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import secrets
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import ServiceConfig
from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerPoolFull
from naruto_daily.login_service.models import parse_submission
from naruto_daily.login_service.runtime import LoginRuntime


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    async def initialize(self):
        pass

    async def put(self, task_id, png, expires_at):
        self.objects[task_id] = (png, expires_at)

    async def delete(self, task_id):
        self.objects.pop(task_id, None)

    async def sweep(self):
        pass


class FakeRedisQueue:
    def __init__(self, records=()):
        self.records = deque(records)
        self.pop_count = 0
        self.requeued = []

    async def initialize(self):
        pass

    async def notification_user_id(self, record):
        return ''

    async def pop(self):
        self.pop_count += 1
        # The real Redis queue yields while waiting for work, including when empty.
        await asyncio.sleep(0.01)
        return self.records.popleft() if self.records else None

    async def set_status(self, record, status, error=None):
        record.status = status
        record.error = error
        return True

    async def mark_authorized(self, record):
        return True

    async def set_execution_status(self, record, status):
        return True

    async def requeue(self, record):
        self.requeued.append(record.id)
        record.status = "queued"
        return True

    async def close(self):
        pass


def record(ttl=30):
    now = datetime.now(timezone.utc)
    body = {
        "id": str(uuid4()), "accessToken": secrets.token_hex(32),
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(
            timespec="milliseconds",
        ).replace("+00:00", "Z"),
    }
    result = parse_submission(body)
    result._queue_payload = "test-payload"
    return result


def config(tmp_path, *, concurrency=2):
    console = tmp_path / "dnconsole.exe"
    console.write_bytes(b"")
    return ServiceConfig(
        redis_url="redis://unused.invalid/0", concurrency=concurrency,
        emulator_driver="ldplayer", ldplayer_console_path=console,
        ldplayer_pool_indices=(0, 2), ldplayer_state_dir=tmp_path / "state",
    )


async def eventually(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_empty_queue_keeps_consuming_without_accessing_pool(monkeypatch, tmp_path):
    async def scenario():
        queue = FakeRedisQueue()
        runtime = LoginRuntime(config(tmp_path), MemoryStorage(), redis_queue=queue)

        def unavailable_pool():
            raise LDPlayerError("The binding file is locked by a device operation")

        monkeypatch.setattr(runtime, "_ldplayer_manager", unavailable_pool)
        await runtime.start()
        try:
            await eventually(lambda: queue.pop_count >= 3)
            assert all(not worker.done() for worker in runtime._workers)
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_two_consumers_reserve_last_slot_once_and_requeue_other_task(monkeypatch, tmp_path):
    class OneSlotPool:
        def __init__(self):
            self.allocated = []

        def allocate(self, task_id, _deadline):
            if self.allocated:
                raise LDPlayerPoolFull("The last slot is already reserved")
            self.allocated.append(task_id)

    async def scenario():
        records = [record(), record()]
        queue = FakeRedisQueue(records)
        runtime = LoginRuntime(config(tmp_path), MemoryStorage(), redis_queue=queue)
        pool = OneSlotPool()
        monkeypatch.setattr(runtime, "_ldplayer_manager", lambda: pool)
        processed = []

        async def hold_active(current):
            processed.append(current.id)
            await runtime._stop_event.wait()

        monkeypatch.setattr(runtime, "_process_record", hold_active)
        await runtime.start()
        try:
            await eventually(lambda: len(queue.requeued) == 1)
            assert pool.allocated == processed == [records[0].id]
            assert queue.requeued == [records[1].id]
            assert records[1].id not in runtime._tasks
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_cross_worker_pool_full_requeues_without_cleanup(monkeypatch, tmp_path):
    class RacingPool:
        def has_capacity(self):
            return True

        def allocate(self, _task_id, _deadline):
            raise LDPlayerPoolFull("occupied by another worker")

    async def scenario():
        task = record()
        queue = FakeRedisQueue([task])
        runtime = LoginRuntime(config(tmp_path, concurrency=1), MemoryStorage(), redis_queue=queue)
        monkeypatch.setattr(runtime, "_ldplayer_manager", lambda: RacingPool())
        processed = []
        cleaned = []
        monkeypatch.setattr(runtime, "_process_record", lambda item: processed.append(item.id))
        monkeypatch.setattr(runtime, "_cleanup_ldplayer", lambda task_id: cleaned.append(task_id))
        await runtime.start()
        try:
            await eventually(lambda: queue.requeued == [task.id])
            assert task.status == "queued"
            assert task.id not in runtime._tasks
            assert processed == []
            assert cleaned == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", ["full", "unavailable"])
def test_task_is_allocated_without_capacity_precheck(monkeypatch, tmp_path, capacity):
    class Pool:
        def __init__(self):
            self.checks = 0
            self.allocated = []

        def has_capacity(self):
            self.checks += 1
            if capacity == "unavailable":
                raise LDPlayerError("The binding file is locked by a device operation")
            return False

        def allocate(self, task_id, _deadline):
            self.allocated.append(task_id)

    async def scenario():
        task = record()
        queue = FakeRedisQueue([task])
        runtime = LoginRuntime(config(tmp_path, concurrency=1), MemoryStorage(), redis_queue=queue)
        pool = Pool()
        monkeypatch.setattr(runtime, "_ldplayer_manager", lambda: pool)

        async def finish(_record):
            # Hold the fake job until shutdown, just like a real in-flight child.
            await runtime._stop_event.wait()

        monkeypatch.setattr(runtime, "_process_record", finish)
        await runtime.start()
        try:
            await eventually(lambda: pool.allocated == [task.id])
            assert pool.checks == 0
            assert queue.pop_count == 1
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_ready_pool_task_is_marked_and_kept(monkeypatch, tmp_path):
    async def scenario():
        runtime = LoginRuntime(config(tmp_path), MemoryStorage(), redis_queue=FakeRedisQueue())
        task = record()
        marked = []
        closed = []
        monkeypatch.setattr(runtime, "_run_ldplayer", lambda _record: asyncio.sleep(
            0, result=b"\x89PNG\r\n\x1a\nfixture",
        ))
        monkeypatch.setattr(runtime, "_mark_ldplayer_ready", lambda task_id: marked.append(task_id))
        monkeypatch.setattr(runtime, "_close_ldplayer", lambda task_id: closed.append(task_id))
        await runtime._process_record(task)
        assert task.status == "qr_ready"
        assert marked == [task.id]
        assert closed == []
    asyncio.run(scenario())


def test_shutdown_releases_pool_before_requeueing_same_task(monkeypatch, tmp_path):
    async def scenario():
        runtime = LoginRuntime(config(tmp_path), MemoryStorage(), redis_queue=FakeRedisQueue())
        runtime._stopping = True
        task = record()
        events = []

        class Reporter:
            def emit(self, *_args, **_kwargs):
                pass

            async def flush(self, task_id):
                events.append(("flush", task_id))

            def finish(self, task_id):
                events.append(("finish", task_id))

        runtime._run_reporter = Reporter()

        async def cleanup(task_id):
            events.append(("close", task_id))
            return True

        async def requeue(item):
            events.append(("requeue", item.id))
            assert item.daily_status == "interrupted"
            assert item.daily_error_code == "worker_stopped"

        monkeypatch.setattr(runtime, "_cleanup_ldplayer", cleanup)
        monkeypatch.setattr(runtime, "_requeue", requeue)
        await runtime._process_record(task)
        assert events == [
            ("close", task.id), ("requeue", task.id), ("flush", task.id), ("finish", task.id),
        ]
    asyncio.run(scenario())


def test_shutdown_does_not_requeue_when_pool_release_fails(monkeypatch, tmp_path):
    async def scenario():
        runtime = LoginRuntime(config(tmp_path), MemoryStorage(), redis_queue=FakeRedisQueue())
        runtime._stopping = True
        task = record()
        requeued = []

        async def cleanup(_task_id):
            return False

        monkeypatch.setattr(runtime, "_cleanup_ldplayer", cleanup)
        monkeypatch.setattr(runtime, "_requeue", lambda item: requeued.append(item.id))
        await runtime._process_record(task)
        assert requeued == []
        assert task.daily_status == "failed"
        assert task.daily_error_code == "instance_cleanup_failed"
    asyncio.run(scenario())


@pytest.mark.parametrize("field, value", [
    ("qr_published", True),
    ("authorized_at", 1.0),
    ("daily_started_at", 1.0),
])
def test_release_and_requeue_rejects_published_or_started_task(monkeypatch, tmp_path, field, value):
    async def scenario():
        queue = FakeRedisQueue()
        runtime = LoginRuntime(config(tmp_path), MemoryStorage(), redis_queue=queue)
        task = record()
        task.status = "preparing_qr"
        setattr(task, field, value)
        closed = []

        async def cleanup(task_id):
            closed.append(task_id)
            return True

        monkeypatch.setattr(runtime, "_cleanup_ldplayer", cleanup)

        assert not await runtime._release_and_requeue(task)

        assert closed == []
        assert queue.requeued == []
        assert task.status == "preparing_qr"
        assert task.daily_status is None
        assert task._queue_payload == "test-payload"
    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["authorized_at", "daily_started_at"])
def test_shutdown_cleans_started_task_without_requeue_even_when_qr_flag_is_missing(monkeypatch, tmp_path, field):
    async def scenario():
        queue = FakeRedisQueue()
        runtime = LoginRuntime(config(tmp_path), MemoryStorage(), redis_queue=queue)
        runtime._stopping = True
        task = record()
        setattr(task, field, 1.0)
        closed = []

        async def cleanup(task_id):
            closed.append(task_id)
            return True

        monkeypatch.setattr(runtime, "_cleanup_ldplayer", cleanup)

        await runtime._process_record(task)

        assert closed == [task.id]
        assert queue.requeued == []
        assert task.daily_status == "interrupted"
    asyncio.run(scenario())
