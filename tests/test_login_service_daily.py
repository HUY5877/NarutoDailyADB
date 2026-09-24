from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from naruto_daily.config import ConfigError, load_settings
from naruto_daily.common.visual import VisualError
from naruto_daily.login_service.daily import DailySession, _templates
from naruto_daily.tasks import TASK_ORDER


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def config_file(tmp_path, *, change=None):
    original = json.loads((PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8"))
    payload = {
        "adb_path": "old-adb.exe", "serial": "old-device", "screen": original["screen"],
        "tasks": {name: [{"type": "tap", "x": index, "y": 0}]
                  for index, name in enumerate(TASK_ORDER) if name != "login"},
    }
    payload["tasks"]["login"] = deepcopy(original["tasks"]["login"])
    if change:
        change(payload)
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    for name in _templates(payload):
        template = tmp_path / name
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_bytes(b"template fixture")
    return path


def session_for(tmp_path, *, change=None, matcher=None, subprocess_runner=None):
    clock = Clock()
    calls = []

    def run(command, **kwargs):
        calls.append((list(command), kwargs))
        output = "" if kwargs.get("text") else b"screenshot fixture"
        error = "" if kwargs.get("text") else b""
        return subprocess.CompletedProcess(command, 0, output, error)

    def match(png, path, roi, threshold):
        if path.name == "start_game_button.png":
            return (900, 900), 1.0
        if path.name in {"main_reward_label.png", "main_reward_label_red_dot.png"}:
            return (1800, 500), 1.0
        return None, 0.0

    session = DailySession(config_file(tmp_path, change=change), Path("current-adb.exe"),
                           "emulator-5580", 20, clock=clock, sleep=clock.sleep,
                           run_subprocess=subprocess_runner or run,
                           template_matcher=matcher or match)
    return session, clock, calls


def test_scan_settings_drop_saved_account_before_environment_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("LOGIN_ACCOUNT_NAME")
    path = config_file(tmp_path)
    settings = load_settings(path, skip_account_login=True)
    assert [action["type"] for action in settings.tasks["login"]] == [
        "finish_login_authorization", "wait", "popup_cleanup", "return_to_main",
    ]
    with pytest.raises(ConfigError, match="LOGIN_ACCOUNT_NAME"):
        load_settings(path)


@pytest.mark.parametrize("login", [[], [{"type": "wait", "seconds": 1}], None])
def test_scan_settings_require_explicit_authorization_handoff(tmp_path, login):
    path = config_file(tmp_path, change=lambda p: p["tasks"].update(login=login))
    with pytest.raises(ConfigError, match="扫码登录"):
        load_settings(path, skip_account_login=True)


def test_daily_session_checks_only_retained_templates(tmp_path):
    path = config_file(tmp_path)
    (tmp_path / "assets/qq_account_picker_marker.png").unlink()
    DailySession(path, Path("adb.exe"), "emulator-5580", 20)
    (tmp_path / "assets/start_game_button.png").unlink()
    with pytest.raises(ConfigError, match="start_game_button"):
        DailySession(path, Path("adb.exe"), "emulator-5580", 20)


def test_daily_session_requires_main_screen_check(tmp_path):
    path = config_file(tmp_path, change=lambda p: p["tasks"]["login"].pop())
    with pytest.raises(ConfigError, match="return_to_main"):
        DailySession(path, Path("adb.exe"), "emulator-5580", 20)


def test_scan_and_daily_reuse_one_serial_and_complete_existing_all_order(tmp_path, capsys):
    session, clock, calls = session_for(tmp_path)
    assert calls == []
    session.wait_for_login(clock() + 300)
    login_calls = len(calls)
    assert session.run_daily(clock() + 300) == len(TASK_ORDER) - 1
    assert all(command[:3] == ["current-adb.exe", "-s", "emulator-5580"]
               for command, _ in calls)
    assert all("devices" not in command for command, _ in calls)
    login_taps = [command[-2:] for command, _ in calls[:login_calls] if "tap" in command]
    assert login_taps == [["900", "900"]]
    daily_taps = [command[-2:] for command, _ in calls[login_calls:] if "tap" in command]
    assert daily_taps == [[str(index), "0"] for index in range(1, len(TASK_ORDER))]
    assert capsys.readouterr().out == ""


def test_daily_does_not_run_before_confirmed_login(tmp_path):
    session, clock, calls = session_for(tmp_path)
    with pytest.raises(RuntimeError, match="先完成扫码登录"):
        session.run_daily(clock() + 300)
    assert calls == []


def test_authorized_login_cleanup_can_outlive_qr_deadline(tmp_path):
    session, clock, calls = session_for(tmp_path)
    qr_deadline = clock() + 10
    session.wait_for_authorization(qr_deadline)
    assert clock() < qr_deadline
    with pytest.raises(RuntimeError, match="先完成扫码登录"):
        session.run_daily(clock() + 300)

    script_deadline = clock() + 300
    session.finish_login(script_deadline)
    assert clock() > qr_deadline
    assert session.run_daily(script_deadline) == len(TASK_ORDER) - 1
    assert all(command[:3] == ["current-adb.exe", "-s", "emulator-5580"]
               for command, _ in calls)


def test_login_cleanup_cannot_start_before_authorization(tmp_path):
    session, clock, calls = session_for(tmp_path)
    with pytest.raises(RuntimeError, match="先完成扫码授权"):
        session.finish_login(clock() + 300)
    assert calls == []


@pytest.mark.parametrize("marker", [
    "start_game_button.png", "qq_agree_game_info.png", "main_reward_label.png",
])
def test_authorization_returns_on_first_confirmed_page_without_clicks_or_fixed_waits(tmp_path, marker):
    def match(png, path, roi, threshold):
        return ((900, 900), 1.0) if path.name == marker else (None, 0.0)

    session, clock, calls = session_for(tmp_path, matcher=match)
    before = clock()
    session.wait_for_authorization(before + 1)
    assert clock() == before
    assert len(calls) == 1  # A single observation, before any button or 3-second wait.
    assert "screencap" in calls[0][0]
    assert not any("tap" in command for command, _ in calls)
    with pytest.raises(RuntimeError, match="先完成扫码登录"):
        session.run_daily(clock() + 300)


def test_disappeared_qr_or_unknown_loading_page_does_not_confirm_authorization(tmp_path):
    session, clock, calls = session_for(tmp_path, matcher=lambda *args: (None, 0.0))
    deadline = clock() + 1
    with pytest.raises(TimeoutError):
        session.wait_for_authorization(deadline)
    assert clock() == deadline
    assert len(calls) == 2  # Half-second polling while awaiting a positive marker.
    with pytest.raises(RuntimeError, match="先完成扫码授权"):
        session.finish_login(clock() + 300)
    assert not any("tap" in command for command, _ in calls)


def test_already_entered_game_does_not_wait_for_start_button_again(tmp_path):
    def match(png, path, roi, threshold):
        return ((1800, 500), 1.0) if path.name == "main_reward_label.png" else (None, 0.0)

    session, clock, calls = session_for(tmp_path, matcher=match)
    session.wait_for_authorization(clock() + 1)
    session.finish_login(clock() + 300)
    assert not any("tap" in command for command, _ in calls)
    assert session.run_daily(clock() + 300) == len(TASK_ORDER) - 1


def test_login_wait_extends_past_old_40_second_authorization_timeout(tmp_path):
    state = {}

    def match(png, path, roi, threshold):
        now = state["clock"]()
        if path.name == "start_game_button.png" and now >= 160:
            return (900, 900), 1.0
        if path.name == "main_reward_label.png" and now >= 160:
            return (1800, 500), 1.0
        return None, 0.0

    session, clock, calls = session_for(tmp_path, matcher=match)
    state["clock"] = clock
    session.wait_for_login(400)
    assert clock() > 160
    assert session.run_daily(500) == len(TASK_ORDER) - 1


def test_login_rejects_missing_main_screen_and_cannot_run_daily(tmp_path):
    def match(png, path, roi, threshold):
        return ((900, 900), 1.0) if path.name == "start_game_button.png" else (None, 0.0)

    session, clock, calls = session_for(tmp_path, matcher=match)
    with pytest.raises(VisualError, match="主界面"):
        session.wait_for_login(400)
    with pytest.raises(RuntimeError, match="先完成扫码登录"):
        session.run_daily(500)


def test_daily_sleep_obeys_whole_task_deadline(tmp_path):
    def change(payload):
        payload["tasks"]["ichiraku_takeout"] = [{"type": "wait", "seconds": 20}]

    session, clock, calls = session_for(tmp_path, change=change)
    session.wait_for_login(400)
    previous_calls = len(calls)
    deadline = clock() + 5
    with pytest.raises(TimeoutError, match="截止时间"):
        session.run_daily(deadline)
    assert clock() == deadline
    assert len(calls) == previous_calls


def test_adb_timeout_is_capped_by_command_limit_and_remaining_deadline(tmp_path):
    session, clock, calls = session_for(tmp_path)
    session.wait_for_login(400)
    assert calls[0][1]["timeout"] == 20
    session.run_daily(clock() + 3)
    assert calls[-1][1]["timeout"] == 3
    assert all(call[1]["capture_output"] for call in calls)


def test_adb_cannot_continue_past_whole_task_deadline(tmp_path):
    session, clock, calls = session_for(tmp_path)
    session.wait_for_login(400)
    reached = []

    def slow_command(command, **kwargs):
        reached.append(command)
        clock.sleep(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    session._run_subprocess = slow_command
    with pytest.raises(subprocess.TimeoutExpired):
        session.run_daily(clock() + 2)
    assert len(reached) == 1


def test_expired_deadline_sends_no_commands(tmp_path):
    session, clock, calls = session_for(tmp_path)
    with pytest.raises(TimeoutError):
        session.wait_for_login(clock())
    assert calls == []


@pytest.mark.parametrize("serial,timeout", [(None, 20), ("", 20), ("device", 0), ("device", float("inf"))])
def test_session_rejects_unbound_device_and_invalid_timeout(tmp_path, serial, timeout):
    with pytest.raises(ValueError):
        DailySession(tmp_path / "unused.json", Path("adb.exe"), serial, timeout)
