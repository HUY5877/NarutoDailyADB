"""Exercise the shared Redis protocol with an isolated Lua-capable fake server."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.login_service.models import HttpError, parse_queue_task
from naruto_daily.login_service.queue import (
    RedisQueueError, RedisTaskQueue, SAFE_DRIVER_ERRORS, SAFE_TASK_ERROR,
)


def task_body(ttl=60):
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    return {
        "version": 1,
        "id": task_id,
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "objectKey": f"login/{task_id}/qr.png",
    }


def session_body(body):
    return {
        "id": body["id"], "status": "queued", "createdAt": body["createdAt"],
        "expiresAt": body["expiresAt"], "simulation": True, "tokenHash": "ab" * 32,
    }


def config():
    return SimpleNamespace(redis_url="redis://unused.invalid", redis_queue_key="test:login:queue", key_prefix="login")


def fake_client():
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    pytest.importorskip("lupa")
    return fakeredis.FakeRedis(decode_responses=True)


async def enqueue(client, body, *, session=None, ttl=60_000):
    key = f"{config().redis_queue_key}:sessions:{body['id']}"
    if session is None:
        session = session_body(body)
    await client.set(key, json.dumps(session), px=ttl)
    payload = json.dumps(body, indent=1)
    await client.lpush(config().redis_queue_key, payload)
    return key, payload


def test_queue_schema_is_tokenless_and_path_is_exact():
    body = task_body()
    record = parse_queue_task(body, "login")
    assert record.access_token == ""
    assert record.id == body["id"]
    assert record.to_view()["simulation"] is True
    assert "accessToken" not in record.to_view()
    nested = body | {"objectKey": f"login/test/{body['id']}/qr.png"}
    assert parse_queue_task(nested, "login/test").id == body["id"]


@pytest.mark.parametrize('lease', [False, True])
@pytest.mark.parametrize('error_code', ['login_finish_failed', 'worker_stopped'])
def test_failed_login_cleanup_releases_only_its_lock_and_preserves_history(lease, error_code):
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            body = task_body() | ({'loginTimeoutSeconds': 600} if lease else {})
            session = session_body(body) | {'userId': str(uuid4()), 'userOptions': {'equipmentStamina': False}}
            prefix = config().redis_queue_key
            active = f"{prefix}:users:{session['userId']}:active"
            await client.set(active, body['id'], ex=60)
            key, _ = await enqueue(client, body, session=session)
            record = await queue.pop()
            assert await queue.set_status(record, 'qr_ready')
            assert await queue.mark_authorized(record)
            saved = json.loads(await client.get(key))
            saved['refreshRequestId'] = str(uuid4())
            await client.set(key, json.dumps(saved), keepttl=True)
            refresh = f'{prefix}:refresh:{record.id}'
            other_refresh = f'{prefix}:refresh:{uuid4()}'
            run, log = f'{prefix}:runs:{record.id}', f'{prefix}:logs:{record.id}'
            await client.set(refresh, 'pending command', ex=600)
            await client.set(other_refresh, 'another task', ex=600)
            await client.set(run, 'execution failure history', ex=86400)
            await client.xadd(log, {'json': 'original execution log'})
            before = await client.pttl(key)
            record.daily_error_code = error_code
            intruder = RedisTaskQueue(config(), client=client)
            assert not await intruder.finish_failed(record)
            assert await client.get(active) == record.id
            assert await queue.finish_failed(record)
            ended = json.loads(await client.get(key))
            assert ended['status'] == 'failed' and ended['errorCode'] == error_code
            assert 'loginAuthorized' not in ended and 'refreshRequestId' not in ended
            for field in ['tokenHash', 'createdAt', 'expiresAt', 'userOptions', 'loginExpiresAt']:
                assert ended.get(field) == saved.get(field)
            assert 0 < await client.pttl(key) <= before
            assert await client.get(active) is None
            assert await client.get(refresh) is None
            assert await client.get(other_refresh) == 'another task'
            assert await client.get(run) == 'execution failure history' and await client.xlen(log) == 1
            assert await client.llen(prefix) == 0, 'failure must not automatically replay the task'
            newer = str(uuid4())
            await client.set(active, newer, ex=600)
            assert await queue.finish_failed(record), 'lost acknowledgement can be retried'
            assert await client.get(active) == newer, 'late cleanup cannot delete a newer account lock'
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['missing', 'no_ttl', 'expired', 'owner', 'identity', 'queued'])
def test_failed_login_cleanup_never_recreates_or_overwrites_unowned_sessions(damage):
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            body = task_body()
            session = session_body(body) | {'userId': str(uuid4())}
            prefix = config().redis_queue_key
            active = f"{prefix}:users:{session['userId']}:active"
            await client.set(active, body['id'], ex=60)
            key, _ = await enqueue(client, body, session=session)
            record = await queue.pop()
            assert await queue.set_status(record, 'qr_ready')
            saved = json.loads(await client.get(key))
            if damage == 'owner': saved['workerId'] = 'new-owner'
            if damage == 'identity': saved['createdAt'] = 'different-envelope'
            if damage == 'queued': saved['status'] = 'queued'
            await client.set(key, json.dumps(saved), keepttl=True)
            if damage == 'missing': await client.delete(key)
            if damage == 'no_ttl': await client.persist(key)
            if damage == 'expired': record.expires_at_ms = 1
            before = await client.get(key)
            assert not await queue.finish_failed(record)
            assert await client.get(key) == before
            assert await client.get(active) == record.id
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_claim_starts_fixed_occupancy_and_refresh_ack_is_scoped_idempotent():
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        body = task_body(ttl=30) | {'loginTimeoutSeconds': 600}
        session = session_body(body) | {'userId': str(uuid4())}
        active_key = f"{config().redis_queue_key}:users:{session['userId']}:active"
        await client.set(active_key, body['id'], px=30000)
        key, _ = await enqueue(client, body, session=session, ttl=30000)
        record = await queue.pop()
        assert record is not None
        claimed = json.loads(await client.get(key))
        assert claimed['expiresAt'] == body['expiresAt']
        assert claimed['loginExpiresAt'] == record.expires_at
        assert record.queue_expires_at == body['expiresAt']
        assert 590000 < await client.pttl(active_key) <= 600000
        assert claimed['refreshSupported'] is True
        deadline = record.expires_at
        request_id = str(uuid4())
        refresh_key = f"{config().redis_queue_key}:refresh:{record.id}"
        other_key = f"{config().redis_queue_key}:refresh:{uuid4()}"
        await client.set(other_key, 'another-task', ex=600)
        command = json.dumps({'taskId': record.id, 'requestId': request_id})
        await client.set(refresh_key, command, ex=600)
        claimed.update(status='preparing_qr', refreshRequestId=request_id)
        await client.set(key, json.dumps(claimed), keepttl=True)
        assert await queue.pending_refresh(record, request_id)
        assert not await queue.complete_refresh(record, str(uuid4()))
        intruder = RedisTaskQueue(config(), client=client)
        assert not await intruder.complete_refresh(record, request_id)
        assert await client.get(refresh_key) == command
        assert await queue.complete_refresh(record, request_id)
        assert await queue.complete_refresh(record, request_id), 'lost acknowledgement can be retried'
        assert await client.get(refresh_key) is None
        assert await client.get(other_key) == 'another-task'
        ready = json.loads(await client.get(key))
        assert ready['qrRevision'] == request_id and 'refreshRequestId' not in ready
        assert ready['loginExpiresAt'] == deadline and record.expires_at == deadline
        assert await queue.mark_authorized(record)
        assert json.loads(await client.get(key))['loginAuthorized'] is True
        await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"version": 2}, {"version": True}, {"version": 1.0}, {"version": "1"},
    {"id": "../bad"}, {"id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
    {"objectKey": "../qr.png"}, {"objectKey": "login/qr.png"},
    {"createdAt": "2000-01-01T00:00:00Z"}, {"createdAt": "2026-02-30T10:00:00Z"},
    {"expiresAt": "2000-01-01T00:00:00Z"}, {"expiresAt": "2100-01-01T00:00:00Z"},
    {"createdAt": None}, {"expiresAt": []},
])
def test_queue_schema_rejects_invalid_metadata(changes):
    with pytest.raises(HttpError):
        parse_queue_task(task_body() | changes, "login")


@pytest.mark.parametrize("body", [None, [], "string", 1, True])
def test_queue_schema_requires_an_object(body):
    with pytest.raises(HttpError):
        parse_queue_task(body, "login")


def test_queue_schema_rejects_invalid_lifetime_and_prefix():
    with pytest.raises(HttpError):
        parse_queue_task(task_body(ttl=601), "login")
    with pytest.raises(HttpError):
        parse_queue_task(task_body(ttl=-1), "login")
    with pytest.raises(HttpError):
        parse_queue_task(task_body(), "../login")
    body = task_body()
    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    body["createdAt"] = future.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with pytest.raises(HttpError):
        parse_queue_task(body, "login")


def test_duplicate_tasks_have_one_owner_and_preserve_auth_and_ttl():
    async def scenario():
        client = fake_client()
        try:
            first, second = RedisTaskQueue(config(), client=client), RedisTaskQueue(config(), client=client)
            assert first.owner != second.owner
            await first.initialize()
            body = task_body()
            session = session_body(body) | {"custom": {"preserved": True}}
            key, payload = await enqueue(client, body, session=session, ttl=45_000)
            await client.lpush(config().redis_queue_key, payload)
            before = await client.pttl(key)
            results = await asyncio.gather(first.pop(), second.pop())
            assert sum(record is not None for record in results) == 1
            claimed = next(record for record in results if record is not None)
            owner = first if results[0] is claimed else second
            other = second if owner is first else first
            stored = json.loads(await client.get(key))
            assert stored == session | {"status": "starting_emulator", "workerId": owner.owner}
            assert claimed._queue_payload == payload
            assert 0 < await client.pttl(key) <= before
            assert not await other.set_status(claimed, "failed")
            assert await owner.set_status(claimed, "opening_game")
            assert await owner.set_status(claimed, "qr_ready")
            assert not await owner.set_status(claimed, "failed")
            stored = json.loads(await client.get(key))
            assert stored["status"] == "qr_ready"
            assert stored["tokenHash"] == session["tokenHash"]
            assert stored["custom"] == {"preserved": True}
            assert 0 < await client.pttl(key) <= before
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"id": str(uuid4())}, {"createdAt": "2000-01-01T00:00:00Z"},
    {"expiresAt": "2100-01-01T00:00:00Z"}, {"simulation": False}, {"simulation": "true"},
    {"tokenHash": "secret"}, {"tokenHash": "z" * 64}, {"tokenHash": None},
    {"status": "starting_emulator"}, {"status": "opening_game"}, {"status": "preparing_qr"},
    {"status": "qr_ready"}, {"status": "failed"}, {"status": "expired"},
])
def test_pop_does_not_claim_conflicting_or_completed_sessions(changes):
    async def scenario():
        client = fake_client()
        try:
            body = task_body()
            key, _ = await enqueue(client, body, session=session_body(body) | changes)
            before = await client.get(key)
            assert await RedisTaskQueue(config(), client=client).pop() is None
            assert await client.get(key) == before
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["missing", "no_ttl", "wrong_type", "invalid_json", "json_scalar"])
def test_pop_does_not_restore_missing_or_invalid_sessions(kind):
    async def scenario():
        client = fake_client()
        try:
            body = task_body()
            key, _ = await enqueue(client, body)
            if kind == "missing":
                await client.delete(key)
            elif kind == "no_ttl":
                await client.persist(key)
            elif kind == "wrong_type":
                await client.delete(key)
                await client.lpush(key, "wrong type")
            elif kind == "invalid_json":
                await client.set(key, "bad JSON", px=60_000)
            else:
                await client.set(key, "42", px=60_000)
            assert await RedisTaskQueue(config(), client=client).pop() is None
            if kind == "missing":
                assert await client.exists(key) == 0
            elif kind == "no_ttl":
                assert await client.pttl(key) == -1
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_bad_payloads_are_discarded_before_claiming():
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            for payload in ["{", "[]", "null", "x" * 8193, json.dumps(task_body(ttl=-1)), "[" * 1500]:
                await client.lpush(config().redis_queue_key, payload)
                assert await queue.pop() is None
            body = task_body()
            key, _ = await enqueue(client, body)
            await client.delete(config().redis_queue_key)
            body["extra"] = "\u4e2d" * 3000
            await client.lpush(config().redis_queue_key, json.dumps(body, ensure_ascii=False))
            assert await queue.pop() is None
            assert json.loads(await client.get(key))["status"] == "queued"
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_status_does_not_recreate_session_or_publish_raw_error():
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            body = task_body()
            key, _ = await enqueue(client, body)
            record = await queue.pop()
            assert await queue.set_status(record, "failed", "redis://password@private-host secret payload")
            stored = json.loads(await client.get(key))
            assert stored["error"] == SAFE_TASK_ERROR
            assert stored["tokenHash"] == session_body(body)["tokenHash"]
            await client.delete(key)
            assert not await queue.set_status(record, "failed", SAFE_TASK_ERROR)
            assert await client.exists(key) == 0
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("safe_error", SAFE_DRIVER_ERRORS.values())
def test_status_publishes_only_approved_ldplayer_errors(safe_error):
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            body = task_body()
            key, _ = await enqueue(client, body)
            record = await queue.pop()
            assert await queue.set_status(record, "failed", safe_error)
            assert json.loads(await client.get(key))["error"] == safe_error
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["qr_ready", "failed", "expired"])
def test_terminal_status_retry_is_idempotent_for_its_owner(status):
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            other = RedisTaskQueue(config(), client=client)
            body = task_body()
            key, _ = await enqueue(client, body)
            record = await queue.pop()
            assert await queue.set_status(record, status, SAFE_TASK_ERROR)
            before = await client.get(key)
            ttl_before = await client.pttl(key)
            assert await queue.set_status(record, status, SAFE_TASK_ERROR)
            assert await client.get(key) == before
            assert 0 < await client.pttl(key) <= ttl_before
            assert not await other.set_status(record, status, SAFE_TASK_ERROR)
            different = "failed" if status != "failed" else "qr_ready"
            assert not await queue.set_status(record, different)
            assert await client.get(key) == before
            await client.delete(key)
            assert not await queue.set_status(record, status, SAFE_TASK_ERROR)
            assert await client.exists(key) == 0
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_redis_driver_does_not_retry_a_command_after_a_lost_response():
    redis = pytest.importorskip("redis")

    async def scenario():
        queue = RedisTaskQueue(config())
        try:
            retry = queue._client.connection_pool.connection_kwargs["retry"]
            attempts = 0

            async def destructive_pop():
                nonlocal attempts
                attempts += 1
                raise redis.exceptions.TimeoutError("response lost after pop")

            async def disconnect(error):
                pass

            with pytest.raises(redis.exceptions.TimeoutError):
                await retry.call_with_retry(destructive_pop, disconnect)
            assert attempts == 1
        finally:
            await queue.close()
    asyncio.run(scenario())


def test_requeue_preserves_original_payload_ttl_and_session_credentials():
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            other = RedisTaskQueue(config(), client=client)
            body = task_body()
            key, payload = await enqueue(client, body)
            record = await queue.pop()
            assert not await other.requeue(record)
            before = await client.pttl(key)
            assert await queue.requeue(record)
            assert await client.lrange(config().redis_queue_key, 0, -1) == [payload]
            assert json.loads(await client.get(key)) == session_body(body)
            assert 0 < await client.pttl(key) <= before
            assert not await queue.requeue(record)
            reclaimed = await other.pop()
            assert reclaimed.id == body["id"]
            assert not await queue.set_status(reclaimed, "failed")
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["list_wrong_type", "missing", "no_ttl", "semantic_expiry", "terminal", "metadata"])
def test_requeue_and_updates_do_not_mutate_invalid_sessions(kind):
    async def scenario():
        client = fake_client()
        try:
            queue = RedisTaskQueue(config(), client=client)
            body = task_body()
            key, _ = await enqueue(client, body)
            record = await queue.pop()
            if kind == "list_wrong_type":
                await client.set(config().redis_queue_key, "occupied")
            elif kind == "missing":
                await client.delete(key)
            elif kind == "no_ttl":
                await client.persist(key)
            elif kind == "semantic_expiry":
                record.expires_at_ms = 0
            elif kind == "terminal":
                stored = json.loads(await client.get(key)) | {"status": "failed"}
                await client.set(key, json.dumps(stored), keepttl=True)
            else:
                record.created_at = "2000-01-01T00:00:00Z"
            before = await client.get(key)
            assert not await queue.requeue(record)
            assert await client.get(key) == before
            if kind != "list_wrong_type":
                assert not await queue.set_status(record, "opening_game")
                assert await client.get(key) == before
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_transport_errors_are_fixed_and_do_not_expose_driver_details():
    class BrokenClient:
        async def ping(self):
            raise RuntimeError("redis://username:password@secret-host payload")

        async def aclose(self):
            pass

    async def scenario():
        queue = RedisTaskQueue(config(), client=BrokenClient())
        with pytest.raises(RedisQueueError) as exc:
            await queue.initialize()
        assert str(exc.value) == "Redis operation failed"
        assert exc.value.__suppress_context__ is True
        await queue.close()
    asyncio.run(scenario())
