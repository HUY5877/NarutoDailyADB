"""扫码捕获只结束刷新等待，不提前确认登录或续期会话。"""

import asyncio
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from naruto_daily.login_service.queue import RedisTaskQueue
from test_login_service_redis import config, enqueue, fake_client, session_body, task_body


@pytest.mark.parametrize('lease', [False, True])
@pytest.mark.parametrize('status', ['preparing_qr', 'qr_ready'])
def test_scan_capture_clears_only_its_refresh_without_authorizing_or_extending_lease(lease, status):
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        try:
            body = task_body() | ({'loginTimeoutSeconds': 600} if lease else {})
            session = session_body(body) | {'userId': str(uuid4())}
            prefix = config().redis_queue_key
            active_key = f"{prefix}:users:{session['userId']}:active"
            await client.set(active_key, body['id'], ex=60)
            key, _ = await enqueue(client, body, session=session)
            record = await queue.pop()
            assert record is not None
            request_id = str(uuid4())
            saved = json.loads(await client.get(key))
            saved.update(status=status, refreshRequestId=request_id)
            await client.set(key, json.dumps(saved), keepttl=True)
            refresh_key = f'{prefix}:refresh:{record.id}'
            other_refresh_key = f'{prefix}:refresh:{uuid4()}'
            await client.set(refresh_key, json.dumps({'taskId': record.id, 'requestId': request_id}), ex=60)
            await client.set(other_refresh_key, 'another task', ex=60)
            before_ttl = await client.pttl(key)
            before_active_ttl = await client.pttl(active_key)
            deadline = record.expires_at, record.expires_at_ms

            assert await queue.mark_scan_captured(record) is True
            assert await queue.mark_scan_captured(record) is True, 'lost acknowledgement can be retried'

            expected = saved | {'scanCaptured': True}
            del expected['refreshRequestId']
            assert json.loads(await client.get(key)) == expected
            assert await client.get(refresh_key) is None
            assert await client.get(other_refresh_key) == 'another task'
            assert not await queue.pending_refresh(record, request_id)
            assert 0 < await client.pttl(key) <= before_ttl
            assert await client.get(active_key) == record.id
            assert 0 < await client.pttl(active_key) <= before_active_ttl
            assert (record.expires_at, record.expires_at_ms) == deadline
            assert await client.exists(f'{prefix}:runs:{record.id}') == 0
        finally:
            await queue.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('damage', [
    'foreign_worker', 'missing_attempt', 'stale_attempt', 'missing', 'no_ttl',
    'semantic_expiry', 'failed', 'expired', 'identity',
])
def test_scan_capture_rejects_invalid_or_finished_sessions_without_clearing_refresh(damage):
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        try:
            body = task_body()
            key, _ = await enqueue(client, body)
            record = current = await queue.pop()
            assert record is not None
            if damage == 'stale_attempt':
                assert await queue.requeue(record)
                current = await queue.pop()
                assert current.attempt_id != record.attempt_id
            assert await queue.set_status(current, 'qr_ready')
            request_id = str(uuid4())
            saved = json.loads(await client.get(key)) | {'refreshRequestId': request_id}
            if damage in {'failed', 'expired'}:
                saved['status'] = damage
            if damage == 'identity':
                saved['createdAt'] = '2000-01-01T00:00:00Z'
            await client.set(key, json.dumps(saved), keepttl=True)
            if damage == 'missing':
                await client.delete(key)
            if damage == 'no_ttl':
                await client.persist(key)
            if damage == 'semantic_expiry':
                record = replace(record, expires_at_ms=1)
            if damage == 'missing_attempt':
                record = replace(record, attempt_id=None)
            caller = RedisTaskQueue(config(), client=client) if damage == 'foreign_worker' else queue
            refresh_key = f'{config().redis_queue_key}:refresh:{record.id}'
            command = json.dumps({'taskId': record.id, 'requestId': request_id})
            await client.set(refresh_key, command, ex=60)
            before = await client.get(key)

            assert await caller.mark_scan_captured(record) is False

            assert await client.get(key) == before
            assert await client.get(refresh_key) == command
        finally:
            await queue.close()

    asyncio.run(scenario())
