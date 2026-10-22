"""Exercise administrator interruptions against the actual Lua in fake Redis."""

import asyncio
import json

import pytest

from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.run_logs import RedisRunReporter
from test_login_service_run_logs import claim, config, fake_client, keys


async def claimed_attempt(client, queue):
    record = await claim(client, queue)
    assert isinstance(record.attempt_id, str) and record.attempt_id
    return record


def reporter_with_callback(cfg, queue, calls):
    return RedisRunReporter(
        cfg, queue, on_interrupted=lambda task_id, attempt_id: calls.append((task_id, attempt_id)),
    )


def pending_update(reporter, record, status, message):
    reporter.emit(record, status=status, message=message)
    return reporter._pending[record.id][-1]


async def admin_interrupt(client, run_key):
    state = json.loads(await client.get(run_key))
    state["status"] = "interrupted"  # Admin changes no owner, attempt or sequence.
    encoded = json.dumps(state)
    await client.set(run_key, encoded, keepttl=True)
    return encoded


async def close_reporters(client, *reporters):
    for reporter in reporters:
        reporter._pending.clear()
        await reporter.close(timeout=0.1)
    await client.aclose()


def test_reporter_accepts_optional_synchronous_interruption_callback():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        reporter = None
        try:
            reporter = RedisRunReporter(cfg, queue, on_interrupted=lambda task_id, attempt_id: None)
        finally:
            await close_reporters(client, *([reporter] if reporter is not None else []))
    asyncio.run(scenario())


def test_each_claim_generates_a_fresh_attempt_and_stores_it_in_the_session():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        try:
            first = await claim(client, queue)
            first_attempt = getattr(first, "attempt_id", None)
            assert isinstance(first_attempt, str) and first_attempt
            session_key = keys(cfg, first)[2]
            assert json.loads(await client.get(session_key))["attemptId"] == first_attempt

            assert await queue.requeue(first)
            second = await queue.pop()
            assert second is not None and second.id == first.id
            second_attempt = getattr(second, "attempt_id", None)
            assert isinstance(second_attempt, str) and second_attempt != first_attempt
            assert json.loads(await client.get(session_key))["attemptId"] == second_attempt
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_reporter_snapshot_carries_the_claim_attempt_identifier():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claimed_attempt(client, queue)
        reporter = RedisRunReporter(cfg, queue)
        try:
            update = pending_update(reporter, record, "running_daily", "日常执行中")
            assert update.snapshot.get("attemptId") == record.attempt_id
        finally:
            await close_reporters(client, reporter)
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["running_daily", "completed", "failed"])
@pytest.mark.parametrize("session_expired", [False, True])
def test_admin_interrupt_between_await_and_eval_rejects_already_built_updates(status, session_expired):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claimed_attempt(client, queue)
        calls = []
        reporter = reporter_with_callback(cfg, queue, calls)
        started, resume = asyncio.Event(), asyncio.Event()
        writing = None

        class PausedTransport:
            async def eval(self, *args):
                started.set()
                await resume.wait()
                return await client.eval(*args)

        try:
            initial = pending_update(reporter, record, "running_daily", "开始日常")
            await reporter._write(record.id, initial)
            incoming = pending_update(reporter, record, status, "中断前已构建的后续更新")
            run_key, _, session_key = keys(cfg, record)
            queue._client = PausedTransport()
            writing = asyncio.create_task(reporter._write(record.id, incoming))
            await asyncio.wait_for(started.wait(), timeout=2)

            interrupted = await admin_interrupt(client, run_key)
            if session_expired:
                await client.delete(session_key)
            resume.set()
            await writing

            assert await client.get(run_key) == interrupted
            assert calls == [(record.id, record.attempt_id)]
            await reporter._write(record.id, incoming)
            again = pending_update(reporter, record, status, "管理员中断后的迟到更新")
            await reporter._write(record.id, again)
            assert await client.get(run_key) == interrupted
            assert calls == [(record.id, record.attempt_id)]
        finally:
            resume.set()
            if writing is not None:
                await asyncio.gather(writing, return_exceptions=True)
            await close_reporters(client, reporter)
    asyncio.run(scenario())


@pytest.mark.parametrize("session_expired", [False, True])
def test_status_only_admin_change_is_detected_even_when_retry_sequence_is_unchanged(session_expired):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claimed_attempt(client, queue)
        calls = []
        reporter = reporter_with_callback(cfg, queue, calls)
        try:
            update = pending_update(reporter, record, "running_daily", "日常执行中")
            await reporter._write(record.id, update)
            run_key, _, session_key = keys(cfg, record)
            interrupted = await admin_interrupt(client, run_key)
            assert json.loads(interrupted)["sequence"] == update.snapshot["sequence"]
            if session_expired:
                await client.delete(session_key)

            await reporter._write(record.id, update)
            await reporter._write(record.id, update)

            assert await client.get(run_key) == interrupted
            assert calls == [(record.id, record.attempt_id)]
        finally:
            await close_reporters(client, reporter)
    asyncio.run(scenario())


def test_worker_completed_before_admin_interrupt_cannot_overwrite_the_later_stop():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claimed_attempt(client, queue)
        calls = []
        reporter = reporter_with_callback(cfg, queue, calls)
        try:
            completed = pending_update(reporter, record, "completed", "执行完毕")
            await reporter._write(record.id, completed)
            run_key = keys(cfg, record)[0]
            assert json.loads(await client.get(run_key))["status"] == "completed"
            interrupted = await admin_interrupt(client, run_key)

            failed = pending_update(reporter, record, "failed", "清理后产生的迟到失败")
            await reporter._write(record.id, failed)

            assert await client.get(run_key) == interrupted
            assert calls == [(record.id, record.attempt_id)]
        finally:
            await close_reporters(client, reporter)
    asyncio.run(scenario())


def test_workers_own_repeated_interrupted_updates_do_not_trigger_admin_callback():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claimed_attempt(client, queue)
        calls = []
        reporter = reporter_with_callback(cfg, queue, calls)
        try:
            running = pending_update(reporter, record, "running_daily", "日常执行中")
            await reporter._write(record.id, running)
            stopped = pending_update(reporter, record, "interrupted", "执行进程主动退出")
            await reporter._write(record.id, stopped)
            await reporter._write(record.id, stopped)
            repeated = pending_update(reporter, record, "interrupted", "重复提交主动退出")
            await reporter._write(record.id, repeated)

            assert json.loads(await client.get(keys(cfg, record)[0]))["status"] == "interrupted"
            assert calls == []
        finally:
            await close_reporters(client, reporter)
    asyncio.run(scenario())


@pytest.mark.parametrize("session_expired", [False, True])
@pytest.mark.parametrize("new_attempt_interrupted", [False, True])
def test_new_attempt_can_restart_but_stale_same_worker_attempt_cannot_write_or_interrupt(
    session_expired, new_attempt_interrupted,
):
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        first = await claimed_attempt(client, queue)
        old_calls, new_calls = [], []
        old_reporter = reporter_with_callback(cfg, queue, old_calls)
        new_reporter = reporter_with_callback(cfg, queue, new_calls)
        try:
            old_initial = pending_update(old_reporter, first, "running_daily", "旧执行开始")
            await old_reporter._write(first.id, old_initial)
            run_key, _, session_key = keys(cfg, first)
            await admin_interrupt(client, run_key)
            # A buffered old update has a larger sequence than a fresh reporter.
            for index in range(3):
                stale = pending_update(old_reporter, first, "completed", f"旧执行迟到更新 {index}")

            assert await queue.requeue(first)
            second = await queue.pop()
            assert second is not None and second.id == first.id
            assert second.attempt_id != first.attempt_id

            restarted = pending_update(new_reporter, second, "running_daily", "重排后的新执行")
            await new_reporter._write(second.id, restarted)
            state = json.loads(await client.get(run_key))
            assert state["status"] == "running_daily"
            assert state["attemptId"] == second.attempt_id
            if new_attempt_interrupted:
                await admin_interrupt(client, run_key)
            before = await client.get(run_key)
            if session_expired:
                await client.delete(session_key)

            await old_reporter._write(first.id, stale)
            assert await client.get(run_key) == before
            assert old_calls == [] and new_calls == []
        finally:
            await close_reporters(client, old_reporter, new_reporter)
    asyncio.run(scenario())
