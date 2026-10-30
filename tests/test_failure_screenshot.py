import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib
import logging
import threading

import pytest


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1cAAAAASUVORK5CYII='
)
TASK_ID = '477ecd99-148b-44b4-91c7-c4fb190542f1'


def helper():
    return importlib.import_module('naruto_daily.login_service.failure_screenshot')


class Storage:
    def __init__(self, failures=0, actual_count=1):
        self.calls = []
        self.saved = []
        self.failures = failures
        self.actual_count = actual_count

    async def put_exception(self, task_id, count, png):
        self.calls.append((task_id, count, png))
        if self.failures:
            self.failures -= 1
            raise RuntimeError('private-sdk-credential-and-url')
        self.saved.append((task_id, self.actual_count, png))
        return self.actual_count


def test_capture_uses_daemon_thread_and_reports_actual_saved_number(caplog):
    module = helper()
    storage = Storage(actual_count=7)
    threads = []

    def capture():
        threads.append(threading.current_thread())
        return PNG

    with caplog.at_level(logging.INFO):
        assert asyncio.run(module.capture_task_failure(TASK_ID, capture, storage)) is None

    assert storage.calls == [(TASK_ID, 1, PNG)]
    assert storage.saved == [(TASK_ID, 7, PNG)]
    assert len(threads) == 1 and threads[0] is not threading.current_thread()
    assert threads[0].daemon
    assert any('7' in record.getMessage() and TASK_ID in record.getMessage()
               for record in caplog.records)
    assert all(getattr(record, 'backend_only', False) for record in caplog.records)


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
def test_capture_exception_is_safe_and_never_uploads(failure, caplog):
    module = helper()
    storage = Storage()

    def capture():
        raise failure('private-adb-capture-output')

    with caplog.at_level(logging.INFO):
        asyncio.run(module.capture_task_failure(TASK_ID, capture, storage))

    assert storage.calls == []
    assert failure.__name__ in caplog.text
    assert 'private-adb-capture-output' not in caplog.text
    assert caplog.records and all(getattr(record, 'backend_only', False) for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize('png', [b'', b'private-invalid-png', 'private-text', bytearray(PNG),
                                b'\x89PNG\r\n\x1a\n' + bytes(16 * 1024 * 1024 - 7)],
                         ids=['empty', 'wrong-signature', 'text', 'mutable', 'oversize'])
def test_invalid_or_oversize_capture_never_reaches_storage(png, caplog):
    module = helper()
    storage = Storage()
    with caplog.at_level(logging.INFO):
        asyncio.run(module.capture_task_failure(TASK_ID, lambda: png, storage))
    assert storage.calls == []
    assert caplog.records
    assert 'private-' not in caplog.text
    assert all(getattr(record, 'backend_only', False) for record in caplog.records)


def test_exactly_sixteen_mib_is_accepted():
    module = helper()
    png = b'\x89PNG\r\n\x1a\n' + bytes(16 * 1024 * 1024 - 8)
    storage = Storage()
    asyncio.run(module.capture_task_failure(TASK_ID, lambda: png, storage))
    assert storage.saved == [(TASK_ID, 1, png)]


@pytest.mark.parametrize('late_failure', [False, True])
def test_capture_timeout_returns_without_waiting_for_daemon_and_discards_late_result(
        monkeypatch, caplog, capsys, late_failure):
    module = helper()
    monkeypatch.setattr(module, 'CAPTURE_TIMEOUT_SECONDS', 0.02)
    release = threading.Event()
    threads, thread_errors, loop_errors = [], [], []
    storage = Storage()
    monkeypatch.setattr(threading, 'excepthook', thread_errors.append)

    def capture():
        threads.append(threading.current_thread())
        assert release.wait(timeout=5)
        if late_failure:
            raise RuntimeError('private-late-capture-output')
        return PNG

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        with caplog.at_level(logging.INFO):
            loop.run_until_complete(asyncio.wait_for(
                module.capture_task_failure(TASK_ID, capture, storage), timeout=1))
        assert threads and threads[0].is_alive()
        assert storage.calls == []
        assert 'timeout' in caplog.text.lower()
    finally:
        loop.close()
        release.set()
        for thread in threads:
            thread.join(timeout=2)
    assert storage.calls == []
    assert thread_errors == [] and loop_errors == []
    assert capsys.readouterr().err == ''
    assert 'private-late-capture-output' not in caplog.text


def test_capture_completes_while_default_executor_is_saturated():
    module = helper()

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered, release = threading.Event(), threading.Event()

        def busy_pipe_reader():
            entered.set()
            assert release.wait(timeout=5)

        occupied = loop.run_in_executor(None, busy_pipe_reader)
        storage = Storage()
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(module.capture_task_failure(TASK_ID, lambda: PNG, storage), 1)
            assert not occupied.done()
            assert storage.saved == [(TASK_ID, 1, PNG)]
        finally:
            release.set()
            await occupied

    asyncio.run(scenario())


def test_retries_completed_upload_failures_without_recapturing(caplog):
    module = helper()
    storage = Storage(failures=2, actual_count=9)
    captures = []

    def capture():
        captures.append(True)
        return PNG

    with caplog.at_level(logging.INFO):
        asyncio.run(module.capture_task_failure(TASK_ID, capture, storage))
    assert captures == [True]
    assert storage.calls == [(TASK_ID, 1, PNG)] * 3
    assert storage.saved == [(TASK_ID, 9, PNG)]
    assert 'RuntimeError' in caplog.text
    assert 'private-sdk-credential-and-url' not in caplog.text
    assert all(getattr(record, 'backend_only', False) for record in caplog.records)


def test_three_failed_upload_attempts_are_best_effort(caplog):
    module = helper()
    storage = Storage(failures=10)
    with caplog.at_level(logging.INFO):
        asyncio.run(module.capture_task_failure(TASK_ID, lambda: PNG, storage))
    assert storage.calls == [(TASK_ID, 1, PNG)] * 3
    assert storage.saved == []
    assert 'private-sdk-credential-and-url' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_upload_attempts_share_one_deadline(monkeypatch, caplog):
    module = helper()
    monkeypatch.setattr(module, 'UPLOAD_TIMEOUT_SECONDS', 0.08)

    async def scenario():
        calls, cancelled = [], []

        class SlowStorage:
            async def put_exception(self, task_id, count, png):
                calls.append((task_id, count, png))
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
                raise RuntimeError('private-slow-sdk-failure')

        await asyncio.wait_for(module.capture_task_failure(TASK_ID, lambda: PNG, SlowStorage()), 1)
        assert calls == [(TASK_ID, 1, PNG)] * 2
        assert cancelled == [True]

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert 'timeout' in caplog.text.lower()
    assert 'private-slow-sdk-failure' not in caplog.text


def test_upload_timeout_cancels_wait_without_starting_overlapping_retry(monkeypatch):
    module = helper()
    monkeypatch.setattr(module, 'UPLOAD_TIMEOUT_SECONDS', 0.02)

    async def scenario():
        calls, cancelled = [], []

        class StuckStorage:
            async def put_exception(self, task_id, count, png):
                calls.append((task_id, count, png))
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)

        await asyncio.wait_for(module.capture_task_failure(TASK_ID, lambda: PNG, StuckStorage()), 1)
        assert calls == [(TASK_ID, 1, PNG)]
        assert cancelled == [True]

    asyncio.run(scenario())


def test_caller_cancellation_propagates_without_upload_retry():
    module = helper()

    async def scenario():
        entered, calls = asyncio.Event(), []

        class StuckStorage:
            async def put_exception(self, task_id, count, png):
                calls.append((task_id, count, png))
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(module.capture_task_failure(TASK_ID, lambda: PNG, StuckStorage()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [(TASK_ID, 1, PNG)]

    asyncio.run(scenario())


@pytest.mark.parametrize('actual_count', [None, True, 0, -1, 'private-invalid-counter'])
def test_invalid_upload_receipt_stops_without_potential_duplicate(actual_count, caplog):
    module = helper()
    storage = Storage(actual_count=actual_count)
    with caplog.at_level(logging.INFO):
        asyncio.run(module.capture_task_failure(TASK_ID, lambda: PNG, storage))
    assert storage.calls == [(TASK_ID, 1, PNG)]
    assert 'private-invalid-counter' not in caplog.text
    assert caplog.records and all(getattr(record, 'backend_only', False) for record in caplog.records)


def test_untrusted_log_labels_do_not_leak_task_or_exception_text(caplog):
    module = helper()
    storage = Storage()
    unusual_error = type('private-type-name\nsecret=credential', (Exception,), {})

    def capture():
        raise unusual_error('private-exception-body')

    with caplog.at_level(logging.INFO):
        asyncio.run(module.capture_task_failure('private-task\nsecret=credential', capture, storage))
    assert storage.calls == []
    assert 'private-' not in caplog.text
    assert 'secret=credential' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
