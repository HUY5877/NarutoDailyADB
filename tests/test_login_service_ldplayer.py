import json
import multiprocessing
from pathlib import Path
import subprocess
import threading
import time

import pytest

from naruto_daily.login_service.ldplayer import (
    LDPlayerError,
    LDPlayerInstance,
    LDPlayerManager,
    LDPlayerPoolFull,
)


TASK_ID = "11111111-1111-4111-8111-111111111111"
NAME = f"nd-{TASK_ID}"


def row(index, name, running=0):
    return f"{index},{name},0,0,{running},0,0,1920,1080,280"


class Result:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeLDPlayer:
    def __init__(self, console, *, template="clean-template"):
        self.console = str(console)
        self.adb = str(Path(console).with_name("adb.exe"))
        self.template = template
        self.instances = {template: [7, 0]}
        self.calls = []
        self.timeouts = []
        self.copy_count = 0
        self._lock = threading.Lock()

    def __call__(self, args, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        with self._lock:
            command = tuple(map(str, args))
            assert kwargs["timeout"] > 0
            if command[:2] != (self.console, "copy"):
                assert kwargs["timeout"] <= 20
            self.calls.append(command)
            self.timeouts.append((command, kwargs["timeout"]))
            if command[:2] == (self.console, "list2"):
                lines = [row(index, name, running) for name, (index, running) in self.instances.items()]
                return Result(("\n".join(lines) + "\n").encode("utf-8"))
            if command[:2] == (self.console, "copy"):
                self.copy_count += 1
                name = command[command.index("--name") + 1]
                source = command[command.index("--from") + 1]
                assert source == self.template
                self.instances[name] = [8, 0]
                return Result()
            if command[:2] == (self.console, "launch"):
                name = command[command.index("--name") + 1]
                self.instances[name][1] = 1
                return Result()
            if command[:2] == (self.console, "quit"):
                name = command[command.index("--name") + 1]
                self.instances[name][1] = 0
                return Result()
            if command[:2] == (self.console, "adb"):
                return Result(b"emulator-5570\r\n")
            if command[0] == self.adb and command[-2:] == ("getprop", "sys.boot_completed"):
                return Result(b"1\r\n")
            if command[0] == self.adb and command[-2:] == ("wm", "size"):
                return Result(b"Physical size: 1920x1080\r\n")
            if (command[0] == self.adb and len(command) == 7
                    and command[3:6] == ("shell", "am", "force-stop")):
                return Result()
            if command[:2] == (self.console, "runapp"):
                return Result()
            raise AssertionError(f"unexpected command: {command}")


class ProcessFakeLDPlayer:
    def __init__(self, console, instances, counters, guard, copy_started=None, copy_delay=0.15):
        self.console = str(console)
        self.instances = instances
        self.counters = counters
        self.guard = guard
        self.copy_started = copy_started
        self.copy_delay = copy_delay

    def __call__(self, args, **kwargs):
        command = tuple(map(str, args))
        if command[:2] == (self.console, "list2"):
            with self.guard:
                lines = [row(index, name, running) for name, (index, running) in self.instances.items()]
            return Result(("\n".join(lines) + "\n").encode())
        if command[:2] == (self.console, "copy"):
            name = command[command.index("--name") + 1]
            with self.guard:
                self.counters["active"] += 1
                self.counters["maximum"] = max(self.counters["maximum"], self.counters["active"])
                self.counters["copies"] += 1
                index = 8 + sum(candidate.startswith("nd-") for candidate in self.instances.keys())
            if self.copy_started is not None:
                self.copy_started.set()
            time.sleep(self.copy_delay)
            with self.guard:
                self.instances[name] = (index, 0)
                self.counters["active"] -= 1
            return Result()
        raise AssertionError(f"unexpected command: {command}")


def allocate_in_process(console, state_dir, task_id, instances, counters, guard, gate, results,
                        copy_started=None, copy_delay=0.15):
    import naruto_daily.login_service.ldplayer as ldplayer

    runner = ProcessFakeLDPlayer(
        console, instances, counters, guard, copy_started, copy_delay,
    )
    ldplayer.subprocess.run = runner
    try:
        if gate is not None:
            gate.wait()
        instance = ldplayer.LDPlayerManager(Path(console), "clean-template", Path(state_dir)).allocate(
            task_id, time.time() + 10,
        )
        results.put(("ok", instance.name))
    except Exception as error:
        import traceback
        results.put(("error", type(error).__name__, str(error), traceback.format_exc()))


def manager(tmp_path, monkeypatch, fake):
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", fake)
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.time.sleep", lambda _: None)
    return LDPlayerManager(Path(fake.console), fake.template, tmp_path / "state")


def pool_manager(tmp_path, monkeypatch, fake, indices=(0, 2)):
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", fake)
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.time.sleep", lambda _: None)
    return LDPlayerManager(Path(fake.console), "", tmp_path / "state", pool_indices=indices)


def test_pool_allocates_distinct_fixed_instances_and_reports_full(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 0], "installed-template": [2, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake)
    second_task = "22222222-2222-4222-8222-222222222222"
    third_task = "33333333-3333-4333-8333-333333333333"

    first = service.allocate(TASK_ID, time.time() + 10)
    second = service.allocate(second_task, time.time() + 10)

    assert first == LDPlayerInstance("existing-zero", 0, None, TASK_ID)
    assert second == LDPlayerInstance("installed-template", 2, None, second_task)
    assert not service.has_capacity()
    with pytest.raises(LDPlayerPoolFull):
        service.allocate(third_task, time.time() + 10)
    assert not any(call[1] in {"copy", "rename", "remove"} for call in fake.calls if call[0] == fake.console)
    bindings = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))["bindings"]
    assert bindings[TASK_ID] == {
        "index": 0, "mode": "pool", "name": "existing-zero", "serial": None, "state": "reserved",
    }
    assert bindings[second_task]["index"] == 2


def test_pool_ready_binding_survives_restart_and_remains_reserved(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    instance = service.allocate(TASK_ID, time.time() + 10)

    service.mark_ready(TASK_ID)
    restarted = LDPlayerManager(console, "", tmp_path / "state", pool_indices=(0,))

    assert restarted.allocate(TASK_ID, time.time() + 10) == instance
    assert not restarted.has_capacity()
    state = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))
    assert state["bindings"][TASK_ID]["state"] == "qr_ready"
    assert not any(call[1] == "quit" for call in fake.calls if call[0] == fake.console)


def test_pool_close_releases_only_after_quit_and_old_task_cannot_reacquire_taken_slot(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 1]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    service.allocate(TASK_ID, time.time() + 10)

    service.close(TASK_ID)

    assert (fake.console, "quit", "--name", "existing-zero") in fake.calls
    assert service.has_capacity()
    second_task = "22222222-2222-4222-8222-222222222222"
    assert service.allocate(second_task, time.time() + 10).index == 0
    with pytest.raises(LDPlayerError, match="released"):
        service.allocate(TASK_ID, time.time() + 10)
    bindings = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))["bindings"]
    assert bindings[TASK_ID]["state"] == "released"
    assert bindings[second_task]["state"] == "reserved"


def test_prepare_retry_allows_only_closed_unready_task_to_rent_again(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 1]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    service.allocate(TASK_ID, time.time() + 10)
    service.close(TASK_ID)

    service.prepare_retry(TASK_ID)
    service.prepare_retry(TASK_ID)

    assert service.has_capacity()
    binding = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))["bindings"][TASK_ID]
    assert binding["state"] == "retryable"
    assert binding["releasedFrom"] == "reserved"
    retried = service.allocate(TASK_ID, time.time() + 10)
    assert retried == LDPlayerInstance("existing-zero", 0, None, TASK_ID)
    binding = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))["bindings"][TASK_ID]
    assert binding["state"] == "reserved"
    assert "releasedFrom" not in binding


def test_prepare_retry_refuses_task_that_already_published_qr(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    service.allocate(TASK_ID, time.time() + 10)
    service.mark_ready(TASK_ID)
    service.close(TASK_ID)

    with pytest.raises(LDPlayerError, match="ready"):
        service.prepare_retry(TASK_ID)
    with pytest.raises(LDPlayerError, match="released"):
        service.allocate(TASK_ID, time.time() + 10)


def test_prepare_retry_is_idempotent_before_a_pool_binding_exists(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))

    service.prepare_retry(TASK_ID)

    assert service.has_capacity()
    assert not (tmp_path / "state" / "bindings.json").exists()


def test_old_pool_instance_object_cannot_control_slot_after_reassignment(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    second_task = "22222222-2222-4222-8222-222222222222"
    old_instance = service.allocate(TASK_ID, time.time() + 10)
    service.close(TASK_ID)
    current_instance = service.allocate(second_task, time.time() + 10)

    assert old_instance.task_id == TASK_ID
    assert current_instance.task_id == second_task
    with pytest.raises(LDPlayerError, match="owner"):
        service.start(old_instance, time.time() + 10)
    calls_before_old_launch = len(fake.calls)
    with pytest.raises(LDPlayerError, match="owner"):
        service.launch_game(old_instance, "com.tencent.naruto", time.time() + 10)
    assert len(fake.calls) == calls_before_old_launch

    assert service.start(current_instance, time.time() + 10) == "emulator-5570"
    current_instance.serial = None  # persisted binding serial is an equivalent trusted source
    launch_start = len(fake.calls)
    service.launch_game(current_instance, "com.tencent.naruto", time.time() + 10)
    assert fake.calls[launch_start:] == [
        (fake.console, "list2"),
        (fake.adb, "-s", "emulator-5570", "shell", "am", "force-stop", "com.tencent.naruto"),
        (fake.adb, "-s", "emulator-5570", "shell", "am", "force-stop", "com.tencent.mobileqq"),
        (fake.console, "runapp", "--name", "existing-zero", "--packagename", "com.tencent.naruto"),
    ]


def test_pool_start_allows_configured_index_zero_and_tracks_serial(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    instance = service.allocate(TASK_ID, time.time() + 10)

    assert service.start(instance, time.time() + 10) == "emulator-5570"
    assert instance.serial == "emulator-5570"
    assert (fake.console, "launch", "--name", "existing-zero") in fake.calls
    assert (fake.console, "adb", "--index", "0", "--command", "get-serialno") in fake.calls


def test_launch_game_requires_serial_before_any_force_stop_or_runapp(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 1]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    instance = service.allocate(TASK_ID, time.time() + 10)
    calls_before = len(fake.calls)

    with pytest.raises(LDPlayerError, match="Start.*before launching"):
        service.launch_game(instance, "com.tencent.naruto", time.time() + 10)

    assert fake.calls[calls_before:] == [(fake.console, "list2")]


def test_list2_starting_state_two_is_busy_until_adb_reports_ready(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"

    class StartingFake(FakeLDPlayer):
        def __call__(self, args, **kwargs):
            command = tuple(map(str, args))
            if command[:2] == (self.console, "launch"):
                self.calls.append(command)
                self.instances["existing-zero"][1] = 2
                return Result()
            if command[:2] == (self.console, "adb"):
                self.instances["existing-zero"][1] = 1
            return super().__call__(args, **kwargs)

    fake = StartingFake(console)
    fake.instances = {"existing-zero": [0, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    instance = service.allocate(TASK_ID, time.time() + 10)

    assert service.start(instance, time.time() + 10) == "emulator-5570"
    assert fake.instances["existing-zero"][1] == 1
    assert fake.calls.count((fake.console, "launch", "--name", "existing-zero")) == 1


def test_close_quits_list2_starting_state_two_and_waits_for_zero(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 2]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))
    service.allocate(TASK_ID, time.time() + 10)

    service.close(TASK_ID)

    assert (fake.console, "quit", "--name", "existing-zero") in fake.calls
    assert fake.instances["existing-zero"][1] == 0


def test_list2_rejects_unknown_runtime_state(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 3]}
    service = pool_manager(tmp_path, monkeypatch, fake, (0,))

    with pytest.raises(LDPlayerError, match="invalid instance information"):
        service.allocate(TASK_ID, time.time() + 10)


def test_pool_status_reports_each_slot_and_ignores_released_history(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances = {"existing-zero": [0, 0], "installed-template": [2, 0]}
    service = pool_manager(tmp_path, monkeypatch, fake)
    second_task = "22222222-2222-4222-8222-222222222222"
    service.allocate(TASK_ID, time.time() + 10)
    service.allocate(second_task, time.time() + 10)
    service.mark_ready(second_task)
    service.close(TASK_ID)

    assert service.pool_status() == [
        {"name": "existing-zero", "index": 0, "taskId": None, "state": "idle"},
        {"name": "installed-template", "index": 2, "taskId": second_task, "state": "qr_ready"},
    ]


def test_allocate_copies_stopped_template_and_persists_binding_atomically(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)

    instance = service.allocate(TASK_ID, time.time() + 10)

    assert instance == LDPlayerInstance(NAME, 8, None)
    assert fake.copy_count == 1
    state = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))
    assert state == {"bindings": {TASK_ID: {"index": 8, "name": NAME, "serial": None}}}
    assert not list((tmp_path / "state").glob("*.tmp"))


def test_allocate_reuses_persisted_instance_without_copying(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    first = manager(tmp_path, monkeypatch, fake).allocate(TASK_ID, time.time() + 10)
    second = LDPlayerManager(console, fake.template, tmp_path / "state")

    reused = second.allocate(TASK_ID, time.time() + 10)

    assert reused == first
    assert fake.copy_count == 1


def test_allocate_serializes_concurrent_requests_for_same_task(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)
    gate = threading.Barrier(3)
    results = []

    def allocate():
        gate.wait()
        results.append(service.allocate(TASK_ID, time.time() + 10))

    workers = [threading.Thread(target=allocate) for _ in range(2)]
    for worker in workers:
        worker.start()
    gate.wait()
    for worker in workers:
        worker.join(2)

    assert results == [LDPlayerInstance(NAME, 8, None)] * 2
    assert fake.copy_count == 1


def test_allocate_serializes_copy_and_preserves_bindings_across_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as shared:
        console = tmp_path / "LDPlayer9" / "ldconsole.exe"
        instances = shared.dict({"clean-template": (7, 0)})
        counters = shared.dict(active=0, maximum=0, copies=0)
        guard = shared.RLock()
        gate = ctx.Barrier(2)
        results = ctx.Queue()
        task_ids = [TASK_ID, "22222222-2222-4222-8222-222222222222"]
        workers = [ctx.Process(
            target=allocate_in_process,
            args=(str(console), str(tmp_path / "state"), task_id, instances, counters, guard, gate, results),
        ) for task_id in task_ids]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            if worker.is_alive():
                worker.terminate()
                worker.join()
                pytest.fail("allocation process did not finish")

        outcomes = [results.get(timeout=1) for _ in workers]
        assert [outcome[0] for outcome in outcomes] == ["ok", "ok"], outcomes
        assert counters["copies"] == 2
        assert counters["maximum"] == 1
        bindings = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))["bindings"]
        assert set(bindings) == set(task_ids)


def test_waiting_for_process_lock_obeys_deadline(tmp_path, monkeypatch):
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as shared:
        console = tmp_path / "LDPlayer9" / "ldconsole.exe"
        instances = shared.dict({"clean-template": (7, 0)})
        counters = shared.dict(active=0, maximum=0, copies=0)
        guard = shared.RLock()
        copy_started = ctx.Event()
        results = ctx.Queue()
        holder = ctx.Process(
            target=allocate_in_process,
            args=(str(console), str(tmp_path / "state"), TASK_ID, instances, counters, guard,
                  None, results, copy_started, 0.5),
        )
        holder.start()
        assert copy_started.wait(5)

        fake = FakeLDPlayer(console)
        service = manager(tmp_path, monkeypatch, fake)
        with pytest.raises(LDPlayerError, match="deadline"):
            service.allocate("22222222-2222-4222-8222-222222222222", time.time() + 0.05)

        holder.join(10)
        if holder.is_alive():
            holder.terminate()
            holder.join()
            pytest.fail("lock holder did not finish")
        assert results.get(timeout=1)[0] == "ok"


def test_allocate_refuses_running_template_without_stopping_it(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances[fake.template][1] = 1
    service = manager(tmp_path, monkeypatch, fake)

    with pytest.raises(LDPlayerError, match="template is running.*close it manually"):
        service.allocate(TASK_ID, time.time() + 10)

    assert not any(call[1] in {"copy", "quit"} for call in fake.calls)


def test_allocate_never_uses_index_zero_as_template(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    fake.instances[fake.template][0] = 0
    service = manager(tmp_path, monkeypatch, fake)

    with pytest.raises(LDPlayerError, match="index 0"):
        service.allocate(TASK_ID, time.time() + 10)

    assert not any(call[1] == "copy" for call in fake.calls)


def test_numeric_template_uses_index_semantics_even_if_a_name_matches(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console, template="7")
    fake.instances["7"] = [9, 1]
    fake.instances["actual-template"] = [7, 0]
    service = manager(tmp_path, monkeypatch, fake)

    assert service.allocate(TASK_ID, time.time() + 10) == LDPlayerInstance(NAME, 8, None)
    assert fake.copy_count == 1


def test_start_refreshes_index_and_verifies_serial_boot_and_resolution(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)
    instance = service.allocate(TASK_ID, time.time() + 10)
    fake.instances[NAME][0] = 8
    instance.index = 99

    serial = service.start(instance, time.time() + 10)

    assert serial == "emulator-5570"
    assert instance == LDPlayerInstance(NAME, 8, "emulator-5570")
    assert (fake.console, "launch", "--name", NAME) in fake.calls
    assert (fake.console, "adb", "--index", "8", "--command", "get-serialno") in fake.calls
    assert (fake.adb, "-s", serial, "shell", "getprop", "sys.boot_completed") in fake.calls
    assert (fake.adb, "-s", serial, "shell", "wm", "size") in fake.calls


def test_start_retries_serial_lookup_until_adb_is_ready(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)
    instance = service.allocate(TASK_ID, time.time() + 10)
    original = fake.__call__
    attempts = 0

    def delayed_serial(args, **kwargs):
        nonlocal attempts
        command = tuple(map(str, args))
        if command[:2] == (fake.console, "adb"):
            attempts += 1
            if attempts == 1:
                fake.calls.append(command)
                return Result(b"", b"ADB not ready", 1)
        return original(args, **kwargs)

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", delayed_serial)

    assert service.start(instance, time.time() + 10) == "emulator-5570"
    assert attempts == 2


def test_start_rejects_wrong_resolution_and_never_accepts_index_zero(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)
    instance = service.allocate(TASK_ID, time.time() + 10)

    original = fake.__call__
    def wrong_size(args, **kwargs):
        command = tuple(map(str, args))
        if command[0] == fake.adb and command[-2:] == ("wm", "size"):
            fake.calls.append(command)
            return Result(b"Physical size: 1280x720\n")
        return original(args, **kwargs)
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", wrong_size)
    with pytest.raises(LDPlayerError, match="1920x1080"):
        service.start(instance, time.time() + 0.05)

    fake.instances[NAME] = [0, 0]
    with pytest.raises(LDPlayerError, match="index 0"):
        service.start(instance, time.time() + 10)


def test_launch_game_refreshes_instance_and_close_only_quits(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)
    instance = service.allocate(TASK_ID, time.time() + 10)
    service.start(instance, time.time() + 10)

    instance.index = 123
    service.launch_game(instance, "com.tencent.naruto", time.time() + 10)
    service.close(TASK_ID)

    assert (fake.console, "runapp", "--name", NAME, "--packagename", "com.tencent.naruto") in fake.calls
    assert (fake.console, "quit", "--name", NAME) in fake.calls
    assert not any(call[1] == "remove" for call in fake.calls if call[0] == fake.console)
    assert (tmp_path / "state" / "bindings.json").exists()
    assert json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))["bindings"][TASK_ID]["name"] == NAME


def test_timed_out_copy_keeps_binding_so_late_instance_can_be_closed(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)
    original = fake.__call__

    def timeout_copy(args, **kwargs):
        command = tuple(map(str, args))
        if command[:2] == (fake.console, "copy"):
            fake.calls.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"SECRET-OUTPUT")
        return original(args, **kwargs)

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", timeout_copy)
    with pytest.raises(LDPlayerError) as exc:
        service.allocate(TASK_ID, time.time() + 10)
    assert "SECRET" not in str(exc.value)
    binding = json.loads((tmp_path / "state" / "bindings.json").read_text("utf-8"))["bindings"][TASK_ID]
    assert binding == {"index": None, "name": NAME, "serial": None}

    fake.instances[NAME] = [8, 1]
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", fake)
    service.close(TASK_ID)
    assert fake.instances[NAME][1] == 0
    assert not any(call[1] == "remove" for call in fake.calls if call[0] == fake.console)


def test_copy_uses_task_deadline_and_exit_one_succeeds_only_when_target_exists(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    original = fake.__call__

    def exit_one_with_instance(args, **kwargs):
        command = tuple(map(str, args))
        if command[:2] == (fake.console, "copy"):
            fake.calls.append(command)
            fake.timeouts.append((command, kwargs["timeout"]))
            fake.instances[NAME] = [8, 0]
            return Result(b"copy reported failure", b"private detail", 1)
        return original(args, **kwargs)

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", exit_one_with_instance)
    service = LDPlayerManager(console, fake.template, tmp_path / "state")

    assert service.allocate(TASK_ID, time.time() + 60) == LDPlayerInstance(NAME, 8, None)
    copy_timeout = next(timeout for command, timeout in fake.timeouts if command[:2] == (fake.console, "copy"))
    assert copy_timeout > 50

    second_task = "22222222-2222-4222-8222-222222222222"
    second_name = f"nd-{second_task}"

    def exit_one_without_instance(args, **kwargs):
        command = tuple(map(str, args))
        if command[:2] == (fake.console, "copy") and command[command.index("--name") + 1] == second_name:
            fake.calls.append(command)
            return Result(b"PRIVATE", b"TOKEN", 1)
        return original(args, **kwargs)

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", exit_one_without_instance)
    with pytest.raises(LDPlayerError) as exc:
        service.allocate(second_task, time.time() + 10)
    assert "PRIVATE" not in str(exc.value)
    assert "TOKEN" not in str(exc.value)


def test_list2_accepts_gb18030_and_errors_never_expose_process_output(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console, template="干净模板")
    original = fake.__call__

    def encoded(args, **kwargs):
        command = tuple(map(str, args))
        if command[:2] == (fake.console, "list2"):
            fake.calls.append(command)
            lines = [row(index, name, running) for name, (index, running) in fake.instances.items()]
            return Result(("\n".join(lines) + "\n").encode("gb18030"))
        if command[:2] == (fake.console, "copy"):
            return Result(b"PRIVATE", b"TOKEN", 1)
        return original(args, **kwargs)

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", encoded)
    service = LDPlayerManager(console, fake.template, tmp_path / "state")
    with pytest.raises(LDPlayerError) as exc:
        service.allocate(TASK_ID, time.time() + 10)
    assert "PRIVATE" not in str(exc.value)
    assert "TOKEN" not in str(exc.value)


def test_invalid_task_package_state_and_expired_deadline_fail_safely(tmp_path, monkeypatch):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    fake = FakeLDPlayer(console)
    service = manager(tmp_path, monkeypatch, fake)

    with pytest.raises(LDPlayerError, match="task id"):
        service.allocate("../../other", time.time() + 10)
    with pytest.raises(LDPlayerError, match="deadline"):
        service.allocate(TASK_ID, time.time() - 1)
    assert not fake.calls

    instance = service.allocate(TASK_ID, time.time() + 10)
    with pytest.raises(LDPlayerError, match="package"):
        service.launch_game(instance, "bad package", time.time() + 10)

    (tmp_path / "state" / "bindings.json").write_text("not-json", "utf-8")
    with pytest.raises(LDPlayerError, match="state"):
        LDPlayerManager(console, fake.template, tmp_path / "state").allocate(TASK_ID, time.time() + 10)
