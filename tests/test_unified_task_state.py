"""The website and worker share one retained task record (isolated Redis only)."""

import asyncio
import json
import time
from uuid import uuid4

import pytest

from test_login_service_run_logs import config, fake_client, body, make_reporter, drain
from test_login_service_worker import MemoryStorage
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.runtime import LoginRuntime


async def unified_claim(client, queue):
    message = body() | {"loginTimeoutSeconds": 600, "userOptions": {"mainTask": "arena_weekly"}}
    run = {
        "id": message["id"], "taskId": message["id"], "userId": str(uuid4()),
        "createdAt": message["createdAt"], "expiresAt": message["expiresAt"],
        "simulation": True, "tokenHash": "ab" * 32, "status": "queued",
        "userOptions": {"mainTask": "arena_weekly"}, "loginType": "android_qq",
        "refreshCount": 2, "qrRevision": "original", "websiteMetadata": "preserve",
        "startedAt": None, "finishedAt": None,
    }
    prefix = queue.config.redis_queue_key
    key = f"{prefix}:runs:{message['id']}"
    active = f"{prefix}:users:{run['userId']}:active"
    await client.set(key, json.dumps(run), ex=604800)
    await client.set(active, message["id"], ex=604800)
    await client.lpush(prefix, json.dumps(message))
    return await queue.pop(), key, active, run


def test_single_run_claim_and_progress_preserve_identity_and_qr_fields():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        reporter = make_reporter(cfg, queue)
        try:
            record, key, active, original = await unified_claim(client, queue)
            assert record is not None
            assert await client.ttl(active) >= 604799
            assert await queue.set_status(record, "qr_ready")
            reporter.start()
            reporter.emit(record, status="waiting_scan")
            await drain(reporter)
            saved = json.loads(await client.get(key))
            for field in original.keys() - {"status"}:
                assert saved[field] == original[field]
            assert saved["status"] == "waiting_scan"
            assert not await client.keys(f"{cfg.redis_queue_key}:sessions:*")
        finally:
            await reporter.close(timeout=.1)
            await client.aclose()
    asyncio.run(scenario())


def test_authorized_execution_and_terminal_survive_login_deadline_with_attempt_fence():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        try:
            record, key, active, _ = await unified_claim(client, queue)
            assert record is not None
            assert await queue.set_status(record, "qr_ready")
            record.authorized_at = time.time()
            record.execution_deadline = time.time() + 2400
            assert await queue.mark_authorized(record)
            saved = json.loads(await client.get(key))
            assert saved["status"] == "finishing_login"
            assert saved["executionDeadline"] == record.execution_deadline
            record.expires_at_ms = time.time() * 1000 - 1
            assert await queue.set_execution_status(record, "running_daily")
            record.daily_error_code = "daily_failed"
            assert await queue.set_execution_status(record, "failed")
            assert json.loads(await client.get(key))["status"] == "failed"
            assert await client.get(active) is None
            record.attempt_id = "stale-attempt"
            assert not await queue.set_execution_status(record, "completed")
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_reporter_cannot_erase_terminal_or_admin_interruption():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        reporter = make_reporter(cfg, queue)
        try:
            record, key, _, _ = await unified_claim(client, queue)
            assert record is not None
            assert await queue.set_execution_status(record, "completed")
            reporter.start()
            reporter.emit(record, status="running_daily")
            await drain(reporter)
            assert json.loads(await client.get(key))["status"] == "completed"
            saved = json.loads(await client.get(key)) | {"status": "interrupted", "errorCode": "admin_interrupted"}
            await client.set(key, json.dumps(saved), keepttl=True)
            assert not await queue.set_execution_status(record, "failed")
            assert json.loads(await client.get(key))["errorCode"] == "admin_interrupted"
        finally:
            await reporter.close(timeout=.1)
            await client.aclose()
    asyncio.run(scenario())


def test_late_authorization_cannot_reclaim_another_tasks_active_index():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        try:
            record, key, active, _ = await unified_claim(client, queue)
            assert await queue.set_status(record, 'qr_ready')
            record.authorized_at = time.time()
            record.execution_deadline = time.time() + 2400
            before = await client.get(key)
            newer = str(uuid4())
            await client.set(active, newer, ex=604800)
            assert not await queue.mark_authorized(record)
            assert await client.get(key) == before
            assert await client.get(active) == newer
            await client.delete(active)
            assert not await queue.mark_authorized(record)
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['completed', 'failed', 'interrupted', 'expired'])
def test_runtime_writes_terminal_without_reporter_after_scan_deadline(status):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue)
        runtime._run_reporter = None
        try:
            record, key, active, _ = await unified_claim(client, queue)
            record.expires_at_ms = 1
            record.daily_status = status
            await runtime._finish_failed_session(record)
            saved = json.loads(await client.get(key))
            assert saved['status'] == status and saved['finishedAt']
            assert await client.get(active) is None
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_expired_queued_run_is_retained_as_terminal_and_only_its_lock_is_released():
    async def scenario():
        from datetime import datetime, timedelta, timezone
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        try:
            message = body()
            message['expiresAt'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            run = {k: message[k] for k in ('id', 'createdAt', 'expiresAt')}
            run.update(taskId=message['id'], userId=str(uuid4()), simulation=True, tokenHash='ab'*32, status='queued')
            key = f"{cfg.redis_queue_key}:runs:{message['id']}"
            active = f"{cfg.redis_queue_key}:users:{run['userId']}:active"
            await client.set(key, json.dumps(run), ex=604800)
            await client.set(active, message['id'], ex=604800)
            await client.lpush(cfg.redis_queue_key, json.dumps(message))
            assert await queue.pop() is None
            saved = json.loads(await client.get(key))
            assert saved['status'] == 'expired' and saved['finishedAt']
            assert saved['tokenHash'] == run['tokenHash']
            assert await client.get(active) is None
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_buffered_terminal_cannot_finish_before_cleanup_commits_outcome():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        reporter = make_reporter(cfg, queue)
        try:
            record, key, _, _ = await unified_claim(client, queue)
            reporter.start()
            reporter.emit(record, status='completed')
            await drain(reporter)
            assert json.loads(await client.get(key))['status'] == 'starting_emulator'
            record.daily_error_code = 'instance_cleanup_failed'
            assert await queue.set_execution_status(record, 'failed')
            assert json.loads(await client.get(key))['errorCode'] == 'instance_cleanup_failed'
        finally:
            await reporter.close(timeout=.1)
            await client.aclose()
    asyncio.run(scenario())


def test_progress_renews_only_its_active_index_to_match_retained_run():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        reporter = make_reporter(cfg, queue)
        try:
            record, key, active, _ = await unified_claim(client, queue)
            await client.expire(active, 10)
            reporter.start()
            reporter.emit(record, status='running_daily')
            await drain(reporter)
            assert abs(await client.ttl(key) - await client.ttl(active)) <= 1
            other = str(uuid4())
            await client.set(active, other, ex=10)
            reporter.emit(record, message='later checkpoint')
            await drain(reporter)
            assert await client.get(active) == other
            assert await client.ttl(active) <= 10
        finally:
            await reporter.close(timeout=.1)
            await client.aclose()
    asyncio.run(scenario())


def test_preparation_failure_is_visible_with_its_code_before_async_reporter():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        try:
            record, key, _, _ = await unified_claim(client, queue)
            record.daily_error_code = 'unexpected_error'
            assert await queue.set_status(record, 'failed')
            saved = json.loads(await client.get(key))
            assert saved['status'] == 'failed'
            assert saved['errorCode'] == 'unexpected_error' and saved['finishedAt']
        finally:
            await client.aclose()
    asyncio.run(scenario())
