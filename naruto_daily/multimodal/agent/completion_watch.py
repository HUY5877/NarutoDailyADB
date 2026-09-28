"""Read-only completion checks that can interrupt an in-flight navigation request."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

LOGGER = logging.getLogger('naruto_daily')
POLL_SECONDS = 0.5
CLEANUP_GRACE_SECONDS = 0.5
T = TypeVar('T')


class CompletionDetected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class CompletionWatch:
    """Run probes and device input in the same event loop, never in a worker thread.

    screenshot/probe/swipe are synchronous, so a movement cannot interleave with
    a probe. Once set, reason is latched before cancelling the SDK; every tool
    also checks this latch, including tool callbacks arriving during cancellation.
    """

    def __init__(self, client, probe: Callable[[bytes], str | None] | None, *,
                 on_complete: Callable[[], None] | None = None):
        if probe is not None and not callable(probe):
            raise ValueError('completion_probe 必须是截图检测函数')
        self.client = client
        self.probe = probe
        self.reason: str | None = None
        self.on_complete = on_complete

    def check(self, png: bytes) -> None:
        self.raise_if_complete()
        if self.probe is None:
            return
        reason = self.probe(png)
        if reason is not None:
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError('completion_probe 完成时必须返回非空原因文字')
            self.reason = reason
            if self.on_complete is not None:
                self.on_complete()
            LOGGER.info('本地画面已确认导航完成，停止移动和模型等待：%s', reason)
            self.raise_if_complete()

    def raise_if_complete(self) -> None:
        if self.reason is not None:
            raise CompletionDetected(self.reason)

    def check_fresh(self) -> None:
        self.raise_if_complete()
        if self.probe is not None:
            self.check(self.client.screenshot())

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            self.check_fresh()

    async def run(self, work: Awaitable[T]) -> T:
        if self.probe is None:
            return await work
        navigation = asyncio.create_task(work)
        monitor = asyncio.create_task(self._monitor())
        try:
            done, _ = await asyncio.wait((navigation, monitor), return_when=asyncio.FIRST_COMPLETED)
            # Completion wins even if an obsolete model response failed concurrently.
            self.raise_if_complete()
            if monitor in done:
                await monitor
            return await navigation
        finally:
            for task in (navigation, monitor):
                if not task.done():
                    task.cancel()
            cleanup = asyncio.gather(navigation, monitor, return_exceptions=True)
            if self.reason is None:
                await cleanup
            else:
                try:
                    # A second native asyncio cancellation interrupts slow SDK
                    # __aexit__ cleanup, including anyio's shielded close path.
                    # Native CLI cleanup has already killed its own child process.
                    await asyncio.wait_for(cleanup, timeout=CLEANUP_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    LOGGER.info('导航模型清理超过 %.1f 秒，已取消剩余等待', CLEANUP_GRACE_SECONDS)
