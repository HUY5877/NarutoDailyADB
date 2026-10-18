"""战斗键盘输入配置通过真实校验器接受或拒绝，不发送设备输入。"""

import json
from copy import deepcopy

import pytest

from naruto_daily.config import ConfigError, _validate_battle, load_settings


def battle_config():
    marker = dict(name="战斗暂停", template="assets/battle_pause.png",
                  left=1750, top=0, right=1880, bottom=140, threshold=.85)
    result = dict(name="战斗结束", template="assets/result.png",
                  left=700, top=250, right=1250, bottom=430, threshold=.9)
    return dict(
        start_timeout_seconds=20, timeout_seconds=120, tap_interval_seconds=0,
        battle_marker=marker, end_targets=[result],
        buttons=[
            dict(name="一技能", x=1500, y=960),
            dict(name="二技能", x=1535, y=750),
            dict(name="大招", x=1725, y=650),
            dict(name="普攻", x=1715, y=885),
        ],
    )


def validate(battle):
    _validate_battle(battle, 1920, 1080, "test.battle")


def seven_button_battle():
    battle = battle_config()
    battle["input_mode"] = "keyboard"
    battle["buttons"].extend([
        dict(name="替身", x=1301, y=966),
        dict(name="通灵", x=1729, y=263),
        dict(name="秘卷", x=1728, y=443),
    ])
    return battle


def test_seven_default_buttons_accept_upper_right_summon_and_scroll_coordinates():
    validate(seven_button_battle())


@pytest.mark.parametrize("names", [
    ("替身",), ("通灵",), ("秘卷",), ("替身", "通灵"), ("通灵", "秘卷"),
])
def test_four_legacy_buttons_may_add_only_the_requested_auxiliary_subset(names):
    battle = seven_button_battle()
    battle["buttons"] = battle["buttons"][:4] + [
        button for button in battle["buttons"][4:] if button["name"] in names
    ]
    validate(battle)


@pytest.mark.parametrize("name", ["训练", "暂停", "组合宏", "自动战斗", "W", "", None])
def test_training_and_unrelated_button_names_are_rejected(name):
    battle = seven_button_battle()
    battle["buttons"][4]["name"] = name
    battle["buttons"][4]["key"] = "SPACE"
    with pytest.raises(ConfigError):
        validate(battle)


def test_duplicate_auxiliary_name_is_rejected_even_when_its_key_is_unique():
    battle = seven_button_battle()
    battle["buttons"][6].update(name="通灵", key="E")
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("key", ["space", "r", "E"])
def test_auxiliary_defaults_cannot_conflict_with_an_explicit_core_key(key):
    battle = seven_button_battle()
    battle["buttons"][0]["key"] = key
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("axis,value", [("x", -1), ("x", 1920), ("y", -1), ("y", 1080)])
def test_auxiliary_coordinates_still_must_stay_inside_the_android_frame(axis, value):
    battle = seven_button_battle()
    battle["buttons"][5][axis] = value
    with pytest.raises(ConfigError):
        validate(battle)


def test_old_coordinate_only_battle_remains_valid_without_mutation():
    battle = battle_config()
    original = deepcopy(battle)
    validate(battle)
    assert battle == original


def test_keyboard_mode_accepts_saved_instance_and_explicit_keys():
    battle = battle_config()
    battle.update(input_mode="keyboard", keyboard=dict(instance_index=0, hold_seconds=.08))
    for button, key in zip(battle["buttons"], ("J", "H", "L", "K")):
        button["key"] = key
    validate(battle)


@pytest.mark.parametrize("input_mode", [None, "adb", "keyboard"])
def test_mode_defaults_remain_compatible_with_coordinate_buttons(input_mode):
    battle = battle_config()
    if input_mode is not None:
        battle["input_mode"] = input_mode
    validate(battle)


@pytest.mark.parametrize("options", [
    {}, {"instance_index": None}, {"instance_index": 0}, {"instance_index": 17},
    {"hold_seconds": .02}, {"hold_seconds": .5},
    {"instance_index": None, "hold_seconds": .08},
])
def test_keyboard_options_accept_omitted_defaults_and_inclusive_limits(options):
    battle = battle_config()
    battle.update(input_mode="keyboard", keyboard=options)
    validate(battle)


@pytest.mark.parametrize("key", ["A", "Z", "a", "z", "0", "9", "SPACE", "space", "SpAcE"])
@pytest.mark.parametrize("input_mode", ["adb", "keyboard"])
def test_ascii_keys_and_space_are_accepted_in_both_modes(key, input_mode):
    battle = battle_config()
    battle["input_mode"] = input_mode
    for button, value in zip(battle["buttons"], (key, "4", "5", "6")):
        button["key"] = value
    validate(battle)


def test_adb_mode_can_retain_keyboard_target_for_later_switching():
    battle = battle_config()
    battle.update(input_mode="adb", keyboard=dict(instance_index=2, hold_seconds=.1))
    for button, key in zip(battle["buttons"], ("j", "h", "l", "k")):
        button["key"] = key
    validate(battle)


@pytest.mark.parametrize("input_mode", ["", "Keyboard", "ADB", "touch", None, 0, True, []])
def test_unrecognized_or_non_string_input_mode_is_rejected(input_mode):
    battle = battle_config()
    battle["input_mode"] = input_mode
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("options", [None, [], "keyboard", 0, False])
def test_keyboard_options_must_be_an_object(options):
    battle = battle_config()
    battle.update(input_mode="keyboard", keyboard=options)
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("instance_index", [-1, True, False, 0.0, .5, "0", [], float("nan"), float("inf")])
def test_instance_index_rejects_negative_boolean_and_non_integer_values(instance_index):
    battle = battle_config()
    battle.update(input_mode="keyboard", keyboard=dict(instance_index=instance_index))
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("hold_seconds", [
    0, .019, .501, -1, True, False, None, ".08", [],
    float("nan"), float("inf"), float("-inf"),
])
def test_hold_duration_must_be_finite_numeric_and_within_limits(hold_seconds):
    battle = battle_config()
    battle.update(input_mode="keyboard", keyboard=dict(hold_seconds=hold_seconds))
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("key", ["", " ", "JJ", "ENTER", "SPACE ", "é", "Ｊ", "ſ", "空格", "\n", 1, True, None, []])
@pytest.mark.parametrize("input_mode", ["adb", "keyboard"])
def test_invalid_button_keys_are_rejected_even_in_adb_mode(key, input_mode):
    battle = battle_config()
    battle["input_mode"] = input_mode
    battle["buttons"][0]["key"] = key
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("keys", [
    ("X", "x", "L", "K"),
    ("space", "SPACE", "L", "K"),
    ("1", "1", "L", "K"),
])
def test_explicit_duplicate_keys_are_rejected_case_insensitively(keys):
    battle = battle_config()
    battle["input_mode"] = "keyboard"
    for button, key in zip(battle["buttons"], keys):
        button["key"] = key
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("button_index,key", [(0, "h"), (1, "J"), (2, "k"), (3, "L")])
def test_explicit_key_cannot_conflict_with_another_buttons_default(button_index, key):
    battle = battle_config()
    battle["input_mode"] = "keyboard"
    battle["buttons"][button_index]["key"] = key
    with pytest.raises(ConfigError):
        validate(battle)


@pytest.mark.parametrize("unknown_location", ["battle", "keyboard", "button"])
def test_unknown_fields_still_fail_validation(unknown_location):
    battle = battle_config()
    battle.update(input_mode="keyboard", keyboard={})
    owner = {"battle": battle, "keyboard": battle["keyboard"], "button": battle["buttons"][0]}[unknown_location]
    owner["unexpected"] = True
    with pytest.raises(ConfigError):
        validate(battle)


def write_task_config(tmp_path, battle):
    action = dict(
        type="handle_abundance", timeout_seconds=20, load_wait_seconds=10,
        challenge_button=dict(name="挑战", template="assets/challenge.png",
                              left=600, top=850, right=970, bottom=1060, threshold=.9),
        result_return=dict(name="返回", template="assets/return.png",
                           left=650, top=950, right=1300, bottom=1080, threshold=.9),
        battle=battle,
    )
    payload = dict(
        adb_path="E:/LDPlayer9/adb.exe", serial=None, screen=dict(width=1920, height=1080),
        tasks=dict(login=[], organization_pray=[], sign_in=[], stamina=[], recruit=[], abundance=[action]),
    )
    path = tmp_path / "battle.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("keyboard_mode", [False, True])
def test_load_settings_accepts_old_and_keyboard_battle_in_full_task(tmp_path, keyboard_mode):
    battle = battle_config()
    if keyboard_mode:
        battle.update(input_mode="keyboard", keyboard=dict(instance_index=None))
        for button, key in zip(battle["buttons"], ("J", "H", "L", "K")):
            button["key"] = key
    settings = load_settings(write_task_config(tmp_path, battle))
    actual = settings.tasks["abundance"][0]["battle"]
    assert actual.get("input_mode", "adb") == ("keyboard" if keyboard_mode else "adb")
    assert [button["name"] for button in actual["buttons"]] == ["一技能", "二技能", "大招", "普攻"]
    if keyboard_mode:
        assert [button["key"] for button in actual["buttons"]] == ["J", "H", "L", "K"]
        assert actual["keyboard"]["instance_index"] is None


def test_load_settings_rejects_default_key_conflict_before_running_a_task(tmp_path):
    battle = battle_config()
    battle["input_mode"] = "keyboard"
    battle["buttons"][0]["key"] = "H"
    with pytest.raises(ConfigError):
        load_settings(write_task_config(tmp_path, battle))


def test_load_settings_accepts_seven_default_buttons_without_extra_training_actions(tmp_path):
    settings = load_settings(write_task_config(tmp_path, seven_button_battle()))
    actual = settings.tasks["abundance"][0]["battle"]
    assert actual["input_mode"] == "keyboard"
    assert [button["name"] for button in actual["buttons"]] == [
        "一技能", "二技能", "大招", "普攻", "替身", "通灵", "秘卷",
    ]
    assert [(button["x"], button["y"]) for button in actual["buttons"][5:]] == [(1729, 263), (1728, 443)]
