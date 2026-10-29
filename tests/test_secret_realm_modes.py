"""跳过完整秘境线路时不触碰入口或房间，并保留其余日常。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from naruto_daily.config import ConfigError
from naruto_daily.login_service.daily import DailySession
from naruto_daily.login_service.log_events import forward_task_logs
from naruto_daily.runner import ActionRunner
from naruto_daily.tasks import TASK_ORDER
from test_login_service_daily import config_file, daily_action, session_for


def test_direct_runner_skip_never_enters_or_handles_secret_and_continues_later_actions(monkeypatch):
    from naruto_daily import runner

    class Device:
        def __init__(self):
            self.taps = []

        def tap(self, x, y):
            self.taps.append((x, y))

        def screenshot(self):
            pytest.fail('跳过秘境时不得观察秘境房间')

    def forbidden(*args, **kwargs):
        pytest.fail('跳过秘境时不得调用入口或处理器')

    monkeypatch.setattr(runner, 'enter_secret_realm', forbidden)
    monkeypatch.setattr(runner, 'handle_secret_realm', forbidden)
    device, events, waits = Device(), [], []
    with forward_task_logs(events.append):
        summary = ActionRunner(device, sleep=waits.append, user_options={'secretRealmMode': 'skip'}).run([
            {'type': 'wait', 'seconds': 1}, {'type': 'enter_secret_realm'},
            {'type': 'handle_secret_realm'}, {'type': 'wait', 'seconds': 2},
        ])
    assert waits == [1.0, 2.0]
    assert device.taps == []
    assert summary.action_count == 4
    assert not any('secretRealmCompletedAt' in event for event in events)


@pytest.mark.parametrize('equipment', [True, False])
def test_skip_session_removes_every_secret_action_without_changing_other_groups(tmp_path, equipment):
    secret_actions = [daily_action(900 + index) for index in range(3)]
    options = {'equipmentStamina': equipment, 'secretRealmMode': 'skip'}
    session, clock, calls = session_for(
        tmp_path, user_options=options,
        change=lambda payload: payload['tasks'].update(secret_realm=deepcopy(secret_actions)),
    )
    expected = [action for name in TASK_ORDER if name not in {'login', 'secret_realm'}
                and (equipment or name != 'equipment_upgrade')
                for action in session.settings.tasks.get(name, [])]
    assert session._daily_actions == expected
    assert session.settings.tasks['secret_realm'] == secret_actions
    assert options == session.user_options
    session.wait_for_login(clock() + 300)
    login_calls = len(calls)
    events = []
    with forward_task_logs(events.append):
        assert session.run_daily(clock() + 300) == len(expected)
    taps = [command[-2:] for command, _ in calls[login_calls:] if 'tap' in command]
    assert taps == [[str(action['left']), str(action['top'])] for action in expected]
    assert not any('secretRealmCompletedAt' in event for event in events)


def test_skipped_secret_templates_are_not_required(tmp_path):
    def change(payload):
        payload['tasks']['secret_realm'] = [{
            'type': 'required_template_tap', 'name': '秘境入口', 'template': 'assets/secret_only.png',
            'left': 0, 'top': 0, 'right': 100, 'bottom': 100, 'threshold': .9,
            'timeout_seconds': 1, 'poll_seconds': .2,
        }]

    path = config_file(tmp_path, change=change)
    (tmp_path / 'assets/secret_only.png').unlink()
    skipped = DailySession(path, Path('adb.exe'), 'emulator-5580', 20,
                           user_options={'secretRealmMode': 'skip'})
    assert all(action.get('template') != 'assets/secret_only.png' for action in skipped._daily_actions)
    with pytest.raises(ConfigError, match='secret_only'):
        DailySession(path, Path('adb.exe'), 'emulator-5580', 20,
                     user_options={'secretRealmMode': 'all'})


def test_skip_independent_secret_route_keeps_login_only(tmp_path):
    example = json.loads((Path(__file__).parents[1] / 'config.example.json').read_text(encoding='utf-8'))

    def change(payload):
        for name in ('secret_realm', 'secret_realm_farm'):
            payload['tasks'][name] = deepcopy(example['tasks'][name])

    session, clock, calls = session_for(tmp_path, change=change, task_name='secret_realm_farm',
                                        user_options={'secretRealmMode': 'skip'})
    assert session._daily_actions == []
    session.wait_for_login(clock() + 300)
    login_calls = len(calls)
    assert session.run_daily(clock() + 300) == 0
    assert len(calls) == login_calls


@pytest.mark.parametrize('mode, once', [('once', True), ('all', False)])
def test_direct_runner_mode_is_passed_to_existing_secret_handler(monkeypatch, mode, once):
    from naruto_daily import runner

    captured = []
    monkeypatch.setattr(runner, 'handle_secret_realm', lambda *a, **kw: captured.append(kw))
    summary = ActionRunner(object(), user_options={'secretRealmMode': mode}).run([
        {'type': 'handle_secret_realm'}, {'type': 'wait', 'seconds': 99},
    ])
    assert summary.action_count == 1
    assert len(captured) == 1
    assert captured[0]['secret_realm_once'] is once
