"""Battle checkpoints refresh state without publishing repeated public logs."""

import asyncio
from dataclasses import replace
import json

import pytest

from naruto_daily.login_service.queue import RedisTaskQueue
from test_login_service_run_logs import claim, config, drain, fake_client, keys, make_reporter


def test_checkpoint_refreshes_existing_snapshot_without_advancing_log_cursor(monkeypatch):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        reporter.start()
        try:
            reporter.emit(record, status="running_daily", message="动作 2/30：战斗", action_index=2, action_total=30)
            await drain(reporter)
            run_key = keys(cfg, record)[0]
            before = json.loads(await client.get(run_key))
            events = list(reporter.log_publisher.events[record.id])
            stamp = "2026-10-22T08:00:00.000Z"
            monkeypatch.setattr("naruto_daily.login_service.run_logs.iso_time", lambda: stamp)
            reporter.refresh(record)
            await drain(reporter)
            after = json.loads(await client.get(run_key))
            assert after["sequence"] > before["sequence"]
            assert after == before | {"sequence": after["sequence"], "updatedAt": stamp}
            assert reporter.log_publisher.events[record.id] == events
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_checkpoint_discovers_admin_interruption_without_a_new_public_log():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        calls = []
        reporter = make_reporter(cfg, queue, on_interrupted=lambda *args: calls.append(args))
        reporter.start()
        try:
            reporter.emit(record, status="running_daily")
            await drain(reporter)
            run_key = keys(cfg, record)[0]
            interrupted = json.loads(await client.get(run_key)) | {"status": "interrupted", "errorCode": "admin_interrupted"}
            encoded = json.dumps(interrupted)
            await client.set(run_key, encoded, keepttl=True)
            ttl = await client.pttl(run_key)
            events = list(reporter.log_publisher.events[record.id])
            reporter.refresh(record)
            await drain(reporter)
            assert calls == [(record.id, record.attempt_id)]
            assert await client.get(run_key) == encoded
            assert 0 < await client.pttl(run_key) <= ttl
            assert reporter.log_publisher.events[record.id] == events
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["missing", "stale_attempt", "terminal", "interrupted", "finished", "closed"])
def test_checkpoint_cannot_create_reopen_or_refresh_another_execution(case):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        reporter.start()
        try:
            if case != "missing":
                reporter.emit(record, status="completed" if case == "terminal" else "running_daily")
                await drain(reporter)
            if case == "stale_attempt":
                record = replace(record, attempt_id="old-attempt")
            elif case == "interrupted":
                record._admin_interrupted = True
            elif case == "finished":
                reporter.finish(record.id)
            elif case == "closed":
                await reporter.close()
            run_key = keys(cfg, record)[0]
            before = await client.get(run_key)
            events = list(reporter.log_publisher.events.get(record.id, []))
            reporter.refresh(record)
            assert not reporter._pending.get(record.id)
            assert await client.get(run_key) == before
            assert reporter.log_publisher.events.get(record.id, []) == events
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_final_outcome_waits_for_an_inflight_checkpoint_and_is_not_lost(outcome, monkeypatch):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        entered, release = asyncio.Event(), asyncio.Event()
        reporter.start()
        original = queue._call
        try:
            reporter.emit(record, status="running_daily")
            await drain(reporter)

            async def paused_call(*args, **kwargs):
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(queue, "_call", paused_call)
            reporter.refresh(record)
            await asyncio.wait_for(entered.wait(), 2)
            reporter.emit(record, status=outcome)
            final_cursor = reporter.log_publisher.events[record.id][-1][0]
            flushing = asyncio.create_task(reporter.flush(record.id))
            await asyncio.sleep(0.05)
            assert not flushing.done()
            release.set()
            assert await flushing
            state = json.loads(await client.get(keys(cfg, record)[0]))
            assert state["status"] == outcome
            assert state["logCursor"] == final_cursor
        finally:
            release.set()
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())
