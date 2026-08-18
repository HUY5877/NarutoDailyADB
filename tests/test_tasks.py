import pytest

from naruto_daily.config import Settings
from naruto_daily.tasks import TaskError, build_all, build_task


def settings_with_tasks(tasks):
    return Settings(None, None, 1280, 720, tasks)


def test_each_task_is_independently_selectable():
    tasks = {
        "login": [{"type": "tap", "x": 0, "y": 0}],
        "sign_in": [{"type": "tap", "x": 1, "y": 1}],
        "stamina": [{"type": "tap", "x": 2, "y": 2}],
        "recruit": [{"type": "tap", "x": 3, "y": 3}],
        "abundance": [{"type": "tap", "x": 4, "y": 4}],
    }
    settings = settings_with_tasks(tasks)

    assert build_task(settings, "recruit") == [{"type": "tap", "x": 3, "y": 3}]


def test_build_all_preserves_approved_order():
    tasks = {name: [{"type": "tap", "x": index, "y": 0}] for index, name in enumerate(
        ["login", "sign_in", "stamina", "recruit", "abundance"], start=1
    )}

    assert [action["x"] for action in build_all(settings_with_tasks(tasks))] == [1, 2, 3, 4, 5]


def test_build_all_rejects_combined_sequence_over_limits():
    tasks = {
        "login": [],
        "sign_in": [{"type": "back"}] * 30,
        "stamina": [{"type": "back"}] * 30,
        "recruit": [{"type": "back"}] * 30,
        "abundance": [{"type": "back"}] * 30,
    }

    with pytest.raises(TaskError, match="100 个动作"):
        build_all(settings_with_tasks(tasks))


def test_unknown_task_is_rejected():
    settings = settings_with_tasks({name: [] for name in ["login", "sign_in", "stamina", "recruit", "abundance"]})

    with pytest.raises(TaskError, match="未知任务"):
        build_task(settings, "purchase")
