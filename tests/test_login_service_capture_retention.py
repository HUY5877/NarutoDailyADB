"""Task completion must leave capture records and Android app state intact."""

import base64
from datetime import datetime, timezone
import json

import pytest

from naruto_daily.login_service import ldplayer_worker, qq_capture
from test_login_service_channels import login_frame
from test_login_service_ldplayer_worker import daily_dependencies, request, PNG
from test_login_service_qq_capture import ATTEMPT, DATABASE, USER, DatabaseBoundary, encoded_result


@pytest.mark.parametrize('outcome', ['success', 'qr_failure', 'login_timeout', 'login_failure', 'daily_failure'])
def test_real_capture_session_preserves_database_and_apps_when_worker_finishes(monkeypatch, tmp_path, outcome):
    """A worker finally calling close/cancel would clear the row and both apps."""
    now = [1_800_000_000.0]
    callback = 'auth://callback?access_token=private'
    database = DatabaseBoundary(rows=[(True,), (callback, datetime.fromtimestamp(now[0], timezone.utc))])
    monkeypatch.setattr(qq_capture.psycopg, 'connect', database.connect)
    monkeypatch.setenv('DATABASE_URL', DATABASE)
    monkeypatch.setenv('QQ_CAPTURE_API_BASE', 'https://example.test')
    calls, app_calls, app_state = [], [], {}
    dependencies = list(daily_dependencies(calls))

    class Manager(dependencies[0]):
        def capture_apps_installed(self, instance, deadline):
            return True

    class Device:
        def __init__(self, adb_path, serial, deadline, command_timeout, *, game_package, login_type):
            self.deadline, self.login_type, self.selected = deadline, login_type, False

        def _run(self, *args):
            if args[:3] == ('shell', 'content', 'call'):
                kind = 'capture' if args[4] == 'content://com.huy.authcapture.automation' else 'login'
                method = args[6]
                argument = args[8] if len(args) == 9 else None
                app_calls.append((kind, method))
                if method == 'capabilities':
                    return encoded_result({'protocol': 1, 'kind': kind})
                if method in ('begin', 'inject'):
                    context = json.loads(base64.urlsafe_b64decode(argument + '=' * (-len(argument) % 4)))
                    app_state[kind] = context
                if method in ('cancel', 'clear'):
                    app_state.pop(kind, None)
                    return encoded_result({'state': 'cancelled'})
                if outcome == 'qr_failure' and method == 'status':
                    return encoded_result({'error': 'fixture capture failure'})
                current = app_state.get(kind) or app_state['capture']
                return encoded_result({'captureId': current['captureId'], 'qrRevision': 1,
                                       'state': 'submitted' if method == 'confirm' else
                                                'qr_ready' if kind == 'capture' else 'ready'})
            if args[:3] == ('exec-out', 'content', 'read'):
                if outcome == 'login_timeout':
                    now[0] = self.deadline
                return PNG
            if args == ('shell', 'am', 'force-stop', 'com.tencent.tim'):
                calls.append('prepare_login')
                return b''
            assert args == ('shell', 'dumpsys', 'activity', 'activities')
            return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity'
                    if self.selected else b'mResumedActivity: com.tencent.KiHan/.GameActivity')

        def screenshot(self):
            return login_frame()

        def hierarchy(self):
            return ''

        def tap(self, x, y):
            self.selected = True

    class Daily:
        def __init__(self, **kwargs):
            pass

        def wait_for_authorization(self, deadline):
            if outcome == 'login_failure':
                raise RuntimeError('fixture game authorization failure')

        def finish_login(self, deadline):
            pass

        def run_daily(self, deadline):
            if outcome == 'daily_failure':
                raise RuntimeError('fixture daily failure')
            return 4

    # Keep the real session, SQL builder, Android provider protocol and injection.
    # Only process/ADB/database IO and the unrelated daily runner are replaced.
    dependencies[0], dependencies[2] = Manager, Device
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: Daily)
    monkeypatch.setattr(ldplayer_worker, '_load_dependencies', lambda: tuple(dependencies))
    from io import StringIO
    output = StringIO()
    code = ldplayer_worker.main(
        input_stream=StringIO(json.dumps(request(captureUserId=USER, captureAttemptId=ATTEMPT,
            dailyConfigPath=str(tmp_path / 'daily.json'), dailyTimeoutSeconds=600)) + '\n'),
        output_stream=output, clock=lambda: now[0], use_watchdog=False,
    )
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert code == (0 if outcome == 'success' else 1)
    assert not any(method in ('cancel', 'clear') for _, method in app_calls)
    assert 'capture' in app_state
    assert len(database.queries) == (1 if outcome in ('qr_failure', 'login_timeout') else 2)
    assert all('callback_url = NULL' not in query and "'cancelled'" not in query
               for query, _ in database.queries)
    if outcome not in ('qr_failure', 'login_timeout'):
        assert app_state['login']['callbackUrl'] == callback + '&auth_time=1800000000000'
        assert "status = 'consumed'" in database.queries[-1][0]
        assert calls == ['allocate', 'start', 'prepare_login', 'launch']
    assert not any(event['type'] == 'capture_started' for event in events)
    if outcome == 'success':
        assert [event['type'] for event in events if event['type'] != 'log'] == [
            'stage', 'stage', 'qr', 'authorized', 'logged_in', 'daily_completed']
    else:
        assert events[-1] == {'type': 'error', 'code': {
            'qr_failure': 'qr_not_found', 'login_timeout': 'login_timeout',
            'login_failure': 'login_failed', 'daily_failure': 'daily_failed',
        }[outcome]}


@pytest.mark.parametrize('invalid', [None, 'owner', 'attempt', 'duplicate', 'late', 'wechat', 'database', 'queue'])
def test_legacy_capture_marker_keeps_protocol_fences_without_cleanup_or_public_progress(monkeypatch, tmp_path, invalid):
    import asyncio
    import sys
    from naruto_daily.login_service.config import ServiceConfig
    from naruto_daily.login_service.models import parse_submission
    from naruto_daily.login_service.runtime import LoginRuntime
    from test_login_service_runtime import MemoryStorage, submission
    from test_login_service_daily_runtime import (CHILD_PROGRAM, RecordingRedis,
                                                 QR, AUTHORIZED, LOGGED_IN, COMPLETED, emit)

    class Reporter:
        def __init__(self):
            self.events = []

        def emit(self, record, **event):
            self.events.append(event)

        async def close(self):
            pass

    async def scenario():
        reporter = Reporter()
        runtime = LoginRuntime(ServiceConfig(ldplayer_state_dir=tmp_path / 'state',
            database_url='' if invalid == 'database' else DATABASE), MemoryStorage(),
            redis_queue=None if invalid == 'queue' else RecordingRedis(), run_reporter=reporter)
        record = parse_submission(submission())
        record.attempt_id = ATTEMPT
        if invalid == 'wechat':
            record.login_type = 'android_wechat'
        boundary = DatabaseBoundary()
        monkeypatch.setattr(qq_capture.psycopg, 'connect', boundary.connect)
        legacy = {'type': 'capture_started'}
        plan = [emit(legacy), emit({'type': 'stage', 'status': 'preparing_qr'}), emit(QR),
                emit(AUTHORIZED), emit(LOGGED_IN), emit(COMPLETED)]
        if invalid == 'duplicate':
            plan.insert(1, emit(legacy))
        elif invalid == 'late':
            plan.pop(0)
            plan.insert(2, emit(legacy))
        message = {'captureUserId': USER, 'captureAttemptId': ATTEMPT, 'plan': plan}
        if invalid == 'owner':
            message.pop('captureUserId')
        elif invalid == 'attempt':
            message['captureAttemptId'] = 'b' * 32
        try:
            operation = asyncio.wait_for(runtime._run_child(record,
                [sys.executable, '-u', '-c', CHILD_PROGRAM], message, daily=True), 3)
            if invalid:
                with pytest.raises(ValueError, match='Unexpected QQ capture start'):
                    await operation
                assert record.daily_status != 'completed'
            else:
                assert await operation == b'\x89PNG\r\n\x1a\nfixture'
                assert list(dict.fromkeys(event['status'] for event in reporter.events)) == [
                    'starting_emulator', 'preparing_qr', 'waiting_scan', 'finishing_login', 'running_daily']
                assert record.daily_status == 'completed'
            assert 'capture' not in json.dumps(reporter.events)
            assert not boundary.connections and not boundary.queries
        finally:
            await runtime.stop()

    asyncio.run(scenario())
