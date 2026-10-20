"""Rejected queue messages become safe, attributable failures before admission."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.login_service import models
from naruto_daily.login_service.config import ServiceConfig
from naruto_daily.login_service.queue import RedisTaskQueue, SAFE_REJECTION_ERRORS


def client():
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    pytest.importorskip("lupa")
    return fakeredis.FakeRedis(decode_responses=True)


def configuration():
    return ServiceConfig(redis_url="redis://unused.invalid", redis_queue_key="test:rejected-tasks",
                         run_log_retention_seconds=120, run_log_max_entries=100)


def message(*, ttl=60, created_delta=0, **changes):
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    return {
        "version": 1, "id": task_id,
        "createdAt": (now + timedelta(seconds=created_delta)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "objectKey": f"login/{task_id}/qr.png",
    } | changes


def task_keys(body):
    return tuple(f"{configuration().redis_queue_key}:{kind}:{body['id']}" for kind in ("sessions", "runs", "logs"))


async def enqueue(redis, body, *, overrides=None, ttl=30_000):
    session = {
        "id": body["id"], "createdAt": body["createdAt"], "expiresAt": body["expiresAt"],
        "status": "queued", "tokenHash": "ab" * 32, "simulation": True,
        "userId": "private-user", "custom": {"preserved": True},
    } | (overrides or {})
    key = task_keys(body)[0]
    await redis.set(key, json.dumps(session), px=ttl)
    await redis.lpush(configuration().redis_queue_key, json.dumps(body))
    return session


async def read_failure(redis, body):
    session_key, run_key, log_key = task_keys(body)
    session, run = json.loads(await redis.get(session_key)), json.loads(await redis.get(run_key))
    assert await redis.exists(log_key) == 0
    assert session["status"] == run["status"] == "failed"
    assert session["error"] == SAFE_REJECTION_ERRORS[session["errorCode"]]
    assert run["errorCode"] == session["errorCode"]
    assert run["updatedAt"] == run["finishedAt"]
    assert run["logSource"] == "disabled"
    return session, run


@pytest.mark.parametrize("authorized", [True, False])
def test_rejection_publishes_directly_only_after_authorization(authorized):
    async def scenario():
        redis = client()
        try:
            body = message(version=2)
            await enqueue(redis, body, overrides={} if authorized else {"workerId": "other"})
            published, finished = [], []

            def emit(task_id, event):
                published.append((task_id, event))
                return f"1800000000000-{len(published)}"

            queue = RedisTaskQueue(configuration(), client=redis)
            queue.log_publisher = SimpleNamespace(emit=emit, finish=finished.append)
            assert await queue.pop() is None
            session_key, run_key, log_key = task_keys(body)
            assert await redis.exists(log_key) == 0
            if authorized:
                state = json.loads(await redis.get(run_key))
                assert state["logSource"] == "sls"
                assert state["logCursor"] == "1800000000000-2"
                assert len(published) == 2
                assert published[0][1]["time"] == body["createdAt"]
                assert published[1][1]["level"] == "ERROR"
                assert published[1][1]["message"] == SAFE_REJECTION_ERRORS["invalid_task_payload"]
                assert published[1][1]["time"] == state["finishedAt"]
                assert finished == [body["id"]]
                assert 119 <= await redis.ttl(run_key) <= 120
            else:
                assert published == finished == []
                assert await redis.exists(run_key) == 0
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_rejection_cursor_cas_cannot_overwrite_a_newer_run():
    async def scenario():
        redis = client()
        try:
            body = message(version=2)
            await enqueue(redis, body)
            run_key = task_keys(body)[1]
            newer = json.dumps({"taskId": body["id"], "workerId": "next-owner", "status": "completed"})

            class RacingTransport:
                def __getattr__(self, name):
                    return getattr(redis, name)

                async def eval(self, *args):
                    from naruto_daily.login_service.queue import _REJECT_LOG_CURSOR
                    if args[0] == _REJECT_LOG_CURSOR:
                        await redis.set(run_key, newer, ex=30)
                    return await redis.eval(*args)

            queue = RedisTaskQueue(configuration(), client=RacingTransport())
            queue.log_publisher = SimpleNamespace(emit=lambda *args: "1800000000000-1", finish=lambda _: None)
            assert await queue.pop() is None
            assert await redis.get(run_key) == newer
            assert 29 <= await redis.ttl(run_key) <= 30
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_legacy_log_key_does_not_block_rejection_or_get_modified():
    async def scenario():
        redis = client()
        try:
            body = message(version=2)
            await enqueue(redis, body)
            run_key, log_key = task_keys(body)[1:]
            await redis.set(log_key, "legacy reserved value", ex=30)
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            assert json.loads(await redis.get(run_key))["status"] == "failed"
            assert await redis.get(log_key) == "legacy reserved value"
            assert 29 <= await redis.ttl(log_key) <= 30
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("offset,ttl", [(-25, 60), (25, 10)])
def test_clock_skew_keeps_task_queued_for_a_healthy_consumer(monkeypatch, offset, ttl):
    async def scenario():
        redis = client()
        try:
            body = message(ttl=ttl)
            original = await enqueue(redis, body)
            session_key, run_key, log_key = task_keys(body)
            original_ttl = await redis.pttl(session_key)
            actual_now = time.time()
            # Do not modify time.time globally: fakeredis must retain the server clock.
            monkeypatch.setattr(models, "time", SimpleNamespace(time=lambda: actual_now + offset))
            queue = RedisTaskQueue(configuration(), client=redis)
            assert await queue.pop() is None
            assert json.loads(await redis.get(session_key)) == original
            assert await redis.lrange(configuration().redis_queue_key, 0, -1) == [json.dumps(body)]
            assert await redis.exists(run_key, log_key) == 0
            assert 0 < await redis.pttl(session_key) <= original_ttl
            monkeypatch.setattr(models, 'time', time)
            healthy = RedisTaskQueue(configuration(), client=redis)
            record = await healthy.pop()
            assert record is not None and record.id == body['id']
            assert await redis.llen(configuration().redis_queue_key) == 0
            assert json.loads(await redis.get(session_key))['workerId'] == healthy.owner
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes,code", [
    ({"version": 2}, "invalid_task_payload"),
    ({"objectKey": "login/wrong/qr.png"}, "invalid_task_payload"),
])
def test_attributable_invalid_envelopes_are_distinguished_from_clock_skew(changes, code):
    async def scenario():
        redis = client()
        try:
            body = message(**changes)
            await enqueue(redis, body)
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            session, _ = await read_failure(redis, body)
            assert session["errorCode"] == code
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'created_delta': 30, 'ttl': 60}, {'ttl': 601}, {'created_delta': 60, 'ttl': 30},
])
def test_invalid_producer_timestamps_stay_queued_without_failure_report(changes):
    async def scenario():
        redis = client()
        try:
            body = message(**changes)
            original = await enqueue(redis, body)
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            assert json.loads(await redis.get(task_keys(body)[0])) == original
            assert await redis.lrange(configuration().redis_queue_key, 0, -1) == [json.dumps(body)]
            assert await redis.exists(*task_keys(body)[1:]) == 0
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_duplicate_rejections_from_two_consumers_are_atomic_and_do_not_refresh_expiry():
    async def scenario():
        redis = client()
        try:
            body = message(version=2)
            await enqueue(redis, body)
            await redis.lpush(configuration().redis_queue_key, json.dumps(body))
            before = await redis.pttl(task_keys(body)[0])
            consumers = [RedisTaskQueue(configuration(), client=redis) for _ in range(2)]
            assert await asyncio.gather(*(worker.pop() for worker in consumers)) == [None, None]
            await read_failure(redis, body)
            snapshot = [await redis.get(key) for key in task_keys(body)[:2]]
            first_log = await redis.xrange(task_keys(body)[2])
            await redis.lpush(configuration().redis_queue_key, json.dumps(body))
            assert await consumers[1].pop() is None
            assert [await redis.get(key) for key in task_keys(body)[:2]] == snapshot
            assert await redis.xrange(task_keys(body)[2]) == first_log
            assert 0 < await redis.pttl(task_keys(body)[0]) <= before
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"id": str(uuid4())}, {"createdAt": "2000-01-01T00:00:00Z"},
    {"expiresAt": "2100-01-01T00:00:00Z"}, {"simulation": False},
    {"tokenHash": "private-invalid-token"}, {"tokenHash": "g" * 64}, {"tokenHash": None},
    {"workerId": "another-worker"}, {"status": "starting_emulator", "workerId": "another-worker"},
    {"status": "opening_game"}, {"status": "preparing_qr"}, {"status": "qr_ready"},
    {"status": "completed"}, {"status": "failed"}, {"status": "expired"},
])
def test_rejection_never_overwrites_conflicting_claimed_or_terminal_sessions(overrides):
    async def scenario():
        redis = client()
        try:
            body = message(version=2)
            await enqueue(redis, body, overrides=overrides)
            session_key, run_key, log_key = task_keys(body)
            original = await redis.get(session_key)
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            assert await redis.get(session_key) == original
            assert await redis.exists(run_key, log_key) == 0
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["missing", "no_ttl", "wrong_type", "invalid_json", "scalar", "semantic_expiry"])
def test_rejection_cannot_recreate_expired_or_untrusted_sessions(kind):
    async def scenario():
        redis = client()
        try:
            body = message(version=2, ttl=-10 if kind == "semantic_expiry" else 60)
            await enqueue(redis, body)
            session_key, run_key, log_key = task_keys(body)
            if kind == "missing":
                await redis.delete(session_key)
            elif kind == "no_ttl":
                await redis.persist(session_key)
            elif kind == "wrong_type":
                await redis.delete(session_key)
                await redis.lpush(session_key, "reserved")
            elif kind == "invalid_json":
                await redis.set(session_key, "not JSON", px=30_000)
            elif kind == "scalar":
                await redis.set(session_key, "42", px=30_000)
            original = await redis.dump(session_key)
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            assert await redis.dump(session_key) == original
            assert await redis.exists(run_key, log_key) == 0
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["run_terminal", "run_foreign", "run_wrong_type", "run_invalid_json"])
def test_rejection_does_not_clobber_existing_terminal_or_foreign_run_data(kind):
    async def scenario():
        redis = client()
        try:
            body = message(version=2)
            await enqueue(redis, body)
            session_key, run_key, log_key = task_keys(body)
            if kind == "run_terminal":
                await redis.set(run_key, json.dumps({"taskId": body["id"], "status": "completed"}))
            elif kind == "run_foreign":
                await redis.set(run_key, json.dumps({"taskId": str(uuid4()), "status": "running_daily"}))
            elif kind == "run_wrong_type":
                await redis.lpush(run_key, "reserved")
            elif kind == "run_invalid_json":
                await redis.set(run_key, "broken JSON")
            else:
                await redis.set(log_key, "reserved")
            before = [await redis.dump(key) for key in (session_key, run_key, log_key)]
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            assert [await redis.dump(key) for key in (session_key, run_key, log_key)] == before
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    "{", "null", '["private-payload-secret"]', "x" * 8193,
    json.dumps(message(id="private-payload-secret")),
    json.dumps(message(createdAt="2026-02-30T12:00:00Z", version=2)),
])
def test_unattributable_rejections_warn_without_leaking_payload_or_creating_state(payload, caplog):
    async def scenario():
        redis = client()
        try:
            await redis.lpush(configuration().redis_queue_key, payload)
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            assert await redis.keys("*") == []
        finally:
            await redis.aclose()
    asyncio.run(scenario())
    assert "Skipped" in caplog.text
    assert "private-payload-secret" not in caplog.text


@pytest.mark.parametrize("failure", ["time", "eval", "lost_reply"])
def test_failed_rejection_reporting_does_not_stop_next_queue_task_or_expose_driver_details(failure, caplog):
    async def scenario():
        redis = client()
        try:
            invalid, valid = message(version=2), message()
            await enqueue(redis, invalid)
            await enqueue(redis, valid)

            class Transport:
                failed = False

                def __getattr__(self, name):
                    return getattr(redis, name)

                async def time(self):
                    if failure == "time":
                        raise ConnectionError("redis://private-user:private-secret@private-host")
                    return await redis.time()

                async def eval(self, *args):
                    from naruto_daily.login_service.queue import _REJECT_SCRIPT
                    if args[0] == _REJECT_SCRIPT and not self.failed and failure in {"eval", "lost_reply"}:
                        self.failed = True
                        if failure == "lost_reply":
                            await redis.eval(*args)
                        raise ConnectionError("redis://private-user:private-secret@private-host")
                    return await redis.eval(*args)

            queue = RedisTaskQueue(configuration(), client=Transport())
            assert await queue.pop() is None
            next_record = await queue.pop()
            assert next_record is not None and next_record.id == valid["id"]
            if failure == "lost_reply":
                await read_failure(redis, invalid)
        finally:
            await redis.aclose()
    asyncio.run(scenario())
    assert ("failure report unavailable" in caplog.text) == (failure != "lost_reply")
    assert "private-secret" not in caplog.text and "private-host" not in caplog.text
