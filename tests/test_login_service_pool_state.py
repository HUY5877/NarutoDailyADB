import json
from pathlib import Path
import time
from uuid import UUID

import pytest

from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerManager
from test_login_service_ldplayer import FakeLDPlayer, TASK_ID, pool_manager


SECOND_TASK = "22222222-2222-4222-8222-222222222222"


def state_path(tmp_path):
    return tmp_path / "state" / "bindings.json"


def read_state(tmp_path):
    return json.loads(state_path(tmp_path).read_text("utf-8"))


def write_state(tmp_path, document):
    path = state_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps(document, ensure_ascii=False), "utf-8")
    return path.read_bytes()


def fake_pool(tmp_path, monkeypatch, indices=(4, 17, 39)):
    fake = FakeLDPlayer(tmp_path / "LDPlayer9" / "ldconsole.exe")
    names = {4: "日常登录甲", 17: "手机 - 备用乙", 39: "现有实例丙"}
    fake.instances = {names[index]: [index, 0] for index in indices}
    return pool_manager(tmp_path, monkeypatch, fake, indices), fake


def slot(name, *, task_id=None, state="idle", serial=None):
    return {"name": name, "serial": serial, "taskId": task_id, "state": state}


def legacy_binding(index, name, state="reserved", serial=None):
    binding = {"index": index, "name": name, "serial": serial,
               "mode": "pool", "state": state}
    if state == "retryable":
        binding["releasedFrom"] = "reserved"
    return binding


@pytest.mark.parametrize("first_operation", ["has_capacity", "pool_status", "allocate"])
def test_first_pool_access_persists_every_configured_slot(tmp_path, monkeypatch, first_operation):
    service, fake = fake_pool(tmp_path, monkeypatch)

    if first_operation == "allocate":
        service.allocate(TASK_ID, time.time() + 10)
    else:
        getattr(service, first_operation)()

    document = read_state(tmp_path)
    expected = {
        str(index): slot(name)
        for name, (index, _) in fake.instances.items()
    }
    if first_operation == "allocate":
        expected["4"].update(taskId=TASK_ID, state="reserved")
    assert document == {"version": 2, "bindings": expected}
    assert fake.copy_count == 0
    assert not list((tmp_path / "state").glob(".bindings-*.tmp"))


def test_repository_idle_template_syncs_to_this_machine_without_device_operations(tmp_path, monkeypatch):
    template_path = Path(__file__).parents[1] / "bindings.json"
    template = json.loads(template_path.read_text("utf-8"))
    assert template["version"] == 2
    assert template["bindings"]
    assert {"4", "17"} <= set(template["bindings"])
    assert "39" not in template["bindings"]
    assert all(binding["state"] == "idle" and binding["taskId"] is None
               and binding["serial"] is None
               for binding in template["bindings"].values())
    service, fake = fake_pool(tmp_path, monkeypatch)
    write_state(tmp_path, template)

    assert service.pool_status() == [
        {"name": "日常登录甲", "index": 4, "taskId": None, "state": "idle"},
        {"name": "手机 - 备用乙", "index": 17, "taskId": None, "state": "idle"},
        {"name": "现有实例丙", "index": 39, "taskId": None, "state": "idle"},
    ]
    assert read_state(tmp_path) == {
        "version": 2,
        "bindings": {
            "4": slot("日常登录甲"),
            "17": slot("手机 - 备用乙"),
            "39": slot("现有实例丙"),
        },
    }
    assert fake.calls == [(fake.console, "list2")]


def test_two_hundred_tasks_reuse_fixed_slots_without_accumulating_history(tmp_path, monkeypatch):
    service, fake = fake_pool(tmp_path, monkeypatch)
    service.has_capacity()
    expected_indices = {"4", "17", "39"}

    for number in range(200):
        task_id = str(UUID(int=number + 1, version=4))
        instance = service.allocate(task_id, time.time() + 10)
        reserved = read_state(tmp_path)
        assert reserved["version"] == 2
        assert set(reserved["bindings"]) == expected_indices
        assert reserved["bindings"][str(instance.index)]["taskId"] == task_id
        service.close(task_id)
        released = read_state(tmp_path)
        assert set(released["bindings"]) == expected_indices
        assert all(binding["taskId"] is None and binding["state"] == "idle"
                   for binding in released["bindings"].values())

    assert fake.copy_count == 0
    assert not any(call[1] in {"copy", "rename", "remove"}
                   for call in fake.calls if call[0] == fake.console)


def test_legacy_history_migrates_to_slots_and_preserves_active_owner_and_serial(tmp_path, monkeypatch):
    service, fake = fake_pool(tmp_path, monkeypatch)
    history = {
        str(UUID(int=number + 1000, version=4)): legacy_binding(
            (4, 17, 39, 99)[number % 4], "历史实例旧名称",
            "released" if number % 2 == 0 else "retryable", "emulator-legacy",
        )
        for number in range(136)
    }
    history[TASK_ID] = legacy_binding(17, "手机 - 备用乙", "qr_ready", "emulator-5812")
    history[SECOND_TASK] = legacy_binding(39, "现有实例丙", "reserved")
    write_state(tmp_path, {"bindings": history})

    assert service.has_capacity()

    assert read_state(tmp_path) == {
        "version": 2,
        "bindings": {
            "4": slot("日常登录甲"),
            "17": slot("手机 - 备用乙", task_id=TASK_ID,
                       state="qr_ready", serial="emulator-5812"),
            "39": slot("现有实例丙", task_id=SECOND_TASK, state="reserved"),
        },
    }
    restarted = LDPlayerManager(Path(fake.console), "", tmp_path / "state",
                               pool_indices=(4, 17, 39))
    retained = restarted.allocate(TASK_ID, time.time() + 10)
    assert (retained.index, retained.name, retained.serial, retained.task_id) == (
        17, "手机 - 备用乙", "emulator-5812", TASK_ID,
    )


@pytest.mark.parametrize("invalid_state", [
    "not_json", "same_index", "active_outside_pool", "active_renamed",
    "active_missing", "same_task_in_two_slots", "invalid_idle_owner",
])
def test_invalid_state_or_migration_conflict_never_overwrites_source(tmp_path, monkeypatch, invalid_state):
    service, fake = fake_pool(tmp_path, monkeypatch)
    document = {"bindings": {TASK_ID: legacy_binding(4, "日常登录甲")}}
    if invalid_state == "same_index":
        document["bindings"][SECOND_TASK] = legacy_binding(4, "日常登录甲")
    elif invalid_state == "active_outside_pool":
        document["bindings"][TASK_ID] = legacy_binding(99, "配置外实例")
        fake.instances["配置外实例"] = [99, 0]
    elif invalid_state == "active_renamed":
        document["bindings"][TASK_ID]["name"] = "被用户改名之前"
    elif invalid_state == "active_missing":
        del fake.instances["日常登录甲"]
    elif invalid_state in {"same_task_in_two_slots", "invalid_idle_owner"}:
        document = {
            "version": 2,
            "bindings": {
                "4": slot("日常登录甲", task_id=TASK_ID, state="reserved"),
                "17": slot("手机 - 备用乙"),
                "39": slot("现有实例丙"),
            },
        }
        if invalid_state == "same_task_in_two_slots":
            document["bindings"]["17"].update(taskId=TASK_ID, state="reserved")
        else:
            document["bindings"]["4"]["state"] = "idle"
    original = write_state(tmp_path, document)
    if invalid_state == "not_json":
        state_path(tmp_path).write_text("{broken state", "utf-8")
        original = state_path(tmp_path).read_bytes()

    with pytest.raises(LDPlayerError):
        service.has_capacity()

    assert state_path(tmp_path).read_bytes() == original
    assert all(call == (fake.console, "list2") for call in fake.calls)
    assert not list((tmp_path / "state").glob(".bindings-*.tmp"))


@pytest.mark.parametrize("duplicate_format", ["legacy_task", "pool_slot"])
def test_duplicate_json_keys_reject_migration_without_dropping_owner(tmp_path, monkeypatch, duplicate_format):
    service, fake = fake_pool(tmp_path, monkeypatch)
    if duplicate_format == "legacy_task":
        first = json.dumps(legacy_binding(4, "日常登录甲"), ensure_ascii=False)
        second = json.dumps(legacy_binding(17, "手机 - 备用乙"), ensure_ascii=False)
        raw = f'{{"bindings":{{"{TASK_ID}":{first},"{TASK_ID}":{second}}}}}'
    else:
        first = json.dumps(slot("日常登录甲", task_id=TASK_ID, state="reserved"),
                           ensure_ascii=False)
        second = json.dumps(slot("日常登录甲"), ensure_ascii=False)
        raw = f'{{"version":2,"bindings":{{"4":{first},"4":{second}}}}}'
    path = state_path(tmp_path)
    path.parent.mkdir()
    path.write_text(raw, "utf-8")
    original = path.read_bytes()

    with pytest.raises(LDPlayerError):
        service.has_capacity()

    # Compare raw bytes: parsing this damaged document would itself hide an owner.
    assert path.read_bytes() == original
    assert TASK_ID in path.read_text("utf-8")
    assert all(call == (fake.console, "list2") for call in fake.calls)
    assert not list((tmp_path / "state").glob(".bindings-*.tmp"))


def test_configuration_change_removes_only_idle_slots_and_adds_real_named_slots(tmp_path, monkeypatch):
    service, fake = fake_pool(tmp_path, monkeypatch)
    service.allocate(TASK_ID, time.time() + 10)
    fake.instances.pop("手机 - 备用乙")
    fake.instances.pop("现有实例丙")
    fake.instances["新配置设备丁"] = [52, 0]
    restarted = LDPlayerManager(Path(fake.console), "", tmp_path / "state",
                               pool_indices=(4, 52))

    assert restarted.pool_status() == [
        {"name": "日常登录甲", "index": 4, "taskId": TASK_ID, "state": "reserved"},
        {"name": "新配置设备丁", "index": 52, "taskId": None, "state": "idle"},
    ]
    assert read_state(tmp_path) == {
        "version": 2,
        "bindings": {
            "4": slot("日常登录甲", task_id=TASK_ID, state="reserved"),
            "52": slot("新配置设备丁"),
        },
    }


def test_configuration_cannot_remove_an_active_slot(tmp_path, monkeypatch):
    service, fake = fake_pool(tmp_path, monkeypatch)
    service.allocate(TASK_ID, time.time() + 10)
    original = state_path(tmp_path).read_bytes()
    restarted = LDPlayerManager(Path(fake.console), "", tmp_path / "state",
                               pool_indices=(17, 39))
    calls_before = len(fake.calls)

    with pytest.raises(LDPlayerError):
        restarted.pool_status()

    assert state_path(tmp_path).read_bytes() == original
    assert all(call == (fake.console, "list2") for call in fake.calls[calls_before:])


def test_idle_rename_refreshes_from_real_listing_but_active_rename_is_rejected(tmp_path, monkeypatch):
    service, fake = fake_pool(tmp_path, monkeypatch)
    service.has_capacity()
    fake.instances["后来修改的名字"] = fake.instances.pop("日常登录甲")

    assert service.pool_status()[0]["name"] == "后来修改的名字"
    assert read_state(tmp_path)["bindings"]["4"]["name"] == "后来修改的名字"
    service.allocate(TASK_ID, time.time() + 10)
    original = state_path(tmp_path).read_bytes()
    fake.instances["占用期间再次改名"] = fake.instances.pop("后来修改的名字")

    with pytest.raises(LDPlayerError):
        service.has_capacity()

    assert state_path(tmp_path).read_bytes() == original


def test_late_operations_from_old_task_cannot_change_new_owner_or_control_device(tmp_path, monkeypatch):
    service, fake = fake_pool(tmp_path, monkeypatch, (4,))
    old_instance = service.allocate(TASK_ID, time.time() + 10)
    service.start(old_instance, time.time() + 10)
    service.close(TASK_ID)
    current_instance = service.allocate(SECOND_TASK, time.time() + 10)
    assert current_instance.serial is None
    service.start(current_instance, time.time() + 10)
    service.mark_ready(SECOND_TASK)
    original = state_path(tmp_path).read_bytes()
    calls_before = len(fake.calls)

    service.close(TASK_ID)
    with pytest.raises(LDPlayerError):
        service.mark_ready(TASK_ID)
    with pytest.raises(LDPlayerError):
        service.start(old_instance, time.time() + 10)
    with pytest.raises(LDPlayerError):
        service.launch_game(old_instance, "com.tencent.naruto", time.time() + 10)

    assert state_path(tmp_path).read_bytes() == original
    assert all(call == (fake.console, "list2") for call in fake.calls[calls_before:])
    assert fake.instances["日常登录甲"][1] == 1
    assert read_state(tmp_path)["bindings"]["4"] == slot(
        "日常登录甲", task_id=SECOND_TASK, state="qr_ready", serial="emulator-5570",
    )


@pytest.mark.parametrize("change_after_quit", ["renamed_and_stopped", "missing"])
def test_cleanup_refuses_to_release_owner_if_instance_changes_after_quit(tmp_path, monkeypatch, change_after_quit):
    service, fake = fake_pool(tmp_path, monkeypatch, (4,))
    fake.instances["日常登录甲"][1] = 1
    service.allocate(TASK_ID, time.time() + 10)
    service.mark_ready(TASK_ID)
    original = state_path(tmp_path).read_bytes()
    original_run = fake.__call__

    def changing_instance(args, **kwargs):
        result = original_run(args, **kwargs)
        command = tuple(map(str, args))
        if command[:2] == (fake.console, "quit"):
            stopped = fake.instances.pop("日常登录甲")
            assert stopped[1] == 0
            if change_after_quit == "renamed_and_stopped":
                fake.instances["关闭期间被改名"] = stopped
        return result

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", changing_instance)

    with pytest.raises(LDPlayerError):
        service.close(TASK_ID)

    assert state_path(tmp_path).read_bytes() == original
    assert read_state(tmp_path)["bindings"]["4"] == slot(
        "日常登录甲", task_id=TASK_ID, state="qr_ready",
    )
    assert fake.calls.count((fake.console, "quit", "--name", "日常登录甲")) == 1
