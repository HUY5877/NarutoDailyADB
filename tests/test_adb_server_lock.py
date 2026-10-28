from contextlib import ExitStack, nullcontext
import importlib
import multiprocessing
import os
import time

import pytest

from naruto_daily.common import execution


def lock_module():
    return importlib.import_module('naruto_daily.common.adb_server_lock')


def acquire_lock():
    return lock_module().battle_server_lock()


def child_acquire(started, entered, results, budget_kind=None):
    try:
        with ExitStack() as stack:
            if budget_kind == 'daily':
                stack.enter_context(execution.execution_scope(time.monotonic() + .15))
            elif budget_kind == 'phase':
                stack.enter_context(execution.execution_scope(time.monotonic() + 10))
                stack.enter_context(execution.execution_phase_scope(.15))
            started.set()
            with acquire_lock():
                entered.set()
            results.put('acquired')
    except Exception as error:
        results.put(type(error).__name__)


def child_abandon_lock(entered, exit_requested):
    with acquire_lock():
        entered.set()
        if exit_requested.wait(5):
            os._exit(0)


def stop_child(process):
    process.join(3)
    if process.is_alive():
        process.terminate()
        process.join(3)


@pytest.mark.skipif(os.name != 'nt', reason='生产 Windows 使用具名 kernel mutex')
def test_named_mutex_excludes_another_real_process_until_release():
    context = multiprocessing.get_context('spawn')
    started, entered, results = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=child_acquire, args=(started, entered, results))
    try:
        with acquire_lock():
            process.start()
            assert started.wait(5)
            assert not entered.wait(.15), '第二进程不能在第一进程持锁时进入'
        assert entered.wait(3)
        assert results.get(timeout=3) == 'acquired'
    finally:
        stop_child(process)
        results.close()


@pytest.mark.skipif(os.name != 'nt', reason='生产 Windows 使用具名 kernel mutex')
def test_body_error_releases_mutex_for_another_real_process():
    with pytest.raises(RuntimeError, match='body failed'):
        with acquire_lock():
            raise RuntimeError('body failed')
    context = multiprocessing.get_context('spawn')
    started, entered, results = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=child_acquire, args=(started, entered, results))
    try:
        process.start()
        assert entered.wait(3)
        assert results.get(timeout=3) == 'acquired'
    finally:
        stop_child(process)
        results.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows owner 进程退出后的真实接管')
def test_abandoned_mutex_from_a_real_process_can_be_taken_over(monkeypatch):
    module = lock_module()
    context = multiprocessing.get_context('spawn')
    entered, exit_requested = context.Event(), context.Event()
    process = context.Process(target=child_abandon_lock, args=(entered, exit_requested))
    api, results = module._windows_api(), []
    wait = api.WaitForSingleObject

    def release_owner_and_wait(handle, milliseconds):
        exit_requested.set()
        result = wait(handle, milliseconds)
        results.append(result)
        return result

    monkeypatch.setattr(api, 'WaitForSingleObject', release_owner_and_wait)
    monkeypatch.setattr(module, '_windows_api', lambda: api)
    try:
        process.start()
        assert entered.wait(5)
        with module.battle_server_lock():
            assert results == [0x80]
    finally:
        exit_requested.set()
        stop_child(process)


@pytest.mark.skipif(os.name != 'nt', reason='生产 Windows 使用具名 kernel mutex')
@pytest.mark.parametrize('budget_kind, expected', [
    ('daily', 'DailyDeadlineExceeded'), ('phase', 'PhaseDeadlineExceeded'),
])
def test_real_mutex_wait_timeout_keeps_the_execution_deadline_type(budget_kind, expected):
    context = multiprocessing.get_context('spawn')
    started, entered, results = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=child_acquire, args=(started, entered, results, budget_kind))
    try:
        with acquire_lock():
            process.start()
            assert started.wait(5)
            assert results.get(timeout=3) == expected
            assert not entered.is_set()
    finally:
        stop_child(process)
        results.close()


@pytest.mark.parametrize('phase', [False, True])
def test_expired_execution_never_enters_the_lock_body(phase):
    now, entered = [0.0], []
    expected = execution.PhaseDeadlineExceeded if phase else execution.DailyDeadlineExceeded
    with pytest.raises(expected) as caught:
        with execution.execution_scope(3000 if phase else 5, clock=lambda: now[0]):
            with execution.execution_phase_scope(5) if phase else nullcontext():
                now[0] = 5
                with acquire_lock():
                    entered.append(True)
    assert type(caught.value) is expected
    assert entered == []


class TimedOutWindowsApi:
    def __init__(self):
        self.waits = []
        self.released, self.closed = [], []

    def CreateMutexW(self, security, owner, name):
        assert name == r'Local\NarutoDailyADB.AdbServer5037'
        return 123

    def WaitForSingleObject(self, handle, milliseconds):
        self.waits.append(milliseconds)
        return 258

    def ReleaseMutex(self, handle):
        self.released.append(handle)
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


@pytest.mark.skipif(os.name != 'nt', reason='Windows 等待结果和毫秒边界')
@pytest.mark.parametrize('daily_seconds, phase_seconds, expected, wait_ms', [
    (3000, 5, execution.PhaseDeadlineExceeded, 5000),
    (5, 180, execution.DailyDeadlineExceeded, 5000),
    (5, 5, execution.DailyDeadlineExceeded, 5000),
    (3000, .0001, execution.PhaseDeadlineExceeded, 1),
])
def test_wait_timeout_classifies_effective_budget_even_before_fake_clock_advances(
        monkeypatch, daily_seconds, phase_seconds, expected, wait_ms):
    module = lock_module()
    assert module is not None
    api = TimedOutWindowsApi()
    monkeypatch.setattr(module, '_windows_api', lambda: api)
    with pytest.raises(expected) as caught:
        with execution.execution_scope(daily_seconds, clock=lambda: 0):
            with execution.execution_phase_scope(phase_seconds):
                with module.battle_server_lock():
                    pytest.fail('WAIT_TIMEOUT 后不能进入临界区')
    assert type(caught.value) is expected
    assert api.waits == [wait_ms]
    assert api.released == [] and api.closed == [123]


@pytest.mark.skipif(os.name != 'nt', reason='Windows 默认等待上限')
def test_default_wait_limit_does_not_claim_that_a_longer_budget_expired(monkeypatch):
    module = lock_module()
    assert module is not None
    api = TimedOutWindowsApi()
    monkeypatch.setattr(module, '_windows_api', lambda: api)
    with execution.execution_scope(3000, clock=lambda: 0):
        with execution.execution_phase_scope(180) as phase:
            with pytest.raises(module.AdbServerLockError):
                with module.battle_server_lock():
                    pytest.fail('WAIT_TIMEOUT 后不能进入临界区')
            assert phase.remaining() == 180
    assert api.waits == [20000]
    assert api.closed == [123]


@pytest.mark.skipif(os.name != 'nt', reason='Windows 被遗弃的 mutex 可继续获取')
def test_abandoned_mutex_is_owned_and_released_normally(monkeypatch):
    module = lock_module()
    assert module is not None
    api, entered = TimedOutWindowsApi(), []
    api.WaitForSingleObject = lambda handle, milliseconds: 0x80
    monkeypatch.setattr(module, '_windows_api', lambda: api)
    with module.battle_server_lock():
        entered.append(True)
    assert entered == [True]
    assert api.released == [123] and api.closed == [123]


@pytest.mark.skipif(os.name != 'nt', reason='Windows 拿锁后复查预算并释放句柄')
def test_deadline_reached_while_acquiring_releases_mutex_without_entering_body(monkeypatch):
    module, now = lock_module(), [0.0]
    api = TimedOutWindowsApi()

    def acquire(handle, milliseconds):
        now[0] = 5
        return 0

    api.WaitForSingleObject = acquire
    monkeypatch.setattr(module, '_windows_api', lambda: api)
    with pytest.raises(execution.PhaseDeadlineExceeded):
        with execution.execution_scope(3000, clock=lambda: now[0]):
            with execution.execution_phase_scope(5):
                with module.battle_server_lock():
                    pytest.fail('拿锁期间预算到期，不应进入临界区')
    assert api.released == [123] and api.closed == [123]
