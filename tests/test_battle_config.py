"""战斗配置只接受日常任务原有的四个坐标按钮。"""

from copy import deepcopy

import pytest

from naruto_daily.config import ConfigError, _validate_battle


def battle_config():
    return dict(
        start_timeout_seconds=20, timeout_seconds=120, tap_interval_seconds=0,
        battle_marker=dict(name="战斗暂停", template="assets/battle_pause.png",
                           left=1750, top=0, right=1880, bottom=140, threshold=.85),
        end_targets=[dict(name="战斗结束", template="assets/result.png",
                          left=700, top=250, right=1250, bottom=430, threshold=.9)],
        buttons=[
            dict(name="一技能", x=1500, y=960),
            dict(name="二技能", x=1535, y=750),
            dict(name="大招", x=1725, y=650),
            dict(name="普攻", x=1715, y=885),
        ],
    )


def validate(battle):
    _validate_battle(battle, 1920, 1080, "test")


def test_legacy_coordinate_battle_remains_valid_without_mutation():
    battle = battle_config()
    original = deepcopy(battle)

    validate(battle)

    assert battle == original


@pytest.mark.parametrize("field,value", [
    ("input_mode", "adb"),
    ("input_mode", "keyboard"),
    ("keyboard", {}),
    ("keyboard", {"instance_index": 0, "hold_seconds": .08}),
])
def test_battle_rejects_removed_keyboard_configuration(field, value):
    battle = battle_config()
    battle[field] = value

    with pytest.raises(ConfigError, match="未知字段"):
        validate(battle)


def test_coordinate_button_rejects_removed_keyboard_key():
    battle = battle_config()
    battle["buttons"][0]["key"] = "J"

    with pytest.raises(ConfigError, match="未知字段"):
        validate(battle)


def test_daily_battle_rejects_seven_buttons():
    battle = battle_config()
    battle["buttons"].extend([
        dict(name="替身", x=1301, y=966),
        dict(name="通灵", x=1729, y=263),
        dict(name="秘卷", x=1728, y=443),
    ])

    with pytest.raises(ConfigError, match="四个战斗按钮"):
        validate(battle)
