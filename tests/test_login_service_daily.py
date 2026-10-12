from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from naruto_daily.config import ConfigError, load_settings
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.execution import DailyDeadlineExceeded, current_execution
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
    # 此组测试保留旧模板配置的兼容性；模型扫码交接另有集成测试。
    payload['tasks']['login'][-1]['main_page_marker'] = dict(
        name='主界面奖励文字', template='assets/main_reward_label.png',
        alternate_templates=['assets/main_reward_label_red_dot.png'],
        left=1700, top=400, right=1920, bottom=700, threshold=.9,
    )
    if change:
        change(payload)
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    for name in _templates(payload):
        template = tmp_path / name
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_bytes(b"template fixture")
    return path


def session_for(tmp_path, *, change=None, matcher=None, subprocess_runner=None, task_name="all", user_options=None):
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
                           template_matcher=matcher or match, task_name=task_name,
                           user_options=user_options)
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


@pytest.mark.parametrize("enabled", [True, False])
def test_equipment_option_changes_only_equipment_route_and_keeps_other_daily_actions(tmp_path, enabled):
    # Multiple actions represent equipment menu navigation, material processing
    # and returning to the main screen. Disabling must remove the whole route.
    equipment_actions = [{"type": "tap", "x": 100 + index, "y": 0} for index in range(3)]
    options = {"equipmentStamina": enabled}
    session, clock, calls = session_for(
        tmp_path, change=lambda p: p["tasks"].update(equipment_upgrade=equipment_actions),
        user_options=options,
    )
    assert session.settings.tasks["equipment_upgrade"] == equipment_actions
    assert options == {"equipmentStamina": enabled}
    session.wait_for_login(clock() + 300)
    login_calls = len(calls)
    expected_actions = [
        action for name in TASK_ORDER if name != "login" and (enabled or name != "equipment_upgrade")
        for action in session.settings.tasks.get(name, [])
    ]
    assert session.run_daily(clock() + 300) == len(expected_actions)
    daily_taps = [command[-2:] for command, _ in calls[login_calls:] if "tap" in command]
    assert daily_taps == [[str(action["x"]), str(action["y"])] for action in expected_actions]
    assert session.settings.tasks["equipment_upgrade"] == equipment_actions


def test_disabled_equipment_route_does_not_require_its_templates(tmp_path):
    def change(payload):
        payload["tasks"]["equipment_upgrade"] = [{
            "type": "optional_template_tap", "template": "assets/equipment_only.png",
            "left": 0, "top": 0, "right": 100, "bottom": 100, "threshold": .9,
        }]

    path = config_file(tmp_path, change=change)
    (tmp_path / "assets/equipment_only.png").unlink()
    DailySession(path, Path("adb.exe"), "emulator-5580", 20,
                 user_options={"equipmentStamina": False})
    with pytest.raises(ConfigError, match="equipment_only"):
        DailySession(path, Path("adb.exe"), "emulator-5580", 20)


def test_local_secret_session_runs_only_shared_secret_route_after_scan(tmp_path, monkeypatch):
    from types import SimpleNamespace
    original = json.loads((PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8"))

    def change(payload):
        for name in ("secret_realm_farm", "secret_realm"):
            payload['tasks'][name] = deepcopy(original['tasks'][name])

    monkeypatch.delenv("LOGIN_ACCOUNT_NAME")
    session, clock, calls = session_for(tmp_path, change=change, task_name="secret_realm_farm")
    session.wait_for_login(clock() + 300)
    actions_run = []
    def run(actions):
        actions_run.extend(actions)
        return SimpleNamespace(action_count=len(actions))
    monkeypatch.setattr(session, "_runner", lambda: SimpleNamespace(run=run))
    assert session.run_daily(clock() + 300) == 3
    assert actions_run == [*session.settings.tasks['secret_realm_farm'],
                           *session.settings.tasks['secret_realm'][1:]]
    assert [a['type'] for a in actions_run] == ['enter_secret_realm', 'handle_secret_realm', 'return_to_main']
    assert all(command[:3] == ['current-adb.exe', '-s', 'emulator-5580'] for command, _ in calls)


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


def test_authorized_screen_skips_refresh_callback_and_refresh_can_handoff_authorization(tmp_path):
    session, clock, _ = session_for(tmp_path)
    session.wait_for_authorization(clock() + 30, on_wait=lambda _: pytest.fail('already authorized'))
    authorized = [False]
    def matcher(png, path, roi, threshold):
        return ((900, 900), 1) if authorized[0] and path.name == 'start_game_button.png' else (None, 0)
    session, clock, _ = session_for(tmp_path, matcher=matcher)
    def refresh(check):
        authorized[0] = True
        assert check()
    session.wait_for_authorization(clock() + 30, on_wait=refresh)
    assert session._authorized


@pytest.mark.parametrize('with_refresh', [False, True])
def test_portrait_qr_waits_for_landscape_authorization_and_keeps_refresh_responsive(tmp_path, monkeypatch, with_refresh):
    matches = []

    def match(*args):
        matches.append(args[1].name)
        return find_template_in_png(*args)

    session, clock, calls = session_for(tmp_path, matcher=match)
    target = session._login_actions[0]['start_game']
    template_path = PROJECT_ROOT / target['template']
    (tmp_path / target['template']).write_bytes(template_path.read_bytes())
    portrait = np.full((1920, 1080, 3), 255, np.uint8)
    instruction = cv2.imdecode(np.frombuffer(
        (PROJECT_ROOT / 'assets/login_wechat_qr_instruction.png').read_bytes(), np.uint8,
    ), cv2.IMREAD_COLOR)
    height, width = instruction.shape[:2]
    portrait[1210:1210 + height, 324:324 + width] = instruction
    landscape = np.zeros((1080, 1920, 3), np.uint8)
    template = cv2.imdecode(np.frombuffer(template_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    height, width = template.shape[:2]
    left, top = target['left'] + 12, target['top'] + 12
    landscape[top:top + height, left:left + width] = template
    frames = iter([portrait, np.full_like(portrait, 255), landscape])
    monkeypatch.setattr(session.client, 'screenshot', lambda: cv2.imencode('.png', next(frames))[1].tobytes())
    refreshes = []

    def refresh(check):
        refreshes.append(clock())
        assert not session._authorized
        assert matches == []
        # 刷新回调再次观察竖屏时也必须继续等待，不能进入横屏模板或模型识别。
        assert not check()
        assert matches == []

    session.wait_for_authorization(clock() + 30, on_wait=refresh if with_refresh else None)
    assert session._authorized
    assert not session._already_in_game
    assert matches == ['start_game_button.png']
    assert len(refreshes) == (1 if with_refresh else 0)
    assert calls == []


@pytest.mark.parametrize('with_refresh', [False, True])
def test_portrait_loading_or_expired_qr_preserves_original_login_deadline(tmp_path, monkeypatch, with_refresh):
    session, clock, calls = session_for(tmp_path, matcher=lambda *args: pytest.fail('portrait must not match landscape targets'))
    png = cv2.imencode('.png', np.full((1920, 1080, 3), 255, np.uint8))[1].tobytes()
    monkeypatch.setattr(session.client, 'screenshot', lambda: png)
    refreshes = []

    def refresh(check):
        refreshes.append(clock())
        assert not check()

    deadline = clock() + 1
    with pytest.raises(DailyDeadlineExceeded):
        session.wait_for_authorization(deadline, on_wait=refresh if with_refresh else None)
    assert clock() == deadline
    assert not session._authorized
    assert bool(refreshes) == with_refresh
    assert calls == []


def test_login_cleanup_cannot_start_before_authorization(tmp_path):
    session, clock, calls = session_for(tmp_path)
    with pytest.raises(RuntimeError, match="先完成扫码授权"):
        session.finish_login(clock() + 300)
    assert calls == []


@pytest.mark.parametrize('failed', [False, True])
def test_login_cleanup_logs_extra_main_check_and_preserves_its_failure(tmp_path, monkeypatch, caplog, failed):
    session, clock, calls = session_for(tmp_path)
    session._authorized = True
    failure = RuntimeError('main check failed')
    observations = []
    deadline = clock() + 30

    def run(actions):
        assert current_execution().deadline == deadline
        assert '开始扫码登录收尾' in caplog.text
        assert '登录收尾动作已完成，额外复核主界面' not in caplog.text
        assert actions[0]['type'] == 'finish_login_authorization'
        assert actions[0]['timeout_seconds'] == 30
        clock.sleep(4)

    def match(png, path, roi, threshold):
        assert current_execution().deadline == deadline
        assert current_execution().remaining() == 26
        assert '登录收尾动作已完成，额外复核主界面' in caplog.text
        observations.append(path.name)
        if failed:
            raise failure
        return (1800, 500), 1.0

    monkeypatch.setattr(session, '_runner', lambda: SimpleNamespace(run=run))
    session._template_matcher = match
    with caplog.at_level('INFO', logger='naruto_daily'):
        if failed:
            with pytest.raises(RuntimeError) as caught:
                session.finish_login(clock() + 30)
            assert caught.value is failure
        else:
            session.finish_login(clock() + 30)
    assert observations == ['main_reward_label.png']
    assert session._logged_in is not failed
    assert '外围剩余时间 30.00 秒' in caplog.text
    assert '外围剩余时间 26.00 秒' in caplog.text
    assert len(calls) == 1 and 'screencap' in calls[0][0]
    assert current_execution() is None


def test_login_finish_and_daily_bind_the_same_deadline_without_renewal(tmp_path, monkeypatch):
    session, clock, _ = session_for(tmp_path)
    session.wait_for_authorization(clock() + 10)
    deadline = clock() + 1500
    seen = []

    def run(actions):
        budget = current_execution()
        seen.append((budget.deadline, budget.remaining()))
        clock.sleep(20)
        return SimpleNamespace(action_count=len(actions))

    monkeypatch.setattr(session, "_runner", lambda: SimpleNamespace(run=run))
    session.finish_login(deadline)
    session.run_daily(deadline)
    assert seen == [(deadline, 1500), (deadline, 1480)]
    assert current_execution() is None


def test_expired_daily_scope_never_runs_more_actions(tmp_path, monkeypatch):
    session, clock, _ = session_for(tmp_path)
    session._logged_in = True
    reached = []
    monkeypatch.setattr(session, "_runner", lambda: reached.append(True))
    with pytest.raises(DailyDeadlineExceeded):
        session.run_daily(clock())
    assert reached == []
    assert current_execution() is None


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
    with pytest.raises(DailyDeadlineExceeded):
        session.wait_for_login(400)
    assert clock() == 400
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
