import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import subprocess
import threading
from uuid import uuid4

import pytest

from naruto_daily.login_service import module_timeout


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1cAAAAASUVORK5CYII='
)
SERIAL = 'emulator-5554'
ADB = Path('diagnostic-adb.exe')


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Storage:
    def __init__(self):
        self.calls = []
        self.saved = []
        self.saved_event = asyncio.Event()
        self.failures = 0
        self.next_count = None

    async def put_exception(self, task_id, count, png):
        self.calls.append((task_id, count, png))
        if self.failures:
            self.failures -= 1
            raise RuntimeError('signed-url-token-must-stay-private')
        actual_count = self.next_count if self.next_count is not None else count
        self.next_count = None
        self.saved.append((task_id, actual_count, png))
        self.saved_event.set()
        return actual_count


def monitor_for(monkeypatch, *, storage=None, run=None):
    calls = []

    def capture(command, **kwargs):
        calls.append((command, kwargs))
        if run is not None:
            return run(command, **kwargs)
        return subprocess.CompletedProcess(command, 0, PNG, b'')

    monkeypatch.setattr(module_timeout.subprocess, 'run', capture)
    clock = Clock()
    storage = storage or Storage()
    task_id = str(uuid4())
    monitor = module_timeout.ModuleTimeoutMonitor(task_id, ADB, storage, clock=clock)
    return monitor, clock, storage, calls, task_id


def test_capture_starts_at_600_seconds_and_only_once_per_invocation(monkeypatch):
    async def scenario():
        monitor, clock, storage, calls, task_id = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 599
        monitor.check()
        await asyncio.sleep(0.01)
        assert calls == []
        clock.now = 600
        monitor.check()
        await asyncio.wait_for(storage.saved_event.wait(), 1)
        clock.now = 1800
        monitor.check()
        monitor.end()
        await monitor.close()
        assert storage.saved == [(task_id, 1, PNG)]
        assert len(calls) == 1
        command, options = calls[0]
        assert command == [str(ADB), '-s', SERIAL, 'exec-out', 'screencap', '-p']
        assert options['capture_output'] is True
        assert options['timeout'] == 15
        assert options['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    asyncio.run(scenario())


def test_reentering_same_module_starts_a_fresh_timer(monkeypatch):
    async def scenario():
        monitor, clock, storage, calls, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 599
        monitor.end()
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await asyncio.sleep(0.01)
        assert calls == []
        clock.now = 1199
        monitor.check()
        await monitor.close()
        assert len(storage.saved) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['begin', 'end'])
def test_module_transition_captures_due_image_before_poller_runs(monkeypatch, boundary):
    async def scenario():
        monitor, clock, storage, _, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        if boundary == 'begin':
            monitor.begin('return_to_main', SERIAL)
        else:
            monitor.end()
        await monitor.close()
        assert len(storage.saved) == 1

    asyncio.run(scenario())


def test_end_before_threshold_prevents_any_later_capture(monkeypatch):
    async def scenario():
        monitor, clock, storage, calls, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 599
        monitor.end()
        clock.now = 1000
        monitor.check()
        await monitor.close()
        assert calls == []
        assert storage.saved == []

    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['enter_secret_realm', 'handle_secret_realm'])
def test_secret_realm_is_excluded_but_its_nested_return_has_own_timer(monkeypatch, name):
    async def scenario():
        monitor, clock, storage, calls, _ = monitor_for(monkeypatch)
        monitor.begin(name, SERIAL)
        clock.now = 1000
        monitor.check()
        await asyncio.sleep(0.01)
        assert calls == []
        monitor.begin('return_to_main', SERIAL)
        clock.now = 1600
        monitor.check()
        monitor.end()
        clock.now = 2600
        monitor.end()
        await monitor.close()
        assert len(storage.saved) == 1

    asyncio.run(scenario())


def test_parent_timer_survives_nested_module_and_secret_placeholder(monkeypatch):
    async def scenario():
        monitor, clock, storage, _, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 100
        monitor.begin('enter_secret_realm', SERIAL)
        monitor.end()
        clock.now = 300
        monitor.begin('return_to_main', SERIAL)
        clock.now = 600
        monitor.check()
        await asyncio.wait_for(storage.saved_event.wait(), 1)
        assert len(storage.saved) == 1
        clock.now = 900
        monitor.check()
        await monitor.close()
        assert [row[1] for row in storage.saved] == [1, 2]

    asyncio.run(scenario())


def test_multiple_modules_follow_actual_storage_counter(monkeypatch):
    async def scenario():
        storage = Storage()
        storage.next_count = 4
        monitor, clock, _, _, _ = monitor_for(monkeypatch, storage=storage)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.end()
        await asyncio.wait_for(storage.saved_event.wait(), 1)
        monitor.begin('return_to_main', SERIAL)
        clock.now = 1200
        monitor.end()
        await monitor.close()
        assert [row[1] for row in storage.calls] == [1, 5]
        assert [row[1] for row in storage.saved] == [4, 5]

    asyncio.run(scenario())


@pytest.mark.parametrize('serial', [None, '', ' ', 'unknown', '-d', 'emulator-5554 other', 'emulator-5554\n'])
def test_unbound_or_unsafe_serial_never_uses_default_adb_device(monkeypatch, serial):
    async def scenario():
        monitor, clock, storage, calls, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', serial)
        clock.now = 600
        monitor.end()
        await monitor.close()
        assert calls == []
        assert storage.saved == []

    asyncio.run(scenario())


def test_task_refuses_switch_to_another_device(monkeypatch):
    async def scenario():
        monitor, clock, storage, calls, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        monitor.end()
        monitor.begin('return_to_main', 'emulator-5556')
        clock.now = 600
        monitor.end()
        await monitor.close()
        assert calls == []
        assert storage.saved == []

    asyncio.run(scenario())


@pytest.mark.parametrize('output', [b'', b'not-png', b'\x89PNG\r\n\x1a\n' + b'x' * (16 * 1024 * 1024)],
                         ids=['empty', 'non-png', 'over-16-mib'])
def test_invalid_or_oversized_screenshot_is_not_uploaded(monkeypatch, output):
    async def scenario():
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, output, b'')

        monitor, clock, storage, calls, _ = monitor_for(monkeypatch, run=run)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await monitor.close()
        assert len(calls) == 1
        assert storage.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['timeout', 'exit', 'exception'])
def test_screenshot_failure_does_not_escape_or_retry_capture(monkeypatch, caplog, failure):
    async def scenario():
        def run(command, **kwargs):
            if failure == 'exit':
                return subprocess.CompletedProcess(command, 1, PNG, b'private-stderr')
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, 15, stderr=b'private-stderr')
            raise RuntimeError('private-stderr')

        monitor, clock, storage, calls, _ = monitor_for(monkeypatch, run=run)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        clock.now = 1200
        monitor.check()
        await monitor.close()
        assert len(calls) == 1
        assert storage.calls == []

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert caplog.records
    assert 'private-stderr' not in caplog.text
    assert all(getattr(record, 'backend_only', False) for record in caplog.records)


def test_upload_retries_same_png_and_counter_without_recapturing(monkeypatch, caplog):
    async def scenario():
        storage = Storage()
        storage.failures = 2
        monitor, clock, _, calls, task_id = monitor_for(monkeypatch, storage=storage)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await monitor.close()
        assert storage.calls == [(task_id, 1, PNG)] * 3
        assert storage.saved == [(task_id, 1, PNG)]
        assert len(calls) == 1

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert 'signed-url-token-must-stay-private' not in caplog.text
    assert all(getattr(record, 'backend_only', False) for record in caplog.records)


def test_exhausted_upload_does_not_advance_success_counter(monkeypatch):
    async def scenario():
        storage = Storage()
        storage.failures = 3
        monitor, clock, _, _, _ = monitor_for(monkeypatch, storage=storage)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.end()
        # The second capture is independent of the first failed upload.
        monitor.begin('return_to_main', SERIAL)
        clock.now = 1200
        monitor.end()
        await monitor.close()
        assert [row[1] for row in storage.calls] == [1, 1, 1, 1]
        assert [row[1] for row in storage.saved] == [1]

    asyncio.run(scenario())


def test_background_poller_fires_without_new_business_events(monkeypatch):
    async def scenario():
        monitor, clock, storage, _, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        try:
            await asyncio.wait_for(storage.saved_event.wait(), 2)
            assert len(storage.saved) == 1
        finally:
            await monitor.close()

    asyncio.run(scenario())


def test_slow_upload_does_not_block_next_capture_or_allow_counter_race(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        second_capture = threading.Event()
        active_uploads = 0
        capture_count = 0

        class SlowStorage(Storage):
            async def put_exception(self, task_id, count, png):
                nonlocal active_uploads
                active_uploads += 1
                assert active_uploads == 1
                entered.set()
                try:
                    await release.wait()
                    return await super().put_exception(task_id, count, png)
                finally:
                    active_uploads -= 1

        def run(command, **kwargs):
            nonlocal capture_count
            capture_count += 1
            if capture_count == 2:
                second_capture.set()
            return subprocess.CompletedProcess(command, 0, PNG, b'')

        storage = SlowStorage()
        monitor, clock, _, _, _ = monitor_for(monkeypatch, storage=storage, run=run)
        try:
            monitor.begin('claim_rewards', SERIAL)
            clock.now = 600
            monitor.end()
            await asyncio.wait_for(entered.wait(), 1)
            monitor.begin('return_to_main', SERIAL)
            clock.now = 1200
            monitor.check()
            assert await asyncio.to_thread(second_capture.wait, 1)
            assert storage.saved == []
            monitor.end()
        finally:
            release.set()
            await monitor.close()
        assert [row[1] for row in storage.saved] == [1, 2]

    asyncio.run(scenario())


def test_end_and_close_allow_started_upload_to_finish(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled = False

        class SlowStorage(Storage):
            async def put_exception(self, task_id, count, png):
                nonlocal cancelled
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                return await super().put_exception(task_id, count, png)

        storage = SlowStorage()
        monitor, clock, _, _, _ = monitor_for(monkeypatch, storage=storage)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await asyncio.wait_for(entered.wait(), 1)
        monitor.end()
        closing = asyncio.create_task(monitor.close())
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert not cancelled
        release.set()
        await asyncio.wait_for(closing, 1)
        assert not cancelled
        assert len(storage.saved) == 1

    asyncio.run(scenario())


def test_cancelled_close_still_drains_work_and_stops_entire_stack(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class SlowStorage(Storage):
            async def put_exception(self, task_id, count, png):
                entered.set()
                await release.wait()
                return await super().put_exception(task_id, count, png)

        storage = SlowStorage()
        monitor, clock, _, calls, _ = monitor_for(monkeypatch, storage=storage)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await asyncio.wait_for(entered.wait(), 1)
        monitor.begin('return_to_main', SERIAL)
        closing = asyncio.create_task(monitor.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0.01)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        clock.now = 2000
        monitor.check()
        monitor.begin('claim_rewards', SERIAL)
        await monitor.close()
        await asyncio.sleep(0)
        assert len(calls) == 1
        assert len(storage.saved) == 1
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(scenario())


def test_close_has_a_bound_when_storage_never_returns(monkeypatch, caplog):
    async def scenario():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class StuckStorage(Storage):
            async def put_exception(self, task_id, count, png):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        # Shorten only the configured shutdown grace; exercise the real cleanup.
        monkeypatch.setattr(module_timeout, 'CLOSE_TIMEOUT_SECONDS', 0.02)
        monitor, clock, _, _, _ = monitor_for(monkeypatch, storage=StuckStorage())
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(monitor.close(), 1)
        assert cancelled.is_set()
        await asyncio.sleep(0)
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert caplog.records
    assert all(getattr(record, 'backend_only', False) for record in caplog.records)


def test_upload_timeout_does_not_start_overlapping_requests(monkeypatch):
    async def scenario():
        attempts = []

        class StuckStorage(Storage):
            async def put_exception(self, task_id, count, png):
                attempts.append((count, png))
                await asyncio.Event().wait()

        monkeypatch.setattr(module_timeout, 'UPLOAD_TIMEOUT_SECONDS', 0.01)
        monitor, clock, _, calls, _ = monitor_for(monkeypatch, storage=StuckStorage())
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await asyncio.wait_for(monitor.close(), 1)
        assert attempts == [(1, PNG)]
        assert len(calls) == 1

    asyncio.run(scenario())


def test_slow_sdk_upload_is_awaited_without_creating_duplicate_objects(monkeypatch):
    from types import SimpleNamespace
    from naruto_daily.login_service.storage import AliyunObjectStorage
    from oss_fakes import FakeBucket

    class AlreadyExists(Exception):
        status = 409
        code = 'FileAlreadyExists'

    class DelayedBucket(FakeBucket):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()
            self.lock = threading.Lock()
            self.calls = 0

        def put_object(self, key, body, headers):
            with self.lock:
                self.calls += 1
                first = self.calls == 1
            if first:
                self.entered.set()
                assert self.release.wait(3)
            try:
                with self.lock:
                    if key in self.objects:
                        raise AlreadyExists()
                    return super().put_object(key, body, headers)
            finally:
                if first:
                    self.finished.set()

    async def scenario():
        bucket = DelayedBucket()
        storage = AliyunObjectStorage('login', SimpleNamespace(), bucket=bucket)
        monitor, clock, _, _, task_id = monitor_for(monkeypatch, storage=storage)
        monkeypatch.setattr(module_timeout, 'UPLOAD_TIMEOUT_SECONDS', 0.05)
        monitor.begin('return_to_main', SERIAL)
        clock.now = 600
        monitor.check()
        closing = asyncio.create_task(monitor.close())
        try:
            async with asyncio.timeout(1):
                while not bucket.entered.is_set():
                    await asyncio.sleep(0.001)
            # Longer than the old per-attempt timeout, within the total budget.
            await asyncio.sleep(0.08)
        finally:
            bucket.release.set()
            await closing
            await asyncio.to_thread(bucket.finished.wait, 1)
        assert list(bucket.objects) == [f'login/exceptions/{task_id}_1.png']
        assert bucket.calls == 1
        assert monitor._count == 1

    asyncio.run(scenario())


def test_independent_tasks_keep_their_own_device_and_image_counter(monkeypatch):
    async def scenario():
        captures = []

        def run(command, **kwargs):
            captures.append(command[2])
            return subprocess.CompletedProcess(command, 0, PNG, b'')

        first, first_clock, first_storage, _, first_id = monitor_for(monkeypatch, run=run)
        second, second_clock, second_storage, _, second_id = monitor_for(monkeypatch, run=run)
        first.begin('claim_rewards', SERIAL)
        second.begin('claim_rewards', 'emulator-5556')
        first_clock.now = 600
        first.check()
        second_clock.now = 599
        second.check()
        await first.close()
        assert first_storage.saved == [(first_id, 1, PNG)]
        assert second_storage.saved == []
        second_clock.now = 600
        second.check()
        await second.close()
        assert second_storage.saved == [(second_id, 1, PNG)]
        assert captures == [SERIAL, 'emulator-5556']

    asyncio.run(scenario())


def test_close_captures_due_module_before_clearing_its_timer(monkeypatch):
    async def scenario():
        monitor, clock, storage, _, _ = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        await monitor.close()
        assert len(storage.saved) == 1

    asyncio.run(scenario())


def test_diagnostic_logs_identify_task_module_and_saved_number(monkeypatch, caplog):
    async def scenario():
        monitor, clock, storage, _, task_id = monitor_for(monkeypatch)
        monitor.begin('claim_rewards', SERIAL)
        clock.now = 600
        monitor.check()
        await monitor.close()
        assert len(storage.saved) == 1
        messages = [record.getMessage() for record in caplog.records]
        assert len(messages) >= 2
        assert all(task_id in message and 'claim_rewards' in message for message in messages)
        assert any('1' in message for message in messages)

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert all(getattr(record, 'backend_only', False) for record in caplog.records)


def test_capture_runs_when_worker_pipe_reads_fill_default_executor(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        release = threading.Event()
        occupied = threading.Event()

        def blocked_pipe_read():
            occupied.set()
            release.wait(5)

        pipe_read = loop.run_in_executor(None, blocked_pipe_read)
        monitor, clock, storage, _, _ = monitor_for(monkeypatch)
        try:
            while not occupied.is_set():
                await asyncio.sleep(0.001)
            monitor.begin('claim_rewards', SERIAL)
            clock.now = 600
            monitor.check()
            await asyncio.wait_for(storage.saved_event.wait(), 0.5)
            assert len(storage.saved) == 1
        finally:
            release.set()
            await pipe_read
            await monitor.close()

    asyncio.run(scenario())
