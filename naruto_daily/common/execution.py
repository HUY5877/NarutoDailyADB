"""One daily deadline, with optional tighter limits for recoverable phases."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import time
from typing import Callable, Iterator, NoReturn, TypeVar


DAILY_TIMEOUT_SECONDS = 50 * 60
MODEL_REQUEST_TIMEOUT_SECONDS = 30
LOGGER = logging.getLogger("naruto_daily")
T = TypeVar("T")


class DailyDeadlineExceeded(TimeoutError):
    """The shared execution deadline has expired; no further input is allowed."""


class PhaseDeadlineExceeded(DailyDeadlineExceeded):
    """Only the current phase has expired; its caller may recover afterward."""


@dataclass(frozen=True)
class ExecutionBudget:
    deadline: float
    clock: Callable[[], float] = time.monotonic
    sleep_impl: Callable[[float], None] = time.sleep

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            self.raise_deadline_exceeded()
        return remaining

    def raise_deadline_exceeded(self, message: str | None = None) -> NoReturn:
        raise DailyDeadlineExceeded(message or "执行总时限已到，停止全部操作（日常总时限最多 50 分钟）")

    def sleep(self, seconds: float) -> None:
        self.sleep_impl(min(max(0.0, seconds), self.remaining()))
        self.remaining()


@dataclass(frozen=True)
class _PhaseExecutionBudget(ExecutionBudget):
    parent: ExecutionBudget | None = None

    def remaining(self) -> float:
        if self.parent is not None:
            self.parent.remaining()
        return super().remaining()

    def raise_deadline_exceeded(self, message: str | None = None) -> NoReturn:
        if self.parent is not None:
            self.parent.remaining()
            # A bounded operation can time out just before the injected clock
            # advances. Classify by the effective deadline even in that case.
            if self.parent.deadline <= self.deadline:
                self.parent.raise_deadline_exceeded(message)
        raise PhaseDeadlineExceeded(message or "本阶段执行时限已到，停止本阶段操作")


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


@contextmanager
def execution_phase_scope(
    seconds: float, *, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[ExecutionBudget]:
    """Temporarily tighten the active budget without replacing its deadline."""
    parent = current_execution()
    if parent is not None:
        parent.remaining()
        clock, sleep = parent.clock, parent.sleep_impl
    deadline = clock() + float(seconds)
    if parent is not None:
        deadline = min(deadline, parent.deadline)
    budget = _PhaseExecutionBudget(deadline, clock, sleep, parent)
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
    from naruto_daily.multimodal.diagnostics import is_model_api_failure
    from naruto_daily.common.visual import NonRetryableVisualError, VisualError

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
            LOGGER.warning("%s接口暂时失败；10 秒后重新截图重试，总时限剩余 %.2f 秒：%s", name, remaining, exc,
                           extra={'backend_only': is_model_api_failure(exc)})
            budget.sleep(10)
        except NonRetryableVisualError:
            raise
        except VisualError as exc:
            budget = current_execution()
            if budget is None:
                raise
            remaining = budget.remaining()
            LOGGER.info("%s尚未确认；1 秒后重新截图，总时限剩余 %.2f 秒：%s", name, remaining, exc)
            budget.sleep(1)


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
