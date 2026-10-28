"""Serialize Windows worker processes starting the shared ADB server on 5037."""
from contextlib import contextmanager
import ctypes
import math
import os
from threading import Lock

from .execution import check_execution, current_execution


MUTEX_NAME = r'Local\NarutoDailyADB.AdbServer5037'
DEFAULT_WAIT_SECONDS = 20.0
_fallback_lock = Lock()


class AdbServerLockError(RuntimeError):
    """The server lock could not be acquired or released."""


def _windows_api():
    from ctypes import wintypes

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _wait_timeout(budget, limited_by_budget):
    if limited_by_budget:
        budget.raise_deadline_exceeded('等待 ADB server 锁达到执行截止时间')
    check_execution()
    raise AdbServerLockError('等待 ADB server 锁超时')


@contextmanager
def battle_server_lock():
    """Use a named mutex on Windows; other platforms use a process-local lock."""
    check_execution()
    budget = current_execution()
    remaining = budget.remaining() if budget is not None else DEFAULT_WAIT_SECONDS
    seconds = min(DEFAULT_WAIT_SECONDS, remaining)
    limited_by_budget = budget is not None and remaining <= DEFAULT_WAIT_SECONDS
    if os.name != 'nt':
        acquired = _fallback_lock.acquire(timeout=seconds)
        if not acquired:
            _wait_timeout(budget, limited_by_budget)
        try:
            check_execution()
            yield
        finally:
            _fallback_lock.release()
        return

    api = _windows_api()
    handle = api.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise AdbServerLockError(f'创建 ADB server 锁失败（Windows 错误码 {ctypes.get_last_error()}）')
    owned = False
    try:
        result = api.WaitForSingleObject(handle, max(1, math.ceil(seconds * 1000)))
        if result == 258:
            _wait_timeout(budget, limited_by_budget)
        if result not in (0, 0x80):
            raise AdbServerLockError(f'等待 ADB server 锁失败（Windows 错误码 {ctypes.get_last_error()}）')
        # WAIT_ABANDONED also transfers ownership after the previous owner exits.
        owned = True
        check_execution()
        yield
    finally:
        try:
            if owned and not api.ReleaseMutex(handle):
                raise AdbServerLockError(f'释放 ADB server 锁失败（Windows 错误码 {ctypes.get_last_error()}）')
        finally:
            api.CloseHandle(handle)
