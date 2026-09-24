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
    entries = await redis.xrange(log_key)
    assert len(entries) == 1
    event = json.loads(entries[0][1]["json"])
    assert session["status"] == run["status"] == "failed"
    assert event["level"] == "ERROR"
    assert event["message"] == session["error"] == SAFE_REJECTION_ERRORS[session["errorCode"]]
    assert run["errorCode"] == session["errorCode"]
    assert run["updatedAt"] == run["finishedAt"] == event["time"]
    return session, run, event


@pytest.mark.parametrize("offset,ttl", [(-25, 60), (25, 10)])
def test_25_second_clock_skew_fails_immediately_with_redis_based_diagnostic_time(monkeypatch, offset, ttl):
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
            session, run, event = await read_failure(redis, body)
            assert session["errorCode"] == "worker_clock_skew"
            assert session["tokenHash"] == original["tokenHash"]
            assert session["custom"] == original["custom"]
            assert session["createdAt"] == original["createdAt"] and session["expiresAt"] == original["expiresAt"]
            assert "workerId" not in session
            assert 0 < await redis.pttl(session_key) <= original_ttl
            assert 118 <= await redis.ttl(run_key) <= 120 and 118 <= await redis.ttl(log_key) <= 120
            assert abs(models.timestamp_ms(event["time"]) / 1000 - actual_now) < 2
            assert run["startedAt"] is None
            assert "tokenHash" not in json.dumps([run, event]) and "private-user" not in json.dumps([run, event])
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes,code", [
    ({"version": 2}, "invalid_task_payload"),
    ({"objectKey": "login/wrong/qr.png"}, "invalid_task_payload"),
    ({"created_delta": 30, "ttl": 60}, "invalid_task_timestamps"),
    ({"ttl": 601}, "invalid_task_timestamps"),
    ({"created_delta": 60, "ttl": 30}, "invalid_task_timestamps"),
])
def test_attributable_invalid_envelopes_are_distinguished_from_clock_skew(changes, code):
    async def scenario():
        redis = client()
        try:
            body = message(**changes)
            await enqueue(redis, body)
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            session, _, _ = await read_failure(redis, body)
            assert session["errorCode"] == code
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_invalid_producer_timestamps_are_not_misdiagnosed_as_worker_clock_skew(monkeypatch):
    async def scenario():
        redis = client()
        try:
            body = message(created_delta=120, ttl=180)
            await enqueue(redis, body)
            real_now = time.time()
            monkeypatch.setattr(models, "time", SimpleNamespace(time=lambda: real_now - 25))
            assert await RedisTaskQueue(configuration(), client=redis).pop() is None
            session, _, _ = await read_failure(redis, body)
            assert session["errorCode"] == "invalid_task_timestamps"
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


@pytest.mark.parametrize("kind", ["run_terminal", "run_foreign", "run_wrong_type", "run_invalid_json", "log_wrong_type"])
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
                    if not self.failed and failure in {"eval", "lost_reply"}:
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
    assert "failure report unavailable" in caplog.text
    assert "private-secret" not in caplog.text and "private-host" not in caplog.text
