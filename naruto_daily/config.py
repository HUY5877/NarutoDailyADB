from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """配置缺失、含义不明确或不符合安全限制。"""


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Settings:
    adb_path: Path
    serial: str | None
    screen_width: int
    screen_height: int
    tasks: dict[str, list[dict[str, Any]]]


ROOT_KEYS = {"adb_path", "serial", "screen", "tasks"}
TASK_NAMES = {"login", "sign_in", "stamina", "recruit", "abundance"}
ACTION_KEYS = {
    "tap": {"type", "x", "y"},
    "swipe": {"type", "x1", "y1", "x2", "y2", "duration_ms"},
    "wait": {"type", "seconds"},
    "back": {"type"},
    "optional_template_tap": {"type", "template", "left", "top", "right", "bottom", "threshold"},
}


def _require_exact_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ConfigError(f"{context} 包含未知字段：{sorted(unknown)}")
    if missing:
        raise ConfigError(f"{context} 缺少字段：{sorted(missing)}")


def _coordinate(value: Any, limit: int, context: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < limit:
        raise ConfigError(f"{context} 坐标必须在 0 到 {limit - 1} 之间")


def _validate_action(action: Any, width: int, height: int, context: str) -> float:
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        raise ConfigError(f"{context} 必须是包含 type 的动作对象")
    action_type = action["type"]
    if action_type not in ACTION_KEYS:
        raise ConfigError(f"{context} 包含不支持的动作：{action_type!r}")
    _require_exact_keys(action, ACTION_KEYS[action_type], context)
    if action_type == "tap":
        _coordinate(action["x"], width, context)
        _coordinate(action["y"], height, context)
    elif action_type == "swipe":
        _coordinate(action["x1"], width, context)
        _coordinate(action["x2"], width, context)
        _coordinate(action["y1"], height, context)
        _coordinate(action["y2"], height, context)
        if not isinstance(action["duration_ms"], int) or not 1 <= action["duration_ms"] <= 10_000:
            raise ConfigError(f"{context} 的 duration_ms 必须在 1 到 10000 之间")
    elif action_type == "wait":
        seconds = action["seconds"]
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds <= 300:
            raise ConfigError(f"{context} 的等待时间必须在 0 到 300 秒之间")
        return float(seconds)
    elif action_type == "optional_template_tap":
        template = action["template"]
        if not isinstance(template, str) or not template.strip() or Path(template).is_absolute() or ".." in Path(template).parts:
            raise ConfigError(f"{context} 的模板必须是配置目录内的相对路径")
        left, top, right, bottom = (action[key] for key in ("left", "top", "right", "bottom"))
        _coordinate(left, width, context)
        _coordinate(top, height, context)
        if not isinstance(right, int) or not left < right <= width:
            raise ConfigError(f"{context} 的 right 必须大于 left 且不超过屏幕宽度")
        if not isinstance(bottom, int) or not top < bottom <= height:
            raise ConfigError(f"{context} 的 bottom 必须大于 top 且不超过屏幕高度")
        threshold = action["threshold"]
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 1:
            raise ConfigError(f"{context} 的 threshold 必须大于 0 且不超过 1")
    return 0.0


def load_settings(path: Path) -> Settings:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法加载配置：{exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("配置根节点必须是对象")
    _require_exact_keys(payload, ROOT_KEYS, "configuration")
    screen = payload["screen"]
    if not isinstance(screen, dict):
        raise ConfigError("screen 必须是对象")
    _require_exact_keys(screen, {"width", "height"}, "screen")
    width, height = screen["width"], screen["height"]
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise ConfigError("screen width 必须是正整数")
    if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
        raise ConfigError("screen height 必须是正整数")
    tasks = payload["tasks"]
    if not isinstance(tasks, dict):
        raise ConfigError("tasks 必须是对象")
    _require_exact_keys(tasks, TASK_NAMES, "tasks")
    validated: dict[str, list[dict[str, Any]]] = {}
    for task_name, actions in tasks.items():
        if not isinstance(actions, list):
            raise ConfigError(f"任务 {task_name} 必须是列表")
        if len(actions) > 100:
            raise ConfigError(f"任务 {task_name} 超过 100 个动作")
        total_wait = sum(_validate_action(action, width, height, f"{task_name}[{index}]") for index, action in enumerate(actions))
        if total_wait > 300:
            raise ConfigError(f"任务 {task_name} 超过 300 秒")
        validated[task_name] = actions
    serial = payload["serial"]
    if serial is not None and (not isinstance(serial, str) or not serial.strip()):
        raise ConfigError("serial 必须是 null 或非空字符串")
    if not isinstance(payload["adb_path"], str) or not payload["adb_path"].strip():
        raise ConfigError("adb_path 必须是非空字符串")
    return Settings(Path(payload["adb_path"]), serial, width, height, validated)
