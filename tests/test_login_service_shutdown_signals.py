"""Actual console signals are confined to a disposable offline subprocess."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_repeated_sigint_keeps_worker_alive_until_sls_recovers_and_all_logs_upload(tmp_path):
    # Do not inherit service credentials or invoke the CLI's configuration loading.
    child_env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
    ) if key in os.environ}
    child_env["PYTHONIOENCODING"] = "utf-8"
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--signal-child", str(tmp_path)],
        cwd=tmp_path, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        output, errors = child.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        # This is the exact subprocess created above, never the pytest process or a process group.
        child.kill()
        output, errors = child.communicate(timeout=5)
        raise AssertionError(f"signal child did not finish: {output}\n{errors}") from None
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
    assert child.returncode == 0, f"child exit={child.returncode}\n{output}\n{errors}"
    report = json.loads(output.strip())
    assert report["signals"] == ["SIGINT", "SIGINT"]
    assert report["alive_while_sls_failed"] is True
    assert report["outage_during_shutdown_seconds"] >= 2.1
    assert report["failed_upload_attempts"] >= 2
    assert report["uploaded_events"] == report["accepted_events"] == 6
    assert report["final_checkpoint_counts"] == [1, 5]
    assert report["stop_reporter_calls"] == report["redis_close_calls"] == 1
    assert report["pending_after_exit"] == 0


async def _signal_child(state_dir):
    import asyncio
    import signal
    import time
    from uuid import UUID

    # The helper is run from a temporary cwd and imports code only, never machine config.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from naruto_daily.common import environment
    environment.PROJECT_ROOT = state_dir
    environment._initialized = True
    from naruto_daily.login_service import __main__ as entrypoint
    from naruto_daily.login_service import runtime as runtime_module
    from naruto_daily.login_service import sls_logs
    from naruto_daily.login_service.config import ServiceConfig

    failed = asyncio.Event()
    expected = []
    task_ids = [str(UUID("123e4567-e89b-42d3-a456-426614174000")),
                str(UUID("123e4567-e89b-42d3-a456-426614174001"))]
    runtime = None

    class Writer:
        online = False
        failed_attempts = 0

        def __init__(self):
            self.uploaded = []

        async def put(self, records):
            if not self.online:
                self.failed_attempts += 1
                failed.set()
                raise RuntimeError("offline fixture transport unavailable")
            self.uploaded.extend(dict(row) for row in records)
            await asyncio.sleep(0)

    writer = Writer()

    def emit(task_id, message):
        item_id = runtime._log_publisher.emit(task_id, {
            "time": "2026-10-20T09:00:00.000Z", "level": "INFO", "message": message,
        })
        assert item_id is not None
        expected.append((task_id, item_id, message))

    class Queue:
        close_calls = 0

        async def initialize(self):
            pass

        async def pop(self):
            await asyncio.sleep(0.01)
            return None

        async def close(self):
            # Shutdown must not release the backing runtime before its log publisher drained.
            assert runtime._log_publisher._closed
            self.close_calls += 1

    class Storage:
        async def initialize(self):
            pass

        async def sweep(self):
            pass

        async def delete(self, _task_id):
            pass

    class Reporter:
        close_calls = 0

        def start(self):
            for index in range(5):
                emit(task_ids[1] if index == 4 else task_ids[0], f"queued-{index}")

        async def close(self):
            self.close_calls += 1
            emit(task_ids[0], "last-record-during-shutdown")
            for task_id in task_ids:
                runtime._log_publisher.finish(task_id)

    queue, reporter = Queue(), Reporter()
    real_runtime, real_publisher = runtime_module.LoginRuntime, sls_logs.SlsLogPublisher

    def create_runtime(config):
        nonlocal runtime
        runtime = real_runtime(config, storage=Storage(), redis_queue=queue, run_reporter=reporter)
        return runtime

    # The runtime, entrypoint and publisher remain real; only external service IO is replaced.
    runtime_module.LoginRuntime = create_runtime
    sls_logs.SlsLogPublisher = lambda config: real_publisher(
        config, writer=writer, interval=0.005, retry_interval=0.02, batch_size=2,
    )
    config = ServiceConfig(redis_url="redis://unused.invalid/0", redis_queue_key="test:signals",
                           concurrency=1, emulator_driver="mock", sls_enabled=True, app_env="test",
                           ldplayer_state_dir=state_dir, sweep_interval_ms=10)
    worker = asyncio.create_task(entrypoint.run_worker(config))
    await asyncio.wait_for(failed.wait(), 3)
    assert isinstance(runtime, real_runtime)
    assert isinstance(runtime._log_publisher, real_publisher)

    signal.raise_signal(signal.SIGINT)
    async with asyncio.timeout(3):
        while not runtime._log_publisher._closing:
            await asyncio.sleep(0.005)
    started = time.monotonic()
    signal.raise_signal(signal.SIGINT)
    # Exceed the former two-second close deadline while retries continue to fail.
    await asyncio.sleep(2.2)
    elapsed = time.monotonic() - started
    assert not worker.done(), "repeated Ctrl+C must not finish while logs remain in memory"
    assert not runtime._log_publisher._closed
    assert runtime._log_publisher._count == len(expected) == 6
    assert not writer.uploaded and queue.close_calls == 0

    writer.online = True
    await asyncio.wait_for(asyncio.shield(worker), 3)
    assert not worker.cancelled() and worker.exception() is None
    uploaded = [row for row in writer.uploaded if row["kind"] == "event"]
    assert [(row["taskId"], row["id"], row["message"]) for row in uploaded] == expected
    assert len({row["id"] for row in uploaded}) == len(expected)
    markers = {row["taskId"]: row for row in writer.uploaded if row["kind"] == "checkpoint"}
    assert len(markers) == 2
    for task_id, marker in markers.items():
        task_events = [row for row in uploaded if row["taskId"] == task_id]
        assert marker["entryCount"] == str(len(task_events))
        assert marker["firstId"] == task_events[0]["id"]
        assert marker["throughId"] == task_events[-1]["id"]
        assert marker["truncated"] == "false"
    assert runtime._log_publisher._closed and not runtime._log_publisher._pending
    assert runtime._log_publisher._task.done()
    print(json.dumps({
        "signals": ["SIGINT", "SIGINT"], "alive_while_sls_failed": True,
        "outage_during_shutdown_seconds": elapsed, "failed_upload_attempts": writer.failed_attempts,
        "uploaded_events": len(uploaded), "accepted_events": len(expected),
        "final_checkpoint_counts": sorted(int(row["entryCount"]) for row in markers.values()),
        "stop_reporter_calls": reporter.close_calls, "redis_close_calls": queue.close_calls,
        "pending_after_exit": runtime._log_publisher._count,
    }), flush=True)


if __name__ == "__main__":
    import asyncio
    assert sys.argv[1] == "--signal-child"
    asyncio.run(_signal_child(Path(sys.argv[2])))
