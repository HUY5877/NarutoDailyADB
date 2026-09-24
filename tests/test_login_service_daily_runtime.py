"""Exercise the QR-to-daily lifecycle without a real Redis server or emulator."""

import asyncio
import base64
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import sys
import threading
import time
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import PROJECT_ROOT, ServiceConfig, load_config
from naruto_daily.login_service.ldplayer import LDPlayerPoolFull
from naruto_daily.login_service.models import parse_submission
from naruto_daily.login_service.runtime import LoginRuntime


PNG = b"\x89PNG\r\n\x1a\nfixture"
QR = {"type": "qr", "png": base64.b64encode(PNG).decode("ascii")}
AUTHORIZED = {"type": "authorized"}
LOGGED_IN = {"type": "logged_in"}
COMPLETED = {"type": "daily_completed", "actionCount": 3}

# These children exchange the actual JSON-line protocol over OS pipes. File gates
# let each test inspect the live child before allowing the next lifecycle event.
CHILD_PROGRAM = """
import json, pathlib, sys, time
message = json.loads(sys.stdin.readline())
for step in message['plan']:
    if 'exit' in step:
        sys.exit(step['exit'])
    elif 'wait' in step:
        deadline = time.monotonic() + 15
        while not pathlib.Path(step['wait']).exists():
            if time.monotonic() > deadline:
                sys.exit(9)
            time.sleep(0.01)
    else:
        print(json.dumps(step['emit']), flush=True)
"""


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.put_ids = []
        self.deleted = []

    async def initialize(self):
        pass

    async def put(self, task_id, png, expires_at):
        self.objects[task_id] = (png, expires_at)
        self.put_ids.append(task_id)

    async def delete(self, task_id):
        self.objects.pop(task_id, None)
        self.deleted.append(task_id)

    async def sweep(self):
        pass


class RecordingRedis:
    def __init__(self):
        self.statuses = []
        self.requeued = []

    async def set_status(self, record, status, error=None):
        self.statuses.append((record.id, status, error))
        return True

    async def requeue(self, record):
        self.requeued.append(record.id)
        return True

    async def close(self):
        pass


class RecordingPool:
    def __init__(self, task_id):
        self.task_id = task_id
        self.state = "reserved"
        self.closed = []
        self.ready = []
        self.retries = []

    def has_capacity(self):
        return self.state in {"released", "retryable"}

    def mark_ready(self, task_id):
        assert task_id == self.task_id
        self.ready.append(task_id)
        self.state = "qr_ready"

    def close(self, task_id):
        assert task_id == self.task_id
        self.closed.append(task_id)
        self.state = "released"

    def prepare_retry(self, task_id):
        assert self.state == "released"
        self.retries.append(task_id)
        self.state = "retryable"


class ConsumerRedis(RecordingRedis):
    def __init__(self, records):
        super().__init__()
        self.records = deque(records)
        self.popped = []

    async def initialize(self):
        pass

    async def pop(self):
        if self.records:
            record = self.records.popleft()
            self.popped.append(record.id)
            return record
        # Like a bounded Redis BRPOP, an empty queue yields and periodically
        # lets the consumer observe its shutdown flag.
        await asyncio.sleep(0.01)
        return None


class SlotPool:
    def __init__(self, size):
        self.size = size
        self.active = {}
        self.allocated = {}
        self.ready = []
        self.closed = []
        self._lock = threading.Lock()

    def has_capacity(self):
        with self._lock:
            return len(self.active) < self.size

    def allocate(self, task_id, _deadline):
        with self._lock:
            occupied = set(self.active.values())
            index = next((index for index in range(self.size) if index not in occupied), None)
            if index is None:
                raise LDPlayerPoolFull("All test slots are occupied")
            self.active[task_id] = self.allocated[task_id] = index

    def mark_ready(self, task_id):
        with self._lock:
            assert task_id in self.active
            self.ready.append(task_id)

    def close(self, task_id):
        with self._lock:
            self.active.pop(task_id)
            self.closed.append(task_id)


def consuming_runtime(tmp_path, monkeypatch, records, plans, *, concurrency):
    console = tmp_path / "ldconsole.exe"
    console.write_bytes(b"")
    config = ServiceConfig(
        redis_url="redis://unused.invalid/0", emulator_driver="ldplayer",
        concurrency=concurrency, ldplayer_console_path=console,
        ldplayer_pool_indices=tuple(range(concurrency)),
        ldplayer_state_dir=tmp_path / "state", daily_timeout_seconds=10,
    )
    storage, queue, pool = MemoryStorage(), ConsumerRedis(records), SlotPool(concurrency)
    runtime = LoginRuntime(config, storage, redis_queue=queue)
    runtime._manager = pool

    async def run_child(current):
        return await runtime._run_child(
            current, [sys.executable, "-u", "-c", CHILD_PROGRAM],
            {"plan": plans[current.id]}, daily=True,
        )

    monkeypatch.setattr(runtime, "_run_ldplayer", run_child)
    return runtime, storage, queue, pool


def record_with_ttl(ttl=5):
    now = datetime.now(timezone.utc)
    return parse_submission({
        "id": str(uuid4()), "accessToken": "a" * 64,
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    })


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def emit(event):
    return {"emit": event}


def gate(path):
    return {"wait": str(path)}


def make_runtime(tmp_path, monkeypatch, record, plan, *, daily_timeout=5):
    config = ServiceConfig(
        redis_url="redis://unused.invalid/0", emulator_driver="ldplayer",
        ldplayer_console_path=tmp_path / "ldconsole.exe", ldplayer_pool_indices=(0,),
        ldplayer_state_dir=tmp_path / "state", daily_config_path=tmp_path / "daily.json",
        daily_timeout_seconds=daily_timeout,
    )
    storage, queue, pool = MemoryStorage(), RecordingRedis(), RecordingPool(record.id)
    runtime = LoginRuntime(config, storage, redis_queue=queue)
    runtime._manager = pool
    runtime._tasks[record.id] = record

    async def run_child(current):
        return await runtime._run_child(
            current, [sys.executable, "-u", "-c", CHILD_PROGRAM], {"plan": plan}, daily=True,
        )

    monkeypatch.setattr(runtime, "_run_ldplayer", run_child)
    return runtime, storage, queue, pool


def run_report(runtime, record):
    path = runtime.config.ldplayer_state_dir / "runs" / f"{record.id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def start_processing(runtime, record):
    work = asyncio.create_task(runtime._process_record(record))
    # stop() must await the same consumer that owns this child.
    runtime._workers.append(work)
    return work


def test_daily_qr_is_published_while_child_waits_and_slot_remains_reserved(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        login_gate, finish_gate = tmp_path / "login", tmp_path / "finish"
        plan = [emit(QR), gate(login_gate), emit(LOGGED_IN), gate(finish_gate), emit(COMPLETED)]
        runtime, storage, queue, pool = make_runtime(tmp_path, monkeypatch, record, plan)
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.daily_status == "waiting_scan" and bool(pool.ready))
            child = runtime._children[record.id]
            assert child.poll() is None
            assert not work.done()
            assert record.qr_published is True
            assert record.daily_started_at is None
            assert storage.put_ids == [record.id]
            assert storage.objects[record.id][0] == PNG
            assert pool.ready == [record.id]
            assert not pool.has_capacity()
            assert run_report(runtime, record)["status"] == "waiting_scan"

            login_gate.touch()
            await eventually(lambda: record.daily_status == "running_daily")
            assert record.daily_started_at is not None
            assert child.poll() is None
            assert not pool.has_capacity()
            assert all(status != "running_daily" for _, status, _ in queue.statuses)

            finish_gate.touch()
            await asyncio.wait_for(work, 5)
            assert record.daily_status == "completed"
            assert run_report(runtime, record)["actionCount"] == 3
            assert storage.put_ids == [record.id]
            assert pool.closed == [record.id]
            assert pool.has_capacity()
            assert queue.requeued == []
            assert child.poll() == 0
            assert record.id not in runtime._children
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_daily_continues_past_original_qr_deadline_and_expired_image_is_deleted(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl(1.2)
        finish_gate = tmp_path / "finish"
        runtime, storage, queue, pool = make_runtime(
            tmp_path, monkeypatch, record,
            [emit(QR), emit(LOGGED_IN), gate(finish_gate), emit(COMPLETED)],
        )
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.daily_status == "running_daily")
            child = runtime._children[record.id]
            await eventually(lambda: time.time() * 1000 > record.expires_at_ms + 100)
            await runtime._sweep()
            assert record.status == "expired"
            assert record.id not in storage.objects
            assert child.poll() is None
            assert record.daily_status == "running_daily"
            assert not work.done()
            assert not pool.has_capacity()
            finish_gate.touch()
            await asyncio.wait_for(work, 5)
            assert record.daily_status == "completed"
            assert run_report(runtime, record)["status"] == "completed"
            assert pool.closed == [record.id]
            assert queue.requeued == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_daily_timeout_has_its_own_budget_and_releases_instance(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl(5)
        runtime, storage, queue, pool = make_runtime(
            tmp_path, monkeypatch, record,
            [emit(QR), emit(LOGGED_IN), gate(tmp_path / "never")], daily_timeout=0.2,
        )
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(work, 3)
            assert record.expires_at_ms > time.time() * 1000
            assert record.daily_status == "failed"
            assert run_report(runtime, record)["errorCode"] == "daily_timeout"
            assert pool.closed == [record.id]
            assert queue.requeued == []
            assert record.id not in runtime._children
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_unscanned_qr_expires_and_releases_instance_without_requeue(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl(0.8)
        runtime, storage, queue, pool = make_runtime(
            tmp_path, monkeypatch, record, [emit(QR), gate(tmp_path / "never")],
        )
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.qr_published)
            child = runtime._children[record.id]
            await asyncio.wait_for(work, 3)
            assert record.daily_status == "failed"
            assert run_report(runtime, record)["errorCode"] == "login_timeout"
            assert pool.closed == [record.id]
            assert queue.requeued == []
            assert child.poll() is not None
            await runtime._sweep()
            assert record.id not in storage.objects
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_authorized_login_cleanup_can_cross_qr_expiry_without_killing_the_child(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl(1.0)
        login_gate, finish_gate = tmp_path / "enter-game", tmp_path / "finish-daily"
        runtime, storage, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(AUTHORIZED), gate(login_gate), emit(LOGGED_IN),
            gate(finish_gate), emit(COMPLETED),
        ], daily_timeout=5)
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.daily_status == "finishing_login")
            assert record.authorized_at is not None and record.daily_started_at is None
            child = runtime._children[record.id]
            original_deadline = run_report(runtime, record)["deadline"]
            assert original_deadline == record.authorized_at + 5
            assert run_report(runtime, record)["authorizedAt"] == record.authorized_at
            await eventually(lambda: time.time() * 1000 > record.expires_at_ms + 50)
            assert runtime._lookup(record.id) is record  # Lookup expiry must also preserve the authorized child.
            await runtime._sweep()
            assert record.status == "expired" and record.id not in storage.objects
            assert child.poll() is None and not work.done()
            assert record.daily_status == "finishing_login"
            login_gate.touch()
            await eventually(lambda: record.daily_status == "running_daily")
            assert record.daily_started_at * 1000 > record.expires_at_ms
            assert run_report(runtime, record)["deadline"] == original_deadline
            finish_gate.touch()
            await asyncio.wait_for(work, 3)
            assert record.daily_status == "completed"
            assert run_report(runtime, record)["authorizedAt"] == record.authorized_at
            assert pool.closed == [record.id] and not queue.requeued
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_authorized_login_finishing_has_its_own_timeout_reason(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl(5)
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(AUTHORIZED), gate(tmp_path / "never-enter-game"),
        ], daily_timeout=0.2)
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(work, 3)
            assert record.authorized_at is not None and record.daily_started_at is None
            assert record.daily_error_code == "login_finish_timeout"
            assert run_report(runtime, record)["errorCode"] == "login_finish_timeout"
            assert record.expires_at_ms > time.time() * 1000
            assert pool.closed == [record.id] and not queue.requeued
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_logged_in_after_authorization_does_not_extend_the_execution_budget(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl(5)
        login_gate = tmp_path / "login-gate"
        runtime, _, _, _ = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(AUTHORIZED), gate(login_gate), emit(LOGGED_IN), gate(tmp_path / "never-finish"),
        ], daily_timeout=0.8)
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.authorized_at is not None)
            await eventually(lambda: time.time() >= record.authorized_at + 0.45)
            login_gate.touch()
            await eventually(lambda: record.daily_started_at is not None)
            # A second budget at logged_in would leave ~0.8s; only ~0.35s should remain.
            await asyncio.wait_for(asyncio.shield(work), 0.6)
            assert record.daily_error_code == "daily_timeout"
            assert run_report(runtime, record)["deadline"] == record.authorized_at + 0.8
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_authorized_login_failure_keeps_safe_child_reason(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, _, _, _ = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(AUTHORIZED), emit({"type": "error", "code": "login_finish_failed",
                                            "message": "private child detail"}),
        ])
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(work, 3)
            assert record.daily_error_code == "login_finish_failed"
            assert run_report(runtime, record)["errorCode"] == "login_finish_failed"
            assert "private" not in json.dumps(run_report(runtime, record))
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("logged_in", [False, True])
def test_shutdown_after_qr_cleans_up_without_replaying_a_scanned_account(monkeypatch, tmp_path, logged_in):
    async def scenario():
        record = record_with_ttl()
        plan = [emit(QR)] + ([emit(LOGGED_IN)] if logged_in else []) + [gate(tmp_path / "never")]
        runtime, storage, queue, pool = make_runtime(tmp_path, monkeypatch, record, plan)
        work = start_processing(runtime, record)
        try:
            expected = "running_daily" if logged_in else "waiting_scan"
            await eventually(lambda: record.daily_status == expected)
            child = runtime._children[record.id]
            await asyncio.wait_for(runtime.stop(), 3)
            assert work.done()
            assert child.poll() is not None
            assert record.daily_status == "interrupted"
            assert run_report(runtime, record)["status"] == "interrupted"
            assert pool.closed == [record.id]
            assert pool.retries == []
            assert queue.requeued == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_shutdown_before_qr_preserves_existing_retry_behavior(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, storage, queue, pool = make_runtime(tmp_path, monkeypatch, record, [gate(tmp_path / "never")])
        start_processing(runtime, record)
        try:
            await eventually(lambda: record.id in runtime._children)
            await asyncio.wait_for(runtime.stop(), 3)
            assert not record.qr_published
            assert pool.closed == [record.id]
            assert pool.retries == [record.id]
            assert queue.requeued == [record.id]
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("events", [
    [QR],
    [LOGGED_IN, QR, COMPLETED],
    [QR, COMPLETED],
    [QR, QR, LOGGED_IN, COMPLETED],
    [QR, LOGGED_IN, LOGGED_IN, COMPLETED],
    [QR, LOGGED_IN, COMPLETED, COMPLETED],
    [QR, LOGGED_IN, {"type": "daily_completed", "actionCount": -1}],
    [QR, LOGGED_IN, {"type": "daily_completed", "actionCount": True}],
    [AUTHORIZED, QR, LOGGED_IN, COMPLETED],
    [QR, AUTHORIZED, AUTHORIZED, LOGGED_IN, COMPLETED],
    [QR, LOGGED_IN, AUTHORIZED, COMPLETED],
    [QR, AUTHORIZED, COMPLETED],
], ids=["qr-only", "login-before-qr", "done-before-login", "duplicate-qr", "duplicate-login", "duplicate-done", "negative-count", "boolean-count",
        "authorized-before-qr", "duplicate-authorized", "authorized-after-logged-in", "done-before-login-finishes"])
def test_incomplete_or_out_of_order_daily_protocol_cannot_succeed(monkeypatch, tmp_path, events):
    async def scenario():
        record = record_with_ttl()
        runtime, storage, queue, pool = make_runtime(tmp_path, monkeypatch, record, [emit(event) for event in events])
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(work, 3)
            assert record.daily_status == "failed"
            report = run_report(runtime, record)
            assert report["status"] == "failed"
            assert report["errorCode"] in {"protocol_error", "daily_failed", "unexpected_error"}
            assert pool.closed == [record.id]
            assert queue.requeued == []
            assert len(storage.put_ids) <= 1
            assert record.id not in runtime._children
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_daily_failure_report_does_not_leak_child_exception_details(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, storage, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit({
                "type": "error", "code": "unexpected_error",
                "message": "redis://private-password@internal-host/0",
            }),
        ])
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(work, 3)
            assert record.daily_status == "failed"
            report = run_report(runtime, record)
            assert report["errorCode"]
            assert "private-password" not in json.dumps(report)
            assert "internal-host" not in json.dumps(report)
            assert pool.closed == [record.id]
            assert queue.requeued == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_completion_event_does_not_hide_a_nonzero_child_exit(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, storage, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit(COMPLETED), {"exit": 7},
        ])
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(work, 3)
            assert record.daily_status == "failed"
            assert run_report(runtime, record)["status"] == "failed"
            assert pool.closed == [record.id]
            assert queue.requeued == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_ldplayer_child_receives_daily_configuration_and_uses_daily_protocol(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        config = ServiceConfig(
            emulator_driver="ldplayer", ldplayer_console_path=tmp_path / "ldconsole.exe",
            daily_config_path=tmp_path / "daily.json", daily_timeout_seconds=123,
        )
        runtime = LoginRuntime(config, MemoryStorage())
        captured = {}

        async def capture(current, command, message, *, daily=False):
            captured.update(current=current, command=command, message=message, daily=daily)
            return PNG

        monkeypatch.setattr(runtime, "_run_child", capture)
        await runtime._run_ldplayer(record)
        assert captured["daily"] is True
        assert captured["current"] is record
        assert captured["message"]["dailyConfigPath"] == str(config.daily_config_path)
        assert captured["message"]["dailyTimeoutSeconds"] == 123
    asyncio.run(scenario())


def test_daily_configuration_defaults_and_relative_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = load_config({})
    assert config.daily_config_path == PROJECT_ROOT / "config.example.json"
    assert config.daily_timeout_seconds == 7200
    configured = load_config({"DAILY_CONFIG_PATH": "config.example.json", "DAILY_TIMEOUT_SECONDS": "300"})
    assert configured.daily_config_path == PROJECT_ROOT / "config.example.json"
    assert configured.daily_timeout_seconds == 300


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "never"])
def test_daily_configuration_rejects_invalid_timeout(value):
    with pytest.raises(ValueError, match="DAILY_TIMEOUT_SECONDS"):
        load_config({"DAILY_TIMEOUT_SECONDS": value})


def test_unwritable_run_journal_does_not_lose_consumer_or_skip_instance_cleanup(monkeypatch, tmp_path):
    async def scenario():
        records = [record_with_ttl(10), record_with_ttl(10)]
        plans = {
            records[0].id: [emit(QR), emit(LOGGED_IN), emit({"type": "error", "code": "unexpected_error"})],
            records[1].id: [emit(QR), emit(LOGGED_IN), emit(COMPLETED)],
        }
        runtime, storage, queue, pool = consuming_runtime(
            tmp_path, monkeypatch, records, plans, concurrency=1,
        )
        runtime.config.ldplayer_state_dir.mkdir(parents=True)
        journal = runtime.config.ldplayer_state_dir / "runs"
        journal.write_text("A file cannot contain run reports", encoding="utf-8")
        await runtime.start()
        try:
            await eventually(lambda: len(pool.closed) == 2)
            assert records[0].daily_status == "failed"
            assert records[1].daily_status == "completed"
            assert queue.popped == [record.id for record in records]
            assert pool.closed == [record.id for record in records]
            assert pool.has_capacity()
            assert queue.requeued == []
            assert len(runtime._workers) == 1
            assert not runtime._workers[0].done()
            assert journal.is_file()
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_three_consumers_hold_slots_until_daily_completion_before_claiming_fourth(monkeypatch, tmp_path):
    async def scenario():
        records = [record_with_ttl(10) for _ in range(4)]
        login_gates = {record.id: tmp_path / f"login-{record.id}" for record in records}
        finish_gates = {record.id: tmp_path / f"finish-{record.id}" for record in records}
        plans = {
            record.id: [emit(QR), gate(login_gates[record.id]), emit(LOGGED_IN),
                        gate(finish_gates[record.id]), emit(COMPLETED)]
            for record in records
        }
        runtime, storage, queue, pool = consuming_runtime(
            tmp_path, monkeypatch, records, plans, concurrency=3,
        )
        await runtime.start()
        try:
            first_three = records[:3]
            await eventually(lambda: all(record.daily_status == "waiting_scan" for record in first_three))
            assert queue.popped == [record.id for record in first_three]
            assert list(queue.records) == [records[3]]
            assert len(runtime._children) == 3
            assert set(pool.active.values()) == {0, 1, 2}
            assert not pool.has_capacity()
            for record in first_three:
                login_gates[record.id].touch()
            await eventually(lambda: all(record.daily_status == "running_daily" for record in first_three))
            assert queue.popped == [record.id for record in first_three]
            assert pool.closed == []
            assert not pool.has_capacity()

            released_index = pool.allocated[records[0].id]
            finish_gates[records[0].id].touch()
            await eventually(lambda: records[3].daily_status == "waiting_scan")
            assert records[0].daily_status == "completed"
            assert pool.closed == [records[0].id]
            assert queue.popped == [record.id for record in records]
            assert pool.allocated[records[3].id] == released_index
            assert len(runtime._children) == 3
            assert all(record.daily_status == "running_daily" for record in records[1:3])

            login_gates[records[3].id].touch()
            for record in records[1:]:
                finish_gates[record.id].touch()
            await eventually(lambda: len(pool.closed) == 4)
            assert all(record.daily_status == "completed" for record in records)
            assert all(not consumer.done() for consumer in runtime._workers)
            assert pool.has_capacity()
            assert queue.requeued == []
            assert len(storage.put_ids) == 4
            assert not runtime._children
        finally:
            await runtime.stop()
    asyncio.run(scenario())
