"""奖励入口坐标仅在同一任务、同一客户端及相同截图尺寸内复用。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass
class RewardButtonCache:
    _entry: tuple[tuple[int, int], tuple[tuple[int, int], float]] | None = None

    def get(self, size):
        if self._entry is not None and self._entry[0] != size:
            self.clear()
        return None if self._entry is None else self._entry[1]

    def remember(self, size, result):
        center, score = result
        self._entry = size, (tuple(center), score)

    def clear(self):
        self._entry = None


_task_cache: ContextVar[tuple[object, RewardButtonCache] | None] = ContextVar(
    'reward_button_task_cache', default=None,
)


def current_reward_button_cache(client) -> RewardButtonCache | None:
    current = _task_cache.get()
    return current[1] if current is not None and current[0] is client else None


@contextmanager
def reward_button_cache_scope(client) -> Iterator[RewardButtonCache]:
    existing = current_reward_button_cache(client)
    if existing is not None:
        yield existing
        return
    cache = RewardButtonCache()
    token = _task_cache.set((client, cache))
    try:
        yield cache
    finally:
        cache.clear()
        _task_cache.reset(token)
