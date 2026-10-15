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
        "objectKey": f"login-test-jev/{task_id}/qr.png",
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


async def enqueue(client, runtime, body, *, ttl=30_000, session_fields=None):
    # Match the public TypeScript envelope/session contract; no bearer token is queued.
    session = {
        "id": body["id"], "status": "queued", "createdAt": body["createdAt"],
        "expiresAt": body["expiresAt"], "simulation": True,
        "tokenHash": sha256(b"test-only-browser-session-token").hexdigest(),
    }
    session.update(session_fields or {})
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


async def wait_for_run_status(client, runtime, task_id, status, *, timeout=8):
    key = f"{runtime.config.redis_queue_key}:runs:{task_id}"
    async with asyncio.timeout(timeout):
        while True:
            raw = await client.get(key)
            run = json.loads(raw) if raw else None
            if run is not None and run["status"] == status:
                return run
            await asyncio.sleep(0.01)


async def assert_interrupted_run(client, runtime, task_id):
    prefix = runtime.config.redis_queue_key
    run = json.loads(await client.get(f"{prefix}:runs:{task_id}"))
    assert run["status"] == "interrupted"
    assert run["errorCode"] == "worker_stopped"
    assert run["workerId"] == runtime._redis_queue.owner
    assert run["finishedAt"] is not None
    assert datetime.fromisoformat(run["finishedAt"].replace("Z", "+00:00"))
    logs = [json.loads(fields["json"]) for _, fields in await client.xrange(f"{prefix}:logs:{task_id}")]
    assert logs[-1]["level"] == "WARNING"
    assert "停止" in logs[-1]["message"] and "中断" in logs[-1]["message"]
    journal_path = runtime.config.ldplayer_state_dir / "runs" / f"{task_id}.json"
    assert journal_path.is_file(), "Stopping a consumed task must persist its interrupted execution journal"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "interrupted"
    assert journal["errorCode"] == "worker_stopped"


async def assert_requeued_session(client, runtime, key, payload, original, before_ttl):
    assert json.loads(await client.get(key)) == original
    assert 0 < await client.pttl(key) <= before_ttl
    assert await client.lrange(runtime.config.redis_queue_key, 0, -1) == [payload]


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


def test_shutdown_records_interrupted_run_and_next_worker_preserves_ready_image(tmp_path):
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        first = worker(server, storage, step_delay_ms=5000, ldplayer_state_dir=tmp_path / "first")
        second = worker(server, storage, ldplayer_state_dir=tmp_path / "second")
        body = task_body()
        user_id = str(uuid4())
        key, payload, original = await enqueue(producer, first, body, session_fields={
            "userId": user_id, "userOptions": {"equipmentStamina": False},
        })
        active_key = f"{first.config.redis_queue_key}:users:{user_id}:active"
        await producer.set(active_key, body["id"], px=30_000)
        before_ttl = await producer.pttl(key)
        await first.start()
        try:
            await eventually(lambda: body["id"] in first._children)
            child = first._children[body["id"]]
            await wait_for_run_status(producer, first, body["id"], "starting_emulator")
            log_key = f"{first.config.redis_queue_key}:logs:{body['id']}"
            original_logs = await producer.xrange(log_key)
            await asyncio.wait_for(first.stop(), 3)
            assert child.poll() is not None
            assert storage.put_ids == []
            await assert_requeued_session(producer, first, key, payload, original, before_ttl)
            assert await producer.get(active_key) == body["id"]
            await assert_interrupted_run(producer, first, body["id"])
            assert (await producer.xrange(log_key))[:len(original_logs)] == original_logs
            await second.start()
            ready = await wait_for_status(producer, key, "qr_ready")
            assert ready["tokenHash"] == original["tokenHash"]
            completed = await wait_for_run_status(producer, second, body["id"], "completed")
            assert completed["workerId"] == second._redis_queue.owner
            assert completed["workerId"] != first._redis_queue.owner
            assert completed["finishedAt"] is not None
            assert "errorCode" not in completed
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


def test_cancelling_entrypoint_worker_records_interrupted_run_and_requeues(monkeypatch, tmp_path):
    from naruto_daily.login_service import __main__ as entrypoint
    from naruto_daily.login_service import runtime as runtime_module

    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        runtime = worker(server, storage, step_delay_ms=5000, ldplayer_state_dir=tmp_path)
        key, payload, original = await enqueue(producer, runtime, task_body(), ttl=10_000)
        before_ttl = await producer.pttl(key)
        task_id = original["id"]
        # Keep run_worker and the runtime lifecycle real; isolate only Redis and OSS.
        monkeypatch.setattr(runtime_module, "LoginRuntime", lambda config: runtime)
        running = asyncio.create_task(entrypoint.run_worker(runtime.config))
        try:
            await eventually(lambda: task_id in runtime._children)
            child = runtime._children[task_id]
            await wait_for_run_status(producer, runtime, task_id, "starting_emulator")
            # asyncio.Runner handles the first Ctrl+C by cancelling its main task.
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 3)
            assert child.poll() is not None
            assert runtime._children == {}
            assert all(consumer.done() for consumer in runtime._workers)
            assert storage.put_ids == []
            await assert_requeued_session(producer, runtime, key, payload, original, before_ttl)
            await assert_interrupted_run(producer, runtime, task_id)
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_cancelling_consumer_terminates_child_and_records_interrupted_run(tmp_path):
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        runtime = worker(server, storage, step_delay_ms=5000, ldplayer_state_dir=tmp_path)
        body = task_body()
        key, payload, original = await enqueue(producer, runtime, body, ttl=10_000)
        before_ttl = await producer.pttl(key)
        await runtime.start()
        try:
            await eventually(lambda: body["id"] in runtime._children)
            child = runtime._children[body["id"]]
            await wait_for_run_status(producer, runtime, body["id"], "starting_emulator")
            consumer = runtime._workers[0]
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(consumer, 3)
            await runtime.stop()
            assert child.poll() is not None
            assert runtime._children == {}
            assert storage.put_ids == []
            await assert_requeued_session(producer, runtime, key, payload, original, before_ttl)
            await assert_interrupted_run(producer, runtime, body["id"])
        finally:
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_old_consumer_cleanup_does_not_interrupt_new_attempt_with_same_owner(monkeypatch, tmp_path):
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        runtime = worker(server, MemoryStorage(), step_delay_ms=5000, ldplayer_state_dir=tmp_path)
        body = task_body()
        key, _, _ = await enqueue(producer, runtime, body)
        run_key = f"{runtime.config.redis_queue_key}:runs:{body['id']}"
        handoff_runs = []
        real_requeue = runtime._requeue

        async def requeue_and_claim_next_attempt(record):
            await real_requeue(record)
            # Another consumer on this runtime shares its queue owner. Claim
            # immediately so the old attempt's finally runs after the handoff.
            next_record = await runtime._redis_queue.pop()
            assert next_record is not None and next_record.id == body["id"]
            await runtime._set_status(next_record, "starting_emulator")
            await runtime._run_reporter.flush(next_record.id)
            handoff_runs.append(json.loads(await producer.get(run_key)))

        monkeypatch.setattr(runtime, "_requeue", requeue_and_claim_next_attempt)
        await runtime.start()
        try:
            await eventually(lambda: body["id"] in runtime._children)
            child = runtime._children[body["id"]]
            await wait_for_run_status(producer, runtime, body["id"], "starting_emulator")
            consumer = runtime._workers[0]
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(consumer, 3)
            await runtime._run_reporter.flush(body["id"])
            assert child.poll() is not None
            run = json.loads(await producer.get(run_key))
            assert run["status"] == "starting_emulator"
            assert run["workerId"] == runtime._redis_queue.owner
            assert run["finishedAt"] is None and "errorCode" not in run
            assert run == handoff_runs[0]
            session = json.loads(await producer.get(key))
            assert session["status"] == "starting_emulator"
            assert session["workerId"] == runtime._redis_queue.owner
            assert await producer.lrange(runtime.config.redis_queue_key, 0, -1) == []
        finally:
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_shutdown_before_initial_run_write_still_records_interrupted_run(tmp_path):
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        runtime = worker(server, storage, step_delay_ms=5000, ldplayer_state_dir=tmp_path)
        body = task_body()
        key, payload, original = await enqueue(producer, runtime, body, ttl=10_000)
        before_ttl = await producer.pttl(key)
        run_key = f"{runtime.config.redis_queue_key}:runs:{body['id']}"
        client = runtime._redis_queue._client
        publish_started, allow_publish = asyncio.Event(), asyncio.Event()

        class PausedPublishClient:
            def __getattr__(self, name):
                return getattr(client, name)

            async def eval(self, *args):
                if args[1] == 3 and args[2] == run_key and not publish_started.is_set():
                    publish_started.set()
                    await allow_publish.wait()
                return await client.eval(*args)

        runtime._redis_queue._client = PausedPublishClient()
        stopping = None
        await runtime.start()
        try:
            await asyncio.wait_for(publish_started.wait(), 3)
            await eventually(lambda: body["id"] in runtime._children)
            child = runtime._children[body["id"]]
            assert await producer.get(run_key) is None
            stopping = asyncio.create_task(runtime.stop())
            # Hold the initial write until the real runtime has queued its stop
            # outcome, then let the real publisher drain both updates to Redis.
            await eventually(lambda: any(
                update.snapshot["status"] == "interrupted"
                for update in runtime._run_reporter._pending.get(body["id"], ())
            ))
            assert await producer.get(run_key) is None
            allow_publish.set()
            await asyncio.wait_for(stopping, 3)
            assert child.poll() is not None
            assert storage.put_ids == []
            await assert_requeued_session(producer, runtime, key, payload, original, before_ttl)
            await assert_interrupted_run(producer, runtime, body["id"])
        finally:
            allow_publish.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, 3)
            else:
                await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_shutdown_keeps_interrupted_history_without_recreating_deleted_session(tmp_path):
    async def scenario():
        server = fake_server()
        producer = fake_client(server)
        storage = MemoryStorage()
        runtime = worker(server, storage, step_delay_ms=5000, ldplayer_state_dir=tmp_path)
        body = task_body()
        key, _, _ = await enqueue(producer, runtime, body)
        await runtime.start()
        try:
            await eventually(lambda: body["id"] in runtime._children)
            child = runtime._children[body["id"]]
            await wait_for_run_status(producer, runtime, body["id"], "starting_emulator")
            assert await producer.delete(key) == 1
            await asyncio.wait_for(runtime.stop(), 3)
            assert child.poll() is not None
            assert storage.put_ids == []
            assert await producer.get(key) is None
            assert await producer.lrange(runtime.config.redis_queue_key, 0, -1) == []
            await assert_interrupted_run(producer, runtime, body["id"])
        finally:
            await runtime.stop()
            await producer.aclose()

    asyncio.run(scenario())


def test_shutdown_during_initial_status_write_does_not_spawn_and_requeues(monkeypatch, tmp_path):
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

        config = ServiceConfig(
            redis_url="redis://unused.invalid/0", concurrency=1, step_delay_ms=0,
            ldplayer_state_dir=tmp_path,
        )
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
            await assert_requeued_session(producer, runtime, key, payload, original, before_ttl)
            await assert_interrupted_run(producer, runtime, original["id"])
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
