"""Verify the Python queue/worker behavior without Redis, OSS, or an emulator."""

import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
import secrets
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import PROJECT_ROOT, ServiceConfig, load_config
from naruto_daily.login_service.models import HttpError, MAX_TTL_MS, equal_secret, parse_submission
from naruto_daily.login_service.queue import MockRedisQueue
from naruto_daily.login_service.runtime import LoginRuntime


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.put_ids = []
        self.sweep_count = 0
        self.delete_failures = 0
        self.fail_put = False

    async def initialize(self):
        pass

    async def put(self, task_id, png, expires_at):
        if self.fail_put:
            raise RuntimeError("Secret storage SDK detail must not escape")
        self.objects[task_id] = (png, expires_at)
        self.put_ids.append(task_id)

    async def delete(self, task_id):
        if self.delete_failures:
            self.delete_failures -= 1
            raise OSError("temporary failure")
        self.objects.pop(task_id, None)

    async def sweep(self):
        self.sweep_count += 1


def submission(ttl=5):
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid4()), "accessToken": secrets.token_hex(32),
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_config_defaults_and_project_relative_storage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = load_config({})
    assert config.port == 4320
    assert config.concurrency == 2
    assert config.max_queue_size == 100
    assert config.max_tasks == 500
    assert config.local_storage_dir == PROJECT_ROOT / ".data/login-service/objects"
    assert load_config({"LOCAL_STORAGE_DIR": "relative-objects"}).local_storage_dir == PROJECT_ROOT / "relative-objects"
    assert load_config({"LOCAL_STORAGE_DIR": str(tmp_path)}).local_storage_dir == tmp_path


@pytest.mark.parametrize("env", [
    {"HOST": "0.0.0.0"}, {"SERVICE_TOKEN": "short"}, {"PORT": "not-a-number"},
    {"PORT": "-1"}, {"WORKER_CONCURRENCY": "0"}, {"MAX_TASKS": "1.2"},
    {"STORAGE_DRIVER": "bad"}, {"EMULATOR_DRIVER": "bad"}, {"OSS_KEY_PREFIX": "../unsafe"},
    {"STORAGE_DRIVER": "oss"},
])
def test_config_rejects_invalid_values(env):
    with pytest.raises(ValueError):
        load_config(env)


def test_config_allows_explicit_oss_and_non_loopback_credentials():
    config = load_config({
        "HOST": "0.0.0.0", "SERVICE_TOKEN": "unique-token-for-test", "STORAGE_DRIVER": "oss",
        "OSS_REGION": "oss-cn-hangzhou", "OSS_BUCKET": "example", "OSS_ACCESS_KEY_ID": "test-id",
        "OSS_ACCESS_KEY_SECRET": "test-secret", "OSS_STS_TOKEN": "test-sts", "OSS_KEY_PREFIX": "/login/test/",
    })
    assert config.key_prefix == "login/test"
    assert config.oss_sts_token == "test-sts"


@pytest.mark.parametrize("changes", [
    {"id": "../path"}, {"accessToken": "short"}, {"createdAt": "bad"},
    {"createdAt": "2026-02-30T10:00:00Z"}, {"expiresAt": "2100-01-01T00:00:00Z"},
    {"expiresAt": "2000-01-01T00:00:00Z"}, {"expiresAt": "2100-01-01T00:00:00+00:00"},
])
def test_submission_rejects_malformed_or_expired_input(changes):
    with pytest.raises(HttpError) as exc:
        parse_submission(submission() | changes)
    assert exc.value.status == 400


def test_submission_ttl_and_secret_validation():
    with pytest.raises(HttpError):
        parse_submission(submission(MAX_TTL_MS / 1000 + 1))
    with pytest.raises(HttpError):
        parse_submission([])
    body = submission()
    body["id"] = body["id"].upper()
    record = parse_submission(body)
    assert record.id == body["id"].lower()
    assert equal_secret(body["accessToken"], record.access_token)
    assert not equal_secret(None, record.access_token)
    assert not equal_secret("\u4e2d\u6587", record.access_token)
    assert "accessToken" not in record.to_view()


def test_mock_redis_fifo_capacity_waiters_and_close():
    async def scenario():
        queue = MockRedisQueue(2)
        assert queue.lpush("first")
        assert queue.lpush("second")
        assert not queue.lpush("full")
        assert await queue.brpop() == "first"
        queue.remove(lambda item: item == "second")
        waiter = asyncio.create_task(queue.brpop())
        await asyncio.sleep(0)
        assert queue.lpush("direct")
        assert await waiter == "direct"
        cancelled = asyncio.create_task(queue.brpop())
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        assert queue.lpush("after-cancel")
        assert await queue.brpop() == "after-cancel"
        pending = asyncio.create_task(queue.brpop())
        await asyncio.sleep(0)
        queue.close()
        assert await pending is None
        assert await queue.brpop() is None
        assert not queue.lpush("closed")
    asyncio.run(scenario())


def test_runtime_rejects_unstarted_and_stopped_service():
    async def scenario():
        runtime = LoginRuntime(ServiceConfig(), MemoryStorage())
        with pytest.raises(HttpError) as exc:
            runtime.submit(submission())
        assert exc.value.status == 503
        await runtime.start()
        await runtime.start()
        await runtime.stop()
        await runtime.stop()
        with pytest.raises(HttpError) as exc:
            runtime.submit(submission())
        assert exc.value.status == 503
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            await runtime.start()
    asyncio.run(scenario())


def test_submission_is_idempotent_and_session_protected():
    async def scenario():
        runtime = LoginRuntime(ServiceConfig(), MemoryStorage())
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            assert runtime.submit(body) == task_id
            assert runtime.stats == {"queued": 1, "active": 0, "tasks": 1}
            with pytest.raises(HttpError) as exc:
                runtime.submit(body | {"accessToken": secrets.token_hex(32)})
            assert exc.value.status == 409
            view = runtime.get_task(task_id.upper(), body["accessToken"])
            assert view["status"] == "queued"
            assert view["simulation"] is True
            assert "accessToken" not in view
            assert runtime.get_task(task_id, None) is None
            assert runtime.get_task(task_id, secrets.token_hex(32)) is None
            assert runtime.get_task("../bad", body["accessToken"]) is None
            assert not runtime.object_available(task_id)
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("config", [ServiceConfig(max_queue_size=1), ServiceConfig(max_tasks=1)])
def test_runtime_enforces_queue_and_task_capacity(config):
    async def scenario():
        runtime = LoginRuntime(config, MemoryStorage())
        await runtime.start()
        try:
            runtime.submit(submission())
            with pytest.raises(HttpError) as exc:
                runtime.submit(submission())
            assert exc.value.status == 429
            assert runtime.stats["tasks"] == 1
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_child_process_generates_png_and_exposes_stages():
    pytest.importorskip("qrcode")
    from PIL import Image

    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=60), storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            observed = {"queued"}
            pids = set()
            async with asyncio.timeout(5):
                while True:
                    view = runtime.get_task(task_id, body["accessToken"])
                    observed.add(view["status"])
                    pids.update(child.pid for child in runtime._children.values())
                    if view["status"] in {"qr_ready", "failed"}:
                        break
                    await asyncio.sleep(0.005)
            assert observed == {"queued", "starting_emulator", "opening_game", "preparing_qr", "qr_ready"}
            assert len(pids) == 1 and os.getpid() not in pids
            png, expires_at = storage.objects[task_id]
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert Image.open(BytesIO(png)).size == (360, 360)
            assert expires_at == body["expiresAt"]
            assert runtime.object_available(task_id)
            assert runtime.stats["active"] == 0
        finally:
            await runtime.stop()
        assert storage.objects == {}
    asyncio.run(scenario())


def test_each_job_gets_an_isolated_child_and_concurrency_is_bounded():
    pytest.importorskip("qrcode")

    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(concurrency=2, step_delay_ms=100), storage)
        await runtime.start()
        try:
            task_ids = [runtime.submit(submission()) for _ in range(4)]
            pids = set()
            async with asyncio.timeout(5):
                while len(storage.put_ids) < 4:
                    assert runtime.stats["active"] <= 2
                    pids.update(child.pid for child in runtime._children.values())
                    await asyncio.sleep(0.005)
            assert len(pids) == 4
            assert set(storage.put_ids) == set(task_ids)
            assert len({png for png, _ in storage.objects.values()}) == 4
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_expiry_kills_running_child_and_removes_retained_task():
    pytest.importorskip("qrcode")

    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=1000, retention_ms=200, sweep_interval_ms=10), storage)
        await runtime.start()
        try:
            body = submission(ttl=0.3)
            task_id = runtime.submit(body)
            await eventually(lambda: task_id in runtime._children)
            child = runtime._children[task_id]
            await eventually(lambda: (runtime.get_task(task_id, body["accessToken"]) or {}).get("status") == "expired")
            assert not runtime.object_available(task_id)
            await eventually(lambda: child.poll() is not None)
            await eventually(lambda: runtime.stats["tasks"] == 0)
            assert runtime.get_task(task_id, body["accessToken"]) is None
            assert storage.objects == {}
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_expired_queued_job_never_launches_child():
    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(), storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            runtime._tasks[task_id].expires_at_ms = 0
            assert runtime.get_task(task_id, body["accessToken"])["status"] == "expired"
            await asyncio.sleep(0)
            assert runtime.stats["active"] == 0
            assert runtime.stats["queued"] == 0
            assert storage.put_ids == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_shutdown_terminates_and_reaps_child():
    pytest.importorskip("qrcode")

    async def scenario():
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=5000), MemoryStorage())
        await runtime.start()
        task_id = runtime.submit(submission())
        await eventually(lambda: task_id in runtime._children)
        child = runtime._children[task_id]
        await asyncio.wait_for(runtime.stop(), 2)
        assert child.poll() is not None
        assert runtime.stats == {"queued": 0, "active": 0, "tasks": 0}
    asyncio.run(scenario())


def test_unsupported_ldplayer_fails_explicitly():
    async def scenario():
        runtime = LoginRuntime(ServiceConfig(emulator_driver="ldplayer"), MemoryStorage())
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await eventually(lambda: runtime.get_task(task_id, body["accessToken"])["status"] == "failed")
            view = runtime.get_task(task_id, body["accessToken"])
            assert "LDPlayer adapter is not implemented" in view["error"]
            assert runtime.stats["active"] == 0
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_storage_failure_is_redacted_and_sweep_retries_cleanup():
    pytest.importorskip("qrcode")

    async def scenario():
        storage = MemoryStorage()
        storage.fail_put = True
        storage.delete_failures = 1
        config = ServiceConfig(step_delay_ms=0, sweep_interval_ms=10, storage_sweep_interval_ms=10)
        runtime = LoginRuntime(config, storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await eventually(lambda: runtime.get_task(task_id, body["accessToken"])["status"] == "failed")
            assert "Secret" not in runtime.get_task(task_id, body["accessToken"])["error"]
            await eventually(lambda: task_id not in runtime._pending_deletes)
            assert storage.delete_failures == 0
            assert storage.sweep_count > 0
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_expiry_during_upload_removes_late_object():
    pytest.importorskip("qrcode")

    async def scenario():
        upload_started = asyncio.Event()
        finish_upload = asyncio.Event()

        class DelayedStorage(MemoryStorage):
            async def put(self, task_id, png, expires_at):
                upload_started.set()
                await finish_upload.wait()
                await super().put(task_id, png, expires_at)

        storage = DelayedStorage()
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=0, sweep_interval_ms=10), storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await asyncio.wait_for(upload_started.wait(), 5)
            runtime._tasks[task_id].expires_at_ms = 0
            assert runtime.get_task(task_id, body["accessToken"])["status"] == "expired"
            await eventually(lambda: task_id not in runtime._pending_deletes)
            finish_upload.set()
            await eventually(lambda: runtime.stats["active"] == 0)
            assert not runtime.object_available(task_id)
            assert storage.objects == {}
        finally:
            finish_upload.set()
            await runtime.stop()
    asyncio.run(scenario())
