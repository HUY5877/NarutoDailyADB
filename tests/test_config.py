import json
from pathlib import Path

import pytest

from naruto_daily.config import ConfigError, load_settings


def write_config(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def valid_payload() -> dict:
    return {
        "adb_path": "E:/LDPlayer9/adb.exe",
        "serial": None,
        "screen": {"width": 1280, "height": 720},
        "tasks": {
            "login": [],
            "organization_pray": [],
            "sign_in": [{"type": "tap", "x": 100, "y": 200}, {"type": "wait", "seconds": 1.5}],
            "stamina": [],
            "recruit": [],
            "abundance": [],
        },
    }


def test_load_settings_returns_validated_values(tmp_path):
    settings = load_settings(write_config(tmp_path, valid_payload()))

    assert settings.adb_path == Path("E:/LDPlayer9/adb.exe")
    assert (settings.screen_width, settings.screen_height) == (1280, 720)
    assert settings.tasks["sign_in"][0] == {"type": "tap", "x": 100, "y": 200}


def test_optional_image_retry_has_no_task_wait_budget(tmp_path):
    payload = valid_payload()
    payload["tasks"]["sign_in"] = [{"type": "wait", "seconds": 300}, {
        "type": "optional_template_tap", "template": "button.png",
        "left": 0, "top": 0, "right": 100, "bottom": 100, "threshold": 0.85,
    }]
    settings = load_settings(write_config(tmp_path, payload))
    assert len(settings.tasks["sign_in"]) == 2


def test_login_task_accepts_real_1920_by_1080_coordinates(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [
        {"type": "tap", "x": 158, "y": 924},
        {"type": "tap", "x": 1605, "y": 813},
        {"type": "tap", "x": 961, "y": 834},
        {"type": "tap", "x": 961, "y": 806},
        {"type": "tap", "x": 961, "y": 910},
    ]

    settings = load_settings(write_config(tmp_path, payload))

    assert [action["x"] for action in settings.tasks["login"]] == [158, 1605, 961, 961, 961]


def test_optional_template_tap_validates_region_and_threshold(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "optional_template_tap",
        "template": "assets/claim_button.png",
        "left": 780,
        "top": 820,
        "right": 1140,
        "bottom": 1000,
        "threshold": 0.85,
    }]

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["login"][0]["threshold"] == 0.85


def test_popup_cleanup_accepts_two_safe_targets(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "popup_cleanup",
        "rounds": 4,
        "idle_rounds": 1,
        "wait_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "targets": [
            {
                "name": "登录奖励领取按钮",
                "template": "assets/claim_button.png",
                "left": 780, "top": 820, "right": 1140, "bottom": 1000,
                "threshold": 0.85,
            },
            {
                "name": "推广弹窗关闭按钮",
                "template": "assets/promo_close.png",
                "left": 1700, "top": 0, "right": 1920, "bottom": 180,
                "threshold": 0.85,
            },
        ],
    }]

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["login"][0]["rounds"] == 4
    assert settings.tasks["login"][0]["pre_click_wait_seconds"] == 3
    assert [target["name"] for target in settings.tasks["login"][0]["targets"]] == ["登录奖励领取按钮", "推广弹窗关闭按钮"]


def test_required_template_tap_accepts_timeout_and_poll_interval(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "required_template_tap",
        "name": "开始游戏按钮",
        "template": "assets/start_game_button.png",
        "left": 650, "top": 780, "right": 1270, "bottom": 1020,
        "threshold": 0.85,
        "timeout_seconds": 20,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
    }]

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["login"][0]["timeout_seconds"] == 20
    assert settings.tasks["login"][0]["poll_seconds"] == 1
    assert settings.tasks["login"][0]["pre_click_wait_seconds"] == 3
    assert settings.tasks["login"][0]["post_click_wait_seconds"] == 3


def test_finish_login_authorization_accepts_optional_second_page(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "finish_login_authorization",
        "timeout_seconds": 40,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "second_authorization": {
            "name": "第二次授权同意按钮",
            "template": "assets/qq_agree_game_info.png",
            "left": 700, "top": 700, "right": 1220, "bottom": 930,
            "threshold": 0.85,
        },
        "start_game": {
            "name": "开始游戏按钮",
            "template": "assets/start_game_button.png",
            "left": 650, "top": 780, "right": 1270, "bottom": 1020,
            "threshold": 0.85
        }
    }]

    settings = load_settings(write_config(tmp_path, payload))

    action = settings.tasks["login"][0]
    assert action["timeout_seconds"] == 40
    assert action["second_authorization"]["template"] == "assets/qq_agree_game_info.png"
    assert action["pre_click_wait_seconds"] == 3
    assert action["post_click_wait_seconds"] == 3


def test_old_click_actions_without_wait_fields_are_still_accepted(tmp_path):
    project_root = Path(__file__).parents[1]
    payload = json.loads((project_root / "config.example.json").read_text(encoding="utf-8"))
    for actions in payload["tasks"].values():
        for action in actions:
            if action["type"] in {"required_template_tap", "finish_login_authorization"}:
                action.pop("pre_click_wait_seconds")
                action.pop("post_click_wait_seconds")
                if action["type"] == "finish_login_authorization":
                    action["second_authorization"] = {
                        "name": "第二次授权页面",
                        "template": "assets/qq_second_authorization_marker.png",
                        "left": 0, "top": 200, "right": 1300, "bottom": 450,
                        "threshold": 0.85,
                        "click_x": 961, "click_y": 806,
                    }

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["login"][2]["pre_click_wait_seconds"] == 3
    assert settings.tasks["login"][2]["post_click_wait_seconds"] == 3
    second = settings.tasks["login"][4]["second_authorization"]
    assert second == {
        "name": "第二次授权同意按钮",
        "template": "assets/qq_agree_game_info.png",
        "left": 700, "top": 700, "right": 1220, "bottom": 930,
        "threshold": 0.85,
    }


def test_return_to_main_accepts_ten_round_close_settings(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "main_page_marker": {
            "name": "主界面奖励入口",
            "template": "assets/reward_entry.png",
            "left": 1700, "top": 400, "right": 1920, "bottom": 700,
            "threshold": 0.90,
        },
        "close_target": {
            "name": "通用关闭按钮",
            "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.95,
        },
    }]

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["login"][0]["max_rounds"] == 10
    assert settings.tasks["login"][0]["main_page_marker"]["threshold"] == 0.90
    assert settings.tasks["login"][0]["close_target"]["threshold"] == 0.95


def test_return_to_main_rejects_close_threshold_below_safe_margin(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "main_page_marker": {
            "name": "主界面奖励入口",
            "template": "assets/reward_entry.png",
            "left": 1700, "top": 400, "right": 1920, "bottom": 700,
            "threshold": 0.90,
        },
        "close_target": {
            "name": "通用关闭按钮",
            "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.94,
        },
    }]

    with pytest.raises(ConfigError, match="关闭按钮阈值不能低于 0.95"):
        load_settings(write_config(tmp_path, payload))


def test_return_to_main_rejects_main_page_marker_threshold_below_safe_margin(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "main_page_marker": {
            "name": "主界面奖励入口",
            "template": "assets/reward_entry.png",
            "left": 1700, "top": 400, "right": 1920, "bottom": 700,
            "threshold": 0.89,
        },
        "close_target": {
            "name": "通用关闭按钮",
            "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.95,
        },
    }]

    with pytest.raises(ConfigError, match="主界面标记阈值不能低于 0.90"):
        load_settings(write_config(tmp_path, payload))


def test_return_to_main_rejects_main_page_marker_outside_reward_area(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "main_page_marker": {
            "name": "主界面奖励入口",
            "template": "assets/reward_entry.png",
            "left": 1400, "top": 100, "right": 1920, "bottom": 900,
            "threshold": 0.90,
        },
        "close_target": {
            "name": "通用关闭按钮",
            "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.95,
        },
    }]

    with pytest.raises(ConfigError, match="主界面标记搜索区域必须限制在右侧奖励区域"):
        load_settings(write_config(tmp_path, payload))


def test_old_return_to_main_action_is_migrated_to_safe_settings(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{
        "type": "return_to_main",
        "max_rounds": 10,
        "idle_rounds": 2,
        "poll_seconds": 1,
        "close_target": {
            "name": "通用关闭按钮",
            "template": "assets/promo_close.png",
            "left": 1700, "top": 0, "right": 1920, "bottom": 180,
            "threshold": 0.82,
        },
    }]

    settings = load_settings(write_config(tmp_path, payload))

    action = settings.tasks["login"][0]
    assert action["close_target"]["threshold"] == 0.95
    assert action["main_page_marker"] == {
        "name": "主界面奖励入口",
        "template": "assets/reward_entry.png",
        "left": 1700, "top": 400, "right": 1920, "bottom": 700,
        "threshold": 0.90,
    }


def test_coin_fortune_accepts_two_free_clicks_in_safe_region(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["organization_pray"] = [{
        "type": "handle_coin_fortune",
        "clicks": 2,
        "timeout_seconds": 20,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "free_button": {
            "name": "免费一次",
            "template": "assets/free_fortune_once.png",
            "left": 1000, "top": 700, "right": 1450, "bottom": 1050,
            "threshold": 0.85,
        },
    }]

    settings = load_settings(write_config(tmp_path, payload))

    action = settings.tasks["organization_pray"][0]
    assert action["clicks"] == 2
    assert action["free_button"]["right"] == 1450


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda action: action.update({"clicks": 1}), "必须固定为 2"),
        (lambda action: action["free_button"].update({"right": 1750}), "免费招财搜索区域"),
    ],
)
def test_coin_fortune_rejects_paid_button_risk(tmp_path, mutate, message):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    action = {
        "type": "handle_coin_fortune",
        "clicks": 2,
        "timeout_seconds": 20,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "free_button": {
            "name": "免费一次",
            "template": "assets/free_fortune_once.png",
            "left": 1000, "top": 700, "right": 1450, "bottom": 1050,
            "threshold": 0.85,
        },
    }
    payload["tasks"]["organization_pray"] = [action]
    mutate(action)

    with pytest.raises(ConfigError, match=message):
        load_settings(write_config(tmp_path, payload))


def test_find_card_and_click_accepts_horizontal_scan_settings(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["organization_pray"] = [{
        "type": "find_card_and_click",
        "name": "组织祈福",
        "template": "assets/organization_pray_label.png",
        "left": 0, "top": 0, "right": 1920, "bottom": 1080,
        "threshold": 0.85,
        "button_template": "assets/go_now_button.png",
        "button_threshold": 0.85,
        "completed_template": "assets/completed_stamp.png",
        "completed_threshold": 0.90,
        "button_left_offset": -140,
        "button_top_offset": 80,
        "button_right_offset": 140,
        "button_bottom_offset": 260,
        "max_swipes": 6,
        "swipe_x1": 1650, "swipe_y1": 550,
        "swipe_x2": 350, "swipe_y2": 550,
        "swipe_duration_ms": 500,
        "wait_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "completed_skip_type": "handle_organization_prayer",
    }]

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["organization_pray"][0]["button_template"] == "assets/go_now_button.png"
    assert settings.tasks["organization_pray"][0]["completed_template"] == "assets/completed_stamp.png"
    assert settings.tasks["organization_pray"][0]["completed_skip_type"] == "handle_organization_prayer"
    assert settings.tasks["organization_pray"][0]["pre_click_wait_seconds"] == 3


def test_free_recruit_accepts_red_dot_and_safe_button_targets(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["recruit"] = [{
        "type": "handle_free_recruit",
        "multimodal": {"base_url": "${MULTIMODAL_BASE_URL}", "model": "${MULTIMODAL_MODEL}",
                       "timeout_seconds": 20, "max_calls": 6},
        "timeout_seconds": 40,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 0.5,
        "post_click_wait_seconds": 0.5,
        "advanced_tab": {
            "name": "高级招募标签", "template": "assets/advanced_recruit_tab.png",
            "left": 0, "top": 150, "right": 300, "bottom": 350, "threshold": 0.90,
        },
        "advanced_red_dot": {
            "name": "高级招募红点", "template": "assets/recruit_red_dot.png",
            "left": 170, "top": 180, "right": 270, "bottom": 300, "threshold": 0.95,
        },
        "ordinary_tab": {
            "name": "普通招募标签", "template": "assets/ordinary_recruit_tab.png",
            "left": 0, "top": 780, "right": 300, "bottom": 1030, "threshold": 0.90,
        },
        "ordinary_red_dot": {
            "name": "普通招募红点", "template": "assets/recruit_red_dot.png",
            "left": 170, "top": 800, "right": 270, "bottom": 980, "threshold": 0.95,
        },
        "free_button": {
            "name": "免费招募按钮",
            "left": 900, "top": 800, "right": 1500, "bottom": 910, "click_x": 1330, "click_y": 865,
        },
        "result_confirm": {
            "name": "招募结果确定按钮", "template": "assets/recruit_confirm.png",
            "left": 350, "top": 700, "right": 800, "bottom": 1000, "threshold": 0.90,
        },
    }]

    settings = load_settings(write_config(tmp_path, payload))

    action = settings.tasks["recruit"][0]
    assert action["timeout_seconds"] == 40
    assert action["advanced_red_dot"]["template"] == action["ordinary_red_dot"]["template"]
    assert action["free_button"]["right"] == 1500
    assert action["result_confirm"]["right"] == 800


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda action: action["free_button"].update({"right": 1750}),
            "免费招募搜索区域",
        ),
        (
            lambda action: action["result_confirm"].update({"right": 1100}),
            "招募结果确定按钮搜索区域",
        ),
        (
            lambda action: action["advanced_red_dot"].update({"threshold": 0.89}),
            "招募红点阈值",
        ),
        (
            lambda action: action["ordinary_red_dot"].update({"left": 0}),
            "普通招募红点搜索区域",
        ),
        (
            lambda action: action["ordinary_red_dot"].update(
                {"template": "assets/another_red_dot.png"}
            ),
            "必须共用同一张红点模板",
        ),
        (
            lambda action: action.update({"pre_click_wait_seconds": -1}),
            "pre_click_wait_seconds 必须在 0 到 10 之间",
        ),
    ],
)
def test_free_recruit_rejects_paid_button_and_unrelated_red_dot_risk(
    tmp_path,
    mutate,
    message,
):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    action = {
        "type": "handle_free_recruit",
        "multimodal": {"base_url": "${MULTIMODAL_BASE_URL}", "model": "${MULTIMODAL_MODEL}",
                       "timeout_seconds": 20, "max_calls": 6},
        "timeout_seconds": 40,
        "poll_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "advanced_tab": {
            "name": "高级招募标签", "template": "assets/advanced_recruit_tab.png",
            "left": 0, "top": 150, "right": 300, "bottom": 350, "threshold": 0.90,
        },
        "advanced_red_dot": {
            "name": "高级招募红点", "template": "assets/recruit_red_dot.png",
            "left": 170, "top": 180, "right": 270, "bottom": 300, "threshold": 0.95,
        },
        "ordinary_tab": {
            "name": "普通招募标签", "template": "assets/ordinary_recruit_tab.png",
            "left": 0, "top": 780, "right": 300, "bottom": 1030, "threshold": 0.90,
        },
        "ordinary_red_dot": {
            "name": "普通招募红点", "template": "assets/recruit_red_dot.png",
            "left": 170, "top": 800, "right": 270, "bottom": 980, "threshold": 0.95,
        },
        "free_button": {
            "name": "免费招募按钮",
            "left": 900, "top": 800, "right": 1500, "bottom": 910, "click_x": 1330, "click_y": 865,
        },
        "result_confirm": {
            "name": "招募结果确定按钮", "template": "assets/recruit_confirm.png",
            "left": 350, "top": 700, "right": 800, "bottom": 1000, "threshold": 0.90,
        },
    }
    payload["tasks"]["recruit"] = [action]
    mutate(action)

    with pytest.raises(ConfigError, match=message):
        load_settings(write_config(tmp_path, payload))


def test_free_recruit_requires_fixed_1920_by_1080_screen(tmp_path):
    project_root = Path(__file__).parents[1]
    payload = json.loads((project_root / "config.example.json").read_text(encoding="utf-8"))
    payload["screen"] = {"width": 2560, "height": 1440}

    with pytest.raises(ConfigError, match="只支持 1920×1080 安卓画面"):
        load_settings(write_config(tmp_path, payload))


def test_organization_prayer_action_accepts_reward_and_coin_targets(tmp_path):
    payload = valid_payload()
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["organization_pray"] = [{
        "type": "handle_organization_prayer",
        "timeout_seconds": 20,
        "poll_seconds": 1,
        "wait_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
        "max_claims": 3,
        "dismiss_x": 1500,
        "dismiss_y": 200,
        "yesterday_reward": {
            "name": "昨日奖励", "template": "assets/yesterday_reward.png",
            "left": 500, "top": 150, "right": 850, "bottom": 380, "threshold": 0.85,
        },
        "panel_marker": {
            "name": "昨日祈福奖励面板", "template": "assets/yesterday_panel.png",
            "left": 400, "top": 50, "right": 1550, "bottom": 350, "threshold": 0.95,
        },
        "claim_button": {
            "name": "可领取奖励", "template": "assets/prayer_claim.png",
            "left": 400, "top": 100, "right": 1550, "bottom": 980, "threshold": 0.85,
        },
        "coin_prayer": {
            "name": "6000铜币焚香祈福", "template": "assets/coin_prayer.png",
            "left": 650, "top": 750, "right": 1100, "bottom": 1020, "threshold": 0.85,
        },
    }]

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["organization_pray"][0]["max_claims"] == 3
    assert settings.tasks["organization_pray"][0]["post_click_wait_seconds"] == 3


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda action: action["coin_prayer"].update({"right": 1500}), "铜币祈福搜索区域"),
        (lambda action: action["coin_prayer"].update({"threshold": 0.40}), "铜币祈福阈值"),
        (lambda action: action.update({"dismiss_x": 900}), "奖励关闭坐标"),
    ],
)
def test_organization_prayer_rejects_unsafe_click_settings(tmp_path, mutate, message):
    project_root = Path(__file__).parents[1]
    payload = json.loads((project_root / "config.example.json").read_text(encoding="utf-8"))
    action = next(
        item for item in payload["tasks"]["organization_pray"]
        if item["type"] == "handle_organization_prayer"
    )
    mutate(action)

    with pytest.raises(ConfigError, match=message):
        load_settings(write_config(tmp_path, payload))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["screen"].update(width=0), "screen width"),
        (lambda p: p["tasks"]["sign_in"][0].update(x=-1), "坐标"),
        (lambda p: p["tasks"]["sign_in"][0].update(x=1280), "坐标"),
        (lambda p: p["tasks"]["sign_in"][0].update(type="purchase"), "不支持的动作"),
        (lambda p: p["tasks"]["sign_in"][0].update(extra=True), "未知字段"),
    ],
)
def test_invalid_configuration_is_rejected(tmp_path, mutate, message):
    payload = valid_payload()
    mutate(payload)

    with pytest.raises(ConfigError, match=message):
        load_settings(write_config(tmp_path, payload))


def test_many_actions_with_large_total_wait_are_accepted(tmp_path):
    payload = valid_payload()
    payload["tasks"]["sign_in"] = [{"type": "wait", "seconds": 300}] * 101

    settings = load_settings(write_config(tmp_path, payload))
    assert settings.tasks["sign_in"] == payload["tasks"]["sign_in"]


def test_single_wait_above_supported_range_is_rejected(tmp_path):
    payload = valid_payload()
    payload["tasks"]["sign_in"] = [{"type": "wait", "seconds": 301}]

    with pytest.raises(ConfigError, match="300 秒"):
        load_settings(write_config(tmp_path, payload))


def test_all_return_actions_use_extended_close_area():
    project_root = Path(__file__).parents[1]
    settings = load_settings(project_root / "config.example.json")

    login_return = next(
        action for action in settings.tasks["login"]
        if action["type"] == "return_to_main"
    )
    prayer_return = next(
        action for action in settings.tasks["organization_pray"]
        if action["type"] == "return_to_main"
    )
    recruit_return = next(
        action for action in settings.tasks["recruit"]
        if action["type"] == "return_to_main"
    )

    assert login_return["close_target"] == prayer_return["close_target"]
    for action in (login_return, recruit_return):
        assert (
            action["close_target"]["left"],
            action["close_target"]["top"],
            action["close_target"]["right"],
            action["close_target"]["bottom"],
        ) == (1650, 0, 1920, 300)
