import base64
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path

import pytest

from naruto_daily.login_service import ldplayer_worker


TASK_ID = "11111111-1111-4111-8111-111111111111"
PNG = b"\x89PNG\r\n\x1a\nqr-fixture"


def request(now: float = 1_800_000_000.0, **changes):
    expires_at = datetime.fromtimestamp(now + 300, timezone.utc).isoformat().replace("+00:00", "Z")
    value = {
        "type": "start",
        "id": TASK_ID,
        "expiresAt": expires_at,
        "consolePath": "C:/LDPlayer/ldconsole.exe",
        "template": "clean-template",
        "stateDir": "C:/service/state",
        "package": "com.tencent.naruto",
        "commandTimeout": 23,
    }
    value.update(changes)
    return value


def invoke(monkeypatch, message, dependencies, *, now=1_800_000_000.0):
    monkeypatch.setattr(ldplayer_worker, "_load_dependencies", lambda: dependencies)
    output = StringIO()
    code = ldplayer_worker.main(
        input_stream=StringIO(json.dumps(message) + "\n"),
        output_stream=output,
        clock=lambda: now,
        use_watchdog=False,
    )
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


def test_worker_runs_ldplayer_and_qr_navigation_protocol(monkeypatch):
    calls = []

    class DriverError(RuntimeError):
        pass

    class Manager:
        def __init__(self, **kwargs):
            calls.append(("manager", kwargs))
            self.adb_path = Path("C:/LDPlayer/adb.exe")

        def allocate(self, task_id, deadline):
            calls.append(("allocate", task_id, deadline))
            return "instance"

        def start(self, instance, deadline):
            calls.append(("start", instance, deadline))
            return "emulator-5570"

        def launch_game(self, instance, package, deadline):
            calls.append(("launch", instance, package, deadline))

    class Device:
        def __init__(self, adb_path, serial, deadline, command_timeout=20):
            calls.append(("device", adb_path, serial, deadline, command_timeout))

    def fetch(device):
        calls.append(("fetch", device.__class__.__name__))
        return PNG

    now = 1_800_000_000.0
    code, events = invoke(monkeypatch, request(now), (Manager, DriverError, Device, fetch), now=now)

    deadline = now + 300
    assert code == 0
    assert events == [
        {"type": "stage", "status": "starting_emulator"},
        {"type": "stage", "status": "opening_game"},
        {"type": "stage", "status": "preparing_qr"},
        {"type": "qr", "png": base64.b64encode(PNG).decode("ascii")},
    ]
    assert calls == [
        ("manager", {
            "console_path": Path("C:/LDPlayer/ldconsole.exe"),
            "template": "clean-template",
            "state_dir": Path("C:/service/state"),
            "command_timeout": 23.0,
        }),
        ("allocate", TASK_ID, deadline),
        ("start", "instance", deadline),
        ("launch", "instance", "com.tencent.naruto", deadline),
        ("device", Path("C:/LDPlayer/adb.exe"), "emulator-5570", deadline, 23.0),
        ("fetch", "Device"),
    ]


def test_worker_passes_nonempty_pool_indices_and_allows_empty_template(monkeypatch):
    observed = {}

    class DriverError(RuntimeError):
        pass

    class Manager:
        adb_path = Path("adb.exe")

        def __init__(self, **kwargs):
            observed.update(kwargs)

        def allocate(self, task_id, deadline):
            return object()

        def start(self, instance, deadline):
            return "emulator-5570"

        def launch_game(self, instance, package, deadline):
            pass

    class Device:
        def __init__(self, *args, **kwargs):
            pass

    code, _ = invoke(
        monkeypatch,
        request(template="", poolIndices=[0, 2]),
        (Manager, DriverError, Device, lambda device: PNG),
    )

    assert code == 0
    assert observed["pool_indices"] == (0, 2)
    assert observed["template"] == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "other"},
        {"id": "not-a-uuid"},
        {"id": "11111111-1111-1111-8111-111111111111"},
        {"expiresAt": "2030-01-01T00:00:00"},
        {"expiresAt": datetime.fromtimestamp(1_800_000_000, timezone.utc).isoformat()},
        {"expiresAt": datetime.fromtimestamp(1_800_000_601, timezone.utc).isoformat()},
        {"consolePath": ""},
        {"template": ""},
        {"stateDir": ""},
        {"package": ""},
        {"commandTimeout": True},
        {"commandTimeout": 0},
        {"poolIndices": "0,2"},
        {"poolIndices": [-1]},
        {"poolIndices": [True]},
        {"poolIndices": [0, 0]},
        {"template": "", "poolIndices": []},
    ],
)
def test_invalid_start_request_only_emits_configuration_error(monkeypatch, changes):
    code, events = invoke(
        monkeypatch,
        request(**changes),
        dependencies=None,
    )

    assert code == 1
    assert events == [{"type": "error", "code": "configuration_error"}]


def test_worker_maps_each_operation_failure_without_exposing_exception(monkeypatch):
    class DriverError(RuntimeError):
        pass

    class Manager:
        adb_path = Path("adb.exe")

        def __init__(self, **kwargs):
            pass

        def allocate(self, task_id, deadline):
            raise DriverError("redis://user:secret@private-host")

    class Device:
        pass

    code, events = invoke(
        monkeypatch,
        request(),
        (Manager, DriverError, Device, lambda device: PNG),
    )

    assert code == 1
    assert events[-1] == {"type": "error", "code": "emulator_start_failed"}
    assert "secret" not in json.dumps(events)


def test_timeout_and_invalid_qr_have_fixed_error_codes(monkeypatch):
    class DriverError(RuntimeError):
        pass

    class TimeoutManager:
        adb_path = Path("adb.exe")

        def __init__(self, **kwargs):
            pass

        def allocate(self, task_id, deadline):
            raise TimeoutError("private timeout detail")

    code, events = invoke(
        monkeypatch, request(), (TimeoutManager, DriverError, object, lambda device: PNG)
    )
    assert code == 1
    assert events[-1] == {"type": "error", "code": "command_timeout"}

    class Manager:
        adb_path = Path("adb.exe")

        def __init__(self, **kwargs):
            pass

        def allocate(self, task_id, deadline):
            return object()

        def start(self, instance, deadline):
            return "emulator-5570"

        def launch_game(self, instance, package, deadline):
            pass

    class Device:
        def __init__(self, *args, **kwargs):
            pass

    code, events = invoke(
        monkeypatch, request(), (Manager, DriverError, Device, lambda device: b"not-png")
    )
    assert code == 1
    assert events[-1] == {"type": "error", "code": "qr_not_found"}


def test_game_launch_driver_error_has_specific_safe_code(monkeypatch):
    class DriverError(RuntimeError):
        pass

    class Manager:
        adb_path = Path("adb.exe")

        def __init__(self, **kwargs):
            pass

        def allocate(self, task_id, deadline):
            return object()

        def start(self, instance, deadline):
            return "emulator-5570"

        def launch_game(self, instance, package, deadline):
            raise DriverError("private game output")

    code, events = invoke(
        monkeypatch, request(), (Manager, DriverError, object, lambda device: PNG)
    )

    assert code == 1
    assert events[-1] == {"type": "error", "code": "game_launch_failed"}
    assert "private" not in json.dumps(events)
