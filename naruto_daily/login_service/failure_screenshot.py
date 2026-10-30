"""Best-effort diagnostics before a failed task releases its bound device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import re
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import ObjectStorage


CAPTURE_TIMEOUT_SECONDS = 15.0
UPLOAD_TIMEOUT_SECONDS = 45.0
_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_MAX_PNG_BYTES = 16 * 1024 * 1024
logger = logging.LoggerAdapter(logging.getLogger(__name__), {'backend_only': True})


def _label(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value) else 'unknown'


async def _capture(capture: Callable[[], bytes]) -> tuple[object, str | None]:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[tuple[object, str | None]] = loop.create_future()

    def settle(png: object, reason: str | None) -> None:
        if not result.done():
            result.set_result((png, reason))

    def run() -> None:
        png, reason = None, None
        try:
            png = capture()
        except BaseException as error:
            # A thread traceback can expose subprocess output. Transfer only a
            # bounded exception type, including cancellation inside capture.
            reason = _label(type(error).__name__)
        try:
            loop.call_soon_threadsafe(settle, png, reason)
        except RuntimeError:
            # A timed-out daemon may finish after the caller's loop closes.
            pass

    # Child stdout/stderr reads may have occupied the default executor.
    threading.Thread(target=run, name='task-failure-capture', daemon=True).start()
    return await result


async def capture_task_failure(task_id: str, capture: Callable[[], bytes], storage: ObjectStorage) -> None:
    """Capture once, then upload with bounded retries; never alter task state.

    The caller owns device binding and cleanup. Caller cancellation propagates;
    diagnostic failures and either timeout do not replace the task's failure.
    """
    log_task_id = _label(task_id)
    try:
        async with asyncio.timeout(CAPTURE_TIMEOUT_SECONDS):
            png, reason = await _capture(capture)
    except TimeoutError:
        logger.warning('任务失败截图未保存 taskId=%s stage=capture reason=timeout', log_task_id)
        return
    except Exception as error:
        logger.warning('任务失败截图未保存 taskId=%s stage=capture reason=%s',
                       log_task_id, _label(type(error).__name__))
        return
    if reason is not None:
        logger.warning('任务失败截图未保存 taskId=%s stage=capture reason=%s', log_task_id, reason)
        return
    if not isinstance(png, bytes) or not png.startswith(_PNG_SIGNATURE) or len(png) > _MAX_PNG_BYTES:
        logger.warning('任务失败截图未保存 taskId=%s stage=capture reason=invalid_png', log_task_id)
        return

    try:
        # One deadline covers all attempts. Cancelling a storage await cannot
        # stop its SDK daemon, so a timeout must never start another upload.
        async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
            for _ in range(3):
                try:
                    actual_count = await storage.put_exception(task_id, 1, png)
                except Exception as error:
                    logger.warning('任务失败截图未保存 taskId=%s stage=upload reason=%s',
                                   log_task_id, _label(type(error).__name__))
                    continue
                if type(actual_count) is not int or actual_count < 1:
                    # The write may have succeeded; do not duplicate it just
                    # because its receipt violates the storage contract.
                    logger.warning('任务失败截图未保存 taskId=%s stage=upload reason=invalid_counter', log_task_id)
                    return
                logger.info('任务失败截图已保存 taskId=%s stage=upload number=%s', log_task_id, actual_count)
                return
    except TimeoutError:
        logger.warning('任务失败截图未保存 taskId=%s stage=upload reason=timeout', log_task_id)
