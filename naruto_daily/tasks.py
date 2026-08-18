from __future__ import annotations

from typing import Literal

from .config import Settings


TaskName = Literal["sign_in", "stamina", "recruit", "abundance"]
TASK_ORDER: tuple[TaskName, ...] = ("sign_in", "stamina", "recruit", "abundance")


class TaskError(ValueError):
    """Raised when a requested task is unknown or exceeds safety limits."""


def build_task(settings: Settings, name: TaskName) -> list[dict]:
    if name not in TASK_ORDER:
        raise TaskError(f"Unknown task: {name!r}")
    return [dict(action) for action in settings.tasks[name]]


def build_all(settings: Settings) -> list[dict]:
    actions = [action for name in TASK_ORDER for action in build_task(settings, name)]
    if len(actions) > 100:
        raise TaskError("Combined flow exceeds 100 actions")
    wait_seconds = sum(float(action.get("seconds", 0)) for action in actions if action.get("type") == "wait")
    if wait_seconds > 300:
        raise TaskError("Combined flow exceeds 300 seconds")
    return actions
