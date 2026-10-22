"""Admin interruption stops one execution; no real Redis or emulator is used."""
import asyncio
import json
import sys

import pytest

from naruto_daily.login_service.runtime import LoginRuntime
from naruto_daily.login_service.queue import RedisTaskQueue
from test_login_service_run_logs import (
    MemoryStorage, claim, config, drain, eventually, fake_client, keys, make_reporter,
)
from test_login_service_terminal_logs import SUCCESS_PROTOCOL


class Child:
    def __init__(self):
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True


def test_callback_stops_only_the_matching_task_and_attempt(tmp_path):
    async def scenario():
        client, cfg = fake_client(), config(ldplayer_state_dir=tmp_path)
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)
        current, other = Child(), Child()
        runtime._tasks[record.id] = record
        runtime._children.update({record.id: current, "another-task": other})
        try:
            callback = getattr(reporter, "on_interrupted", None)
            if callback:
                callback(record.id, record.attempt_id)
            assert current.killed, "Redis interruption must stop its execution child"
            assert not other.killed and not runtime._stopping
            assert record.daily_status == "interrupted"
            assert record.daily_error_code == "admin_interrupted"
            saved = json.loads((tmp_path / "runs" / f"{record.id}.json").read_text())
            assert saved["status"] == "interrupted"
        finally:
            reporter._pending.clear()
            await reporter.close(timeout=0)
            await client.aclose()
    asyncio.run(scenario())


def test_callback_from_old_attempt_cannot_kill_current_attempt(tmp_path):
    async def scenario():
        client, cfg = fake_client(), config(ldplayer_state_dir=tmp_path)
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)
        current = Child()
        runtime._tasks[record.id] = record
        runtime._children[record.id] = current
        try:
            callback = getattr(reporter, "on_interrupted", None)
            if callback:
                callback(record.id, "old-attempt")
            assert not current.killed and not record._admin_interrupted
            assert record.daily_status is None
        finally:
            reporter._pending.clear()
            await reporter.close(timeout=0)
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("published_qr", [False, True])
def test_publish_callback_releases_instance_without_requeue_or_failure_hold(
    tmp_path, monkeypatch, published_qr,
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

        class Pool:
            closed = []

            def mark_ready(self, task_id):
                pass

            def close(self, task_id):
                self.closed.append(task_id)

        pool = Pool()
        runtime._manager = pool
        program = "import json,sys,time\nfor event in json.loads(sys.stdin.readline())['events']:\n print(json.dumps(event),flush=True)\ntime.sleep(60)\n"

        async def child(current):
            events = SUCCESS_PROTOCOL[:3] if published_qr else [
                {"type": "stage", "status": "opening_game"},
            ]
            return await runtime._run_child(current, [sys.executable, "-u", "-c", program],
                                            {"events": events}, daily=True)

        monkeypatch.setattr(runtime, "_run_ldplayer", child)
        reporter.start()
        work = asyncio.create_task(runtime._process_record(record))
        try:
            await eventually(lambda: record.daily_status == "running_daily" if published_qr
                             else record.status == "opening_game")
            await drain(reporter)
            execution = runtime._children[record.id]
            run_key, _, session_key = keys(cfg, record)
            interrupted = json.loads(await client.get(run_key))
            interrupted["status"] = "interrupted"
            await client.set(run_key, json.dumps(interrupted), keepttl=True)
            reporter.emit(record, message="next progress write", action_index=1, action_total=2)
            await drain(reporter)
            await asyncio.sleep(.05)
            assert execution.poll() is not None, "The next write must stop a blocked child"
            await asyncio.wait_for(work, 3)
            await drain(reporter)
            assert record.daily_status == "interrupted"
            assert pool.closed == [record.id]
            assert record.id not in runtime._children and not runtime._stopping
            assert await client.llen(cfg.redis_queue_key) == 0
            assert json.loads(await client.get(run_key)) == interrupted
            assert json.loads(await client.get(session_key))["status"] == "failed"
        finally:
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await reporter.close(timeout=0)
            await client.aclose()
    asyncio.run(scenario())
