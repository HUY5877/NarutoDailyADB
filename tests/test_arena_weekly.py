"""周胜协议、独立动作链与从战斗开始计时的停止边界，不连接真实设备。"""

import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.execution import check_execution, current_execution, DailyDeadlineExceeded
from naruto_daily.config import ConfigError
from naruto_daily.runner import ActionRunner
from naruto_daily.login_service import daily, ldplayer_worker, runtime as runtime_module
from naruto_daily.login_service.models import parse_queue_task
from naruto_daily.login_service.user_options import parse_user_options
from test_login_service_daily import daily_action, session_for
from test_login_service_ldplayer_worker import daily_dependencies, request
from test_login_service_terminal_logs import make_runtime, PNG, SUCCESS_PROTOCOL, no_reader_tasks, eventually
from test_login_service_user_options import queue_body


@pytest.mark.parametrize('main_task', ['daily', 'arena_weekly'])
def test_client_main_task_is_preserved_through_queue_options(main_task):
    supplied = dict(mainTask=main_task, equipmentStamina=False, secretRealmMode='skip')
    assert parse_user_options(supplied) == supplied
    record = parse_queue_task(queue_body() | {'userOptions': supplied}, 'login')
    assert record.user_options == supplied
    assert record.user_options is not supplied


@pytest.mark.parametrize('main_task', [None, '', 'arena', 'DAILY', True, 1, [], {}])
def test_unknown_main_tasks_are_rejected(main_task):
    with pytest.raises(ValueError):
        parse_user_options({'mainTask': main_task})


def weekly_session(tmp_path):
    return session_for(tmp_path, user_options={'mainTask': 'arena_weekly'}, change=lambda data: data['tasks'].update(
        arena_weekly=[daily_action(123), {'type': 'wait', 'seconds': 45}]))


def test_weekly_runs_only_its_entry_then_repeats_duel_for_forty_minutes(tmp_path, monkeypatch):
    session, clock, commands = weekly_session(tmp_path)
    session._logged_in = True
    calls, starts = [], []
    def battle(client, battle, **kwargs):
        assert client is session.client
        assert battle['end_targets'] == [] and kwargs.get('model') is None
        assert current_execution().deadline == starts[0]
        assert kwargs['clock'] is clock
        calls.append(clock())
        clock.sleep(600)
        check_execution()
    monkeypatch.setattr(daily, 'run_duel_battle', battle, raising=False)
    before = clock()
    assert session.run_daily(before + 60, on_arena_started=starts.append) == 2
    assert starts == [before + 45 + 2400]
    assert calls == [before + 45 + offset for offset in (0, 600, 1200, 1800)]
    assert clock() == starts[0]
    assert [cmd[-2:] for cmd, _ in commands if 'tap' in cmd] == [['123', '0']]
    assert current_execution() is None


def test_weekly_battle_is_one_monitored_module_for_the_whole_forty_minutes(tmp_path, monkeypatch):
    from naruto_daily.common.module_reporting import module_reporting
    session, clock, _ = weekly_session(tmp_path)
    session._logged_in = True
    events = []

    def battle(*args, **kwargs):
        assert events[-1]['name'] == 'arena_weekly'
        assert events[-1]['state'] == 'started'
        clock.sleep(600)
        check_execution()

    monkeypatch.setattr(daily, 'run_duel_battle', battle)
    with module_reporting(events.append):
        session.run_daily(clock() + 60)
    weekly = [event for event in events if event['name'] == 'arena_weekly']
    assert [event['state'] for event in weekly] == ['started', 'finished']


@pytest.mark.parametrize('error', [AdbError('input failed'), RuntimeError('failed'), DailyDeadlineExceeded('early'),
                                  TimeoutError('early'), subprocess.TimeoutExpired('adb', 20)])
def test_weekly_does_not_hide_errors_before_its_deadline(tmp_path, monkeypatch, error):
    session, clock, _ = weekly_session(tmp_path)
    session._logged_in = True
    def battle(*args, **kwargs):
        raise error
    monkeypatch.setattr(daily, 'run_duel_battle', battle, raising=False)
    with pytest.raises(type(error)):
        session.run_daily(clock() + 60)


def test_adb_timeout_at_the_battle_deadline_is_a_normal_timed_finish(tmp_path, monkeypatch):
    session, clock, _ = weekly_session(tmp_path)
    session._logged_in = True
    def battle(*args, **kwargs):
        clock.sleep(2400)
        raise subprocess.TimeoutExpired('adb', 2400)
    monkeypatch.setattr(daily, 'run_duel_battle', battle)
    assert session.run_daily(clock() + 60) == 2


def test_weekly_requires_its_own_route_instead_of_running_daily(tmp_path):
    with pytest.raises(ConfigError, match='周胜|arena_weekly'):
        session_for(tmp_path, user_options={'mainTask': 'arena_weekly'})


def test_example_weekly_route_scans_main_scene_and_confirms_friendly_match():
    payload = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text(encoding='utf-8'))
    actions = payload['tasks']['arena_weekly']
    assert [a['type'] for a in actions] == ['prepare_arena_matchmaking', 'enter_arena']
    preparation, action = actions
    assert preparation['scroll_swipes'] == 10
    assert (preparation['checkbox_x_offset'], preparation['checkbox_y_offset']) == (337, 3)
    assert preparation['settings_button']['template'] == 'assets/main_settings.png'
    assert preparation['players_only_label']['template'] == 'assets/arena_players_only_label.png'
    assert preparation['return_to_main']['type'] == 'return_to_main'
    assert preparation['return_to_main']['close_target']['left'] == 1500
    login_return = next(a for a in payload['tasks']['login'] if a['type'] == 'return_to_main')
    assert login_return['close_target']['left'] == 1650
    assert action['max_swipes'] == 15
    assert action['battle_entry']['template'] == 'assets/arena_ninjutsu_battle.png'
    assert action['ready_marker']['template'] == 'assets/arena_friendly_match.png'


@pytest.mark.parametrize('action_index,handler_name', [(0, 'prepare_arena_matchmaking'), (1, 'enter_arena')])
def test_runner_dispatches_arena_steps_with_recognition_dependencies(monkeypatch, tmp_path, action_index, handler_name):
    import naruto_daily.runner as runner
    action = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text(encoding='utf-8'))['tasks']['arena_weekly'][action_index]
    client, matcher, calls = object(), object(), []
    def enter(actual_client, actual_action, **kwargs):
        calls.append((actual_client, actual_action, kwargs))
    monkeypatch.setattr(runner, handler_name, enter)
    result = ActionRunner(client, template_matcher=matcher, asset_root=tmp_path,
                          sleep=lambda _: None, user_options={'mainTask': 'arena_weekly'}).run([action])
    assert result.action_count == 1
    assert len(calls) == 1
    assert calls[0][0] is client and calls[0][1] == action
    assert calls[0][2]['template_matcher'] is matcher
    assert calls[0][2]['asset_root'] == tmp_path


def test_child_publishes_battle_deadline_and_completes_normally_at_that_deadline(monkeypatch, tmp_path):
    now = [1_800_000_000.0]
    class Session:
        def __init__(self, **options):
            assert options['user_options']['mainTask'] == 'arena_weekly'
        def wait_for_authorization(self, deadline):
            now[0] += 2
        def finish_login(self, deadline):
            now[0] += 3
        def run_daily(self, deadline, *, on_arena_started):
            now[0] += 70
            end = now[0] + 2400
            on_arena_started(end)
            now[0] = end
            return 2
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: Session)
    monkeypatch.setattr(ldplayer_worker, '_load_dependencies', lambda: daily_dependencies([]))
    message = request(now[0], dailyConfigPath=str(tmp_path / 'daily.json'), dailyTimeoutSeconds=3000,
                      userOptions={'mainTask': 'arena_weekly'})
    output = io.StringIO()
    code = ldplayer_worker.main(input_stream=io.StringIO(json.dumps(message) + '\n'), output_stream=output,
                                clock=lambda: now[0], use_watchdog=False)
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert code == 0
    assert [e['type'] for e in events if e['type'] != 'log'] == [
        'stage', 'stage', 'stage', 'qr', 'authorized', 'logged_in', 'arena_started', 'daily_completed']
    assert next(e for e in events if e['type'] == 'arena_started')['executionDeadline'] == now[0]
    assert events[-1] == {'type': 'daily_completed', 'actionCount': 2}


def test_parent_kills_a_stuck_weekly_child_at_battle_deadline_and_marks_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, 'ARENA_WEEKLY_SECONDS', 1, raising=False)
    program = '''
import json,sys,time
message=json.loads(sys.stdin.readline())
for event in message['events']:
    print(json.dumps(event),flush=True)
print(json.dumps({'type':'arena_started','executionDeadline':time.time()+1}),flush=True)
time.sleep(60)
'''
    async def scenario():
        runtime, reporter, record = make_runtime(tmp_path)
        record.user_options = parse_user_options({'mainTask': 'arena_weekly'})
        work = asyncio.create_task(runtime._run_child(record, [sys.executable, '-u', '-c', program],
            {'events': SUCCESS_PROTOCOL[:3]}, daily=True))
        try:
            await eventually(lambda: record.execution_deadline is not None and record.execution_deadline < time.time() + 2)
            snapshot = json.loads((tmp_path / 'state/runs' / f'{record.id}.json').read_text(encoding='utf-8'))
            assert snapshot['status'] == 'running_daily'
            assert snapshot['deadline'] == record.execution_deadline
            result = await asyncio.wait_for(work, 5)
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        assert result == PNG and record.daily_status == 'completed'
        assert record.daily_error_code is None
        assert runtime._children[record.id].poll() is not None
        assert no_reader_tasks()
    asyncio.run(scenario())


@pytest.mark.parametrize('events,main_task', [
    (SUCCESS_PROTOCOL[:3], 'daily'),
    (SUCCESS_PROTOCOL[:1], 'arena_weekly'),
])
def test_parent_rejects_battle_start_for_daily_or_before_login(tmp_path, events, main_task):
    program = '''
import json,sys,time
message=json.loads(sys.stdin.readline())
for event in message['events']:
    print(json.dumps(event),flush=True)
print(json.dumps({'type':'arena_started','executionDeadline':time.time()+2400}),flush=True)
'''
    async def scenario():
        runtime, reporter, record = make_runtime(tmp_path)
        record.user_options = parse_user_options({'mainTask': main_task})
        with pytest.raises(ValueError):
            await runtime._run_child(record, [sys.executable, '-u', '-c', program], {'events': events}, daily=True)
        assert record.daily_status != 'completed'
    asyncio.run(scenario())


def test_weekly_deadline_closes_instance_without_failure_hold_and_keeps_worker_running(tmp_path, monkeypatch):
    from test_login_service_daily_runtime import make_runtime as lifecycle_runtime, record_with_ttl
    from test_login_service_qq_notify import QQTransport, make_notifier, request_json, token_response
    from test_login_service_task_notifications import drain_notifications, enable_notifications
    monkeypatch.setattr(runtime_module, 'ARENA_WEEKLY_SECONDS', .2)
    monkeypatch.setattr(runtime_module, 'FAILED_INSTANCE_HOLD_SECONDS', 60)
    program = '''
import json,sys,time
message=json.loads(sys.stdin.readline())
for event in message['events']:
    print(json.dumps(event),flush=True)
print(json.dumps({'type':'arena_started','executionDeadline':time.time()+.2}),flush=True)
time.sleep(60)
'''
    async def scenario():
        record = record_with_ttl()
        record.user_options = parse_user_options({'mainTask': 'arena_weekly'})
        runtime, storage, queue, pool = lifecycle_runtime(tmp_path, monkeypatch, record, [])
        enable_notifications(runtime, queue)
        transport = QQTransport(token_response(), {'id': 'qr-message'}, {'id': 'result-message'})
        _, runtime._qq_notifier = make_notifier(monkeypatch, transport)
        async def child(current):
            return await runtime._run_child(current, [sys.executable, '-u', '-c', program],
                                             {'events': SUCCESS_PROTOCOL[:3]}, daily=True)
        monkeypatch.setattr(runtime, '_run_ldplayer', child)
        try:
            await asyncio.wait_for(runtime._process_record(record), 5)
            assert record.daily_status == 'completed' and record.daily_error_code is None
            assert pool.closed == [record.id] and pool.state == 'released'
            assert not queue.failed_sessions and not runtime._stopping
            await drain_notifications(runtime)
            assert len(transport.requests) == 3
            assert request_json(transport.requests[-1][0])['content'] == '雪花，决斗场周胜任务结束啦！辛苦啦～'
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('deadline', [None, True, '2400', -1, 0, 1, float('nan'), float('inf'), 'too_long'])
def test_parent_rejects_invalid_or_excessive_battle_deadline(tmp_path, deadline):
    program = '''
import json,sys,time
message=json.loads(sys.stdin.readline())
for event in message['events']:
    print(json.dumps(event),flush=True)
end=message['deadline']
if end=='too_long': end=time.time()+2401
print(json.dumps({'type':'arena_started','executionDeadline':end}),flush=True)
'''
    async def scenario():
        runtime, reporter, record = make_runtime(tmp_path)
        record.user_options = parse_user_options({'mainTask': 'arena_weekly'})
        with pytest.raises(ValueError):
            await runtime._run_child(record, [sys.executable, '-u', '-c', program],
                                     {'events': SUCCESS_PROTOCOL[:3], 'deadline': deadline}, daily=True)
        assert record.daily_status != 'completed'
    asyncio.run(scenario())
