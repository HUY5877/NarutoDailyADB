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

from oss_fakes import OSS_ENV

from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service.models import parse_queue_task
from naruto_daily.login_service.queue import RedisTaskQueue, SAFE_DRIVER_ERRORS
from naruto_daily.login_service.run_logs import RedisRunReporter, SAFE_RUN_ERRORS, STAGE_MESSAGES, iso_time
from naruto_daily.login_service.runtime import LoginRuntime, _WorkerError


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


@pytest.mark.parametrize("terminal,error_code", [
    ("completed", None), ("failed", "daily_timeout"),
    ("expired", "login_timeout"), ("interrupted", "worker_stopped"),
])
def test_run_username_survives_progress_terminal_and_missing_session(terminal, error_code):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        record.username = "小鸣人"
        reporter = RedisRunReporter(cfg, queue)
        reporter.start()
        try:
            run_key, stream_key, session_key = keys(cfg, record)
            reporter.emit(record, status="starting_emulator")
            await drain(reporter)
            assert json.loads(await client.get(run_key))["username"] == "小鸣人"

            reporter.emit(record, status="running_daily")
            reporter.emit(record, message="日常动作", action_index=1, action_total=3)
            await drain(reporter)
            progress = json.loads(await client.get(run_key))
            assert progress["status"] == "running_daily"
            assert progress["username"] == "小鸣人"
            assert progress["actionIndex"] == 1 and progress["actionTotal"] == 3

            await client.delete(session_key)
            record.username = None  # Later progress must not remove an already-known name.
            reporter.emit(record, message="二维码会话过期后的进度")
            await drain(reporter)
            assert json.loads(await client.get(run_key))["username"] == "小鸣人"
            reporter.emit(record, status=terminal, error_code=error_code)
            reporter.finish(record.id)
            await drain(reporter)
            final = json.loads(await client.get(run_key))
            assert final["status"] == terminal
            assert final["username"] == "小鸣人"
            assert final["finishedAt"] is not None
            assert await client.exists(session_key) == 0
            assert record.id not in reporter._states

            logs = await entries(client, stream_key)
            assert all("username" not in event for _, event in logs)
            public = json.dumps([final, logs])
            for private_value in ("userId", "private-user-id", "tokenHash", "access_token", "accessToken"):
                assert private_value not in public
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("missing_username", [None, ""])
def test_same_status_publishes_new_username_once_and_keeps_legacy_snapshot_compatible(missing_username):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        record.username = missing_username
        reporter = RedisRunReporter(cfg, queue)
        reporter.start()
        try:
            run_key, stream_key, _ = keys(cfg, record)
            reporter.emit(record, status="starting_emulator")
            await drain(reporter)
            original = json.loads(await client.get(run_key))
            assert "username" not in original

            record.username = "小鸣人"
            reporter.emit(record, status="starting_emulator")
            await drain(reporter)
            named = json.loads(await client.get(run_key))
            assert named["username"] == "小鸣人"
            assert named["status"] == original["status"]
            assert named["sequence"] > original["sequence"]
            assert len(await entries(client, stream_key)) == 2

            reporter.emit(record, status="starting_emulator")
            await drain(reporter)
            assert json.loads(await client.get(run_key))["sequence"] == named["sequence"]
            assert len(await entries(client, stream_key)) == 2
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
            record.authorized_at = datetime.now(timezone.utc).timestamp()
            reporter.emit(record, status="finishing_login")
            await drain(reporter)
            authorized = json.loads(await client.get(run_key))
            assert authorized["status"] == "finishing_login"
            assert authorized["authorizedAt"] == iso_time(record.authorized_at)
            assert authorized["startedAt"] is None
            assert (await entries(client, stream_key))[-1][1]["message"] == "扫码授权已完成，正在进入游戏"
            record.daily_started_at = datetime.now(timezone.utc).timestamp()
            reporter.emit(record, status="running_daily")
            await drain(reporter)
            assert 0 < await client.pttl(session_key) <= before
            assert 605398 <= await client.ttl(run_key) <= 605400
            assert 718 <= await client.ttl(stream_key) <= 720
            await client.delete(session_key)  # Simulate QR-session expiration while daily work continues.
            reporter.emit(record, message="daily after QR expiration")
            reporter.emit(record, status="completed")
            reporter.finish(record.id)
            await drain(reporter)
            state = json.loads(await client.get(run_key))
            assert state["status"] == "completed"
            assert state["authorizedAt"] == iso_time(record.authorized_at)
            assert state["startedAt"] == iso_time(record.daily_started_at)
            assert state["finishedAt"] is not None
            assert 604798 <= await client.ttl(run_key) <= 604800
            assert 118 <= await client.ttl(stream_key) <= 120
            assert await client.exists(session_key) == 0
            assert record.id not in reporter._states
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("stale_status", ["failed", "interrupted"])
def test_owner_handoff_rejects_old_owner_before_and_after_qr_session_expiration(stale_status):
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
            first_reporter.emit(record, status=stale_status, message="stale owner write")
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


@pytest.mark.parametrize("session_state", ["queued", "failed", "missing"])
def test_interrupted_run_rejects_late_progress_without_an_active_claim(session_state):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = RedisRunReporter(cfg, queue)
        try:
            reporter.emit(record, status="interrupted", error_code="worker_stopped")
            await reporter._write(record.id, reporter._pending[record.id][-1])
            run_key, stream_key, session_key = keys(cfg, record)
            if session_state == "missing":
                await client.delete(session_key)
            elif session_state == "failed":
                record.daily_error_code = "worker_stopped"
                assert await queue.finish_failed(record)
            else:
                assert await queue.requeue(record)
            before = await client.get(run_key)
            reporter.emit(record, status="starting_emulator", message="late progress")
            await reporter._write(record.id, reporter._pending[record.id][-1])
            assert await client.get(run_key) == before
            assert len(await entries(client, stream_key)) == 1
            if session_state == "queued":
                retried = await queue.pop()
                assert retried is not None
                reporter.emit(retried, status="starting_emulator", message="new attempt")
                await reporter._write(retried.id, reporter._pending[retried.id][-1])
                assert json.loads(await client.get(run_key))["status"] == "starting_emulator"
        finally:
            await reporter.close(timeout=0)
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
            assert completed_logs[-1][1]["message"] == "模拟二维码已生成"
            assert all(event["message"] != STAGE_MESSAGES["completed"] for _, event in completed_logs)
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
            {"type": "stage", "status": "starting_emulator"},
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
            assert [event["message"] for event in logs] == [
                STAGE_MESSAGES["starting_emulator"], STAGE_MESSAGES["opening_game"],
                STAGE_MESSAGES["preparing_qr"], STAGE_MESSAGES["waiting_scan"],
                "scan waiting", STAGE_MESSAGES["running_daily"], "daily action",
                SAFE_RUN_ERRORS["instance_cleanup_failed"] if cleanup_fails else STAGE_MESSAGES["completed"],
            ]
            assert "private cleanup failure detail" not in json.dumps(logs)
            assert storage.put_ids == [record.id]
        finally:
            release.set()
            await asyncio.wait_for(work, 5)
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["emulator_start_failed", "qr_not_found", "daily_failed", None])
def test_failure_before_child_admission_has_a_safe_specific_error_log(monkeypatch, code):
    async def scenario():
        redis, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=redis)
        record = await claim(redis, queue)
        reporter = RedisRunReporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)

        async def fail(current):
            if code is None:
                raise RuntimeError("redis://private-secret@internal-host private diagnostic")
            raise _WorkerError(SAFE_DRIVER_ERRORS[code], code)

        monkeypatch.setattr(runtime, "_run_mock", fail)
        reporter.start()
        try:
            await runtime._process_record(record)
            await drain(reporter)
            run_key, stream_key, _ = keys(cfg, record)
            state = json.loads(await redis.get(run_key))
            logs = await entries(redis, stream_key)
            expected = code or "unexpected_error"
            assert state["errorCode"] == expected
            assert logs[-1][1]["level"] == "ERROR"
            assert logs[-1][1]["message"] == SAFE_RUN_ERRORS[expected]
            assert "private-secret" not in json.dumps(logs)
            assert "internal-host" not in json.dumps(logs)
        finally:
            await reporter.close()
            await redis.aclose()
    asyncio.run(scenario())


def test_parent_keeps_action_boundaries_and_warnings_but_omits_low_level_info():
    reported = []

    class Reporter:
        def emit(self, record, **event):
            reported.append(event)

    runtime = LoginRuntime(config(redis_url=None), MemoryStorage(), run_reporter=Reporter())
    record = parse_queue_task(body(), "login")
    for event in [
        {"message": "截图匹配相似度 0.85", "level": "INFO"},
        {"message": "点击坐标 (10,20)", "level": "INFO"},
        {"message": "动作 2/38：领取昨日奖励并执行铜币祈福", "level": "INFO", "actionIndex": 2, "actionTotal": 38},
        {"message": "本次识别暂未成功", "level": "WARNING"},
        {"message": "任务失败", "level": "ERROR"},
    ]:
        runtime._accept_log(record, {"type": "log", "time": iso_time()} | event)
    assert [event["message"] for event in reported] == [
        "动作 2/38：领取昨日奖励并执行铜币祈福", "本次识别暂未成功", "任务失败",
    ]


def test_secret_realm_completion_is_first_timestamp_and_does_not_change_running_status():
    from types import SimpleNamespace

    reported = []
    runtime = SimpleNamespace(_run_reporter=SimpleNamespace(emit=lambda record, **event: reported.append(event)))
    record = parse_queue_task(body(), 'login')
    record.daily_started_at = 1_800_000_000.0
    record.daily_status = 'running_daily'
    record.execution_deadline = record.daily_started_at + 3_000
    first = '2027-01-15T08:00:01.000Z'
    for stamp in (first, '2027-01-15T08:00:05.000Z'):
        LoginRuntime._accept_log(runtime, record, {
            'type': 'log', 'time': stamp, 'level': 'INFO', 'message': '首次秘境成功结算',
            'secretRealmCompletedAt': stamp,
        })
    assert record.secret_realm_completed_at == first
    assert record.daily_status == 'running_daily'
    assert record.execution_deadline == 1_800_003_000.0
    assert len(reported) == 1


@pytest.mark.parametrize('changes, started, status', [
    ({'secretRealmCompletedAt': None}, True, 'running_daily'),
    ({'secretRealmCompletedAt': True}, True, 'running_daily'),
    ({'secretRealmCompletedAt': '2027-01-15T08:00:02.000Z'}, True, 'running_daily'),
    ({'time': '2027-02-30T08:00:01.000Z', 'secretRealmCompletedAt': '2027-02-30T08:00:01.000Z'}, True, 'running_daily'),
    ({'level': 'ERROR'}, True, 'running_daily'),
    ({}, False, 'running_daily'), ({}, True, 'finishing_login'), ({}, True, 'failed'),
])
def test_invalid_or_out_of_phase_secret_completion_is_rejected(changes, started, status):
    from types import SimpleNamespace

    record = parse_queue_task(body(), 'login')
    record.daily_started_at = 1_800_000_000.0 if started else None
    record.daily_status = status
    runtime = SimpleNamespace(_run_reporter=None)
    event = {
        'type': 'log', 'time': '2027-01-15T08:00:01.000Z', 'level': 'INFO', 'message': '秘境成功',
        'secretRealmCompletedAt': '2027-01-15T08:00:01.000Z',
    } | changes
    with pytest.raises(ValueError):
        LoginRuntime._accept_log(runtime, record, event)
    assert record.secret_realm_completed_at is None


@pytest.mark.parametrize('terminal, code', [
    ('failed', 'daily_timeout'), ('failed', 'daily_failed'), ('interrupted', 'worker_stopped'),
    ('completed', None),
])
def test_redis_run_preserves_secret_completion_when_execution_stops(terminal, code):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = RedisRunReporter(cfg, queue)
        reporter.start()
        try:
            record.daily_status = 'running_daily'
            record.daily_started_at = 1_800_000_000.0
            reporter.emit(record, status='running_daily')
            await drain(reporter)
            run_key, _, _ = keys(cfg, record)
            assert 'secretRealmCompletedAt' not in json.loads(await client.get(run_key))
            runtime = object.__new__(LoginRuntime)
            runtime._run_reporter = reporter
            first = '2027-01-15T08:00:01.000Z'
            runtime._accept_log(record, {
                'type': 'log', 'time': first, 'level': 'INFO', 'message': '秘境已成功结算',
                'secretRealmCompletedAt': first,
            })
            reporter.emit(record, message='继续消耗秘境挑战券')
            await drain(reporter)
            running = json.loads(await client.get(run_key))
            assert running['status'] == 'running_daily'
            assert running['secretRealmCompletedAt'] == first
            reporter.emit(record, status=terminal, error_code=code)
            await drain(reporter)
            final = json.loads(await client.get(run_key))
            assert final['status'] == terminal
            assert final.get('errorCode') == code
            assert final['secretRealmCompletedAt'] == first
            assert final['finishedAt'] is not None
        finally:
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
        load_config({**OSS_ENV, **env})

def test_execution_history_defaults_to_seven_days():
    assert ServiceConfig().run_log_retention_seconds == 604_800
    assert load_config({**OSS_ENV}).run_log_retention_seconds == 604_800
