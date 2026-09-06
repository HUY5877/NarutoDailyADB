from __future__ import annotations

from typing import Literal

from .config import Settings


TaskName = Literal["login", "organization_pray", "sign_in", "stamina", "recruit", "abundance"]
TASK_ORDER: tuple[TaskName, ...] = ("login", "organization_pray", "recruit", "sign_in", "stamina", "abundance")


class TaskError(ValueError):
    """请求的任务不存在或超过安全限制。"""


def build_task(settings: Settings, name: TaskName) -> list[dict]:
    if name not in TASK_ORDER:
        raise TaskError(f"未知任务：{name!r}")
    task_names = ("login", "organization_pray", "recruit", "abundance") if name == "login" else (name,)
    return [dict(action) for task_name in task_names for action in settings.tasks[task_name]]


def build_all(settings: Settings) -> list[dict]:
    actions = [dict(action) for name in TASK_ORDER for action in settings.tasks[name]]
    if len(actions) > 100:
        raise TaskError("组合流程超过 100 个动作")
    wait_seconds = sum(float(action.get("seconds", 0)) for action in actions if action.get("type") == "wait")
    if wait_seconds > 300:
        raise TaskError("组合流程超过 300 秒")
    return actions
