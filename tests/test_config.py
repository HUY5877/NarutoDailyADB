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
    }]

    settings = load_settings(write_config(tmp_path, payload))

    assert settings.tasks["login"][0]["timeout_seconds"] == 20
    assert settings.tasks["login"][0]["poll_seconds"] == 1


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


def test_more_than_one_hundred_actions_is_rejected(tmp_path):
    payload = valid_payload()
    payload["tasks"]["sign_in"] = [{"type": "back"}] * 101

    with pytest.raises(ConfigError, match="100 个动作"):
        load_settings(write_config(tmp_path, payload))


def test_more_than_three_hundred_wait_seconds_is_rejected(tmp_path):
    payload = valid_payload()
    payload["tasks"]["sign_in"] = [{"type": "wait", "seconds": 301}]

    with pytest.raises(ConfigError, match="300 秒"):
        load_settings(write_config(tmp_path, payload))
