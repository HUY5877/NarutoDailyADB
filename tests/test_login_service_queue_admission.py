"""Timestamp eligibility must be checked before an atomic FIFO claim."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.login_service import models
from naruto_daily.login_service.queue import RedisQueueError, RedisTaskQueue, _CLAIM_SCRIPT


CONFIG = SimpleNamespace(redis_url='redis://unused.invalid', redis_queue_key='test:admission', key_prefix='login')


def fake_client():
    fakeredis = pytest.importorskip('fakeredis.aioredis')
    pytest.importorskip('lupa')
    return fakeredis.FakeRedis(decode_responses=True)


async def enqueue(client, *, created_delta=0, ttl=60):
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    body = {'version': 1, 'id': task_id, 'objectKey': f'login/{task_id}/qr.png',
            'createdAt': (now + timedelta(seconds=created_delta)).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'expiresAt': (now + timedelta(seconds=ttl)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')}
    key = f'{CONFIG.redis_queue_key}:sessions:{task_id}'
    session = {**{k: body[k] for k in ('id', 'createdAt', 'expiresAt')},
               'status': 'queued', 'simulation': True, 'tokenHash': 'ab' * 32}
    payload = json.dumps(body)
    await client.set(key, json.dumps(session), px=60_000)
    await client.lpush(CONFIG.redis_queue_key, payload)
    return body, key, payload


def test_two_consumers_peeking_same_head_cannot_pop_the_next_task():
    async def scenario():
        client = fake_client()
        try:
            first, first_key, _ = await enqueue(client)
            second, second_key, second_payload = await enqueue(client)
            both_ready = asyncio.Event()

            class Transport:
                claims = 0

                def __getattr__(self, name):
                    return getattr(client, name)

                async def eval(self, script, *args):
                    if script == _CLAIM_SCRIPT and self.claims < 2:
                        self.claims += 1
                        if self.claims == 2:
                            both_ready.set()
                        await both_ready.wait()
                    return await client.eval(script, *args)

            transport = Transport()
            workers = [RedisTaskQueue(CONFIG, client=transport) for _ in range(2)]
            records = await asyncio.wait_for(asyncio.gather(*(worker.pop() for worker in workers)), 2)
            assert [record.id for record in records if record] == [first['id']]
            assert json.loads(await client.get(first_key))['status'] == 'starting_emulator'
            assert json.loads(await client.get(second_key))['status'] == 'queued'
            assert await client.lrange(CONFIG.redis_queue_key, 0, -1) == [second_payload]
            assert (await workers[0].pop()).id == second['id']
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_invalid_head_cleanup_cannot_remove_another_consumers_next_task():
    async def scenario():
        client = fake_client()
        try:
            await client.lpush(CONFIG.redis_queue_key, '{')
            body, key, payload = await enqueue(client)

            class Transport:
                def __getattr__(self, name):
                    return getattr(client, name)

                async def lindex(self, *args):
                    value = await client.lindex(*args)
                    assert value == '{'
                    await client.rpop(CONFIG.redis_queue_key)  # Another worker removed the bad head.
                    return value

            assert await RedisTaskQueue(CONFIG, client=Transport()).pop() is None
            assert await client.lrange(CONFIG.redis_queue_key, 0, -1) == [payload]
            assert json.loads(await client.get(key))['status'] == 'queued'
            assert (await RedisTaskQueue(CONFIG, client=client).pop()).id == body['id']
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('missing_session', [False, True])
def test_only_redis_expired_or_missing_sessions_are_pruned_without_blocking_next_task(missing_session):
    async def scenario():
        client = fake_client()
        try:
            stale, stale_key, _ = await enqueue(client, ttl=120 if missing_session else -1, created_delta=60 if missing_session else 0)
            if missing_session:
                await client.delete(stale_key)
            live, _, payload = await enqueue(client)
            queue = RedisTaskQueue(CONFIG, client=client)
            assert await queue.pop() is None
            assert await client.lrange(CONFIG.redis_queue_key, 0, -1) == [payload]
            assert await client.exists(f'{CONFIG.redis_queue_key}:runs:{stale["id"]}') == 0
            assert (await queue.pop()).id == live['id']
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('clock_change', [False, True])
def test_atomic_claim_rechecks_redis_time_and_current_local_clock(monkeypatch, clock_change):
    async def scenario():
        client = fake_client()
        try:
            _, key, payload = await enqueue(client, created_delta=0 if clock_change else 30)
            original = await client.get(key)
            actual = time.time()
            times = iter([actual, actual + 120]) if clock_change else iter([actual + 40] * 2)
            monkeypatch.setattr(models, 'time', SimpleNamespace(time=lambda: next(times)))
            monkeypatch.setattr('naruto_daily.login_service.queue.QUEUE_POLL_SECONDS', 0.001)
            assert await RedisTaskQueue(CONFIG, client=client).pop() is None
            assert await client.get(key) == original
            assert await client.lrange(CONFIG.redis_queue_key, 0, -1) == [payload]
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_retained_task_can_be_claimed_after_local_time_becomes_eligible(monkeypatch, caplog):
    async def scenario():
        client = fake_client()
        try:
            body, key, payload = await enqueue(client)
            actual = time.time()
            local = [actual - 13]
            monkeypatch.setattr(models, 'time', SimpleNamespace(time=lambda: local[0]))
            monkeypatch.setattr('naruto_daily.login_service.queue.QUEUE_POLL_SECONDS', 0.001)
            queue = RedisTaskQueue(CONFIG, client=client)
            ttl = await client.pttl(key)
            assert await queue.pop() is None
            assert await queue.pop() is None
            assert await client.lrange(CONFIG.redis_queue_key, 0, -1) == [payload]
            local[0] = actual
            assert (await queue.pop()).id == body['id']
            assert 0 < await client.pttl(key) <= ttl
            assert await client.llen(CONFIG.redis_queue_key) == 0
            assert sum('left queued' in record.message for record in caplog.records) == 1
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_cancelling_a_time_ineligible_consumer_preserves_task_and_session(monkeypatch):
    async def scenario():
        client = fake_client()
        try:
            _, key, payload = await enqueue(client)
            original = await client.get(key)
            actual = time.time()
            monkeypatch.setattr(models, 'time', SimpleNamespace(time=lambda: actual - 25))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(RedisTaskQueue(CONFIG, client=client).pop(), 0.05)
            assert await client.get(key) == original
            assert await client.lrange(CONFIG.redis_queue_key, 0, -1) == [payload]
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('lost_reply', [False, True])
def test_claim_transport_failure_never_pops_a_second_task_or_retries_blindly(lost_reply):
    async def scenario():
        client = fake_client()
        try:
            _, first_key, first_payload = await enqueue(client)
            _, second_key, second_payload = await enqueue(client)

            class Transport:
                claims = 0

                def __getattr__(self, name):
                    return getattr(client, name)

                async def eval(self, script, *args):
                    assert script == _CLAIM_SCRIPT
                    self.claims += 1
                    if lost_reply:
                        await client.eval(script, *args)
                    raise ConnectionError('redis://private-user:secret@host')

            transport = Transport()
            with pytest.raises(RedisQueueError, match='^Redis operation failed$'):
                await RedisTaskQueue(CONFIG, client=transport).pop()
            assert transport.claims == 1
            assert json.loads(await client.get(first_key))['status'] == ('starting_emulator' if lost_reply else 'queued')
            assert json.loads(await client.get(second_key))['status'] == 'queued'
            assert await client.lrange(CONFIG.redis_queue_key, 0, -1) == ([second_payload] if lost_reply else [second_payload, first_payload])
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_empty_queue_yields_instead_of_busy_polling():
    async def scenario():
        client = fake_client()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(RedisTaskQueue(CONFIG, client=client).pop(), 0.05)
            assert await client.keys('*') == []
        finally:
            await client.aclose()
    asyncio.run(scenario())
