"""Failure diagnostics must never capture an unowned emulator."""

from concurrent.futures import ThreadPoolExecutor
import base64
import json
from pathlib import Path
import subprocess
import threading
import time

import pytest

from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerManager


TASK_ID = "11111111-1111-4111-8111-111111111111"
OTHER_TASK = "22222222-2222-4222-8222-222222222222"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII="
)


class ScreenshotCommands:
    """Only read-only listing, serial verification, and screenshot commands exist."""

    def __init__(self, console, index, name):
        self.console = str(console)
        self.adb = str(console.with_name("adb.exe"))
        self.index = index
        self.name = name
        self.running = True
        self.serial = "emulator-5570"
        self.output = PNG
        self.error = None
        self.on_capture = None
        self.calls = []
        self.timeouts = []

    def __call__(self, arguments, **kwargs):
        command = tuple(map(str, arguments))
        assert kwargs["shell"] is False
        assert kwargs["stdout"] != subprocess.PIPE
        assert kwargs["stderr"] != subprocess.PIPE
        self.calls.append(command)
        self.timeouts.append(kwargs["timeout"])
        if command == (self.console, "list2"):
            output = f"{self.index},{self.name},0,0,{int(self.running)},0,0\n".encode()
        elif command == (self.console, "adb", "--index", str(self.index), "--command", "get-serialno"):
            output = self.serial.encode()
        elif command == (self.adb, "-s", "emulator-5570", "exec-out", "screencap", "-p"):
            if self.on_capture is not None:
                self.on_capture()
            if self.error is not None:
                raise self.error
            output = self.output
        else:
            raise AssertionError(f"unexpected command: {command}")
        kwargs["stdout"].write(output)
        kwargs["stdout"].flush()
        return subprocess.CompletedProcess(command, 0)


def configured_service(tmp_path, monkeypatch, *, pool):
    console = tmp_path / "LDPlayer9" / "ldconsole.exe"
    index = 0 if pool else 8
    name = "pool-zero" if pool else f"nd-{TASK_ID}"
    service = LDPlayerManager(console, "" if pool else "template", tmp_path / "state",
                             pool_indices=(0,) if pool else ())
    binding = {"name": name, "serial": "emulator-5570"}
    if pool:
        binding.update(taskId=TASK_ID, state="reserved")
        document = {"version": 2, "bindings": {"0": binding}}
    else:
        binding["index"] = index
        document = {"bindings": {TASK_ID: binding}}
    service.state_dir.mkdir()
    service._state_path.write_text(json.dumps(document), encoding="utf-8")
    commands = ScreenshotCommands(console, index, name)
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", commands)
    return service, commands


def edit_binding(service, pool, change):
    document = json.loads(service._state_path.read_text("utf-8"))
    change(document["bindings"]["0" if pool else TASK_ID])
    service._state_path.write_text(json.dumps(document), encoding="utf-8")


def assert_no_capture(commands):
    assert not any("screencap" in call for call in commands.calls)


@pytest.mark.parametrize("pool", [False, True])
def test_capture_uses_only_the_running_task_binding_and_preserves_it(tmp_path, monkeypatch, pool):
    service, commands = configured_service(tmp_path, monkeypatch, pool=pool)
    before = service._state_path.read_bytes()

    assert service.capture_task_screenshot(TASK_ID) == PNG

    assert commands.calls[-1] == (
        commands.adb, "-s", "emulator-5570", "exec-out", "screencap", "-p",
    )
    assert all(0 < timeout <= 15 for timeout in commands.timeouts)
    assert service._state_path.read_bytes() == before


@pytest.mark.parametrize("pool", [False, True])
@pytest.mark.parametrize("problem", ["absent_task", "stopped", "missing_instance", "changed_index",
                                      "changed_serial", "unknown_serial", "empty_serial"])
def test_capture_rejects_unverifiable_ownership_before_screenshot(tmp_path, monkeypatch, pool, problem):
    service, commands = configured_service(tmp_path, monkeypatch, pool=pool)
    task_id = TASK_ID
    if problem == "absent_task":
        task_id = OTHER_TASK
    elif problem == "stopped":
        commands.running = False
    elif problem == "missing_instance":
        commands.name = "somebody-elses-emulator"
    elif problem == "changed_index":
        commands.index += 1
    elif problem == "changed_serial":
        commands.serial = "emulator-5590"
    elif problem == "unknown_serial":
        edit_binding(service, pool, lambda binding: binding.update(serial="unknown"))
    elif problem == "empty_serial":
        edit_binding(service, pool, lambda binding: binding.update(serial=None))

    with pytest.raises(LDPlayerError):
        service.capture_task_screenshot(task_id)

    assert_no_capture(commands)


@pytest.mark.parametrize("state,owner", [("idle", None), ("reserved", OTHER_TASK), ("qr_ready", OTHER_TASK)])
def test_released_or_reassigned_pool_slot_is_not_captured(tmp_path, monkeypatch, state, owner):
    service, commands = configured_service(tmp_path, monkeypatch, pool=True)
    edit_binding(service, True, lambda binding: binding.update(state=state, taskId=owner))

    with pytest.raises(LDPlayerError):
        service.capture_task_screenshot(TASK_ID)

    assert_no_capture(commands)


@pytest.mark.parametrize("index", [None, 0, -1])
def test_clone_without_a_valid_allocated_index_is_not_captured(tmp_path, monkeypatch, index):
    service, commands = configured_service(tmp_path, monkeypatch, pool=False)
    edit_binding(service, False, lambda binding: binding.update(index=index))

    with pytest.raises(LDPlayerError):
        service.capture_task_screenshot(TASK_ID)

    assert_no_capture(commands)


@pytest.mark.parametrize("output", [b"", b"secret diagnostic text", b"\x89PNG\r\n\x1a\n",
                                    b"\x89PNG\r\n\x1a\n" + b"x" * (16 * 1024 * 1024)],
                         ids=["empty", "text", "signature-only", "oversized"])
def test_invalid_or_oversized_screenshot_is_rejected_safely(tmp_path, monkeypatch, output):
    service, commands = configured_service(tmp_path, monkeypatch, pool=True)
    commands.output = output

    with pytest.raises(LDPlayerError) as captured:
        service.capture_task_screenshot(TASK_ID)

    assert "secret diagnostic text" not in str(captured.value)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "15", None])
def test_invalid_timeout_fails_before_device_access(tmp_path, monkeypatch, timeout):
    service, commands = configured_service(tmp_path, monkeypatch, pool=True)

    with pytest.raises(LDPlayerError):
        service.capture_task_screenshot(TASK_ID, timeout_seconds=timeout)

    assert commands.calls == []


@pytest.mark.parametrize("error", [OSError("secret"), subprocess.TimeoutExpired("secret", 1)])
def test_capture_command_failure_is_safe_and_releases_lock(tmp_path, monkeypatch, error):
    service, commands = configured_service(tmp_path, monkeypatch, pool=True)
    commands.error = error

    with pytest.raises(LDPlayerError) as captured:
        service.capture_task_screenshot(TASK_ID, timeout_seconds=0.5)

    assert "secret" not in str(captured.value)
    commands.error = None
    assert service.capture_task_screenshot(TASK_ID) == PNG


def test_screenshot_has_a_fresh_but_shared_operation_deadline(tmp_path, monkeypatch):
    service, commands = configured_service(tmp_path, monkeypatch, pool=True)
    now = [1000.0]
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.time.time", lambda: now[0])
    original = commands.__call__

    def advancing_command(arguments, **kwargs):
        result = original(arguments, **kwargs)
        now[0] += 1
        return result

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", advancing_command)

    assert service.capture_task_screenshot(TASK_ID, timeout_seconds=6) == PNG
    assert commands.timeouts == [6, 5, 4, 3]
    now[0] = 1000
    with pytest.raises(LDPlayerError, match="deadline"):
        service.capture_task_screenshot(TASK_ID, timeout_seconds=3.5)


def test_binding_lock_is_held_until_capture_finishes(tmp_path, monkeypatch):
    service, commands = configured_service(tmp_path, monkeypatch, pool=True)
    other = LDPlayerManager(service.console_path, "", service.state_dir, pool_indices=(0,))
    entered = threading.Event()
    release = threading.Event()

    def blocked_capture():
        entered.set()
        assert release.wait(2)

    commands.on_capture = blocked_capture
    with ThreadPoolExecutor(max_workers=1) as executor:
        capture = executor.submit(service.capture_task_screenshot, TASK_ID)
        try:
            assert entered.wait(2)
            with pytest.raises(LDPlayerError, match="deadline"):
                with other._exclusive(time.time() + 0.1):
                    pytest.fail("Screenshot released ownership before the ADB read finished")
        finally:
            release.set()
        assert capture.result(timeout=2) == PNG

    with other._exclusive(time.time() + 1):
        pass
