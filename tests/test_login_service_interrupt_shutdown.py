"""中断清理不能跨执行领取，也必须先于 Redis 连接关闭。"""

import asyncio
import json

from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.run_logs import RedisRunReporter, _PUBLISH
from naruto_daily.login_service.runtime import LoginRuntime
from test_login_service_run_logs import MemoryStorage, claim, config, fake_client, keys


def test_old_attempt_failure_cleanup_cannot_end_reclaimed_session_or_release_its_account():
    async def scenario():
        client = fake_client()
        cfg = config()
        queue = RedisTaskQueue(cfg, client=client)
        try:
            previous = await claim(client, queue)
            session_key = keys(cfg, previous)[2]
            session = json.loads(await client.get(session_key))
            active_key = f"{cfg.redis_queue_key}:users:{session['userId']}:active"
            await client.set(active_key, previous.id, px=30_000)

            assert await queue.requeue(previous)
            current = await queue.pop()
            assert current is not None and current.id == previous.id
            assert current.attempt_id != previous.attempt_id
            original_session = await client.get(session_key)
            original_lock = await client.get(active_key)
            previous.daily_error_code = "admin_interrupted"

            changed = await queue.finish_failed(previous)

            assert not changed, "旧执行的迟到清理不能结束同 worker 重新领取的新执行"
            assert await client.get(session_key) == original_session
            assert await client.get(active_key) == original_lock == current.id
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_shutdown_waits_for_interruption_cleanup_created_while_reporter_is_closing(monkeypatch):
    async def scenario():
        client = fake_client()
        cfg = config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = RedisRunReporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)
        runtime._tasks[record.id] = record
        record._instance_released = True
        run_key, _, session_key = keys(cfg, record)
        session = json.loads(await client.get(session_key))
        active_key = f"{cfg.redis_queue_key}:users:{session['userId']}:active"
        await client.set(active_key, record.id, px=30_000)
        reporter.emit(record, status="running_daily")
        await reporter._write(record.id, reporter._pending[record.id][0])
        state = json.loads(await client.get(run_key))
        state["status"] = "interrupted"
        await client.set(run_key, json.dumps(state), keepttl=True)

        closing = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        events = []
        original_reporter_close = reporter.close
        original_finish_failed = queue.finish_failed
        original_queue_close = queue.close

        class DelayedTransport:
            def __getattr__(self, name):
                return getattr(client, name)

            async def eval(self, *args):
                if args[0] == _PUBLISH:
                    # Last buffered progress only discovers interruption in
                    # reporter.close's drain, after the normal game cleanup.
                    await closing.wait()
                return await client.eval(*args)

        async def close_reporter(**kwargs):
            closing.set()
            await original_reporter_close(**kwargs)

        async def finish_failed(current):
            cleanup_started.set()
            await allow_cleanup.wait()
            assert "queue_closed" not in events, "中断会话清理必须在 Redis 连接关闭之前执行"
            result = await original_finish_failed(current)
            events.append("session_cleaned")
            return result

        async def close_queue():
            events.append("queue_closed")
            await original_queue_close()

        queue._client = DelayedTransport()
        monkeypatch.setattr(reporter, "close", close_reporter)
        monkeypatch.setattr(queue, "finish_failed", finish_failed)
        monkeypatch.setattr(queue, "close", close_queue)
        reporter.start()
        stopping = asyncio.create_task(runtime.stop())
        try:
            await asyncio.wait_for(cleanup_started.wait(), 2)
            # Give shutdown the opportunity to finish its reporter drain. It
            # must still wait for the newly created per-task cleanup.
            try:
                await asyncio.wait_for(asyncio.shield(stopping), .05)
            except TimeoutError:
                pass
            assert not stopping.done(), "stop 必须等待 reporter.close 期间新产生的中断清理"
            assert "queue_closed" not in events

            allow_cleanup.set()
            await asyncio.wait_for(asyncio.shield(stopping), 2)

            assert events == ["session_cleaned", "queue_closed"]
            assert json.loads(await client.get(session_key))["status"] == "failed"
            assert await client.get(active_key) is None
            assert not runtime._interruption_tasks
        finally:
            allow_cleanup.set()
            await asyncio.gather(stopping, return_exceptions=True)
            if runtime._interruption_tasks:
                await asyncio.gather(*list(runtime._interruption_tasks), return_exceptions=True)
            await client.aclose()

    asyncio.run(scenario())
