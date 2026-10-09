"""Run the real Redis worker and mock child against an isolated fake server."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from uuid import uuid4

import pytest

from oss_fakes import OSS_ENV

from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service.queue import RedisTaskQueue, SAFE_TASK_ERROR
from naruto_daily.login_service.runtime import LoginRuntime


class MemoryStorage:
    def __init__(self, *, fail_put=False):
        self.objects = {}
        self.put_ids = []
        self.fail_put = fail_put

    async def initialize(self):
        pass

    async def put(self, task_id, png, expires_at):
        if self.fail_put:
            raise RuntimeError("OSS secret=private-storage-password redis://private-host")
        self.objects[task_id] = (png, expires_at)
        self.put_ids.append(task_id)

    async def delete(self, task_id):
        self.objects.pop(task_id, None)

    async def sweep(self):
        pass


def task_body(ttl=60):
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    return {
        "version": 1, "id": task_id,
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "objectKey": f"login/{task_id}/qr.png",
    }


def fake_server():
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("lupa")
    pytest.importorskip("qrcode")
    return fakeredis.FakeServer()


def fake_client(server):
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    return fakeredis.FakeRedis(server=server, decode_responses=True)


def worker(server, storage, **overrides):
    config = replace(ServiceConfig(
        redis_url="redis://unused.invalid/0", redis_queue_key="test:login:worker",
        concurrency=1, step_delay_ms=0, sweep_interval_ms=10,
    ), **overrides)
    queue = RedisTaskQueue(config, client=fake_client(server))
    return LoginRuntime(config, storage, redis_queue=queue)


async def enqueue(client, runtime, body, *, ttl=30_000):
    # Match the public TypeScript envelope/session contract; no bearer token is queued.
    session = {
        "id": body["id"], "status": "queued", "createdAt": body["createdAt"],
        "expiresAt": body["expiresAt"], "simulation": True,
        "tokenHash": sha256(b"test-only-browser-session-token").hexdigest(),
    }
    key = f"{runtime.config.redis_queue_key}:sessions:{body['id']}"
    payload = json.dumps(body)
    await client.set(key, json.dumps(session), px=ttl)
    await client.lpush(runtime.config.redis_queue_key, payload)
    return key, payload, session


async def wait_for_status(client, key, status, *, timeout=8):
    async with asyncio.timeout(timeout):
        while True:
            raw = await client.get(key)
            session = json.loads(raw) if raw else None
            if session is not None and session["status"] == status:
                return session
            if session is not None and session["status"] in {"failed", "expired"}:
                pytest.fail(f"Task reached {session['status']} while waiting for {status}")
            await asyncio.sleep(0.01)


async def eventually(predicate, *, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_redis_worker_publishes_png_and_stages_without_changing_auth_or_expiry():
    Image = pytest.importorskip("PIL.Image")

    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        runtime = worker(server, storage, step_delay_ms=80)
        body = task_body()
        # Deliberately shorter than expiresAt: a refreshed expiry is observable.
        key, _, original = await enqueue(producer, runtime, body, ttl=10_000)
        before_ttl = await producer.pttl(key)
        observed = {"queued"}
        pids = set()
        await runtime.start()
        try:
            async with asyncio.timeout(8):
                while True:
                    session = json.loads(await producer.get(key))
                    observed.add(session["status"])
                    assert session["tokenHash"] == original["tokenHash"]
                    assert session["expiresAt"] == original["expiresAt"]
                    assert 0 < await producer.pttl(key) <= before_ttl
                    pids.update(child.pid for child in runtime._children.values())
                    if session["status"] in {"qr_ready", "failed"}:
                        break
                    await asyncio.sleep(0.005)
            assert observed == {"queued", "starting_emulator", "opening_game", "preparing_qr", "qr_ready"}
            assert len(pids) == 1 and os.getpid() not in pids
            png, expires_at = storage.objects[body["id"]]
            with Image.open(BytesIO(png)) as image:
                assert image.format == "PNG" and image.size == (360, 360)
            assert expires_at == body["expiresAt"]
            assert storage.put_ids == [body["id"]]
        finally:
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_duplicate_redis_deliveries_produce_one_image():
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        runtime = worker(server, storage, concurrency=2, step_delay_ms=50)
        body = task_body()
        key, payload, _ = await enqueue(producer, runtime, body)
        await producer.lpush(runtime.config.redis_queue_key, *([payload] * 4))
        await runtime.start()
        try:
            await wait_for_status(producer, key, "qr_ready")
            # A completed task redelivered later must remain terminal, too.
            await producer.lpush(runtime.config.redis_queue_key, payload)
            next_body = task_body()
            next_key, _, _ = await enqueue(producer, runtime, next_body)
            await wait_for_status(producer, next_key, "qr_ready")
            assert storage.put_ids.count(body["id"]) == 1
            assert set(storage.put_ids) == {body["id"], next_body["id"]}
            assert await producer.llen(runtime.config.redis_queue_key) == 0
            assert json.loads(await producer.get(key))["status"] == "qr_ready"
        finally:
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_malformed_and_expired_messages_do_not_block_the_next_task():
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        runtime = worker(server, storage)
        expired = task_body(ttl=-1)
        expired["createdAt"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(
            timespec="milliseconds",
        ).replace("+00:00", "Z")
        expired_key, _, _ = await enqueue(producer, runtime, expired)
        expired_session = await producer.get(expired_key)
        missing = task_body()
        missing_key, _, _ = await enqueue(producer, runtime, missing)
        await producer.delete(missing_key)
        for malformed in ["{bad JSON", "[]", "null", "x" * 8193]:
            await producer.lpush(runtime.config.redis_queue_key, malformed)
        valid = task_body()
        valid_key, _, _ = await enqueue(producer, runtime, valid)
        await runtime.start()
        try:
            await wait_for_status(producer, valid_key, "qr_ready")
            assert storage.put_ids == [valid["id"]]
            assert await producer.get(expired_key) == expired_session
            assert await producer.exists(missing_key) == 0
            assert await producer.llen(runtime.config.redis_queue_key) == 0
        finally:
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_storage_failure_publishes_safe_error_and_worker_continues(caplog):
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage(fail_put=True)
        runtime = worker(server, storage)
        body = task_body()
        key, _, original = await enqueue(producer, runtime, body)
        await runtime.start()
        try:
            failed = await wait_for_status(producer, key, "failed")
            assert failed["error"] == SAFE_TASK_ERROR
            assert failed["tokenHash"] == original["tokenHash"]
            assert body["id"] not in storage.objects
            storage.fail_put = False
            next_body = task_body()
            next_key, _, _ = await enqueue(producer, runtime, next_body)
            await wait_for_status(producer, next_key, "qr_ready")
            assert storage.put_ids == [next_body["id"]]
            public_output = json.dumps(failed) + caplog.text
            assert "private-storage-password" not in public_output
            assert "private-host" not in public_output
        finally:
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_shutdown_requeues_active_task_and_next_worker_preserves_ready_image():
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        first = worker(server, storage, step_delay_ms=5000)
        second = worker(server, storage)
        body = task_body()
        key, payload, original = await enqueue(producer, first, body)
        before_ttl = await producer.pttl(key)
        await first.start()
        try:
            await eventually(lambda: body["id"] in first._children)
            child = first._children[body["id"]]
            await asyncio.wait_for(first.stop(), 3)
            assert child.poll() is not None
            assert storage.put_ids == []
            assert json.loads(await producer.get(key)) == original
            assert await producer.lrange(first.config.redis_queue_key, 0, -1) == [payload]
            assert 0 < await producer.pttl(key) <= before_ttl
            await second.start()
            ready = await wait_for_status(producer, key, "qr_ready")
            assert ready["tokenHash"] == original["tokenHash"]
            png = storage.objects[body["id"]]
            await asyncio.wait_for(second.stop(), 3)
            assert storage.objects[body["id"]] == png
            assert storage.put_ids == [body["id"]]
            assert json.loads(await producer.get(key))["status"] == "qr_ready"
            assert await producer.llen(first.config.redis_queue_key) == 0
            assert 0 < await producer.pttl(key) <= before_ttl
        finally:
            await first.stop()
            await second.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_shutdown_during_initial_status_write_does_not_spawn_and_requeues(monkeypatch):
    import naruto_daily.login_service.runtime as runtime_module

    spawned = []
    real_popen = runtime_module.subprocess.Popen

    def record_spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(runtime_module.subprocess, "Popen", record_spawn)

    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        worker_client = fake_client(server)
        status_started = asyncio.Event()
        finish_status = asyncio.Event()

        class PausedStatusClient:
            def __getattr__(self, name):
                return getattr(worker_client, name)

            async def eval(self, *args):
                if args[-2] == "starting_emulator" and not status_started.is_set():
                    status_started.set()
                    await finish_status.wait()
                return await worker_client.eval(*args)

        config = ServiceConfig(redis_url="redis://unused.invalid/0", concurrency=1, step_delay_ms=0)
        storage = MemoryStorage()
        runtime = LoginRuntime(
            config, storage, redis_queue=RedisTaskQueue(config, client=PausedStatusClient()),
        )
        key, payload, original = await enqueue(producer, runtime, task_body())
        before_ttl = await producer.pttl(key)
        stopping = None
        await runtime.start()
        try:
            await asyncio.wait_for(status_started.wait(), 3)
            stopping = asyncio.create_task(runtime.stop())
            await eventually(lambda: runtime._stopping)
            assert not stopping.done()
            finish_status.set()
            await asyncio.wait_for(stopping, 3)
            assert spawned == []
            assert storage.put_ids == []
            assert json.loads(await producer.get(key)) == original
            assert await producer.lrange(config.redis_queue_key, 0, -1) == [payload]
            assert 0 < await producer.pttl(key) <= before_ttl
        finally:
            finish_status.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, 3)
            else:
                await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("url", [
    "redis://test-user:private-password@localhost", "redis://localhost/",
    "redis://localhost:6379/0", "rediss://localhost/0",
    "redis://test-user:private-password@localhost/1", "redis://localhost/15",
])
def test_config_accepts_default_or_explicit_redis_database_and_redacts_url(url):
    config = load_config({**OSS_ENV, "REDIS_URL": url})
    assert config.redis_url == url
    assert url not in repr(config)
    assert "private-password" not in repr(config)


@pytest.mark.parametrize("url", [
    "redis://test-user:private-password@private-host/-1",
    "redis://test-user:private-password@private-host:bad/0",
    "redis://test-user:private-password@private-host/0?db=1",
    "https://test-user:private-password@private-host/0",
])
def test_config_rejects_unsafe_redis_urls_without_exposing_credentials(url):
    with pytest.raises(ValueError) as error:
        load_config({**OSS_ENV, "REDIS_URL": url})
    assert "REDIS_URL" in str(error.value)
    assert "private-password" not in str(error.value)
    assert "private-host" not in str(error.value)
    assert url not in str(error.value)
