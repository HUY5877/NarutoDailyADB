from __future__ import annotations

from typing import Literal

from .config import Settings


TaskName = Literal["login", "organization_pray", "sign_in", "stamina", "recruit", "abundance", "secret_realm"]
TASK_ORDER: tuple[TaskName, ...] = ("login", "organization_pray", "recruit", "sign_in", "stamina", "abundance", "secret_realm")


class TaskError(ValueError):
    """请求的任务不存在。"""


def build_task(settings: Settings, name: TaskName) -> list[dict]:
    if name not in TASK_ORDER:
        raise TaskError(f"未知任务：{name!r}")
    task_names = ("login", "organization_pray", "recruit", "abundance", "secret_realm") if name == "login" else (name,)
    return [dict(action) for task_name in task_names for action in settings.tasks.get(task_name, [])]


def build_all(settings: Settings) -> list[dict]:
    return [dict(action) for name in TASK_ORDER for action in settings.tasks.get(name, [])]
