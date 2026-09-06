from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit



LOGGER = logging.getLogger("naruto_daily")


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
    "authorize_account": {
        "type", "account_name", "timeout_seconds", "poll_seconds",
        "pre_click_wait_seconds", "post_click_wait_seconds", "picker_marker",
        "multimodal", "switch_account", "agree_button",
    },
    "handle_abundance": {
        "type", "timeout_seconds", "pre_click_wait_seconds", "post_click_wait_seconds",
        "load_wait_seconds", "challenge_button", "result_return", "battle",
    },
    "tap": {"type", "x", "y"},
    "swipe": {"type", "x1", "y1", "x2", "y2", "duration_ms"},
    "wait": {"type", "seconds"},
    "back": {"type"},
    "optional_template_tap": {"type", "template", "left", "top", "right", "bottom", "threshold"},
    "popup_cleanup": {
        "type", "rounds", "idle_rounds", "wait_seconds", "pre_click_wait_seconds",
        "post_click_wait_seconds", "targets",
    },
    "return_to_main": {
        "type", "max_rounds", "idle_rounds", "poll_seconds",
        "pre_click_wait_seconds", "post_click_wait_seconds", "main_page_marker", "close_target",
    },
    "required_template_tap": {
        "type", "name", "template", "left", "top", "right", "bottom", "threshold",
        "timeout_seconds", "poll_seconds", "pre_click_wait_seconds", "post_click_wait_seconds",
    },
    "finish_login_authorization": {
        "type", "timeout_seconds", "poll_seconds", "pre_click_wait_seconds", "post_click_wait_seconds",
        "second_authorization", "start_game",
    },
    "find_card_and_click": {
        "type", "name", "template", "left", "top", "right", "bottom", "threshold",
        "button_template", "button_threshold",
        "completed_template", "completed_threshold",
        "button_left_offset", "button_top_offset", "button_right_offset", "button_bottom_offset",
        "max_swipes",
        "swipe_x1", "swipe_y1", "swipe_x2", "swipe_y2",
        "swipe_duration_ms", "wait_seconds", "pre_click_wait_seconds",
        "post_click_wait_seconds", "completed_skip_type",
    },
    "handle_organization_prayer": {
        "type", "timeout_seconds", "poll_seconds", "wait_seconds", "pre_click_wait_seconds",
        "post_click_wait_seconds", "max_claims",
        "dismiss_x", "dismiss_y", "yesterday_reward", "panel_marker", "claim_button", "coin_prayer",
    },
    "handle_coin_fortune": {
        "type", "clicks", "timeout_seconds", "poll_seconds",
        "pre_click_wait_seconds", "post_click_wait_seconds", "free_button",
    },
    "handle_free_recruit": {
        "type", "timeout_seconds", "poll_seconds",
        "pre_click_wait_seconds", "post_click_wait_seconds",
        "advanced_tab", "advanced_red_dot", "ordinary_tab", "ordinary_red_dot",
        "free_button", "result_confirm",
    },
}
CLICK_WAIT_KEYS = {"pre_click_wait_seconds", "post_click_wait_seconds"}


def _require_exact_keys(
    value: dict[str, Any],
    expected: set[str],
    context: str,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    unknown = set(value) - expected
    missing = expected - set(value) - optional
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


def _validate_click_waits(action: dict, context: str) -> tuple[float, float]:
    default_wait = 3 if action.get("type") in {"finish_login_authorization", "authorize_account"} else 1
    pre_wait = action.get("pre_click_wait_seconds", default_wait)
    post_wait = action.get("post_click_wait_seconds", default_wait)
    for key, value in (("pre_click_wait_seconds", pre_wait), ("post_click_wait_seconds", post_wait)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 10:
            raise ConfigError(f"{context} 的 {key} 必须在 0 到 10 之间")
    return float(pre_wait), float(post_wait)


def _normalize_action(action: Any, task_name: str) -> Any:
    """把已发布的旧动作配置迁移为当前安全结构。"""

    if not isinstance(action, dict):
        return action
    if task_name == "login" and action.get("type") in {
        "required_template_tap", "finish_login_authorization", "authorize_account",
    }:
        # 登录按钮保留较长等待；日常动作由执行器默认使用 1 秒。
        action = dict(action)
        action.setdefault("pre_click_wait_seconds", 3)
        action.setdefault("post_click_wait_seconds", 3)
    if action.get("type") == "return_to_main" and "main_page_marker" not in action:
        normalized = dict(action)
        normalized["main_page_marker"] = {
            "name": "主界面奖励入口",
            "template": "assets/reward_entry.png",
            "left": 1700,
            "top": 400,
            "right": 1920,
            "bottom": 700,
            "threshold": 0.90,
        }
        close_target = action.get("close_target")
        if isinstance(close_target, dict):
            normalized_close_target = dict(close_target)
            threshold = normalized_close_target.get("threshold")
            if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and threshold < 0.95:
                normalized_close_target["threshold"] = 0.95
            normalized["close_target"] = normalized_close_target
        LOGGER.info("检测到旧版回主界面配置，已增加主界面识别并提高关闭阈值")
        return normalized

    if action.get("type") != "finish_login_authorization":
        return action
    second = action.get("second_authorization")
    if not isinstance(second, dict):
        return action
    template = second.get("template")
    if (
        not isinstance(template, str)
        or Path(template).name != "qq_second_authorization_marker.png"
        or not {"click_x", "click_y"}.issubset(second)
    ):
        return action

    normalized = dict(action)
    new_template = Path(template).with_name("qq_agree_game_info.png").as_posix()
    normalized["second_authorization"] = {
        "name": "第二次授权同意按钮",
        "template": new_template,
        "left": 700,
        "top": 700,
        "right": 1220,
        "bottom": 930,
        "threshold": second.get("threshold", 0.85),
    }
    LOGGER.info("检测到旧版第二次授权配置，已迁移为按钮模板识别")
    return normalized


def _validate_action(action: Any, width: int, height: int, context: str) -> None:
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        raise ConfigError(f"{context} 必须是包含 type 的动作对象")
    action_type = action["type"]
    if action_type not in ACTION_KEYS:
        raise ConfigError(f"{context} 包含不支持的动作：{action_type!r}")
    if action_type in {
            "required_template_tap", "finish_login_authorization", "return_to_main",
            "handle_coin_fortune", "popup_cleanup", "handle_organization_prayer",
            "handle_free_recruit", "handle_abundance", "authorize_account",
    }:
        optional_keys = CLICK_WAIT_KEYS
    elif action_type == "find_card_and_click":
        optional_keys = {"completed_skip_type"} | CLICK_WAIT_KEYS
    else:
        optional_keys = set()
    _require_exact_keys(action, ACTION_KEYS[action_type], context, optional_keys)
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
        return
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
        _validate_click_waits(action, context)
        return
    elif action_type == "return_to_main":
        max_rounds = action["max_rounds"]
        idle_rounds = action["idle_rounds"]
        poll_seconds = action["poll_seconds"]
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or not 1 <= max_rounds <= 10:
            raise ConfigError(f"{context} 的 max_rounds 必须在 1 到 10 之间")
        if not isinstance(idle_rounds, int) or isinstance(idle_rounds, bool) or not 1 <= idle_rounds <= max_rounds:
            raise ConfigError(f"{context} 的 idle_rounds 必须在 1 到 max_rounds 之间")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.2 <= poll_seconds <= 5:
            raise ConfigError(f"{context} 的 poll_seconds 必须在 0.2 到 5 之间")
        _validate_click_waits(action, context)
        main_page_marker = action["main_page_marker"]
        _validate_template_target(
            main_page_marker,
            width,
            height,
            f"{context}.main_page_marker",
            require_name=True,
        )
        if not (
            main_page_marker["left"] >= 1500
            and main_page_marker["top"] >= 300
            and main_page_marker["right"] <= width
            and main_page_marker["bottom"] <= 800
        ):
            raise ConfigError(f"{context} 的主界面标记搜索区域必须限制在右侧奖励区域")
        if main_page_marker["threshold"] < 0.90:
            raise ConfigError(f"{context} 的主界面标记阈值不能低于 0.90")
        close_target = action["close_target"]
        _validate_template_target(close_target, width, height, f"{context}.close_target", require_name=True)
        if not (
            close_target["left"] >= 1500
            and close_target["top"] <= 200
            and close_target["right"] <= width
            and close_target["bottom"] <= 300
        ):
            raise ConfigError(f"{context} 的关闭按钮搜索区域必须限制在右上角")
        if close_target["threshold"] < 0.95:
            raise ConfigError(f"{context} 的关闭按钮阈值不能低于 0.95")
        return
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
        _validate_click_waits(action, context)
        return
    elif action_type == "authorize_account":
        if (width, height) != (1920, 1080):
            raise ConfigError(f'{context} 的账号选择只支持 1920×1080 安卓画面')
        if not isinstance(action['account_name'], str) or not action['account_name'].strip():
            raise ConfigError(f'{context}.account_name 必须是非空账号昵称')
        for key, lower, upper in [('timeout_seconds', 1, 60), ('poll_seconds', 0.2, 5)]:
            value = action[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
                raise ConfigError(f'{context}.{key} 必须在 {lower} 到 {upper} 之间')
        if action['poll_seconds'] > action['timeout_seconds']:
            raise ConfigError(f'{context}.poll_seconds 不能大于 timeout_seconds')
        _validate_click_waits(action, context)
        for key in ('picker_marker', 'switch_account', 'agree_button'):
            _validate_template_target(action[key], width, height, f'{context}.{key}', require_name=True)
            if action[key]['threshold'] < 0.9:
                raise ConfigError(f'{context}.{key} 的账号识别阈值不能低于 0.90')
        model = action['multimodal']
        if not isinstance(model, dict):
            raise ConfigError(f'{context}.multimodal 必须是对象')
        _require_exact_keys(model, {'base_url', 'model', 'api_key_file', 'timeout_seconds', 'max_calls'}, f'{context}.multimodal')
        url = urlsplit(model['base_url']) if isinstance(model['base_url'], str) else None
        if not url or url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ConfigError(f'{context}.multimodal.base_url 必须是 HTTPS API 地址')
        if not isinstance(model['model'], str) or not model['model'].strip():
            raise ConfigError(f'{context}.multimodal.model 必须是非空模型名称')
        key_file = model['api_key_file']
        if not isinstance(key_file, str) or not key_file.strip() or Path(key_file).is_absolute() or '..' in Path(key_file).parts:
            raise ConfigError(f'{context}.multimodal.api_key_file 必须是配置目录内的相对路径')
        request_timeout = model['timeout_seconds']
        if type(request_timeout) not in (float, int) or not 1 <= request_timeout <= 30:
            raise ConfigError(f'{context}.multimodal.timeout_seconds 必须在 1 到 30 之间')
        if type(model['max_calls']) is not int or not 2 <= model['max_calls'] <= 10:
            raise ConfigError(f'{context}.multimodal.max_calls 必须在 2 到 10 之间')
        return
    elif action_type == "finish_login_authorization":
        timeout_seconds = action["timeout_seconds"]
        poll_seconds = action["poll_seconds"]
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ConfigError(f"{context} 的 timeout_seconds 必须在 1 到 60 之间")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.2 <= poll_seconds <= 5:
            raise ConfigError(f"{context} 的 poll_seconds 必须在 0.2 到 5 之间")
        if poll_seconds > timeout_seconds:
            raise ConfigError(f"{context} 的 poll_seconds 不能大于 timeout_seconds")
        _validate_click_waits(action, context)

        second = action["second_authorization"]
        if not isinstance(second, dict):
            raise ConfigError(f"{context}.second_authorization 必须是对象")
        _require_exact_keys(
            second,
            {"name", "template", "left", "top", "right", "bottom", "threshold"},
            f"{context}.second_authorization",
        )
        _validate_template_target(second, width, height, f"{context}.second_authorization", require_name=True)

        start_game = action["start_game"]
        _validate_template_target(start_game, width, height, f"{context}.start_game", require_name=True)
        return
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
        _validate_click_waits(action, context)
        completed_skip_type = action.get("completed_skip_type", "handle_organization_prayer")
        if completed_skip_type not in {
            "handle_organization_prayer", "handle_coin_fortune", "handle_free_recruit", "handle_abundance",
        }:
            raise ConfigError(f"{context} 的 completed_skip_type 不是允许的页面处理动作")
        return
    elif action_type == "handle_abundance":
        if (width, height) != (1920, 1080):
            raise ConfigError(f"{context} 只支持 1920×1080 安卓画面")
        _validate_click_waits(action, context)
        timeout = action["timeout_seconds"]
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
            raise ConfigError(f"{context} 的 timeout_seconds 必须在 1 到 60 之间")
        if isinstance(action["load_wait_seconds"], bool) or action["load_wait_seconds"] != 10:
            raise ConfigError(f"{context} 的战斗加载等待必须为 10 秒")
        for key in ("challenge_button", "result_return"):
            _validate_template_target(action[key], width, height, f"{context}.{key}", require_name=True)
        challenge = action["challenge_button"]
        if not (challenge["left"] >= 500 and challenge["right"] <= 1000 and challenge["top"] >= 800):
            raise ConfigError(f"{context} 的挑战按钮必须限制在底部中间偏左，避开一键完成")
        if challenge["threshold"] < 0.85:
            raise ConfigError(f"{context} 的挑战识别阈值不能低于 0.85")
        battle = action["battle"]
        if not isinstance(battle, dict):
            raise ConfigError(f"{context}.battle 必须是对象")
        _require_exact_keys(battle, {"start_timeout_seconds", "timeout_seconds", "tap_interval_seconds",
                                    "battle_marker", "end_targets", "buttons"}, f"{context}.battle")
        for key, lower, upper in (("start_timeout_seconds", 1, 60), ("timeout_seconds", 1, 180),
                                  ("tap_interval_seconds", 0.1, 2)):
            value = battle[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not lower <= value <= upper:
                raise ConfigError(f"{context}.battle 的 {key} 必须在 {lower} 到 {upper} 之间")
        _validate_template_target(battle["battle_marker"], width, height, f"{context}.battle.battle_marker", True)
        ends = battle["end_targets"]
        if not isinstance(ends, list) or not 1 <= len(ends) <= 3:
            raise ConfigError(f"{context}.battle 必须配置 1 到 3 个战斗结束标记")
        for index, target in enumerate(ends):
            _validate_template_target(target, width, height, f"{context}.battle.end_targets[{index}]", True)
        buttons = battle["buttons"]
        if not isinstance(buttons, list) or len(buttons) != 4:
            raise ConfigError(f"{context}.battle 必须按顺序配置四个战斗按钮")
        for button, name in zip(buttons, ("一技能", "二技能", "大招", "普攻")):
            if not isinstance(button, dict):
                raise ConfigError(f"{context}.battle 战斗按钮必须是对象")
            _require_exact_keys(button, {"name", "x", "y"}, f"{context}.battle.buttons")
            if button["name"] != name:
                raise ConfigError(f"{context}.battle 按钮顺序必须为一技能、二技能、大招、普攻")
            _coordinate(button["x"], width, context)
            _coordinate(button["y"], height, context)
            if button["x"] < 1200 or button["y"] < 450:
                raise ConfigError(f"{context}.battle 按钮必须位于右下战斗操作区域")
        return
    elif action_type == "handle_organization_prayer":
        timeout_seconds = action["timeout_seconds"]
        poll_seconds = action["poll_seconds"]
        wait_seconds = action["wait_seconds"]
        max_claims = action["max_claims"]
        _validate_click_waits(action, context)
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
        coin_prayer = action["coin_prayer"]
        if not (
            coin_prayer["left"] >= 500
            and coin_prayer["top"] >= 650
            and coin_prayer["right"] <= 1100
            and coin_prayer["bottom"] <= height
        ):
            raise ConfigError(f"{context} 的铜币祈福搜索区域必须限制在左下安全区域")
        if coin_prayer["threshold"] < 0.80:
            raise ConfigError(f"{context} 的铜币祈福阈值不能低于 0.80")
        if not (1200 <= action["dismiss_x"] <= 1700 and 100 <= action["dismiss_y"] <= 400):
            raise ConfigError(f"{context} 的奖励关闭坐标必须位于右上安全空白区域")
        return
    elif action_type == "handle_coin_fortune":
        clicks = action["clicks"]
        timeout_seconds = action["timeout_seconds"]
        poll_seconds = action["poll_seconds"]
        if clicks != 2 or isinstance(clicks, bool):
            raise ConfigError(f"{context} 的 clicks 必须固定为 2")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ConfigError(f"{context} 的 timeout_seconds 必须在 1 到 60 之间")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.2 <= poll_seconds <= 5:
            raise ConfigError(f"{context} 的 poll_seconds 必须在 0.2 到 5 之间")
        if poll_seconds > timeout_seconds:
            raise ConfigError(f"{context} 的 poll_seconds 不能大于 timeout_seconds")
        _validate_click_waits(action, context)
        free_button = action["free_button"]
        _validate_template_target(free_button, width, height, f"{context}.free_button", require_name=True)
        if not (
            free_button["left"] >= 800
            and free_button["top"] >= 650
            and free_button["right"] <= 1500
            and free_button["bottom"] <= height
        ):
            raise ConfigError(f"{context} 的免费招财搜索区域必须限制在底部中间安全区域")
        if free_button["threshold"] < 0.80:
            raise ConfigError(f"{context} 的免费招财阈值不能低于 0.80")
        return
    elif action_type == "handle_free_recruit":
        timeout_seconds = action["timeout_seconds"]
        poll_seconds = action["poll_seconds"]
        if (width, height) != (1920, 1080):
            raise ConfigError(f"{context} 只支持 1920×1080 安卓画面")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 60:
            raise ConfigError(f"{context} 的 timeout_seconds 必须在 1 到 60 之间")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.2 <= poll_seconds <= 5:
            raise ConfigError(f"{context} 的 poll_seconds 必须在 0.2 到 5 之间")
        if poll_seconds > timeout_seconds:
            raise ConfigError(f"{context} 的 poll_seconds 不能大于 timeout_seconds")
        _validate_click_waits(action, context)
        for key in (
            "advanced_tab", "advanced_red_dot", "ordinary_tab", "ordinary_red_dot",
            "free_button", "result_confirm",
        ):
            _validate_template_target(action[key], width, height, f"{context}.{key}", require_name=True)
        advanced_tab = action["advanced_tab"]
        if not (
            advanced_tab["left"] == 0
            and advanced_tab["top"] >= 100
            and advanced_tab["right"] <= 350
            and advanced_tab["bottom"] <= 450
        ):
            raise ConfigError(f"{context} 的高级招募标签搜索区域必须限制在左侧上方")
        ordinary_tab = action["ordinary_tab"]
        if not (
            ordinary_tab["left"] == 0
            and ordinary_tab["top"] >= 700
            and ordinary_tab["right"] <= 350
            and ordinary_tab["bottom"] <= height
        ):
            raise ConfigError(f"{context} 的普通招募标签搜索区域必须限制在左侧下方")
        advanced_red_dot = action["advanced_red_dot"]
        if not (
            advanced_red_dot["left"] >= 150
            and advanced_red_dot["top"] >= 150
            and advanced_red_dot["right"] <= 300
            and advanced_red_dot["bottom"] <= 350
        ):
            raise ConfigError(f"{context} 的高级招募红点搜索区域必须限制在高级招募标签旁")
        ordinary_red_dot = action["ordinary_red_dot"]
        if not (
            ordinary_red_dot["left"] >= 150
            and ordinary_red_dot["top"] >= 700
            and ordinary_red_dot["right"] <= 300
            and ordinary_red_dot["bottom"] <= height
        ):
            raise ConfigError(f"{context} 的普通招募红点搜索区域必须限制在普通招募标签旁")
        if advanced_red_dot["threshold"] < 0.95 or ordinary_red_dot["threshold"] < 0.95:
            raise ConfigError(f"{context} 的招募红点阈值不能低于 0.95")
        if advanced_red_dot["template"] != ordinary_red_dot["template"]:
            raise ConfigError(f"{context} 的高级和普通招募必须共用同一张红点模板")
        free_button = action["free_button"]
        if not (
            free_button["left"] >= 800
            and free_button["top"] >= 650
            and free_button["right"] <= 1500
            and free_button["bottom"] <= height
        ):
            raise ConfigError(f"{context} 的免费招募搜索区域必须限制在底部中间安全区域")
        if free_button["threshold"] < 0.80:
            raise ConfigError(f"{context} 的免费招募阈值不能低于 0.80")
        result_confirm = action["result_confirm"]
        if not (
            result_confirm["left"] >= 300
            and result_confirm["top"] >= 650
            and result_confirm["right"] <= 850
            and result_confirm["bottom"] <= height
        ):
            raise ConfigError(f"{context} 的招募结果确定按钮搜索区域必须限制在左下安全区域")
        if result_confirm["threshold"] < 0.85:
            raise ConfigError(f"{context} 的招募结果确定按钮阈值不能低于 0.85")
        return
    return


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
        normalized_actions = [_normalize_action(action, task_name) for action in actions]
        for index, action in enumerate(normalized_actions):
            _validate_action(action, width, height, f"{task_name}[{index}]")
        validated[task_name] = normalized_actions
    serial = payload["serial"]
    if serial is not None and (not isinstance(serial, str) or not serial.strip()):
        raise ConfigError("serial 必须是 null 或非空字符串")
    if not isinstance(payload["adb_path"], str) or not payload["adb_path"].strip():
        raise ConfigError("adb_path 必须是非空字符串")
    return Settings(Path(payload["adb_path"]), serial, width, height, validated)
