"""Real worker pipes and the real timeout monitor; only ADB/OSS are substituted."""

import asyncio

import pytest

from naruto_daily.login_service.storage import AliyunObjectStorage
from naruto_daily.login_service import runtime as runtime_module
from oss_fakes import FakeBucket
from test_login_service_daily_runtime import (
    AUTHORIZED, COMPLETED, LOGGED_IN, PNG, QR, emit, eventually, gate,
    make_runtime, record_with_ttl,
)


@pytest.fixture(autouse=True)
def no_failure_hold(monkeypatch):
    monkeypatch.setattr(runtime_module, 'FAILED_INSTANCE_HOLD_SECONDS', 0)


def module(name='return_to_main', state='started', serial='emulator-5570'):
    return {'type': 'module', 'state': state, 'name': name, 'serial': serial}


def test_runtime_accepts_module_boundaries_without_changing_completion(tmp_path, monkeypatch):
    async def scenario():
        record = record_with_ttl()
        plan = [emit(QR), emit(AUTHORIZED), emit(LOGGED_IN), emit(module()),
                emit(module(state='finished')), emit(COMPLETED)]
        runtime, _, _, pool = make_runtime(tmp_path, monkeypatch, record, plan)
        await runtime._process_record(record)
        assert record.daily_status == 'completed'
        assert pool.closed == [record.id]
    asyncio.run(scenario())


def test_blocked_child_is_captured_and_later_module_uses_next_filename(tmp_path, monkeypatch):
    from naruto_daily.login_service import module_timeout
    monkeypatch.setattr(module_timeout, 'MODULE_TIMEOUT_SECONDS', 0.05)
    monkeypatch.setattr(module_timeout, 'POLL_SECONDS', 0.01)
    captures = []

    def capture(adb_path, serial):
        captures.append((adb_path, serial))
        return PNG

    monkeypatch.setattr(module_timeout, 'capture_screenshot', capture)

    async def scenario():
        record = record_with_ttl()
        first, second = tmp_path / 'continue-first', tmp_path / 'continue-second'
        plan = [emit(QR), emit(AUTHORIZED), emit(LOGGED_IN), emit(module()), gate(first),
                emit(module(state='finished')), emit(module('handle_free_recruit')), gate(second),
                emit(module('handle_free_recruit', 'finished')), emit(COMPLETED)]
        runtime, _, _, pool = make_runtime(tmp_path, monkeypatch, record, plan)
        bucket = FakeBucket()
        runtime.storage = AliyunObjectStorage('login/test', runtime.config, bucket=bucket)
        work = asyncio.create_task(runtime._process_record(record))
        first_key = f'login/test/exceptions/{record.id}_1.png'
        second_key = f'login/test/exceptions/{record.id}_2.png'
        try:
            await eventually(lambda: first_key in bucket.objects)
            assert not work.done()  # the child is still blocked without new events
            assert len(captures) == 1
            first.touch()
            await eventually(lambda: second_key in bucket.objects)
            second.touch()
            await work
        finally:
            first.touch()
            second.touch()
            await work
        assert record.daily_status == 'completed'
        assert pool.closed == [record.id]
        assert captures == [(runtime.config.ldplayer_console_path.with_name('adb.exe'), 'emulator-5570')] * 2
        assert bucket.objects[first_key][0] == PNG
        assert bucket.objects[second_key][0] == PNG
    asyncio.run(scenario())


@pytest.mark.parametrize('events', [
    [module(state='finished')],
    [module(), module('handle_free_recruit', 'finished')],
    [module(serial='')],
    [module(), module('handle_free_recruit', serial='emulator-5572')],
])
def test_runtime_rejects_unbalanced_or_wrong_device_module_events(tmp_path, monkeypatch, events):
    async def scenario():
        record = record_with_ttl()
        runtime, _, _, _ = make_runtime(tmp_path, monkeypatch, record,
            [emit(QR), emit(AUTHORIZED), emit(LOGGED_IN), *map(emit, events), emit(COMPLETED)])
        await runtime._process_record(record)
        assert record.daily_status == 'failed'
        assert record.daily_error_code == 'protocol_error'
    asyncio.run(scenario())
