from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when configuration is missing, ambiguous, or unsafe."""


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
TASK_NAMES = {"sign_in", "stamina", "recruit", "abundance"}
ACTION_KEYS = {
    "tap": {"type", "x", "y"},
    "swipe": {"type", "x1", "y1", "x2", "y2", "duration_ms"},
    "wait": {"type", "seconds"},
    "back": {"type"},
}


def _require_exact_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ConfigError(f"{context} has unknown keys: {sorted(unknown)}")
    if missing:
        raise ConfigError(f"{context} is missing keys: {sorted(missing)}")


def _coordinate(value: Any, limit: int, context: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < limit:
        raise ConfigError(f"{context} coordinate must be between 0 and {limit - 1}")


def _validate_action(action: Any, width: int, height: int, context: str) -> float:
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        raise ConfigError(f"{context} must be an action object with a type")
    action_type = action["type"]
    if action_type not in ACTION_KEYS:
        raise ConfigError(f"{context} has unsupported action: {action_type!r}")
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
            raise ConfigError(f"{context} duration_ms must be between 1 and 10000")
    elif action_type == "wait":
        seconds = action["seconds"]
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds <= 300:
            raise ConfigError(f"{context} wait must be between 0 and 300 seconds")
        return float(seconds)
    return 0.0


def load_settings(path: Path) -> Settings:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to load configuration: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("Configuration root must be an object")
    _require_exact_keys(payload, ROOT_KEYS, "configuration")
    screen = payload["screen"]
    if not isinstance(screen, dict):
        raise ConfigError("screen must be an object")
    _require_exact_keys(screen, {"width", "height"}, "screen")
    width, height = screen["width"], screen["height"]
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise ConfigError("screen width must be a positive integer")
    if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
        raise ConfigError("screen height must be a positive integer")
    tasks = payload["tasks"]
    if not isinstance(tasks, dict):
        raise ConfigError("tasks must be an object")
    _require_exact_keys(tasks, TASK_NAMES, "tasks")
    validated: dict[str, list[dict[str, Any]]] = {}
    for task_name, actions in tasks.items():
        if not isinstance(actions, list):
            raise ConfigError(f"task {task_name} must be a list")
        if len(actions) > 100:
            raise ConfigError(f"task {task_name} exceeds 100 actions")
        total_wait = sum(_validate_action(action, width, height, f"{task_name}[{index}]") for index, action in enumerate(actions))
        if total_wait > 300:
            raise ConfigError(f"task {task_name} exceeds 300 seconds")
        validated[task_name] = actions
    serial = payload["serial"]
    if serial is not None and (not isinstance(serial, str) or not serial.strip()):
        raise ConfigError("serial must be null or a non-empty string")
    if not isinstance(payload["adb_path"], str) or not payload["adb_path"].strip():
        raise ConfigError("adb_path must be a non-empty string")
    return Settings(Path(payload["adb_path"]), serial, width, height, validated)
