"""Admission uses the slots registered at startup without invoking LDPlayer."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pytest

from naruto_daily.login_service.ldplayer import (
    LDPlayerError, LDPlayerInstance, LDPlayerManager, LDPlayerPoolFull,
)
from test_login_service_ldplayer import FakeLDPlayer, TASK_ID


SECOND_TASK = "22222222-2222-4222-8222-222222222222"


def registered_pool(tmp_path, monkeypatch, indices=(4, 17)):
    fake = FakeLDPlayer(tmp_path / "LDPlayer9" / "ldconsole.exe")
    names = {4: "日常登录甲", 17: "手机 - 备用乙"}
    fake.instances = {names[index]: [index, 0] for index in indices}
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", fake)
    service = LDPlayerManager(Path(fake.console), "", tmp_path / "state", pool_indices=indices)
    service.reset_pool()
    assert fake.calls == [(fake.console, "list2")]
    fake.calls.clear()
    return service, fake


def forbid_listing(*_args, **_kwargs):
    pytest.fail("Admission must use startup bindings without querying LDPlayer")


def test_capacity_allocation_and_ready_use_registered_names_without_console_queries(tmp_path, monkeypatch):
    service, fake = registered_pool(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_list", forbid_listing)

    for _ in range(20):
        assert service.has_capacity()
    first = service.allocate(TASK_ID, time.time() + 10)
    assert first == LDPlayerInstance("日常登录甲", 4, None, TASK_ID)
    assert service.allocate(TASK_ID, time.time() + 10) == first
    assert service.has_capacity()
    service.mark_ready(TASK_ID)
    assert service.allocate(SECOND_TASK, time.time() + 10).name == "手机 - 备用乙"
    assert not service.has_capacity()
    with pytest.raises(LDPlayerPoolFull):
        service.allocate("33333333-3333-4333-8333-333333333333", time.time() + 10)
    assert fake.calls == []


def test_new_manager_observes_current_slot_owners_without_querying_console(tmp_path, monkeypatch):
    service, fake = registered_pool(tmp_path, monkeypatch, (4,))
    other = LDPlayerManager(service.console_path, "", service.state_dir, pool_indices=(4,))
    monkeypatch.setattr(LDPlayerManager, "_list", forbid_listing)

    service.allocate(TASK_ID, time.time() + 10)
    assert not other.has_capacity()
    assert other.allocate(TASK_ID, time.time() + 10).task_id == TASK_ID
    with pytest.raises(LDPlayerPoolFull):
        other.allocate(SECOND_TASK, time.time() + 10)
    assert fake.calls == []


def test_concurrent_managers_reserve_a_registered_slot_only_once(tmp_path, monkeypatch):
    service, fake = registered_pool(tmp_path, monkeypatch, (4,))
    managers = [LDPlayerManager(service.console_path, "", service.state_dir, pool_indices=(4,))
                for _ in range(2)]
    monkeypatch.setattr(LDPlayerManager, "_list", forbid_listing)

    def reserve(item):
        manager, task_id = item
        try:
            return manager.allocate(task_id, time.time() + 5).task_id
        except LDPlayerPoolFull:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        owners = list(executor.map(reserve, zip(managers, [TASK_ID, SECOND_TASK])))
    assert sum(owner is not None for owner in owners) == 1
    assert not service.has_capacity()
    assert fake.calls == []


@pytest.mark.parametrize("operation", ["has_capacity", "allocate", "mark_ready"])
@pytest.mark.parametrize("state", ["missing", "incomplete"])
def test_unregistered_slots_require_startup_without_falling_back_to_console(
    tmp_path, monkeypatch, operation, state,
):
    service = LDPlayerManager(tmp_path / "ldconsole.exe", "", tmp_path / "state", pool_indices=(4, 17))
    path = service.state_dir / "bindings.json"
    if state == "incomplete":
        path.parent.mkdir()
        path.write_text(json.dumps({"version": 2, "bindings": {
            "4": {"name": "日常登录甲", "serial": None, "taskId": None, "state": "idle"},
        }}), "utf-8")
    original = path.read_bytes() if path.exists() else None
    monkeypatch.setattr(service, "_list", forbid_listing)

    with pytest.raises(LDPlayerError, match="not initialized|startup|restart"):
        if operation == "allocate":
            service.allocate(TASK_ID, time.time() + 10)
        elif operation == "mark_ready":
            service.mark_ready(TASK_ID)
        else:
            service.has_capacity()
    assert (path.read_bytes() if path.exists() else None) == original
