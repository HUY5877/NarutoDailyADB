"""周胜没有公开进度时，显式执行检查点仍能发现管理员中断。"""

import asyncio
import json
import sys

from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.run_logs import _PUBLISH
from naruto_daily.login_service.runtime import LoginRuntime
from test_login_service_interrupt_runtime import Child
from test_login_service_run_logs import MemoryStorage, claim, config, drain, fake_client, keys, make_reporter
from test_login_service_terminal_logs import SUCCESS_PROTOCOL


def test_battle_checkpoint_stops_only_its_child_without_public_progress_or_requeue(tmp_path, monkeypatch):
    async def scenario():
        client = fake_client()
        cfg = config(emulator_driver="ldplayer", ldplayer_state_dir=tmp_path,
                     ldplayer_console_path=tmp_path / "ldconsole.exe", ldplayer_pool_indices=(0,))
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        record.user_options["mainTask"] = "arena_weekly"
        reporter = make_reporter(cfg, queue)
        runtime = LoginRuntime(cfg, MemoryStorage(), redis_queue=queue, run_reporter=reporter)
        runtime._tasks[record.id] = record
        unrelated = Child()
        runtime._children["unrelated-task"] = unrelated
        released, held, publications = [], [], []
        progress_seen, ordinary_info_seen = asyncio.Event(), asyncio.Event()
        info_gate, checkpoint_gate = tmp_path / "send-info", tmp_path / "send-checkpoint"
        progress_message = "动作 1/2：决斗场周胜"
        ordinary_message = "决斗场周胜战斗中，继续点击普攻"
        original_accept_log, original_call = runtime._accept_log, queue._call

        class Pool:
            def mark_ready(self, task_id):
                pass

            def close(self, task_id):
                released.append(task_id)

        def accept_log(current, event):
            original_accept_log(current, event)
            if event.get("message") == progress_message:
                progress_seen.set()
            elif event.get("message") == ordinary_message:
                ordinary_info_seen.set()

        async def capture_call(method, *args):
            if method == "eval" and args[0] == _PUBLISH:
                publications.append(json.loads(args[6]))
            return await original_call(method, *args)

        async def hold_failed(current):
            held.append(current.id)

        # File gates control only this harmless test child. It neither imports
        # game code nor connects to Redis, ADB or a real emulator.
        program = r'''
import json,sys,time
from pathlib import Path
message=json.loads(sys.stdin.readline())
for event in message['events']:
    print(json.dumps(event),flush=True)
print(json.dumps({'type':'arena_started','executionDeadline':time.time()+60}),flush=True)
print(json.dumps(message['progress']),flush=True)
for gate,event in message['gatedEvents']:
    while not Path(gate).is_file():
        time.sleep(.01)
    print(json.dumps(event),flush=True)
time.sleep(60)
'''

        async def run_battle(current):
            return await runtime._run_child(current, [sys.executable, "-u", "-c", program], {
                "events": SUCCESS_PROTOCOL[:3],
                "progress": {"type": "log", "time": "2026-10-22T02:11:05.531Z", "level": "INFO",
                             "message": progress_message, "actionIndex": 1, "actionTotal": 2},
                "gatedEvents": [
                    [str(info_gate), {"type": "log", "time": "2026-10-22T02:11:35.531Z", "level": "INFO",
                                      "message": ordinary_message}],
                    [str(checkpoint_gate), {"type": "log", "time": "2026-10-22T02:11:36.531Z", "level": "INFO",
                                            "message": "决斗场周胜执行检查点", "executionCheckpoint": True}],
                ],
            }, daily=True)

        runtime._manager = Pool()
        monkeypatch.setattr(runtime, "_run_ldplayer", run_battle)
        monkeypatch.setattr(runtime, "_accept_log", accept_log)
        monkeypatch.setattr(runtime, "_hold_failed_ldplayer", hold_failed)
        monkeypatch.setattr(queue, "_call", capture_call)
        reporter.start()
        work = asyncio.create_task(runtime._process_record(record))
        try:
            await asyncio.wait_for(progress_seen.wait(), 3)
            await drain(reporter)
            execution = runtime._children[record.id]
            assert execution.pid > 0 and execution.poll() is None
            run_key, _, session_key = keys(cfg, record)
            interrupted = json.loads(await client.get(run_key))
            assert interrupted["status"] == "running_daily"
            interrupted.update(status="interrupted", errorCode="admin_interrupted",
                               updatedAt="2026-10-22T02:11:36.000Z", finishedAt="2026-10-22T02:11:36.000Z")
            interrupted_json = json.dumps(interrupted)
            await client.set(run_key, interrupted_json, keepttl=True)
            previous_publications = len(publications)
            previous_events = list(reporter.log_publisher.events[record.id])

            info_gate.touch()
            await asyncio.wait_for(ordinary_info_seen.wait(), 2)
            await asyncio.sleep(.35)
            assert len(publications) == previous_publications, "普通 INFO 不能暗中触发 Redis 中断查询"
            assert reporter.log_publisher.events[record.id] == previous_events
            assert not record._admin_interrupted and execution.poll() is None
            assert not work.done() and released == []

            checkpoint_gate.touch()
            try:
                await asyncio.wait_for(asyncio.shield(work), 3)
            except TimeoutError:
                assert execution.poll() is None and held == []
                raise AssertionError("有效 INFO 检查点没有触发回写，中断任务的子进程仍在运行") from None
            await drain(reporter)

            assert held == [], "执行检查点不能被当作协议错误进入失败现场保留"
            assert publications[previous_publications]["status"] == "running_daily"
            assert record._admin_interrupted and record.daily_status == "interrupted"
            assert record.daily_error_code == "admin_interrupted"
            assert execution.poll() is not None
            assert not unrelated.killed and runtime._children == {"unrelated-task": unrelated}
            assert released == [record.id]
            assert await client.llen(cfg.redis_queue_key) == 0
            assert await client.get(run_key) == interrupted_json
            assert json.loads(await client.get(session_key))["status"] == "failed"
            assert not runtime._stopping and not runtime._stop_event.is_set()
        finally:
            info_gate.touch()
            checkpoint_gate.touch()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await reporter.close(timeout=0)
            await client.aclose()

    asyncio.run(scenario())
