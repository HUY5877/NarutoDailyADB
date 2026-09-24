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


def daily_dependencies(calls):
    class DriverError(RuntimeError):
        pass

    class Manager:
        adb_path = Path("C:/LDPlayer/adb.exe")

        def __init__(self, **kwargs):
            pass

        def allocate(self, task_id, deadline):
            calls.append("allocate")
            return "bound-instance"

        def start(self, instance, deadline):
            assert instance == "bound-instance"
            calls.append("start")
            return "emulator-5570"

        def launch_game(self, instance, package, deadline):
            assert instance == "bound-instance"
            calls.append("launch")

    class Device:
        def __init__(self, adb_path, serial, deadline, command_timeout):
            assert serial == "emulator-5570"

    def fetch(device):
        calls.append("qr")
        return PNG

    return Manager, DriverError, Device, fetch


def test_daily_worker_publishes_qr_before_login_then_reuses_session_and_resets_deadline(
    monkeypatch, tmp_path,
):
    now = 1_800_000_000.0
    clock = [now]
    calls = []

    class ProtocolOutput(StringIO):
        def flush(self):
            self.flushed = self.getvalue()

    output = ProtocolOutput()
    config_path = tmp_path / "daily.json"

    class DailySession:
        def __init__(self, **kwargs):
            calls.append("configure")
            assert kwargs == {
                "config_path": config_path, "adb_path": Path("C:/LDPlayer/adb.exe"),
                "serial": "emulator-5570", "command_timeout": 23,
            }
            self.logged_in = False

        def wait_for_login(self, deadline):
            calls.append("login")
            assert deadline == now + 300
            assert json.loads(output.flushed.splitlines()[-1])["type"] == "qr"
            clock[0] = now + 299
            self.logged_in = True

        def run_daily(self, deadline):
            calls.append("daily")
            assert self.logged_in
            assert deadline == now + 359
            assert json.loads(output.flushed.splitlines()[-1]) == {"type": "logged_in"}
            clock[0] = now + 340  # Original login expiry must no longer stop daily work.
            return 12

    monkeypatch.setattr(ldplayer_worker, "_load_dependencies", lambda: daily_dependencies(calls))
    monkeypatch.setattr(ldplayer_worker, "_load_daily_session", lambda: DailySession)
    message = request(now, dailyConfigPath=str(config_path), dailyTimeoutSeconds=60)
    code = ldplayer_worker.main(
        input_stream=StringIO(json.dumps(message) + "\n"), output_stream=output,
        clock=lambda: clock[0], use_watchdog=False,
    )
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert code == 0
    assert calls == ["allocate", "start", "configure", "launch", "qr", "login", "daily"]
    assert [event["type"] for event in events] == [
        "stage", "stage", "stage", "qr", "logged_in", "daily_completed",
    ]
    assert events[-1] == {"type": "daily_completed", "actionCount": 12}


@pytest.mark.parametrize("phase,error,expected", [
    ("configure", RuntimeError("private config detail"), "configuration_error"),
    ("configure", TimeoutError("private config detail"), "configuration_error"),
    ("login", RuntimeError("private login detail"), "login_failed"),
    ("login", TimeoutError("private login detail"), "login_timeout"),
    ("daily", RuntimeError("private daily detail"), "daily_failed"),
    ("daily", TimeoutError("private daily detail"), "daily_timeout"),
])
def test_daily_worker_maps_failures_without_leaking_details(monkeypatch, tmp_path, phase, error, expected):
    calls = []

    class DailySession:
        def __init__(self, **kwargs):
            if phase == "configure":
                raise error

        def wait_for_login(self, deadline):
            if phase == "login":
                raise error

        def run_daily(self, deadline):
            raise error

    monkeypatch.setattr(ldplayer_worker, "_load_daily_session", lambda: DailySession)
    code, events = invoke(
        monkeypatch,
        request(dailyConfigPath=str(tmp_path / "daily.json"), dailyTimeoutSeconds=600),
        daily_dependencies(calls),
    )
    assert code == 1
    assert events[-1] == {"type": "error", "code": expected}
    assert "private" not in json.dumps(events)
    assert not any(event["type"] == "daily_completed" for event in events)
    if phase == "configure":
        assert calls == ["allocate", "start"]
    elif phase == "login":
        assert not any(event["type"] == "logged_in" for event in events)


@pytest.mark.parametrize("phase,expected", [("login", "login_timeout"), ("daily", "daily_timeout")])
def test_daily_worker_rejects_success_returned_after_deadline(monkeypatch, tmp_path, phase, expected):
    now = 1_800_000_000.0
    clock = [now]

    class DailySession:
        def __init__(self, **kwargs):
            pass

        def wait_for_login(self, deadline):
            if phase == "login":
                clock[0] = deadline

        def run_daily(self, deadline):
            clock[0] = deadline
            return 1

    monkeypatch.setattr(ldplayer_worker, "_load_dependencies", lambda: daily_dependencies([]))
    monkeypatch.setattr(ldplayer_worker, "_load_daily_session", lambda: DailySession)
    output = StringIO()
    code = ldplayer_worker.main(
        input_stream=StringIO(json.dumps(request(
            now, dailyConfigPath=str(tmp_path / "daily.json"), dailyTimeoutSeconds=60,
        )) + "\n"), output_stream=output, clock=lambda: clock[0], use_watchdog=False,
    )
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert code == 1
    assert events[-1] == {"type": "error", "code": expected}


def test_daily_legacy_prints_cannot_corrupt_stdout_protocol(monkeypatch, tmp_path, capsys):
    class DailySession:
        def __init__(self, **kwargs):
            print("construct daily session")

        def wait_for_login(self, deadline):
            print("wait for login")

        def run_daily(self, deadline):
            print("run daily tasks")
            return 1

    def load_daily():
        print("import legacy tasks")
        return DailySession

    monkeypatch.setattr(ldplayer_worker, "_load_dependencies", lambda: daily_dependencies([]))
    monkeypatch.setattr(ldplayer_worker, "_load_daily_session", load_daily)
    code = ldplayer_worker.main(
        input_stream=StringIO(json.dumps(request(
            dailyConfigPath=str(tmp_path / "daily.json"), dailyTimeoutSeconds=600,
        )) + "\n"), clock=lambda: 1_800_000_000.0, use_watchdog=False,
    )
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert code == 0
    assert events[-1] == {"type": "daily_completed", "actionCount": 1}
    assert captured.err.splitlines() == [
        "import legacy tasks", "construct daily session", "wait for login", "run daily tasks",
    ]


@pytest.mark.parametrize("changes", [
    {"dailyConfigPath": "/absolute/daily.json"},
    {"dailyTimeoutSeconds": 600},
    {"dailyConfigPath": None, "dailyTimeoutSeconds": 600},
    {"dailyConfigPath": "", "dailyTimeoutSeconds": 600},
    {"dailyConfigPath": "  ", "dailyTimeoutSeconds": 600},
    {"dailyConfigPath": "relative/daily.json", "dailyTimeoutSeconds": 600},
    {"dailyConfigPath": [], "dailyTimeoutSeconds": 600},
])
def test_daily_options_must_be_supplied_together_with_an_absolute_path(monkeypatch, changes):
    code, events = invoke(monkeypatch, request(**changes), dependencies=None)
    assert code == 1
    assert events == [{"type": "error", "code": "configuration_error"}]


@pytest.mark.parametrize("timeout", [None, True, "60", 0, -1, 0.9, 86_401, float("nan"), float("inf")])
def test_daily_timeout_is_a_bounded_finite_number(monkeypatch, tmp_path, timeout):
    code, events = invoke(monkeypatch, request(
        dailyConfigPath=str(tmp_path / "daily.json"), dailyTimeoutSeconds=timeout,
    ), dependencies=None)
    assert code == 1
    assert events == [{"type": "error", "code": "configuration_error"}]


@pytest.mark.parametrize("timeout", [1, 1.5, 86_400])
def test_daily_timeout_accepts_bounds_and_fractional_seconds(monkeypatch, tmp_path, timeout):
    class DailySession:
        def __init__(self, **kwargs):
            pass

        def wait_for_login(self, deadline):
            pass

        def run_daily(self, deadline):
            assert deadline == 1_800_000_000.0 + timeout
            return 0

    monkeypatch.setattr(ldplayer_worker, "_load_daily_session", lambda: DailySession)
    code, events = invoke(monkeypatch, request(
        dailyConfigPath=str(tmp_path / "daily.json"), dailyTimeoutSeconds=timeout,
    ), daily_dependencies([]))
    assert code == 0
    assert events[-1] == {"type": "daily_completed", "actionCount": 0}


@pytest.mark.parametrize("action_count", [None, True, -1, 1.5, "1"])
def test_daily_completion_requires_a_nonnegative_integer_count(monkeypatch, tmp_path, action_count):
    class DailySession:
        def __init__(self, **kwargs):
            pass

        def wait_for_login(self, deadline):
            pass

        def run_daily(self, deadline):
            return action_count

    monkeypatch.setattr(ldplayer_worker, "_load_daily_session", lambda: DailySession)
    code, events = invoke(monkeypatch, request(
        dailyConfigPath=str(tmp_path / "daily.json"), dailyTimeoutSeconds=60,
    ), daily_dependencies([]))
    assert code == 1
    assert events[-1] == {"type": "error", "code": "daily_failed"}
