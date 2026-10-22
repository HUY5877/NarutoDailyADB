"""最后一次完成回写才发现管理员中断时，仍释放会话且不通知成功。"""

import asyncio
import json
import time

from naruto_daily.login_service import runtime as runtime_module
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.run_logs import _PUBLISH
from naruto_daily.login_service.runtime import LoginRuntime
from test_login_service_run_logs import MemoryStorage, claim, config, drain, fake_client, keys, make_reporter


def test_admin_interrupt_during_final_completion_write_releases_session_and_suppresses_success(
    tmp_path, monkeypatch,
):
    async def scenario():
        client = fake_client()
        cfg = config(emulator_driver="ldplayer", ldplayer_state_dir=tmp_path,
                     ldplayer_console_path=tmp_path / "ldconsole.exe", ldplayer_pool_indices=(0,))
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)
        runtime._tasks[record.id] = record
        run_key, _, session_key = keys(cfg, record)
        released, notifications = [], []
        interrupted = asyncio.Event()

        class Pool:
            def close(self, task_id):
                released.append(task_id)

        class InterruptOnCompletion:
            def __getattr__(self, name):
                return getattr(client, name)

            async def eval(self, *args):
                if args[0] == _PUBLISH and json.loads(args[6])["status"] == "completed":
                    assert released == [record.id], "模拟器已清理才发布最终完成状态"
                    state = json.loads(await client.get(run_key))
                    state["status"] = "interrupted"
                    await client.set(run_key, json.dumps(state), keepttl=True)
                    interrupted.set()
                return await client.eval(*args)

        async def complete_game(current):
            await runtime._set_status(current, "qr_ready")
            current.qr_published = True
            current.daily_started_at = time.time()
            runtime._set_daily_status(current, "completed", action_count=1)
            return b"completed fake PNG"

        runtime._manager = Pool()
        monkeypatch.setattr(runtime, "_run_ldplayer", complete_game)
        monkeypatch.setattr(runtime, "_notify_task_finished",
                            lambda current, status: notifications.append(status))
        reporter.start()
        try:
            reporter.emit(record, status="starting_emulator")
            await drain(reporter)
            queue._client = InterruptOnCompletion()

            await asyncio.wait_for(runtime._process_record(record), 3)
            await drain(reporter)

            assert interrupted.is_set()
            assert record._admin_interrupted
            assert record.daily_status == "interrupted"
            assert json.loads(await client.get(run_key))["status"] == "interrupted"
            assert json.loads(await client.get(session_key))["status"] == "failed"
            assert "completed" not in notifications
            assert released == [record.id]
            assert not runtime._stopping
        finally:
            await reporter.close(timeout=0)
            await client.aclose()

    asyncio.run(scenario())


def test_admin_interrupt_wakes_existing_failure_hold_and_releases_only_its_instance(
    tmp_path, monkeypatch,
):
    async def scenario():
        client = fake_client()
        cfg = config(emulator_driver="ldplayer", ldplayer_state_dir=tmp_path,
                     ldplayer_console_path=tmp_path / "ldconsole.exe", ldplayer_pool_indices=(0,))
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)
        runtime._tasks[record.id] = record
        run_key, _, session_key = keys(cfg, record)
        released = []
        holding = asyncio.Event()
        original_hold = runtime._hold_failed_ldplayer

        class Pool:
            def close(self, task_id):
                released.append(task_id)

        async def fail_game(current):
            await runtime._set_status(current, "qr_ready")
            current.qr_published = True
            current.daily_started_at = time.time()
            raise RuntimeError("failure before administrator interruption")

        async def observe_hold(current):
            holding.set()
            await original_hold(current)

        runtime._manager = Pool()
        monkeypatch.setattr(runtime_module, "FAILED_INSTANCE_HOLD_SECONDS", 30)
        monkeypatch.setattr(runtime, "_run_ldplayer", fail_game)
        monkeypatch.setattr(runtime, "_hold_failed_ldplayer", observe_hold)
        reporter.start()
        work = asyncio.create_task(runtime._process_record(record))
        try:
            await asyncio.wait_for(holding.wait(), 2)
            await drain(reporter)
            assert not work.done() and released == []
            state = json.loads(await client.get(run_key))
            assert state["status"] == "failed"
            state["status"] = "interrupted"
            await client.set(run_key, json.dumps(state), keepttl=True)
            reporter.emit(record, message="failure hold receives its next status write")
            await drain(reporter)

            assert record._admin_interrupted
            await asyncio.wait_for(asyncio.shield(work), 1)
            await drain(reporter)

            assert released == [record.id]
            assert record.daily_status == "interrupted"
            assert json.loads(await client.get(session_key))["status"] == "failed"
            assert not runtime._stop_event.is_set() and not runtime._stopping
            assert await client.llen(cfg.redis_queue_key) == 0
        finally:
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await reporter.close(timeout=0)
            await client.aclose()

    asyncio.run(scenario())


def test_slow_final_write_does_not_notify_success_and_late_interruption_closes_session(
    tmp_path, monkeypatch,
):
    async def scenario():
        client = fake_client()
        cfg = config(emulator_driver="ldplayer", ldplayer_state_dir=tmp_path,
                     ldplayer_console_path=tmp_path / "ldconsole.exe", ldplayer_pool_indices=(0,))
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)
        runtime._tasks[record.id] = record
        run_key, _, session_key = keys(cfg, record)
        released, notifications = [], []
        writing, resume = asyncio.Event(), asyncio.Event()
        original_flush = reporter.flush

        class Pool:
            def close(self, task_id):
                released.append(task_id)

        class SlowCompletion:
            def __getattr__(self, name):
                return getattr(client, name)

            async def eval(self, *args):
                if args[0] == _PUBLISH and json.loads(args[6])["status"] == "completed":
                    writing.set()
                    await resume.wait()
                    state = json.loads(await client.get(run_key))
                    state["status"] = "interrupted"
                    await client.set(run_key, json.dumps(state), keepttl=True)
                return await client.eval(*args)

        async def short_flush(task_id, *, timeout=2):
            return await original_flush(task_id, timeout=.01)

        async def complete_game(current):
            await runtime._set_status(current, "qr_ready")
            current.qr_published = True
            current.daily_started_at = time.time()
            runtime._set_daily_status(current, "completed", action_count=1)
            return b"completed fake PNG"

        runtime._manager = Pool()
        monkeypatch.setattr(runtime, "_run_ldplayer", complete_game)
        monkeypatch.setattr(runtime, "_notify_task_finished",
                            lambda current, status: notifications.append(status))
        monkeypatch.setattr(reporter, "flush", short_flush)
        reporter.start()
        work = None
        try:
            reporter.emit(record, status="starting_emulator")
            await drain(reporter)
            queue._client = SlowCompletion()
            work = asyncio.create_task(runtime._process_record(record))
            await asyncio.wait_for(writing.wait(), 2)
            await asyncio.wait_for(asyncio.shield(work), 1)
            before_redis_reply = list(notifications)
            assert not resume.is_set()

            resume.set()
            await drain(reporter)
            # A delayed callback may complete its single-task cleanup outside
            # the execution coroutine that already returned after flush timeout.
            try:
                async with asyncio.timeout(1):
                    while json.loads(await client.get(session_key))["status"] != "failed":
                        await asyncio.sleep(.01)
            except TimeoutError:
                pass

            assert "completed" not in before_redis_reply, "Redis 回写结果未知时不能提前通知成功"
            assert json.loads(await client.get(session_key))["status"] == "failed"
            assert record._admin_interrupted and record.daily_status == "interrupted"
            assert json.loads(await client.get(run_key))["status"] == "interrupted"
            assert "completed" not in notifications
            assert released == [record.id]
            assert not runtime._stopping
        finally:
            resume.set()
            if work is not None:
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await reporter.close(timeout=0)
            await client.aclose()

    asyncio.run(scenario())
