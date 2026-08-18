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


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["screen"].update(width=0), "screen width"),
        (lambda p: p["tasks"]["sign_in"][0].update(x=-1), "coordinate"),
        (lambda p: p["tasks"]["sign_in"][0].update(x=1280), "coordinate"),
        (lambda p: p["tasks"]["sign_in"][0].update(type="purchase"), "unsupported action"),
        (lambda p: p["tasks"]["sign_in"][0].update(extra=True), "unknown keys"),
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

    with pytest.raises(ConfigError, match="100 actions"):
        load_settings(write_config(tmp_path, payload))


def test_more_than_three_hundred_wait_seconds_is_rejected(tmp_path):
    payload = valid_payload()
    payload["tasks"]["sign_in"] = [{"type": "wait", "seconds": 301}]

    with pytest.raises(ConfigError, match="300 seconds"):
        load_settings(write_config(tmp_path, payload))
