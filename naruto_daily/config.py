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
TASK_NAMES = {"login", "organization_pray", "sign_in", "stamina", "recruit", "abundance"}
ACTION_KEYS = {
    "tap": {"type", "x", "y"},
    "swipe": {"type", "x1", "y1", "x2", "y2", "duration_ms"},
    "wait": {"type", "seconds"},
    "back": {"type"},
    "optional_template_tap": {"type", "template", "left", "top", "right", "bottom", "threshold"},
    "popup_cleanup": {"type", "rounds", "idle_rounds", "wait_seconds", "targets"},
    "required_template_tap": {
        "type", "name", "template", "left", "top", "right", "bottom", "threshold", "timeout_seconds", "poll_seconds"
    },
    "finish_login_authorization": {"type", "timeout_seconds", "poll_seconds", "second_authorization", "start_game"},
    "find_card_and_click": {
        "type", "name", "template", "left", "top", "right", "bottom", "threshold",
        "button_template", "button_threshold",
        "completed_template", "completed_threshold",
        "button_left_offset", "button_top_offset", "button_right_offset", "button_bottom_offset",
        "max_swipes",
        "swipe_x1", "swipe_y1", "swipe_x2", "swipe_y2",
        "swipe_duration_ms", "wait_seconds",
    },
    "handle_organization_prayer": {
        "type", "timeout_seconds", "poll_seconds", "wait_seconds", "max_claims",
        "dismiss_x", "dismiss_y", "yesterday_reward", "panel_marker", "claim_button", "coin_prayer",
    },
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


def _validate_template_target(target: Any, width: int, height: int, context: str, require_name: bool) -> None:
    keys = {"template", "left", "top", "right", "bottom", "threshold"}
    if require_name:
        keys.add("name")
    if not isinstance(target, dict):
        raise ConfigError(f"{context} 必须是模板目标对象")
    _require_exact_keys(target, keys, context)
    if require_name and (not isinstance(target["name"], str) or not target["name"].strip()):
        raise ConfigError(f"{context} 的 name 必须是非空字符串")
    template = target["template"]
    if not isinstance(template, str) or not template.strip() or Path(template).is_absolute() or ".." in Path(template).parts:
        raise ConfigError(f"{context} 的模板必须是配置目录内的相对路径")
    left, top, right, bottom = (target[key] for key in ("left", "top", "right", "bottom"))
    _coordinate(left, width, context)
    _coordinate(top, height, context)
    if not isinstance(right, int) or not left < right <= width:
        raise ConfigError(f"{context} 的 right 必须大于 left 且不超过屏幕宽度")
    if not isinstance(bottom, int) or not top < bottom <= height:
        raise ConfigError(f"{context} 的 bottom 必须大于 top 且不超过屏幕高度")
    threshold = target["threshold"]
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 1:
        raise ConfigError(f"{context} 的 threshold 必须大于 0 且不超过 1")


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
        target = {key: value for key, value in action.items() if key != "type"}
        _validate_template_target(target, width, height, context, require_name=False)
    elif action_type == "popup_cleanup":
        rounds = action["rounds"]
        idle_rounds = action["idle_rounds"]
        wait_seconds = action["wait_seconds"]
        targets = action["targets"]
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 10:
            raise ConfigError(f"{context} 的 rounds 必须在 1 到 10 之间")
        if not isinstance(idle_rounds, int) or isinstance(idle_rounds, bool) or not 1 <= idle_rounds <= rounds:
            raise ConfigError(f"{context} 的 idle_rounds 必须在 1 到 rounds 之间")
        if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 10:
            raise ConfigError(f"{context} 的 wait_seconds 必须在 0 到 10 之间")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 5:
            raise ConfigError(f"{context} 的 targets 必须包含 1 到 5 个目标")
        for index, target in enumerate(targets):
            _validate_template_target(target, width, height, f"{context}.targets[{index}]", require_name=True)
        return float(rounds * len(targets) * wait_seconds)
    elif action_type == "required_template_tap":
        target_keys = {"name", "template", "left", "top", "right", "bottom", "threshold"}
        target = {key: action[key] for key in target_keys}
        _validate_template_target(target, width, height, context, require_name=True)
        timeout_seconds = action["timeout_seconds"]
        poll_seconds = action["poll_seconds"]
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ConfigError(f"{context} 的 timeout_seconds 必须在 1 到 60 之间")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.2 <= poll_seconds <= 5:
            raise ConfigError(f"{context} 的 poll_seconds 必须在 0.2 到 5 之间")
        if poll_seconds > timeout_seconds:
            raise ConfigError(f"{context} 的 poll_seconds 不能大于 timeout_seconds")
        return float(timeout_seconds)
    elif action_type == "finish_login_authorization":
        timeout_seconds = action["timeout_seconds"]
        poll_seconds = action["poll_seconds"]
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ConfigError(f"{context} 的 timeout_seconds 必须在 1 到 60 之间")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.2 <= poll_seconds <= 5:
            raise ConfigError(f"{context} 的 poll_seconds 必须在 0.2 到 5 之间")
        if poll_seconds > timeout_seconds:
            raise ConfigError(f"{context} 的 poll_seconds 不能大于 timeout_seconds")

        second = action["second_authorization"]
        if not isinstance(second, dict):
            raise ConfigError(f"{context}.second_authorization 必须是对象")
        _require_exact_keys(
            second,
            {"name", "template", "left", "top", "right", "bottom", "threshold", "click_x", "click_y"},
            f"{context}.second_authorization",
        )
        second_target = {key: second[key] for key in ("name", "template", "left", "top", "right", "bottom", "threshold")}
        _validate_template_target(second_target, width, height, f"{context}.second_authorization", require_name=True)
        _coordinate(second["click_x"], width, f"{context}.second_authorization")
        _coordinate(second["click_y"], height, f"{context}.second_authorization")

        start_game = action["start_game"]
        _validate_template_target(start_game, width, height, f"{context}.start_game", require_name=True)
        return float(timeout_seconds)
    elif action_type == "find_card_and_click":
        target_keys = {"name", "template", "left", "top", "right", "bottom", "threshold"}
        target = {key: action[key] for key in target_keys}
        _validate_template_target(target, width, height, context, require_name=True)
        for prefix in ("button", "completed"):
            template = action[f"{prefix}_template"]
            if (
                not isinstance(template, str)
                or not template.strip()
                or Path(template).is_absolute()
                or ".." in Path(template).parts
            ):
                raise ConfigError(f"{context} 的 {prefix}_template 必须是配置目录内的相对路径")
            threshold = action[f"{prefix}_threshold"]
            if (
                not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
                or not 0 < threshold <= 1
            ):
                raise ConfigError(f"{context} 的 {prefix}_threshold 必须大于 0 且不超过 1")
        for key, limit in (
            ("button_left_offset", width), ("button_right_offset", width),
            ("button_top_offset", height), ("button_bottom_offset", height),
        ):
            value = action[key]
            if not isinstance(value, int) or isinstance(value, bool) or not -limit < value < limit:
                raise ConfigError(f"{context} 的 {key} 必须是绝对值小于 {limit} 的整数")
        if action["button_left_offset"] >= action["button_right_offset"]:
            raise ConfigError(f"{context} 的按钮搜索区域左右范围无效")
        if action["button_top_offset"] >= action["button_bottom_offset"]:
            raise ConfigError(f"{context} 的按钮搜索区域上下范围无效")
        max_swipes = action["max_swipes"]
        if not isinstance(max_swipes, int) or isinstance(max_swipes, bool) or not 0 <= max_swipes <= 10:
            raise ConfigError(f"{context} 的 max_swipes 必须在 0 到 10 之间")
        for key, limit in (
            ("swipe_x1", width), ("swipe_x2", width),
            ("swipe_y1", height), ("swipe_y2", height),
        ):
            _coordinate(action[key], limit, f"{context}.{key}")
        duration = action["swipe_duration_ms"]
        if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= 10_000:
            raise ConfigError(f"{context} 的 swipe_duration_ms 必须在 1 到 10000 之间")
        wait_seconds = action["wait_seconds"]
        if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 10:
            raise ConfigError(f"{context} 的 wait_seconds 必须在 0 到 10 之间")
        return float(max_swipes * wait_seconds)
    elif action_type == "handle_organization_prayer":
        timeout_seconds = action["timeout_seconds"]
        poll_seconds = action["poll_seconds"]
        wait_seconds = action["wait_seconds"]
        max_claims = action["max_claims"]
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ConfigError(f"{context} 的 timeout_seconds 必须在 1 到 60 之间")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.2 <= poll_seconds <= 5:
            raise ConfigError(f"{context} 的 poll_seconds 必须在 0.2 到 5 之间")
        if poll_seconds > timeout_seconds:
            raise ConfigError(f"{context} 的 poll_seconds 不能大于 timeout_seconds")
        if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= 10:
            raise ConfigError(f"{context} 的 wait_seconds 必须在 0 到 10 之间")
        if not isinstance(max_claims, int) or isinstance(max_claims, bool) or not 1 <= max_claims <= 3:
            raise ConfigError(f"{context} 的 max_claims 必须在 1 到 3 之间")
        _coordinate(action["dismiss_x"], width, context)
        _coordinate(action["dismiss_y"], height, context)
        for key in ("yesterday_reward", "panel_marker", "claim_button", "coin_prayer"):
            _validate_template_target(action[key], width, height, f"{context}.{key}", require_name=True)
        return float(3 * timeout_seconds + (max_claims + 2) * wait_seconds)
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
