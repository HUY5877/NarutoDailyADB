"""领取标识由 worker 生成，每次重新领取都隔离旧执行。"""

import asyncio
from dataclasses import replace
import json
import re

import pytest

from naruto_daily.login_service.models import parse_queue_task
from naruto_daily.login_service.queue import RedisTaskQueue
from test_login_service_run_logs import body, config, fake_client


@pytest.mark.parametrize("operation", ["set_status", "mark_authorized", "finish_failed"])
def test_stale_attempt_cannot_change_a_reclaimed_session(operation):
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        try:
            key = await enqueue(client, queue, body())
            old = await queue.pop()
            assert await queue.requeue(old)
            current = await queue.pop()
            assert current.attempt_id != old.attempt_id
            before = await client.get(key)
            if operation == "set_status":
                changed = await queue.set_status(old, "opening_game")
            else:
                changed = await getattr(queue, operation)(old)
            assert not changed
            assert await client.get(key) == before
        finally:
            await queue.close()
    asyncio.run(scenario())


async def enqueue(client, queue, message, *, session_changes=None):
    session = {
        "id": message["id"], "createdAt": message["createdAt"],
        "expiresAt": message["expiresAt"], "simulation": True,
        "tokenHash": "ab" * 32, "status": "queued",
    }
    session.update(session_changes or {})
    key = f"{queue.config.redis_queue_key}:sessions:{message['id']}"
    await client.set(key, json.dumps(session), px=30_000)
    await client.lpush(queue.config.redis_queue_key, json.dumps(message))
    return key


def test_same_worker_reclaim_generates_a_new_attempt_and_keeps_the_owner(monkeypatch):
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        attempts = iter(["11" * 16, "22" * 16])
        sizes = []

        def next_attempt(size):
            sizes.append(size)
            return next(attempts)

        monkeypatch.setattr("naruto_daily.login_service.queue.secrets.token_hex", next_attempt)
        message = body()
        try:
            key = await enqueue(client, queue, message)
            first = await queue.pop()
            assert first is not None and first.attempt_id == "11" * 16
            first_session = json.loads(await client.get(key))
            assert first_session["attemptId"] == first.attempt_id
            assert first_session["workerId"] == queue.owner

            assert await queue.requeue(first)
            second = await queue.pop()
            assert second is not None and second.id == first.id
            assert second.attempt_id == "22" * 16
            second_session = json.loads(await client.get(key))
            assert second_session["attemptId"] == second.attempt_id
            assert second_session["workerId"] == first_session["workerId"] == queue.owner
            assert second_session["tokenHash"] == first_session["tokenHash"]
            assert 0 < await client.pttl(key) <= 30_000
            assert sizes == [16, 16]
        finally:
            await queue.close()

    asyncio.run(scenario())


def test_different_tasks_get_different_attempts():
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        try:
            first_key = await enqueue(client, queue, body())
            second_key = await enqueue(client, queue, body())
            first, second = await queue.pop(), await queue.pop()
            assert first is not None and second is not None and first.id != second.id
            assert re.fullmatch(r"[0-9a-f]{32}", first.attempt_id)
            assert re.fullmatch(r"[0-9a-f]{32}", second.attempt_id)
            assert first.attempt_id != second.attempt_id
            assert json.loads(await client.get(first_key))["attemptId"] == first.attempt_id
            assert json.loads(await client.get(second_key))["attemptId"] == second.attempt_id
        finally:
            await queue.close()

    asyncio.run(scenario())


def test_queue_payload_cannot_supply_private_attempt_or_interrupt_fields():
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        message = body() | {
            "attemptId": "external-attempt", "attempt_id": "external-attempt",
            "_admin_interrupted": True,
        }
        parsed = parse_queue_task(message, queue.config.key_prefix)
        assert parsed.attempt_id is None and parsed._admin_interrupted is False
        try:
            key = await enqueue(client, queue, message, session_changes={"attemptId": "external-attempt"})
            record = await queue.pop()
            assert record is not None and re.fullmatch(r"[0-9a-f]{32}", record.attempt_id)
            assert record.attempt_id != "external-attempt"
            assert record._admin_interrupted is False
            assert json.loads(await client.get(key))["attemptId"] == record.attempt_id
        finally:
            await queue.close()

    asyncio.run(scenario())


def test_failed_claim_does_not_replace_the_existing_attempt():
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        try:
            key = await enqueue(client, queue, body(), session_changes={
                "status": "opening_game", "workerId": "other-worker", "attemptId": "existing-attempt",
            })
            original = await client.get(key)

            assert await queue.pop() is None

            assert await client.get(key) == original
        finally:
            await queue.close()

    asyncio.run(scenario())


def test_claim_still_restores_the_previous_sls_cursor():
    async def scenario():
        client = fake_client()
        queue = RedisTaskQueue(config(), client=client)
        observed = []

        class Publisher:
            def observe_cursor(self, cursor, *, task_id):
                observed.append((cursor, task_id))

        queue.log_publisher = Publisher()
        message = body()
        cursor = "1800000000000-3"
        try:
            key = await enqueue(client, queue, message)
            await client.set(f"{queue.config.redis_queue_key}:runs:{message['id']}", json.dumps({
                "taskId": message["id"], "status": "interrupted", "logCursor": cursor,
            }))

            record = await queue.pop()

            assert record is not None and re.fullmatch(r"[0-9a-f]{32}", record.attempt_id)
            assert json.loads(await client.get(key))["attemptId"] == record.attempt_id
            assert observed == [(cursor, message["id"])]
        finally:
            await queue.close()

    asyncio.run(scenario())


def test_attempt_and_interrupt_fields_stay_private_and_do_not_change_record_equality():
    record = parse_queue_task(body(), "login")
    changed = replace(record, attempt_id="private-attempt", _admin_interrupted=True)

    assert record.attempt_id is None and record._admin_interrupted is False
    assert changed == record
    assert "attempt_id" not in repr(changed)
    assert "_admin_interrupted" not in repr(changed)
    assert changed.to_view() == record.to_view()
    assert not {"attempt_id", "attemptId", "_admin_interrupted"}.intersection(changed.to_view())
