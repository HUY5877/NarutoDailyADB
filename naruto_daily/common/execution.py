"""One absolute deadline shared by login cleanup, daily actions and retries."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import time
from typing import Callable, Iterator, TypeVar


DAILY_TIMEOUT_SECONDS = 35 * 60
MODEL_REQUEST_TIMEOUT_SECONDS = 30
LOGGER = logging.getLogger("naruto_daily")
T = TypeVar("T")


class DailyDeadlineExceeded(TimeoutError):
    """The shared execution deadline has expired; no further input is allowed."""


@dataclass(frozen=True)
class ExecutionBudget:
    deadline: float
    clock: Callable[[], float] = time.monotonic
    sleep_impl: Callable[[float], None] = time.sleep

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise DailyDeadlineExceeded("执行总时限已到，停止全部操作（日常总时限最多 35 分钟）")
        return remaining

    def sleep(self, seconds: float) -> None:
        self.sleep_impl(min(max(0.0, seconds), self.remaining()))
        self.remaining()


_execution: ContextVar[ExecutionBudget | None] = ContextVar("daily_execution", default=None)


def current_execution() -> ExecutionBudget | None:
    return _execution.get()


@contextmanager
def execution_scope(
    deadline: float, *, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[ExecutionBudget]:
    existing = current_execution()
    # A nested phase must never restart or extend the enclosing deadline.
    budget = existing if existing is not None else ExecutionBudget(deadline, clock, sleep)
    token = _execution.set(budget)
    try:
        budget.remaining()
        yield budget
        budget.remaining()
    finally:
        _execution.reset(token)


def stage_timeout(legacy_seconds: float) -> float:
    budget = current_execution()
    return budget.remaining() if budget is not None else float(legacy_seconds)


def check_execution() -> None:
    budget = current_execution()
    if budget is not None:
        budget.remaining()


def execution_sleep(seconds: float, sleep: Callable[[float], None] = time.sleep) -> None:
    budget = current_execution()
    if budget is None:
        sleep(seconds)
    else:
        # Honor the caller's sleep implementation, including test clocks.
        sleep(min(max(0.0, seconds), budget.remaining()))
        budget.remaining()


def retry_observation(observe: Callable[[], T], *, name: str = "识别") -> T:
    """Retry only a read-only observation, recapturing its entire frame/metadata."""
    from naruto_daily.multimodal.transport import MultimodalRequestError

    while True:
        check_execution()
        try:
            result = observe()
            check_execution()
            return result
        except DailyDeadlineExceeded:
            raise
        except MultimodalRequestError as exc:
            budget = current_execution()
            if budget is None or not getattr(exc, "retryable", False):
                raise
            remaining = budget.remaining()
            LOGGER.warning("%s接口暂时失败；10 秒后重新截图重试，总时限剩余 %.2f 秒：%s", name, remaining, exc)
            budget.sleep(10)


def action_with_execution_budget(action: dict) -> dict:
    """Replace legacy phase caps in a copy, without changing saved settings."""
    budget = current_execution()
    if budget is None:
        return action
    remaining = budget.remaining()

    def copy(value, model_config=False):
        if isinstance(value, list):
            return [copy(item, model_config) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == "timeout_seconds" or key.endswith("_timeout_seconds"):
                result[key] = min(float(item), MODEL_REQUEST_TIMEOUT_SECONDS) if model_config else remaining
            else:
                result[key] = copy(item, model_config or key == "multimodal")
        return result

    return copy(action)
