"""A bounded in-memory Redis-list substitute with LPUSH/BRPOP FIFO semantics."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Generic, TypeVar


T = TypeVar("T")


class MockRedisQueue(Generic[T]):
    """Use from a single asyncio event loop, as Redis would be used by this service."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._items: deque[T] = deque()
        self._waiters: deque[asyncio.Future[T | None]] = deque()
        self._closed = False

    @property
    def length(self) -> int:
        return len(self._items)

    def lpush(self, item: T) -> bool:
        if self._closed:
            return False
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(item)
                return True
        if len(self._items) >= self._capacity:
            return False
        self._items.appendleft(item)
        return True

    async def brpop(self) -> T | None:
        if self._closed:
            return None
        if self._items:
            return self._items.pop()
        future: asyncio.Future[T | None] = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        try:
            return await future
        finally:
            if future in self._waiters:
                self._waiters.remove(future)

    def remove(self, predicate: Callable[[T], bool]) -> None:
        self._items = deque(item for item in self._items if not predicate(item))

    def close(self) -> None:
        self._closed = True
        self._items.clear()
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
