"""Progress publication and consumer protocol checks without external services."""

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sys
import threading
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service.models import parse_queue_task
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.run_logs import RedisRunReporter, iso_time
from naruto_daily.login_service.runtime import LoginRuntime


def fake_client():
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    pytest.importorskip("lupa")
    return fakeredis.FakeRedis(decode_responses=True)


def config(**changes):
    return replace(ServiceConfig(
        redis_url="redis://unused.invalid/0", redis_queue_key="test:run-logs",
        daily_timeout_seconds=600, max_tasks=5,
    ), **changes)


def body():
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    return {
        "version": 1, "id": task_id,
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "objectKey": f"login/{task_id}/qr.png",
    }


def keys(cfg, record):
    return tuple(f"{cfg.redis_queue_key}:{kind}:{record.id}" for kind in ("runs", "logs", "sessions"))


async def claim(client, queue, *, session_ttl=30_000):
    message = body()
    session = {
        "id": message["id"], "createdAt": message["createdAt"], "expiresAt": message["expiresAt"],
        "simulation": True, "tokenHash": "ab" * 32, "status": "queued", "userId": "private-user-id",
    }
    await client.set(f"{queue.config.redis_queue_key}:sessions:{message['id']}", json.dumps(session), px=session_ttl)
    await client.lpush(queue.config.redis_queue_key, json.dumps(message))
    record = await queue.pop()
    assert record is not None
    return record


async def drain(reporter, *, timeout=5):
    async with asyncio.timeout(timeout):
        while any(reporter._pending.values()):
            await asyncio.sleep(0.01)


async def eventually(predicate, *, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def entries(client, key, minimum="-"):
    return [(item_id, json.loads(fields["json"])) for item_id, fields in await client.xrange(key, min=minimum)]


def test_two_task_streams_and_snapshots_are_isolated_and_public():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        reporter = RedisRunReporter(cfg, queue)
        first, second = await claim(client, queue), await claim(client, queue)
        reporter.start()
        try:
            reporter.emit(first, status="running_daily", message="first task started")
            reporter.emit(second, status="waiting_scan", message="second task waiting")
            reporter.emit(first, message="api_key=private-api-value", action_index=2, action_total=9)
            await drain(reporter)
            first_run, first_logs, _ = keys(cfg, first)
            second_run, second_logs, _ = keys(cfg, second)
            first_state, second_state = json.loads(await client.get(first_run)), json.loads(await client.get(second_run))
            assert first_state["taskId"] == first.id and first_state["status"] == "running_daily"
            assert first_state["actionIndex"] == 2 and first_state["actionTotal"] == 9
            assert second_state["taskId"] == second.id and second_state["status"] == "waiting_scan"
            first_items, second_items = await entries(client, first_logs), await entries(client, second_logs)
            assert [item[1]["message"] for item in first_items] == ["first task started", "api_key=[REDACTED]"]
            assert [item[1]["message"] for item in second_items] == ["second task waiting"]
            public = json.dumps([first_state, second_state, first_items, second_items])
            assert "private-api-value" not in public and "private-user-id" not in public and "tokenHash" not in public
            reporter.finish(first.id)
            reporter.finish(second.id)
            assert not reporter._states
            assert await client.exists(first_run, first_logs, second_run, second_logs) == 4
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_stream_ids_support_incremental_reads_and_exact_bounded_trimming():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = RedisRunReporter(cfg, queue, stream_limit=3)
        reporter.start()
        try:
            reporter.emit(record, status="running_daily", message="message 0")
            await drain(reporter)
            _, stream_key, _ = keys(cfg, record)
            original_cursor = (await entries(client, stream_key))[0][0]
            for index in range(1, 6):
                reporter.emit(record, message=f"message {index}")
            await drain(reporter)
            page = await entries(client, stream_key, "(" + original_cursor)
            assert [event["message"] for _, event in page] == ["message 3", "message 4", "message 5"]
            assert len(await client.xrange(stream_key)) == 3
            assert await entries(client, stream_key, "(" + page[-1][0]) == []
            assert await entries(client, stream_key) == page  # Reading never consumes stream entries.
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_progress_outlives_qr_session_and_terminal_retention_resets_independently():
    async def scenario():
        client, cfg = fake_client(), config(daily_timeout_seconds=600)
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue, session_ttl=5_000)
        reporter = RedisRunReporter(cfg, queue, retention_seconds=120)
        reporter.start()
        try:
            run_key, stream_key, session_key = keys(cfg, record)
            before = await client.pttl(session_key)
            record.daily_started_at = datetime.now(timezone.utc).timestamp()
            reporter.emit(record, status="running_daily")
            await drain(reporter)
            assert 0 < await client.pttl(session_key) <= before
            assert 718 <= await client.ttl(run_key) <= 720
            assert 718 <= await client.ttl(stream_key) <= 720
            await client.delete(session_key)  # Simulate QR-session expiration while daily work continues.
            reporter.emit(record, message="daily after QR expiration")
            reporter.emit(record, status="completed")
            reporter.finish(record.id)
            await drain(reporter)
            state = json.loads(await client.get(run_key))
            assert state["status"] == "completed"
            assert state["startedAt"] == iso_time(record.daily_started_at)
            assert state["finishedAt"] is not None
            assert 118 <= await client.ttl(run_key) <= 120
            assert 118 <= await client.ttl(stream_key) <= 120
            assert await client.exists(session_key) == 0
            assert record.id not in reporter._states
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_owner_handoff_rejects_old_owner_before_and_after_qr_session_expiration():
    async def scenario():
        client, cfg = fake_client(), config()
        first, second = RedisTaskQueue(cfg, client=client), RedisTaskQueue(cfg, client=client)
        record = await claim(client, first)
        first_reporter, second_reporter = RedisRunReporter(cfg, first), RedisRunReporter(cfg, second)
        try:
            first_reporter.emit(record, status="starting_emulator", message="old worker")
            await first_reporter._write(record.id, first_reporter._pending[record.id][-1])
            assert await first.requeue(record)
            next_record = await second.pop()
            assert next_record is not None and next_record.id == record.id
            second_reporter.emit(next_record, status="starting_emulator", message="new worker")
            await second_reporter._write(record.id, second_reporter._pending[record.id][-1])
            run_key, stream_key, session_key = keys(cfg, record)
            new_state = await client.get(run_key)
            first_reporter.emit(record, status="failed", message="stale owner write")
            old_update = first_reporter._pending[record.id][-1]
            await first_reporter._write(record.id, old_update)
            assert await client.get(run_key) == new_state
            await client.delete(session_key)
            await first_reporter._write(record.id, old_update)
            assert await client.get(run_key) == new_state
            second_reporter.emit(next_record, status="completed")
            await second_reporter._write(record.id, second_reporter._pending[record.id][-1])
            state = json.loads(await client.get(run_key))
            assert state["status"] == "completed" and state["workerId"] == second.owner
            assert [item[1]["message"] for item in await entries(client, stream_key)][:2] == ["old worker", "new worker"]
            assert len(await entries(client, stream_key)) == 3
        finally:
            await first_reporter.close(timeout=0)
            await second_reporter.close(timeout=0)
            await client.aclose()
    asyncio.run(scenario())


def test_progress_cannot_create_unclaimed_or_foreign_task_data():
    async def scenario():
        client, cfg = fake_client(), config()
        owner, stranger = RedisTaskQueue(cfg, client=client), RedisTaskQueue(cfg, client=client)
        unclaimed = parse_queue_task(body(), "login")
        claimed = await claim(client, owner)
        reporter = RedisRunReporter(cfg, stranger)
        reporter.start()
        try:
            reporter.emit(unclaimed, status="running_daily")
            reporter.emit(claimed, status="running_daily")
            await drain(reporter)
            assert await client.exists(*keys(cfg, unclaimed)[:2], *keys(cfg, claimed)[:2]) == 0
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_same_worker_reclaims_task_after_local_cleanup_without_resetting_sequence():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = RedisRunReporter(cfg, queue)
        reporter.start()
        try:
            reporter.emit(record, status="starting_emulator", message="first attempt")
            await drain(reporter)
            run_key, stream_key, _ = keys(cfg, record)
            original_sequence = json.loads(await client.get(run_key))["sequence"]
            assert await queue.requeue(record)
            reporter.finish(record.id)
            assert record.id not in reporter._states
            retried = await queue.pop()
            assert retried is not None and retried.id == record.id
            reporter.emit(retried, status="opening_game", message="retry attempt")
            await drain(reporter)
            state = json.loads(await client.get(run_key))
            assert state["sequence"] > original_sequence
            assert state["status"] == "opening_game"
            assert [event["message"] for _, event in await entries(client, stream_key)] == ["first attempt", "retry attempt"]
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_write_accepted_then_response_lost_retries_without_duplicate_stream_event():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)

        class LoseFirstReply:
            calls = 0

            async def eval(self, *args):
                result = await client.eval(*args)
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("redis://private:credential@private-host")
                return result

        transport = LoseFirstReply()
        queue._client = transport
        reporter = RedisRunReporter(cfg, queue)
        reporter.start()
        try:
            reporter.emit(record, status="completed", message="exactly one completion")
            reporter.finish(record.id)
            await drain(reporter)
            run_key, stream_key, _ = keys(cfg, record)
            assert transport.calls == 2
            assert len(await entries(client, stream_key)) == 1
            assert json.loads(await client.get(run_key))["sequence"] == 1
            assert record.id not in reporter._states
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_disconnection_has_bounded_nonblocking_buffers_and_recovers_final_state():
    async def scenario():
        client, cfg = fake_client(), config(max_tasks=2)
        queue = RedisTaskQueue(cfg, client=client)
        records = [await claim(client, queue) for _ in range(3)]
        disconnected = asyncio.Event()

        class OfflineTransport:
            offline = True

            async def eval(self, *args):
                if self.offline:
                    disconnected.set()
                    raise ConnectionError("secret=private-network-detail")
                return await client.eval(*args)

        transport = OfflineTransport()
        queue._client = transport
        reporter = RedisRunReporter(cfg, queue, buffer_limit=4)
        reporter.start()
        try:
            reporter.emit(records[0], status="running_daily")
            await asyncio.wait_for(disconnected.wait(), 1)
            for record in records:
                for index in range(30):
                    reporter.emit(record, message=f"step {index}")
            reporter.emit(records[0], status="completed")
            reporter.finish(records[0].id)
            assert len(reporter._states) == 2
            assert records[2].id not in reporter._states
            assert all(len(pending) <= 4 for pending in reporter._pending.values())
            assert reporter._pending[records[0].id][-1].snapshot["status"] == "completed"
            assert records[0].id in reporter._finished
            # The game coroutine continues while the reporter sleeps/retries offline.
            marker = []
            async def continue_game():
                await asyncio.sleep(0)
                marker.append("next action")
            await asyncio.wait_for(continue_game(), 0.5)
            assert marker == ["next action"]
            transport.offline = False
            await drain(reporter)
            run_key, stream_key, _ = keys(cfg, records[0])
            assert json.loads(await client.get(run_key))["status"] == "completed"
            messages = [event["message"] for _, event in await entries(client, stream_key)]
            assert any("未保留" in message for message in messages)
            assert records[0].id not in reporter._states
            reporter.emit(records[2], status="running_daily")
            await drain(reporter)
            assert await client.exists(keys(cfg, records[2])[0]) == 1
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_slow_inflight_write_does_not_remove_a_newer_buffered_event():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowTransport:
            async def eval(self, *args):
                entered.set()
                await release.wait()
                return await client.eval(*args)

        queue._client = SlowTransport()
        reporter = RedisRunReporter(cfg, queue, buffer_limit=2)
        reporter.start()
        try:
            reporter.emit(record, status="running_daily", message="inflight")
            await asyncio.wait_for(entered.wait(), 1)
            for index in range(4):
                reporter.emit(record, message=f"new {index}")
            reporter.emit(record, status="completed", message="final event")
            reporter.finish(record.id)
            release.set()
            await drain(reporter)
            run_key, stream_key, _ = keys(cfg, record)
            assert json.loads(await client.get(run_key))["status"] == "completed"
            assert (await entries(client, stream_key))[-1][1]["message"] == "final event"
            assert any("new 3" in event["message"] for _, event in await entries(client, stream_key))
        finally:
            release.set()
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_shutdown_abandons_unreachable_redis_within_deadline_without_leaking_details(caplog):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        blocked = asyncio.Event()

        class HangingTransport:
            async def eval(self, *args):
                blocked.set()
                await asyncio.Event().wait()

        queue._client = HangingTransport()
        reporter = RedisRunReporter(cfg, queue)
        reporter.start()
        reporter.emit(record, status="completed")
        await asyncio.wait_for(blocked.wait(), 1)
        await asyncio.wait_for(reporter.close(timeout=0.03), 0.5)
        assert not reporter._pending and not reporter._states
        assert reporter._task.done()
        await client.aclose()
    asyncio.run(scenario())
    assert "Some task progress could not be sent" in caplog.text


class MemoryStorage:
    def __init__(self):
        self.put_ids = []

    async def put(self, task_id, png, expires_at):
        self.put_ids.append(task_id)

    async def delete(self, task_id):
        pass


def test_mock_completed_run_is_not_overwritten_when_its_qr_later_expires(monkeypatch):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = RedisRunReporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)

        async def make_qr(current):
            return b"\x89PNG\r\n\x1a\nfixture"

        monkeypatch.setattr(runtime, "_run_mock", make_qr)
        reporter.start()
        try:
            await runtime._process_record(record)
            await drain(reporter)
            run_key, stream_key, _ = keys(cfg, record)
            completed_snapshot = await client.get(run_key)
            completed_logs = await entries(client, stream_key)
            assert json.loads(completed_snapshot)["status"] == "completed"
            runtime._expire(record)
            await drain(reporter)
            assert await client.get(run_key) == completed_snapshot
            assert await entries(client, stream_key) == completed_logs
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_real_child_log_events_preserve_protocol_and_publish_terminal_after_cleanup(monkeypatch, tmp_path, cleanup_fails):
    async def scenario():
        client = fake_client()
        cfg = config(emulator_driver="ldplayer", ldplayer_console_path=tmp_path / "console.exe",
                     ldplayer_pool_indices=(0,), ldplayer_state_dir=tmp_path / "state")
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        storage = MemoryStorage()
        reporter = RedisRunReporter(cfg, queue)
        runtime = LoginRuntime(cfg, storage, redis_queue=queue, run_reporter=reporter)
        cleaning, release = threading.Event(), threading.Event()

        class Pool:
            def mark_ready(self, task_id):
                pass

            def close(self, task_id):
                cleaning.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test cleanup gate timed out")
                if cleanup_fails:
                    raise RuntimeError("private cleanup failure detail")

        runtime._manager = Pool()
        png = b"\x89PNG\r\n\x1a\nfixture"
        plan = [
            {"type": "log", "time": iso_time(), "level": "INFO", "message": "initializing"},
            {"type": "stage", "status": "opening_game"},
            {"type": "stage", "status": "preparing_qr"},
            {"type": "qr", "png": base64.b64encode(png).decode("ascii")},
            {"type": "log", "time": iso_time(), "level": "INFO", "message": "scan waiting", "actionIndex": 1, "actionTotal": 4},
            {"type": "logged_in"},
            {"type": "log", "time": iso_time(), "level": "INFO", "message": "daily action", "actionIndex": 1, "actionTotal": 3},
            {"type": "daily_completed", "actionCount": 3},
        ]
        program = "import json,sys\nfor event in json.loads(sys.stdin.readline())['plan']:\n print(json.dumps(event),flush=True)\n"

        async def run_child(current):
            return await runtime._run_child(current, [sys.executable, "-u", "-c", program], {"plan": plan}, daily=True)

        monkeypatch.setattr(runtime, "_run_ldplayer", run_child)
        reporter.start()
        work = asyncio.create_task(runtime._process_record(record))
        try:
            await eventually(cleaning.is_set)
            await drain(reporter)
            run_key, stream_key, session_key = keys(cfg, record)
            state = json.loads(await client.get(run_key))
            assert state["status"] == "running_daily" and state["finishedAt"] is None
            release.set()
            await asyncio.wait_for(work, 5)
            await drain(reporter)
            state = json.loads(await client.get(run_key))
            assert state["status"] == ("failed" if cleanup_fails else "completed")
            if cleanup_fails:
                assert state["errorCode"] == "instance_cleanup_failed"
            assert json.loads(await client.get(session_key))["status"] == "qr_ready"
            logs = [event for _, event in await entries(client, stream_key)]
            assert any(event["message"] == "daily action" and event["actionTotal"] == 3 for event in logs)
            assert "private cleanup failure detail" not in json.dumps(logs)
            assert storage.put_ids == [record.id]
        finally:
            release.set()
            await asyncio.wait_for(work, 5)
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"message": ""}, {"message": "x" * 1001}, {"time": "2026-02-30T12:00:00Z"},
    {"time": "2026-09-24T12:00:00+00:00"}, {"level": "DEBUG"},
    {"actionIndex": True, "actionTotal": 3}, {"actionIndex": 4, "actionTotal": 3},
    {"actionIndex": 1}, {"actionIndex": 1, "actionTotal": 100_001},
])
def test_parent_rejects_invalid_log_metadata_before_publication(changes):
    class Reporter:
        def emit(self, *args, **kwargs):
            pytest.fail("Invalid log reached the publisher")

    runtime = LoginRuntime(config(redis_url=None), MemoryStorage(), run_reporter=Reporter())
    record = parse_queue_task(body(), "login")
    event = {"type": "log", "time": iso_time(), "level": "INFO", "message": "valid text"} | changes
    with pytest.raises(ValueError):
        runtime._accept_log(record, event)


@pytest.mark.parametrize("env", [
    {"RUN_LOG_RETENTION_SECONDS": "59"}, {"RUN_LOG_RETENTION_SECONDS": "604801"},
    {"RUN_LOG_MAX_ENTRIES": "99"}, {"RUN_LOG_MAX_ENTRIES": "10001"},
])
def test_run_log_config_rejects_unbounded_or_too_short_retention(env):
    with pytest.raises(ValueError):
        load_config(env)
