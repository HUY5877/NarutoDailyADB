from pathlib import Path

import pytest

from naruto_daily.config import Settings, load_settings
from naruto_daily.tasks import TaskError, build_all, build_task


def settings_with_tasks(tasks):
    return Settings(None, None, 1280, 720, tasks)


def test_each_task_is_independently_selectable():
    tasks = {
        "login": [{"type": "tap", "x": 0, "y": 0}],
        "organization_pray": [{"type": "tap", "x": 5, "y": 5}],
        "sign_in": [{"type": "tap", "x": 1, "y": 1}],
        "stamina": [{"type": "tap", "x": 2, "y": 2}],
        "recruit": [{"type": "tap", "x": 3, "y": 3}],
        "abundance": [{"type": "tap", "x": 4, "y": 4}],
    }
    settings = settings_with_tasks(tasks)

    assert build_task(settings, "recruit") == [{"type": "tap", "x": 3, "y": 3}]


def test_login_main_flow_continues_to_prayer_recruit_and_abundance_after_cleanup():
    tasks = {
        "login": [{"type": "popup_cleanup", "rounds": 1}],
        "organization_pray": [{"type": "tap", "x": 5, "y": 5}],
        "sign_in": [],
        "stamina": [],
        "recruit": [{"type": "tap", "x": 3, "y": 3}],
        "abundance": [{"type": "handle_abundance"}, {"type": "return_to_main"}],
    }

    assert build_task(settings_with_tasks(tasks), "login") == [
        {"type": "popup_cleanup", "rounds": 1},
        {"type": "tap", "x": 5, "y": 5},
        {"type": "tap", "x": 3, "y": 3},
        {"type": "handle_abundance"},
        {"type": "return_to_main"},
    ]


@pytest.mark.parametrize('builder', [build_all, lambda settings: build_task(settings, 'login')])
def test_daily_flow_places_squad_assault_between_survival_and_secret_realm(builder):
    tasks = {
        "login": [],
        "organization_pray": [],
        "recruit": [],
        "abundance": [{"type": "handle_abundance"}],
        "survival_challenge": [{"type": "handle_survival_challenge"}],
        "squad_assault": [{"type": "handle_squad_assault"}],
        "secret_realm": [{"type": "enter_secret_realm"}],
    }

    assert [action["type"] for action in builder(settings_with_tasks(tasks))] == [
        "handle_abundance", "handle_survival_challenge", "handle_squad_assault", "enter_secret_realm",
    ]


def test_squad_assault_is_independent_and_kept_out_of_secret_farm():
    login = {"type": "wait", "seconds": 1}
    squad = {"type": "handle_squad_assault"}
    farm = {"type": "enter_secret_realm", "method": "ninja_challenge"}
    secret = {"type": "handle_secret_realm"}
    settings = settings_with_tasks({
        "login": [login], "squad_assault": [squad], "secret_realm_farm": [farm],
        "secret_realm": [{"type": "enter_secret_realm", "method": "rewards"}, secret],
    })
    assert build_task(settings, "squad_assault") == [squad]
    assert build_task(settings, "secret_realm_farm") == [login, farm, secret]


def test_example_squad_route_uses_reward_card_skip_and_returns_before_secret_realm():
    root = Path(__file__).parents[1]
    settings = load_settings(root / 'config.example.json')
    route = build_task(settings, 'squad_assault')
    assert [action['type'] for action in route] == [
        'return_to_main', 'main_menu_tap', 'find_card_and_click',
        'handle_squad_assault', 'return_to_main',
    ]
    assert route[1]['main_menu'] == 'rewards'
    assert route[2]['completed_skip_type'] == 'handle_squad_assault'
    assert route[2]['button_template'] == 'assets/go_now_button.png'
    assert route[2]['completed_template'] == 'assets/completed_stamp.png'
    assert 'multimodal' not in route[3]
    assert route[3]['poll_seconds'] == 5
    for key in ('ready_marker', 'match_button', 'quadruple_checked', 'quadruple_unchecked',
                'continue_button', 'limit_marker', 'dialog_marker'):
        assert (root / route[3][key]['template']).is_file()
    assert (root / route[2]['template']).is_file()
    for builder in (build_all, lambda value: build_task(value, 'login')):
        actions = builder(settings)
        survival = next(i for i, action in enumerate(actions) if action['type'] == 'handle_survival_challenge')
        squad = next(i for i, action in enumerate(actions) if action['type'] == 'handle_squad_assault')
        secret = next(i for i, action in enumerate(actions) if action['type'] == 'enter_secret_realm')
        assert survival < squad < secret
    assert not any(action['type'] == 'handle_squad_assault' for action in build_task(settings, 'secret_realm_farm'))


def test_build_all_preserves_approved_order():
    tasks = {name: [{"type": "tap", "x": index, "y": 0}] for index, name in enumerate(
        ["login", "organization_pray", "sign_in", "stamina", "recruit", "abundance"], start=1
    )}

    assert [action["x"] for action in build_all(settings_with_tasks(tasks))] == [1, 2, 5, 3, 4, 6]


def test_build_all_accepts_many_actions_and_large_total_wait():
    tasks = {
        "login": [],
        "organization_pray": [],
        "sign_in": [{"type": "wait", "seconds": 300}] * 30,
        "stamina": [{"type": "wait", "seconds": 300}] * 30,
        "recruit": [{"type": "wait", "seconds": 300}] * 30,
        "abundance": [{"type": "wait", "seconds": 300}] * 30,
    }

    assert build_all(settings_with_tasks(tasks)) == [{"type": "wait", "seconds": 300}] * 120


def test_unknown_task_is_rejected():
    settings = settings_with_tasks({name: [] for name in ["login", "organization_pray", "sign_in", "stamina", "recruit", "abundance"]})

    with pytest.raises(TaskError, match="未知任务"):
        build_task(settings, "purchase")
