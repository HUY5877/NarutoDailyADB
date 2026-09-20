from __future__ import annotations

from typing import Literal

from .config import Settings


TaskName = Literal["login", "ichiraku_takeout", "equipment_upgrade", "organization_pray", "sign_in", "stamina", "recruit", "abundance", "survival_challenge", "secret_realm", "secret_realm_farm"]
TASK_ORDER: tuple[TaskName, ...] = ("login", "ichiraku_takeout", "equipment_upgrade", "organization_pray", "recruit", "sign_in", "stamina", "abundance", "survival_challenge", "secret_realm")


class TaskError(ValueError):
    """请求的任务不存在。"""


def build_task(settings: Settings, name: TaskName) -> list[dict]:
    if name == "secret_realm_farm":
        # 仅使用登录本身，不能递归调用 build_task("login")，否则会带上每日任务。
        entry = settings.tasks.get("secret_realm_farm", [])
        secret = settings.tasks.get("secret_realm", [])
        shared_start = next((i for i, a in enumerate(secret) if a["type"] == "handle_secret_realm"), None)
        if not entry or shared_start is None:
            raise TaskError("刷秘境线路缺少入口或公共秘境处理配置，请更新配置文件")
        return [dict(a) for a in [*settings.tasks.get("login", []), *entry, *secret[shared_start:]]]
    if name not in TASK_ORDER:
        raise TaskError(f"未知任务：{name!r}")
    task_names = ("login", "ichiraku_takeout", "equipment_upgrade", "organization_pray", "recruit", "abundance", "survival_challenge", "secret_realm") if name == "login" else (name,)
    return [dict(action) for task_name in task_names for action in settings.tasks.get(task_name, [])]


def build_all(settings: Settings) -> list[dict]:
    return [dict(action) for name in TASK_ORDER for action in settings.tasks.get(name, [])]
