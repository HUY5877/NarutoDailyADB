"""Parent-process diagnostics for individual daily module invocations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import ObjectStorage


MODULE_TIMEOUT_SECONDS = 600.0
POLL_SECONDS = 1.0
CAPTURE_TIMEOUT_SECONDS = 15
UPLOAD_TIMEOUT_SECONDS = 15.0
CLOSE_TIMEOUT_SECONDS = 65.0
_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_MAX_PNG_BYTES = 16 * 1024 * 1024
_SERIAL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]*')
_EXCLUDED_MODULES = frozenset({'enter_secret_realm', 'handle_secret_realm'})
logger = logging.LoggerAdapter(logging.getLogger(__name__), {'backend_only': True})


def _valid_serial(serial: object) -> bool:
    return (isinstance(serial, str) and serial.lower() != 'unknown'
            and _SERIAL.fullmatch(serial) is not None)


def _valid_png(png: object) -> bool:
    return (isinstance(png, bytes) and png.startswith(_PNG_SIGNATURE)
            and len(png) <= _MAX_PNG_BYTES)


def _log_label(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value) else 'unknown'


def capture_screenshot(adb_path: Path, serial: str) -> bytes:
    """Use a separate ADB process, without the business execution deadline."""
    if not _valid_serial(serial):
        raise ValueError('Invalid diagnostic device serial')
    result = subprocess.run(
        [str(adb_path), '-s', serial, 'exec-out', 'screencap', '-p'],
        capture_output=True,
        timeout=CAPTURE_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if result.returncode != 0 or not _valid_png(result.stdout):
        raise ValueError('Invalid diagnostic screenshot')
    return result.stdout


@dataclass
class _Invocation:
    name: str
    serial: str | None
    started_at: float
    triggered: bool = False


class ModuleTimeoutMonitor:
    """Track nested calls in the parent loop; never interrupt the worker.

    Construct and call this object on the parent's running event loop. A module
    end removes its timer but lets an already-triggered diagnostic finish.
    """

    def __init__(self, task_id: str, adb_path: Path, storage: ObjectStorage, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._task_id = task_id
        self._log_task_id = _log_label(task_id)
        self._adb_path = adb_path
        self._storage = storage
        self._clock = clock
        self._serial: str | None = None
        self._stack: list[_Invocation] = []
        self._count = 0
        self._upload_lock = asyncio.Lock()
        self._diagnostics: set[asyncio.Task[None]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._poll_task = asyncio.create_task(self._poll(), name='module-timeout-poll')

    def begin(self, name: str, serial: str) -> None:
        if self._closed:
            return
        # Account for a due parent even if its periodic check has not run yet.
        self.check()
        bound_serial = None
        log_name = _log_label(name)
        if not _valid_serial(serial) or (self._serial is not None and serial != self._serial):
            logger.warning('模块超时截图跳过：设备绑定无效，任务=%s 模块=%s', self._log_task_id, log_name)
        else:
            self._serial = serial
            if name not in _EXCLUDED_MODULES:
                bound_serial = serial
        # Excluded and invalid calls still occupy a stack entry, so their end
        # event cannot accidentally end the parent invocation.
        self._stack.append(_Invocation(log_name, bound_serial, self._clock()))

    def end(self) -> None:
        if self._closed:
            return
        self.check()
        if self._stack:
            self._stack.pop()

    def check(self) -> None:
        """Schedule each newly-due capture once, without awaiting diagnostic I/O."""
        if self._closed:
            return
        now = self._clock()
        for invocation in self._stack:
            if (invocation.serial is None or invocation.triggered
                    or now - invocation.started_at < MODULE_TIMEOUT_SECONDS):
                continue
            invocation.triggered = True
            logger.info('模块运行满时限，开始截图，任务=%s 模块=%s', self._log_task_id, invocation.name)
            diagnostic = asyncio.create_task(self._collect(invocation.name, invocation.serial),
                                             name='module-timeout-diagnostic')
            self._diagnostics.add(diagnostic)
            diagnostic.add_done_callback(self._diagnostics.discard)

    async def _poll(self) -> None:
        while not self._closed:
            await asyncio.sleep(POLL_SECONDS)
            self.check()

    async def _capture(self, serial: str) -> bytes:
        # Worker stdout/stderr reads may occupy every default executor thread.
        # A dedicated bounded ADB thread must still be able to collect evidence.
        loop = asyncio.get_running_loop()
        result: asyncio.Future[bytes] = loop.create_future()

        def settle(png: bytes | None) -> None:
            if result.done():
                return
            if png is None:
                result.set_exception(RuntimeError('Diagnostic capture failed'))
            else:
                result.set_result(png)

        def capture() -> None:
            try:
                png = capture_screenshot(self._adb_path, serial)
            except Exception:
                png = None
            try:
                loop.call_soon_threadsafe(settle, png)
            except RuntimeError:
                pass

        threading.Thread(target=capture, name='module-timeout-capture', daemon=True).start()
        return await result

    async def _collect(self, name: str, serial: str) -> None:
        try:
            png = await self._capture(serial)
            if not _valid_png(png):
                raise ValueError('Invalid diagnostic screenshot')
        except Exception:
            logger.warning('模块超时截图失败，任务继续执行，任务=%s 模块=%s', self._log_task_id, name)
            return
        # Capture promptly even while another image uploads. Only the counter
        # and its upload retries are serialized, independently of polling.
        async with self._upload_lock:
            count = self._count + 1
            try:
                # One shared budget, not a timeout that cancels and reissues
                # each request: a cancelled coroutine cannot stop its SDK
                # thread, and overlapping writes could save this image twice.
                async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS * 3):
                    for _ in range(3):
                        try:
                            actual_count = await self._storage.put_exception(self._task_id, count, png)
                            if type(actual_count) is not int or actual_count < count:
                                raise ValueError('Invalid diagnostic image counter')
                        except Exception:
                            logger.warning('模块超时截图上传失败，任务继续执行，任务=%s 模块=%s 编号=%s',
                                           self._log_task_id, name, count)
                        else:
                            self._count = actual_count
                            logger.info('模块超时截图已保存，任务=%s 模块=%s 编号=%s',
                                        self._log_task_id, name, actual_count)
                            return
            except TimeoutError:
                logger.warning('模块超时截图上传等待超时，停止重试，任务=%s 模块=%s 编号=%s',
                               self._log_task_id, name, count)

    def stop_tracking(self) -> None:
        """Freeze module timers while previously triggered diagnostics finish."""
        if not self._closed:
            # A hard worker deadline can end the task just before the periodic
            # poll runs. Preserve any diagnostic that became due meanwhile.
            self.check()
            self._closed = True
            self._stack.clear()
            self._poll_task.cancel()

    async def close(self) -> None:
        """Stop all timers and finish already-triggered work within a bound."""
        if self._close_task is None:
            self.stop_tracking()
            self._close_task = asyncio.create_task(self._drain(), name='module-timeout-close')
        cancelled = False
        # A cancellation of the business task must not abandon an upload or
        # the cleanup itself. Restore that cancellation once cleanup finishes.
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _drain(self) -> None:
        await asyncio.gather(self._poll_task, return_exceptions=True)
        pending = set(self._diagnostics)
        if not pending:
            return
        _, pending = await asyncio.wait(pending, timeout=CLOSE_TIMEOUT_SECONDS)
        if pending:
            logger.warning('模块超时截图清理超时，已停止剩余取证任务，任务=%s 模块=all', self._log_task_id)
            for diagnostic in pending:
                diagnostic.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
