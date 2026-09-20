import pytest

from naruto_daily.config import Settings
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


def test_login_main_flow_places_survival_challenge_before_secret_realm():
    tasks = {
        "login": [],
        "organization_pray": [],
        "recruit": [],
        "abundance": [{"type": "handle_abundance"}],
        "survival_challenge": [{"type": "handle_survival_challenge"}],
        "secret_realm": [{"type": "enter_secret_realm"}],
    }

    assert [action["type"] for action in build_task(settings_with_tasks(tasks), "login")] == [
        "handle_abundance", "handle_survival_challenge", "enter_secret_realm",
    ]


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
