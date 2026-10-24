from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import threading
import time

import pytest

from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerManager
from test_login_service_ldplayer import FakeLDPlayer, command_result


TASK = "11111111-1111-4111-8111-111111111111"
OTHER_TASK = "22222222-2222-4222-8222-222222222222"
CAPTURE = "com.huy.authcapture"
TIM = "com.tencent.tim"


@pytest.fixture
def installed_apps(tmp_path, monkeypatch):
    console = tmp_path / "ldconsole.exe"
    runner = FakeLDPlayer(console)
    runner.instances["other-slot"] = [8, 0]
    state = {
        "packages": {"android", "com.tencent.tim.fake"},
        "probe_output": None,
        "probe_error": None,
        "install_output": b"Performing Streamed Install\nSuccess\n",
        "install_error": None,
        "install_returncode": 0,
        "installed_package": CAPTURE,
        "live_serial": "emulator-5570",
        "install_hook": None,
        "installs": [],
        "probes": [],
    }

    def run(args, **kwargs):
        command = tuple(map(str, args))
        if command[-4:] == ("shell", "pm", "list", "packages"):
            state["probes"].append(command)
            if state["probe_error"]:
                raise state["probe_error"]
            output = state["probe_output"]
            if output is None:
                output = "\n".join("package:" + value for value in sorted(state["packages"])).encode()
            return command_result(kwargs, output)
        if command[:2] == (str(console), "adb"):
            return command_result(kwargs, state["live_serial"].encode() + b"\n")
        if len(command) > 3 and command[3] == "install":
            state["installs"].append((command, kwargs["timeout"]))
            if state["install_hook"]:
                state["install_hook"]()
            if state["install_error"]:
                raise state["install_error"]
            if state["installed_package"]:
                state["packages"].add(state["installed_package"])
            return command_result(
                kwargs, state["install_output"], b"private-install-detail", state["install_returncode"],
            )
        return runner(args, **kwargs)

    monkeypatch.setattr("naruto_daily.login_service.ldplayer.subprocess.run", run)
    manager = LDPlayerManager(console, "", tmp_path / "state", pool_indices=(7, 8))
    instance = manager.allocate(TASK, time.time() + 20)
    manager.start(instance, time.time() + 20)
    apk = tmp_path / "download.apk"
    apk.write_bytes(b"verified APK contents")
    return manager, instance, apk, state, runner


@pytest.mark.parametrize("present,expected", [
    (set(), {CAPTURE, TIM}),
    ({CAPTURE}, {TIM}),
    ({TIM}, {CAPTURE}),
    ({CAPTURE, TIM}, set()),
])
def test_missing_capture_apps_returns_exact_absent_packages(installed_apps, present, expected):
    manager, instance, _, state, _ = installed_apps
    state["packages"].update(present)
    missing = manager.missing_capture_apps(instance, time.time() + 20)
    assert type(missing) is set
    assert missing == expected
    assert state["probes"] == [
        (str(manager.adb_path), "-s", "emulator-5570", "shell", "pm", "list", "packages"),
    ]


@pytest.mark.parametrize("output", [
    b"", b"Error: private-detail", b"package:", b"package:bad package",
    b"package:com.huy.authcapture\nError: private-detail", b"\xff",
])
def test_missing_capture_apps_rejects_unreliable_package_lists(installed_apps, output):
    manager, instance, _, state, _ = installed_apps
    state["probe_output"] = output
    with pytest.raises(LDPlayerError) as error:
        manager.missing_capture_apps(instance, time.time() + 20)
    assert "private-detail" not in str(error.value)


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("adb private-detail", 20), OSError("private-detail"),
])
def test_missing_capture_apps_does_not_guess_after_probe_failure(installed_apps, error):
    manager, instance, _, state, _ = installed_apps
    state["probe_error"] = error
    with pytest.raises(LDPlayerError) as caught:
        manager.missing_capture_apps(instance, time.time() + 20)
    assert "private-detail" not in str(caught.value)


@pytest.mark.parametrize("method", ["missing_capture_apps", "install_missing_app"])
@pytest.mark.parametrize("invalid", ["owner", "bound_serial", "live_serial", "unknown_serial", "stopped", "renamed"])
def test_strict_apk_operations_reject_changed_ownership_before_package_access(installed_apps, method, invalid):
    manager, instance, apk, state, runner = installed_apps
    if invalid == "owner":
        instance.task_id = OTHER_TASK
    elif invalid == "bound_serial":
        instance.serial = "emulator-9999"
    elif invalid == "live_serial":
        state["live_serial"] = "emulator-9999"
    elif invalid == "unknown_serial":
        state["live_serial"] = "unknown"
    elif invalid == "stopped":
        runner.instances[instance.name][1] = 0
    else:
        runner.instances["renamed"] = runner.instances.pop(instance.name)
    arguments = (instance, time.time() + 20) if method == "missing_capture_apps" else (instance, apk, CAPTURE, time.time() + 20)
    with pytest.raises(LDPlayerError) as error:
        getattr(manager, method)(*arguments)
    assert type(error.value).__name__ == "LDPlayerOwnershipError"
    assert not state["probes"] and not state["installs"]


@pytest.mark.parametrize("package", [CAPTURE, TIM])
def test_install_missing_app_uses_non_replacing_adb_install_and_verifies_package(installed_apps, package):
    manager, instance, apk, state, _ = installed_apps
    state["installed_package"] = package
    manager.install_missing_app(instance, apk, package, time.time() + 120)
    assert state["installs"][0][0] == (str(manager.adb_path), "-s", "emulator-5570", "install", str(apk))
    assert 50 < state["installs"][0][1] <= 60
    assert package in state["packages"]
    assert len(state["probes"]) == 2
    assert manager.command_timeout == 20


def test_install_missing_app_respects_earlier_task_deadline(installed_apps):
    manager, instance, apk, state, _ = installed_apps
    manager.install_missing_app(instance, apk, CAPTURE, time.time() + 5)
    assert 0 < state["installs"][0][1] <= 5


def test_install_missing_app_skips_existing_package(installed_apps):
    manager, instance, apk, state, _ = installed_apps
    state["packages"].add(CAPTURE)
    manager.install_missing_app(instance, apk, CAPTURE, time.time() + 20)
    assert not state["installs"]


def test_install_missing_app_rechecks_after_another_installer_wins(installed_apps):
    manager, instance, apk, state, _ = installed_apps
    assert CAPTURE in manager.missing_capture_apps(instance, time.time() + 20)
    state["packages"].add(CAPTURE)
    manager.install_missing_app(instance, apk, CAPTURE, time.time() + 20)
    assert not state["installs"]


@pytest.mark.parametrize("invalid", ["package", "missing", "directory", "extension"])
def test_install_missing_app_rejects_unsupported_or_non_apk_input(installed_apps, invalid):
    manager, instance, apk, state, _ = installed_apps
    package = CAPTURE
    if invalid == "package":
        package = "com.tencent.mobileqq"
    elif invalid == "missing":
        apk.unlink()
    elif invalid == "directory":
        apk.unlink()
        apk.mkdir()
    else:
        apk = apk.rename(apk.with_suffix(".txt"))
    with pytest.raises(LDPlayerError):
        manager.install_missing_app(instance, apk, package, time.time() + 20)
    assert not state["installs"]


@pytest.mark.parametrize("failure", ["failure_text", "empty", "returncode", "missing_after_success", "timeout", "os_error"])
def test_install_missing_app_reports_safe_failure_and_requires_real_package(installed_apps, failure):
    manager, instance, apk, state, _ = installed_apps
    state["installed_package"] = None
    if failure == "failure_text":
        state["install_output"] = b"Failure [private-install-detail]"
    elif failure == "empty":
        state["install_output"] = b""
    elif failure == "returncode":
        state["install_returncode"] = 1
    elif failure == "timeout":
        state["install_error"] = subprocess.TimeoutExpired("private-install-detail", 60)
    elif failure == "os_error":
        state["install_error"] = OSError("private-install-detail")
    with pytest.raises(LDPlayerError) as error:
        manager.install_missing_app(instance, apk, CAPTURE, time.time() + 120)
    assert "private-install-detail" not in str(error.value)
    assert CAPTURE not in state["packages"]


@pytest.mark.parametrize("change", ["owner", "serial", "index", "failure_and_owner"])
def test_install_missing_app_rejects_instance_changes_during_install(installed_apps, change):
    manager, instance, apk, state, runner = installed_apps

    def change_instance():
        if change in {"owner", "failure_and_owner"}:
            path = manager.state_dir / "bindings.json"
            document = json.loads(path.read_text("utf-8"))
            document["bindings"]["7"]["taskId"] = OTHER_TASK
            path.write_text(json.dumps(document), "utf-8")
        elif change == "serial":
            state["live_serial"] = "emulator-9999"
        else:
            runner.instances[instance.name][0] = 9

    state["install_hook"] = change_instance
    if change == "failure_and_owner":
        state["install_error"] = OSError("private-install-detail")
    with pytest.raises(LDPlayerError) as error:
        manager.install_missing_app(instance, apk, CAPTURE, time.time() + 120)
    assert type(error.value).__name__ == "LDPlayerOwnershipError"


def test_install_missing_app_does_not_hold_global_lock_during_install(installed_apps):
    manager, instance, apk, state, _ = installed_apps
    installing = threading.Event()
    finish_install = threading.Event()

    def delay_install():
        installing.set()
        assert finish_install.wait(5), "capacity probe was blocked by APK installation"

    state["install_hook"] = delay_install
    observer = LDPlayerManager(manager.console_path, "", manager.state_dir, command_timeout=0.5, pool_indices=(7, 8))
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(manager.install_missing_app, instance, apk, CAPTURE, time.time() + 10)
        try:
            assert installing.wait(2)
            assert observer.has_capacity()
        finally:
            finish_install.set()
        operation.result(timeout=2)


@pytest.mark.parametrize("method", ["missing_capture_apps", "install_missing_app"])
def test_strict_apk_operations_reject_expired_tasks(installed_apps, method):
    manager, instance, apk, state, _ = installed_apps
    arguments = (instance, time.time() - 1) if method == "missing_capture_apps" else (instance, apk, CAPTURE, time.time() - 1)
    with pytest.raises(LDPlayerError, match="deadline"):
        getattr(manager, method)(*arguments)
    assert not state["installs"] and not state["probes"]


@pytest.mark.parametrize("outcome", ["success", "timeout"])
def test_install_missing_app_never_returns_after_task_deadline(installed_apps, monkeypatch, outcome):
    manager, instance, apk, state, _ = installed_apps
    now = [time.time()]
    deadline = now[0] + 10
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.time.time", lambda: now[0])

    def reach_deadline():
        now[0] = deadline

    state["install_hook"] = reach_deadline
    if outcome == "timeout":
        state["install_error"] = subprocess.TimeoutExpired("adb", 10)
    with pytest.raises(LDPlayerError, match="deadline"):
        manager.install_missing_app(instance, apk, CAPTURE, deadline)


def test_install_missing_app_post_install_probe_failure_remains_recoverable(installed_apps):
    manager, instance, apk, state, _ = installed_apps

    def invalidate_probe():
        state["probe_output"] = b"Error: private-install-detail"

    state["install_hook"] = invalidate_probe
    with pytest.raises(LDPlayerError) as error:
        manager.install_missing_app(instance, apk, CAPTURE, time.time() + 20)
    assert type(error.value) is LDPlayerError
    assert "private-install-detail" not in str(error.value)


@pytest.mark.parametrize("probe_output", [None, b"Error: private-probe-detail"])
def test_verify_capture_instance_accepts_owned_running_instance_without_package_probe(installed_apps, probe_output):
    manager, instance, _, state, _ = installed_apps
    state["probe_output"] = probe_output
    assert manager.verify_capture_instance(instance, time.time() + 20) is None
    assert not state["probes"] and not state["installs"]


@pytest.mark.parametrize("invalid", ["owner", "live_serial", "stopped"])
def test_verify_capture_instance_hard_fails_if_fallback_target_changed(installed_apps, invalid):
    manager, instance, _, state, runner = installed_apps
    if invalid == "owner":
        instance.task_id = OTHER_TASK
    elif invalid == "live_serial":
        state["live_serial"] = "emulator-9999"
    else:
        runner.instances[instance.name][1] = 0
    with pytest.raises(LDPlayerError) as error:
        manager.verify_capture_instance(instance, time.time() + 20)
    assert type(error.value).__name__ == "LDPlayerOwnershipError"
    assert not state["probes"] and not state["installs"]
