"""Capture rounds are task capabilities, never a lookup of a user's latest account."""

import importlib
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import traceback

import pytest

from naruto_daily.login_service.config import load_config
from oss_fakes import OSS_ENV

USER = '11111111-1111-4111-8111-111111111111'
TASK = '22222222-2222-4222-8222-222222222222'
ATTEMPT = 'a' * 32
DATABASE = 'postgresql://private:secret@db.invalid/naruto'


def module():
    try:
        return importlib.import_module('naruto_daily.login_service.qq_capture')
    except ModuleNotFoundError:
        pytest.fail('QQ capture integration is not implemented')


def test_capture_endpoint_is_optional_but_must_be_an_https_origin():
    assert load_config(OSS_ENV | {'QQ_CAPTURE_API_BASE': 'https://example.test/'}).qq_capture_api_base == 'https://example.test'
    for value in ('http://example.test', 'https://u:p@example.test', 'https://example.test/path',
                  'https://example.test?token=x', 'https://example.test/#x', 'https://example.test\n',
                  'https://example.test:8443'):
        with pytest.raises(ValueError, match='QQ_CAPTURE_API_BASE'):
            load_config(OSS_ENV | {'QQ_CAPTURE_API_BASE': value})


class DatabaseBoundary:
    def __init__(self, rows=(), failure=False):
        self.rows = iter(rows)
        self.queries = []
        self.connections = []
        self.failure = failure

    def connect(self, url, **kwargs):
        if self.failure:
            raise RuntimeError(DATABASE + ' access_token=private-token')
        self.connections.append((url, kwargs))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def cursor(self):
        return self

    def execute(self, query, params):
        self.queries.append((' '.join(query.split()), params))

    def fetchone(self):
        return next(self.rows, None)


def store(monkeypatch, boundary):
    capture = module()
    monkeypatch.setattr(capture.psycopg, 'connect', boundary.connect)
    return capture.CaptureStore(DATABASE, user_id=USER, task_id=TASK,
                                attempt_id=ATTEMPT, login_type='ios_qq', deadline=1_800_000_300)


def test_begin_round_stores_only_token_hash_and_refresh_requires_previous_round(monkeypatch):
    boundary = DatabaseBoundary(rows=[(True,), (True,)])
    client = store(monkeypatch, boundary)
    first = client.begin()
    second = client.begin(previous=first)
    assert first.capture_id != second.capture_id and first.token != second.token
    assert len(first.token) == 64 and first.token not in repr(first)
    query, params = boundary.queries[0]
    assert first.token not in repr(params)
    assert sha256(first.token.encode()).hexdigest() in params
    assert USER in params and TASK in params and ATTEMPT in params
    query, params = boundary.queries[1]
    assert first.capture_id in params and second.capture_id in params
    assert 'capture_id = %s' in query and 'attempt_id = %s' in query and 'owner_id = %s' in query
    assert "status IN ('pending', 'completed')" in query
    assert all(options['connect_timeout'] <= 5 and 'statement_timeout=5000' in options['options']
               for _, options in boundary.connections)


def test_consume_is_atomic_and_binds_every_identity_and_both_deadlines(monkeypatch):
    captured = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
    boundary = DatabaseBoundary(rows=[(True,), ('auth://callback?access_token=private', captured)])
    client = store(monkeypatch, boundary)
    current = client.begin()
    authorization = client.consume(current)
    assert authorization.callback_url == 'auth://callback?access_token=private'
    assert authorization.captured_at_epoch_ms == 1792821600000
    assert 'private' not in repr(authorization)
    query, params = boundary.queries[-1]
    assert query.startswith('UPDATE ') and "status = 'consumed'" in query
    assert "status = 'completed'" in query
    assert 'expires_at > clock_timestamp()' in query and 'authorization_expires_at > clock_timestamp()' in query
    assert params == (TASK, USER, ATTEMPT, current.capture_id)


def test_missing_round_never_yields_a_saved_authorization(monkeypatch):
    boundary = DatabaseBoundary(rows=[(True,), None])
    client = store(monkeypatch, boundary)
    current = client.begin()
    assert client.consume(current) is None


def test_database_failure_has_no_credential_or_callback_exception_chain(monkeypatch):
    client = store(monkeypatch, DatabaseBoundary(failure=True))
    with pytest.raises(module().CaptureError) as result:
        client.begin()
    formatted = ''.join(traceback.format_exception(result.value))
    assert 'private' not in formatted and 'secret' not in formatted and 'access_token' not in formatted


def test_auth_time_uses_real_capture_time_without_overwriting_existing_time():
    capture = module()
    assert capture.login_callback_url('auth://callback?access_token=abc', 1792821600000) == 'auth://callback?access_token=abc&auth_time=1792821600000'
    assert capture.login_callback_url('auth://callback?access_token=abc&auth_time=1000', 1792821600000).endswith('auth_time=1000')


def test_fragment_callback_retains_its_payload_location_and_original_time():
    capture = module()
    assert capture.login_callback_url('auth://tauth.qq.com/#ret=0&access_token=abc', 1792821600000) == (
        'auth://tauth.qq.com/#ret=0&access_token=abc&auth_time=1792821600000')
    value = 'auth://tauth.qq.com/#ret=0&access_token=abc&auth_time=1000'
    assert capture.login_callback_url(value, 1792821600000) == value


@pytest.mark.parametrize('channel,left,right', [('android_qq', 1400, 1850), ('ios_qq', 950, 1400)])
def test_injection_selects_requested_game_channel_then_confirms_current_round(monkeypatch, channel, left, right):
    from test_login_service_channels import login_frame
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    calls, taps = [], []
    class Device:
        deadline = 200.
        login_type = channel
        def _run(self, *args):
            calls.append(args)
            if args[:3] == ('shell', 'dumpsys', 'activity'):
                return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity' if taps else
                        b'mResumedActivity: com.tencent.KiHan/.GameActivity')
            return b''
        def screenshot(self): return login_frame()
        def hierarchy(self): return ''
        def tap(self, x, y): taps.append((x, y))
    class Login:
        def call(self, method, argument=None):
            calls.append((method, argument))
            return {'captureId': session.current.capture_id, 'state': 'submitted' if method == 'confirm' else 'ready'}
    session.device, session.login = Device(), Login()
    session.prepare_login()
    session.inject(capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000))
    assert calls[0] == ('shell', 'am', 'force-stop', 'com.tencent.tim')
    assert len(taps) == 1 and left < taps[0][0] < right
    injected = next(item[1] for item in calls if item[0] == 'inject')
    assert injected == {'taskId': TASK, 'captureId': session.current.capture_id,
                        'callbackUrl': 'auth://callback?access_token=private&auth_time=1792821600000'}
    assert ('confirm', session.current.capture_id) in calls


class RestoredLoginDevice:
    """A game restores QQ authorization before this task selects its channel."""

    def __init__(self, boundary, *, channel='ios_qq', cancellation='leaves', deadline=200.):
        from test_login_service_channels import login_frame
        self.clock = boundary.clock
        self.login_type, self.deadline = channel, deadline
        self.cancellation = cancellation
        self.events = []
        self.back_at = None
        self.taps = []
        self.frames = 0
        self.game_frame = login_frame()

    def _run(self, *args):
        assert args == ('shell', 'dumpsys', 'activity', 'activities')
        is_login = (self.back_at is None or self.cancellation == 'stuck'
                    or self.clock[0] - self.back_at < 1.5 or self.taps
                    or self.cancellation == 'reopens' and self.frames > 0)
        return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity'
                if is_login else b'mResumedActivity: com.tencent.KiHan/.GameActivity')

    def back(self):
        assert self.back_at is None, 'Cancelling the restored request must not send Back twice'
        self.back_at = self.clock[0]
        self.events.append('back')

    def screenshot(self):
        assert self.back_at is not None and self.clock[0] - self.back_at >= 1.5, (
            'Wait until the cancelled authorization page has actually left')
        self.frames += 1
        self.events.append('game_frame')
        return self.game_frame

    def hierarchy(self):
        return ''

    def tap(self, x, y):
        self.taps.append((x, y))
        self.events.append('channel_tap')


@pytest.mark.parametrize('channel,left,right', [('android_qq', 1400, 1850), ('ios_qq', 950, 1400)])
def test_injection_cancels_restored_login_and_selects_requested_channel(monkeypatch, channel, left, right):
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    device = RestoredLoginDevice(boundary, channel=channel)

    class Login:
        def call(self, method, argument=None):
            assert device.taps, 'A restored request must never receive this task authorization'
            device.events.append(method)
            if method == 'inject':
                assert argument['taskId'] == TASK
                assert argument['captureId'] == session.current.capture_id
            else:
                assert method == 'confirm' and argument == session.current.capture_id
            return {'captureId': session.current.capture_id,
                    'state': 'submitted' if method == 'confirm' else 'ready'}

    session.device, session.login = device, Login()
    session.inject(capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000))

    assert len(device.taps) == 1 and left < device.taps[0][0] < right
    assert device.events == ['back', 'game_frame', 'channel_tap', 'inject', 'confirm']


@pytest.mark.parametrize('deadline,expected_error', [(200., 'capture'), (102.25, 'deadline')])
def test_injection_refuses_unselected_tim_page_when_cancel_cannot_leave(monkeypatch, deadline, expected_error):
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    session.device = device = RestoredLoginDevice(boundary, cancellation='stuck', deadline=deadline)

    with pytest.raises(capture.CaptureError if expected_error == 'capture' else TimeoutError):
        session.inject(capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000))

    assert device.events == ['back']
    assert boundary.clock[0] == min(110., deadline)
    assert not any(event[:2] == ('login', 'inject') for event in boundary.events)


def test_injection_refuses_unselected_login_that_reopens_before_channel_tap(monkeypatch):
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    session.device = device = RestoredLoginDevice(boundary, cancellation='reopens')

    class Login:
        def call(self, method, argument=None):
            device.events.append(method)
            return {'captureId': session.current.capture_id,
                    'state': 'submitted' if method == 'confirm' else 'ready'}

    session.login = Login()

    with pytest.raises(capture.CaptureError):
        session.inject(capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000))

    assert device.events == ['back', 'game_frame']
    assert not device.taps


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
@pytest.mark.parametrize('stuck', [False, True])
def test_injection_cancels_old_wechat_before_selecting_current_qq(monkeypatch, channel, stuck):
    from test_login_service_channels import login_frame, wechat_frame
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    events = []

    class Device:
        deadline = 200.
        login_type = channel
        cancelled_at = None
        selected = False
        def _run(self, *args):
            return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity'
                    if self.selected else b'mResumedActivity: com.tencent.KiHan/.GameActivity')
        def screenshot(self):
            return wechat_frame() if self.cancelled_at is None or stuck else login_frame()
        def hierarchy(self): return ''
        def back(self): pytest.fail('WeChat requires its own Cancel control')
        def tap(self, x, y):
            if y < 111:
                assert self.cancelled_at is None, 'Cancel must only be tapped once'
                self.cancelled_at = boundary.clock[0]
                events.append('wechat_cancel')
            else:
                assert boundary.clock[0] - self.cancelled_at >= 3
                assert (1400 < x < 1850) if channel == 'android_qq' else (950 < x < 1400)
                self.selected = True
                events.append('requested_qq')

    class Login:
        def call(self, method, argument=None):
            assert session.device.selected
            events.append(method)
            return {'captureId': session.current.capture_id,
                    'state': 'submitted' if method == 'confirm' else 'ready'}

    session.device, session.login = Device(), Login()
    authorization = capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000)
    if stuck:
        with pytest.raises(capture.CaptureError):
            session.inject(authorization)
        assert events == ['wechat_cancel']
        assert boundary.clock[0] <= 115
    else:
        session.inject(authorization)
        assert events == ['wechat_cancel', 'requested_qq', 'inject', 'confirm']


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
@pytest.mark.parametrize('stuck', [False, True])
@pytest.mark.parametrize('native_layer', [False, True])
def test_capture_leaves_old_plain_qq_qr_before_opening_huy_login(monkeypatch, channel, stuck, native_layer):
    import numpy as np
    from test_login_service_channels import login_frame
    from test_login_service_web_qr import frame as qq_frame
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    events = []

    class Device:
        deadline = 200.
        login_type = channel
        backed = 0
        selected = False
        def _run(self, *args):
            return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity'
                    if self.selected else b'mResumedActivity: com.tencent.KiHan/.GameActivity')
        def screenshot(self):
            if not self.backed or stuck:
                return qq_frame()
            if native_layer and self.backed == 1:
                return np.full((1080, 1920, 3), 255, np.uint8)
            return login_frame()
        def hierarchy(self):
            if native_layer and self.backed == 1 and not stuck:
                return ('<hierarchy><node package="com.tencent.mobileqq" text="其他登录方式" '
                        'bounds="[990,910][1240,1080]" /></hierarchy>')
            return ''
        def back(self):
            assert self.backed < (2 if native_layer else 1)
            events.append('old_qq_cancel' if self.backed == 0 else 'qq_layer_cancel')
            self.backed += 1
        def tap(self, x, y):
            assert self.backed
            assert (1400 < x < 1850) if channel == 'android_qq' else (950 < x < 1400)
            self.selected = True
            events.append('requested_qq')

    class Login:
        def call(self, method, argument=None):
            assert session.device.selected
            events.append(method)
            return {'captureId': session.current.capture_id,
                    'state': 'submitted' if method == 'confirm' else 'ready'}

    session.device, session.login = Device(), Login()
    authorization = capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000)
    if stuck:
        with pytest.raises(capture.CaptureError):
            session.inject(authorization)
        assert events == ['old_qq_cancel']
        assert boundary.clock[0] <= 115
    else:
        session.inject(authorization)
        assert events == ['old_qq_cancel'] + (['qq_layer_cancel'] if native_layer else []) + [
            'requested_qq', 'inject', 'confirm']


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_capture_reselects_qq_if_restored_wechat_arrives_after_channel_tap(monkeypatch, channel):
    from test_login_service_channels import login_frame, wechat_frame
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    events = []

    class Device:
        deadline = 140.
        login_type = channel
        taps = 0
        cancelled_at = None
        def _run(self, *args):
            return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity'
                    if self.taps == 2 else b'mResumedActivity: com.tencent.KiHan/.GameActivity')
        def screenshot(self):
            return wechat_frame() if self.taps == 1 and self.cancelled_at is None else login_frame()
        def hierarchy(self): return ''
        def back(self): pytest.fail('Use WeChat cancellation')
        def tap(self, x, y):
            if y < 111:
                assert self.cancelled_at is None
                self.cancelled_at = boundary.clock[0]
                events.append('cancel_wechat')
            else:
                if self.taps:
                    assert boundary.clock[0] - self.cancelled_at >= 3
                self.taps += 1
                events.append('select_qq')

    class Login:
        def call(self, method, argument=None):
            assert session.device.taps == 2
            events.append(method)
            return {'captureId': session.current.capture_id,
                    'state': 'submitted' if method == 'confirm' else 'ready'}

    session.device, session.login = Device(), Login()
    session.inject(capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000))
    assert events == ['select_qq', 'cancel_wechat', 'select_qq', 'inject', 'confirm']


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_capture_reselects_after_old_start_game_arrives_after_qq_tap(monkeypatch, channel):
    from test_login_service_channels import login_frame
    from test_login_service_qr_navigation import frame_with
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    game = frame_with('start_game', 780, 820)
    back = frame_with('game_back', 1680, 30)
    game[:170, 1650:1850] = back[:170, 1650:1850]
    events = []

    class Device:
        deadline = 140.
        login_type = channel
        channel_taps = 0
        returned = False
        def _run(self, *args):
            return (b'mResumedActivity: com.tencent.tim/com.tencent.open.agent.AgentActivity'
                    if self.channel_taps == 2 else b'mResumedActivity: com.tencent.KiHan/.GameActivity')
        def screenshot(self):
            return game if self.channel_taps == 1 and not self.returned else login_frame()
        def hierarchy(self): return ''
        def back(self): pytest.fail('The game has its own identified Back control')
        def tap(self, x, y):
            if y < 170:
                assert 1650 <= x < 1850 and not self.returned
                self.returned = True
                events.append('game_back')
            else:
                self.channel_taps += 1
                events.append('select_qq')

    session.device = Device()
    session._open_login_channel()
    assert events == ['select_qq', 'game_back', 'select_qq']


def encoded_result(value):
    return ('Bundle[{result=' + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=') + '}]').encode()


def test_provider_encodes_credentials_as_one_shell_safe_argument_and_reads_only_current_qr():
    capture = module()
    calls = []
    class Device:
        def _run(self, *arguments):
            calls.append(arguments)
            return encoded_result({'captureId': TASK, 'state': 'qr_ready'})
    provider = capture.AutomationProvider(Device(), 'com.huy.authcapture.automation')
    assert provider.call('begin', {'captureId': TASK, 'captureToken': 'secret & $token'})['state'] == 'qr_ready'
    assert calls[0][:5] == ('shell', 'content', 'call', '--uri', 'content://com.huy.authcapture.automation')
    assert all('secret' not in part and '&' not in part and '$' not in part for part in calls[0])
    assert json.loads(base64.urlsafe_b64decode(calls[0][-1] + '=='))['captureToken'] == 'secret & $token'


@pytest.mark.parametrize('reply', [b'private output', b'Bundle[{result=invalid}]', encoded_result([]),
                                  encoded_result({'error': 'access_token=private'})])
def test_bad_provider_responses_are_sanitized(reply):
    capture = module()
    class Device:
        def _run(self, *_):
            return reply
    with pytest.raises(capture.CaptureError) as result:
        capture.AutomationProvider(Device(), 'com.huy.authcapture.automation').call('status', TASK)
    assert 'private' not in ''.join(traceback.format_exception(result.value))


def test_runtime_uses_owned_redis_identity_not_queue_payload(monkeypatch):
    import asyncio
    from pathlib import Path
    from dataclasses import replace
    from test_login_service_worker import worker, fake_server, MemoryStorage, task_body, enqueue
    async def scenario():
        runtime = worker(fake_server(), MemoryStorage(), ldplayer_console_path=Path('console.exe'))
        body = task_body() | {'userId': TASK, 'loginType': 'ios_qq'}
        await enqueue(runtime._redis_queue._client, runtime, body, session_fields={'userId': USER})
        record = await runtime._redis_queue.pop()
        captured = {}
        async def run(current, command, message, **kwargs):
            captured.update(message)
            return b'png'
        monkeypatch.setattr(runtime, '_run_child', run)
        await runtime._run_ldplayer(record)
        assert captured['captureUserId'] == USER
        assert captured['captureAttemptId'] == record.attempt_id
        assert 'DATABASE_URL' not in json.dumps(captured)
        record.login_type = 'ios_wechat'
        captured.clear()
        await runtime._run_ldplayer(record)
        assert 'captureUserId' not in captured and 'captureAttemptId' not in captured
        await runtime.stop()
    asyncio.run(scenario())


def test_identity_transport_failure_preserves_legacy_fallback_without_fabricating_identity(monkeypatch):
    import asyncio
    from pathlib import Path
    from test_login_service_worker import worker, fake_server, MemoryStorage, task_body, enqueue
    from naruto_daily.login_service.queue import RedisQueueError
    async def scenario():
        runtime = worker(fake_server(), MemoryStorage(), ldplayer_console_path=Path('console.exe'))
        await enqueue(runtime._redis_queue._client, runtime, task_body(), session_fields={'userId': USER})
        record = await runtime._redis_queue.pop()
        async def unavailable(record): raise RedisQueueError('Identity unavailable')
        async def run(record, command, message, **kwargs):
            assert 'captureUserId' not in message and 'captureAttemptId' not in message
            return b'legacy'
        monkeypatch.setattr(runtime._redis_queue, 'notification_user_id', unavailable)
        monkeypatch.setattr(runtime, '_run_child', run)
        assert await runtime._run_ldplayer(record) == b'legacy'
        await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel,expected', [('android_qq', True), ('ios_qq', True), ('ios_wechat', False)])
def test_child_receives_only_selected_capture_configuration(monkeypatch, channel, expected):
    import asyncio
    from naruto_daily.login_service.config import ServiceConfig
    from naruto_daily.login_service.runtime import LoginRuntime
    from test_login_service_runtime import MemoryStorage, submission
    from naruto_daily.login_service.models import parse_submission
    runtime = LoginRuntime(ServiceConfig(database_url=DATABASE, qq_capture_api_base='https://example.test'), MemoryStorage())
    record = parse_submission(submission() | {'loginType': channel})
    observed = {}
    class SpawnCaptured(Exception):
        pass
    def popen(command, **kwargs):
        observed.update(kwargs['env'])
        raise SpawnCaptured
    monkeypatch.setenv('DATABASE_URL', 'stale-secret')
    monkeypatch.setattr('naruto_daily.login_service.runtime.subprocess.Popen', popen)
    with pytest.raises(SpawnCaptured):
        asyncio.run(runtime._run_child(record, [], {}, daily=True))
    assert observed.get('DATABASE_URL') == (DATABASE if expected else None)
    assert observed.get('QQ_CAPTURE_API_BASE') == ('https://example.test' if expected else None)


@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq'])
def test_worker_captures_before_opening_game_and_requires_game_authorization(monkeypatch, tmp_path, channel):
    from naruto_daily.login_service import ldplayer_worker
    from test_login_service_ldplayer_worker import daily_dependencies, invoke, request, PNG
    calls = []
    dependencies = list(daily_dependencies(calls))
    class Manager(dependencies[0]):
        def capture_apps_installed(self, instance, deadline):
            assert instance == 'bound-instance'
            calls.append('probe')
            return True
    dependencies[0] = Manager
    class Capture:
        def __init__(self, device, **kwargs):
            assert kwargs['user_id'] == USER and kwargs['attempt_id'] == ATTEMPT
            calls.append('capture_config')
        def prepare(self):
            calls.append('capture_qr')
            return PNG
        def wait(self, **kwargs):
            calls.append('database_capture')
            return 'authorization'
        def prepare_login(self):
            calls.append('reset_login')
        def inject(self, authorization):
            assert authorization == 'authorization'
            calls.append('inject')
    class Daily:
        def __init__(self, **kwargs):
            calls.append('daily_config')
        def wait_for_authorization(self, deadline):
            calls.append('game_authorization')
        def finish_login(self, deadline):
            calls.append('finish_login')
        def run_daily(self, deadline):
            calls.append('daily')
            return 4
    monkeypatch.setattr(ldplayer_worker, '_load_capture_session', lambda: Capture, raising=False)
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: Daily)
    code, events = invoke(monkeypatch, request(loginType=channel, captureUserId=USER, captureAttemptId=ATTEMPT,
        dailyConfigPath=str(tmp_path / 'daily.json'), dailyTimeoutSeconds=600), tuple(dependencies))
    assert code == 0
    assert calls == ['allocate', 'start', 'daily_config', 'probe', 'capture_config', 'capture_qr',
                     'database_capture', 'reset_login', 'launch', 'inject', 'game_authorization',
                     'finish_login', 'daily']
    assert [event['type'] for event in events if event['type'] != 'log'] == [
        'stage', 'stage', 'qr', 'authorized', 'logged_in', 'daily_completed']


@pytest.mark.parametrize('channel', ['android_wechat', 'ios_wechat'])
def test_wechat_never_probes_capture_apps(monkeypatch, tmp_path, channel):
    from naruto_daily.login_service import ldplayer_worker
    from test_login_service_ldplayer_worker import daily_dependencies, invoke, request
    dependencies = list(daily_dependencies([]))
    class Manager(dependencies[0]):
        def capture_apps_installed(self, *_):
            pytest.fail('WeChat must not use capture app discovery')
    dependencies[0] = Manager
    class Daily:
        def __init__(self, **kwargs): pass
        def wait_for_authorization(self, deadline): pass
        def finish_login(self, deadline): pass
        def run_daily(self, deadline): return 1
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: Daily)
    code, _ = invoke(monkeypatch, request(loginType=channel, dailyConfigPath=str(tmp_path / 'daily.json'),
                                         dailyTimeoutSeconds=600), tuple(dependencies))
    assert code == 0


@pytest.mark.parametrize('installed,expected', [([], False), (['com.huy.authcapture'], False),
    (['com.tencent.tim'], False), (['com.huy.authcapture', 'com.tencent.tim'], True)])
def test_package_detection_is_exact_and_on_the_owned_instance(monkeypatch, tmp_path, installed, expected):
    import time
    from naruto_daily.login_service.ldplayer import LDPlayerManager
    from test_login_service_ldplayer import FakeLDPlayer, command_result
    console = tmp_path / 'ldconsole.exe'
    runner = FakeLDPlayer(console)
    probes = []
    def run(args, **kwargs):
        parts = tuple(map(str, args))
        if 'list' in parts and 'packages' in parts:
            probes.append(parts)
            return command_result(kwargs, ('\n'.join('package:' + value for value in installed + ['com.tencent.tim.fake'])).encode())
        return runner(args, **kwargs)
    monkeypatch.setattr('naruto_daily.login_service.ldplayer.subprocess.run', run)
    manager = LDPlayerManager(console, 'clean-template', tmp_path / 'state')
    instance = manager.allocate(TASK, time.time() + 20)
    manager.start(instance, time.time() + 20)
    assert manager.capture_apps_installed(instance, time.time() + 20) is expected
    assert probes == [(str(console.with_name('adb.exe')), '-s', 'emulator-5570', 'shell', 'pm', 'list', 'packages')]


def package_probe_boundary(monkeypatch, tmp_path, probe, calls):
    from test_login_service_ldplayer import FakeLDPlayer, command_result
    console = tmp_path / 'ldconsole.exe'
    runner = FakeLDPlayer(console)
    probes = []

    def run(args, **kwargs):
        parts = tuple(map(str, args))
        if parts[-4:] == ('shell', 'pm', 'list', 'packages'):
            probes.append(parts)
            result = probe(args, kwargs)
            return command_result(kwargs, result.stdout, result.stderr, result.returncode)
        if parts[:2] == (str(console), 'runapp'):
            calls.append('game')
        return runner(args, **kwargs)

    monkeypatch.setattr('naruto_daily.login_service.ldplayer.subprocess.run', run)
    return console, runner, probes


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'os_error', 'decode', 'empty', 'non_package', 'partial'])
def test_capture_package_probe_failure_preserves_legacy_login(monkeypatch, tmp_path, caplog, failure):
    import subprocess
    import time
    from naruto_daily.login_service.ldplayer import LDPlayerManager
    from test_login_service_ldplayer import Result

    def probe(args, kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, kwargs['timeout'], stderr=b'private-token')
        if failure == 'os_error':
            raise OSError('private-token')
        return {
            'nonzero': Result(b'private-token', b'private-token', 1),
            'decode': Result(b'\xff'), 'empty': Result(),
            'non_package': Result(b'Error: private-token'),
            'partial': Result(b'package:com.huy.authcapture\npackage:com.tencent.tim\nError: private-token'),
        }[failure]

    calls = []
    console, _, probes = package_probe_boundary(monkeypatch, tmp_path, probe, calls)
    manager = LDPlayerManager(console, 'clean-template', tmp_path / 'state')
    deadline = time.time() + 20
    instance = manager.allocate(TASK, deadline)
    manager.start(instance, deadline)
    assert manager.capture_apps_installed(instance, deadline) is False
    manager.launch_game(instance, 'com.tencent.naruto', deadline)
    assert calls == ['game'] and len(probes) == 1
    assert 'private-token' not in caplog.text
    assert any(record.levelname == 'WARNING' for record in caplog.records)


@pytest.mark.parametrize('invalid', ['owner', 'serial', 'stopped', 'deadline',
                                     'deadline_during_probe', 'deadline_after_output'])
def test_capture_package_fallback_never_bypasses_task_ownership_or_deadline(monkeypatch, tmp_path, invalid):
    import subprocess
    import time
    from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerManager
    from test_login_service_ldplayer import Result

    now = [time.time()]
    monkeypatch.setattr('naruto_daily.login_service.ldplayer.time.time', lambda: now[0])
    deadline = now[0] + 20

    def probe(args, kwargs):
        now[0] = deadline
        if invalid == 'deadline_after_output':
            return Result(b'package:com.huy.authcapture\npackage:com.tencent.tim')
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])

    console, runner, probes = package_probe_boundary(monkeypatch, tmp_path, probe, [])
    manager = LDPlayerManager(console, '', tmp_path / 'state', pool_indices=(7,))
    instance = manager.allocate(TASK, deadline)
    manager.start(instance, deadline)
    if invalid == 'owner':
        instance.task_id = USER
    elif invalid == 'serial':
        instance.serial = 'emulator-9999'
    elif invalid == 'stopped':
        runner.instances['clean-template'][1] = 0
    elif invalid == 'deadline':
        now[0] = deadline
    with pytest.raises(LDPlayerError):
        manager.capture_apps_installed(instance, deadline)
    assert len(probes) == int(invalid in ('deadline_during_probe', 'deadline_after_output'))


@pytest.mark.parametrize('mode', ['qr_only', 'daily', 'arena_weekly'])
@pytest.mark.parametrize('channel', ['android_qq', 'ios_qq', 'android_wechat', 'ios_wechat'])
@pytest.mark.parametrize('installed', [[], ['com.huy.authcapture'], ['com.tencent.tim'],
                                       ['com.huy.authcapture', 'com.tencent.tim']])
def test_worker_package_matrix_preserves_qr_daily_and_weekly_protocol(
    monkeypatch, tmp_path, mode, channel, installed,
):
    from naruto_daily.login_service import ldplayer_worker
    from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerManager
    from test_login_service_ldplayer import Result
    from test_login_service_ldplayer_worker import invoke, request, PNG

    calls = []
    console, _, probes = package_probe_boundary(monkeypatch, tmp_path,
        lambda *_: Result('\n'.join('package:' + name for name in
            installed + ['com.android.settings', 'com.tencent.tim.fake']).encode()), calls)
    uses_capture = mode != 'qr_only' and channel in ('android_qq', 'ios_qq') and len(installed) == 2
    probes_apps = mode != 'qr_only' and channel in ('android_qq', 'ios_qq')

    def forbidden(*args, **kwargs):
        pytest.fail('Package dispatch must not access a real capture provider or database')

    monkeypatch.setattr(module().psycopg, 'connect', forbidden)
    monkeypatch.setattr(module().AutomationProvider, 'call', forbidden)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('QQ_CAPTURE_API_BASE', raising=False)

    class Device:
        def __init__(self, adb_path, serial, deadline, command_timeout, *, game_package, login_type):
            assert serial == 'emulator-5570' and login_type == channel

    class Capture:
        def __init__(self, device, **kwargs):
            assert uses_capture
        def prepare(self):
            calls.append('capture_qr')
            return PNG
        def wait(self, **kwargs):
            calls.append('capture_wait')
            return 'current-authorization'
        def prepare_login(self):
            calls.append('capture_reset')
        def inject(self, authorization):
            assert authorization == 'current-authorization'
            calls.append('capture_inject')

    def load_capture():
        assert uses_capture, 'Legacy login must not load capture configuration or dependencies'
        return Capture

    class Daily:
        def __init__(self, **kwargs):
            assert mode != 'qr_only'
        def wait_for_authorization(self, deadline):
            calls.append('game_authorized')
        def finish_login(self, deadline):
            calls.append('login_finished')
        def run_daily(self, deadline, **kwargs):
            if mode == 'arena_weekly':
                kwargs['on_arena_started'](1_800_001_000.0)
            else:
                assert not kwargs
            calls.append(mode)
            return 3

    def fetch(device, *, require_channel_selection):
        assert require_channel_selection is True
        calls.append('legacy_qr')
        return PNG

    monkeypatch.setattr(ldplayer_worker, '_load_capture_session', load_capture)
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: Daily)
    changes = dict(consolePath=str(console), stateDir=str(tmp_path / 'state'),
                   commandTimeout=20, loginType=channel)
    if mode != 'qr_only':
        changes.update(dailyConfigPath=str(tmp_path / 'daily.json'), dailyTimeoutSeconds=600)
    if mode == 'arena_weekly':
        changes['userOptions'] = {'mainTask': 'arena_weekly'}
    code, events = invoke(monkeypatch, request(**changes), (LDPlayerManager, LDPlayerError, Device, fetch))
    assert code == 0
    protocol = [event for event in events if event['type'] != 'log']
    expected = [{'type': 'stage', 'status': 'starting_emulator'}]
    if not uses_capture:
        expected += [{'type': 'stage', 'status': 'opening_game'}]
    expected += [{'type': 'stage', 'status': 'preparing_qr'},
                 {'type': 'qr', 'png': base64.b64encode(PNG).decode()}]
    if mode != 'qr_only':
        expected += [{'type': 'authorized', 'executionDeadline': 1_800_000_600.0}, {'type': 'logged_in'}]
        if mode == 'arena_weekly':
            expected += [{'type': 'arena_started', 'executionDeadline': 1_800_001_000.0}]
        expected += [{'type': 'daily_completed', 'actionCount': 3}]
    assert protocol == expected
    expected_calls = (['capture_qr', 'capture_wait', 'capture_reset', 'game', 'capture_inject']
                      if uses_capture else ['game', 'legacy_qr'])
    if mode != 'qr_only':
        expected_calls += ['game_authorized', 'login_finished', mode]
    assert calls == expected_calls
    assert len(probes) == int(probes_apps)


class SessionBoundary:
    def __init__(self, capture, *, refresh=False):
        self.capture = capture
        self.events = []
        self.rounds = []
        self.refresh = refresh
        self.clock = [100.0]
        self.device = type('Device', (), {'deadline': 200., 'login_type': 'ios_qq'})()

    def setup(self, monkeypatch):
        boundary = self
        capture = self.capture
        class Store:
            def __init__(self, *args, **kwargs):
                self.task_id = kwargs['task_id']
                self.user_id = kwargs['user_id']
                self.attempt_id = kwargs['attempt_id']
            def begin(self, previous=None):
                boundary.events.append(('begin', previous.capture_id if previous else None))
                current = capture.CaptureRound(str(__import__('uuid').uuid4()), 'b' * 64)
                boundary.rounds.append(current)
                return current
            def consume(self, current):
                boundary.events.append(('consume', current.capture_id))
                return capture.CapturedAuthorization('auth://callback?access_token=private', 1792821600000)
        class Provider:
            def __init__(self, device, authority):
                self.kind = 'capture' if authority == 'com.huy.authcapture.automation' else 'login'
            def call(self, method, argument=None):
                boundary.events.append((self.kind, method, argument))
                if method == 'capabilities':
                    return {'protocol': 1, 'kind': self.kind}
                if method in ('begin', 'refresh'):
                    assert set(argument) == {'userId', 'taskId', 'attemptId', 'captureId', 'captureToken',
                                             'apiBase', 'expiresAtEpochMs', 'loginType'}
                    self.current = argument['captureId']
                return {'captureId': getattr(self, 'current', argument), 'state': 'qr_ready', 'qrRevision': 1}
            def qr(self, capture_id):
                boundary.events.append(('qr', capture_id))
                return b'\x89PNG\r\n\x1a\nround'
        monkeypatch.setattr(capture, 'CaptureStore', Store)
        monkeypatch.setattr(capture, 'AutomationProvider', Provider)
        return capture.QQCaptureSession(self.device, user_id=USER, task_id=TASK, attempt_id=ATTEMPT,
            database_url=DATABASE, api_base='https://example.test', clock=lambda: self.clock[0],
            sleep=lambda seconds: self.clock.__setitem__(0, self.clock[0] + seconds))


def test_refresh_rotates_database_round_before_app_and_before_consumption(monkeypatch):
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    request_id = '33333333-3333-4333-8333-333333333333'
    class Refresh:
        def poll(self):
            return request_id
    published = []
    authorization = session.wait(refresh_requests=Refresh(), publish=published.append)
    assert authorization.callback_url.startswith('auth://')
    first, second = boundary.rounds
    assert ('consume', first.capture_id) not in boundary.events
    assert boundary.events.index(('begin', first.capture_id)) < next(
        i for i, event in enumerate(boundary.events) if event[:2] == ('capture', 'refresh'))
    assert ('consume', second.capture_id) in boundary.events
    assert published[0] == {'type': 'refreshing', 'requestId': request_id}
    assert published[1]['type'] == 'qr' and published[1]['requestId'] == request_id


@pytest.mark.parametrize('missing', ['user_id', 'attempt_id', 'database_url', 'api_base', 'non_https_port'])
def test_both_apps_missing_configuration_fails_before_provider_io(monkeypatch, missing):
    capture = module()
    class Device:
        deadline = 200.
        login_type = 'android_qq'
        def _run(self, *_):
            pytest.fail('Invalid capture configuration must not touch provider')
    args = dict(user_id=USER, task_id=TASK, attempt_id=ATTEMPT, database_url=DATABASE, api_base='https://example.test')
    if missing == 'non_https_port':
        args['api_base'] = 'https://example.test:8443'
    else:
        args[missing] = None
    with pytest.raises(capture.CaptureConfigurationError):
        capture.QQCaptureSession(Device(), **args)


@pytest.mark.parametrize('capabilities', [b'old version', encoded_result({'protocol': 0, 'kind': 'capture'}),
                                       encoded_result({'protocol': 1, 'kind': 'wrong'})])
def test_old_provider_version_is_a_configuration_error_before_database_io(monkeypatch, capabilities):
    capture = module()
    class Device:
        deadline = 200.
        login_type = 'android_qq'
        def _run(self, *_): return capabilities
    monkeypatch.setattr(capture.psycopg, 'connect', lambda *a, **k: pytest.fail('Must check app version before DB write'))
    with pytest.raises(capture.CaptureConfigurationError):
        capture.QQCaptureSession(Device(), user_id=USER, task_id=TASK, attempt_id=ATTEMPT,
                                database_url=DATABASE, api_base='https://example.test')


def test_capture_wait_keeps_original_deadline(monkeypatch):
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    session.prepare()
    consumed = []
    session.store.consume = lambda current: consumed.append(current.capture_id) or None
    boundary.clock[0] = 199.8
    with pytest.raises(TimeoutError):
        session.wait(publish=lambda _: pytest.fail('No refresh requested'))
    assert len(consumed) == 1
    assert boundary.clock[0] == 200.


def test_capture_qr_rejects_a_different_round_without_exporting_pixels(monkeypatch):
    capture = module()
    boundary = SessionBoundary(capture)
    session = boundary.setup(monkeypatch)
    original = session.capture.call
    def call(method, argument=None):
        if method == 'status': return {'captureId': USER, 'state': 'qr_ready', 'qrRevision': 1}
        return original(method, argument)
    session.capture.call = call
    with pytest.raises(capture.CaptureError): session.prepare()
    assert not any(event[0] == 'qr' for event in boundary.events)


@pytest.mark.parametrize('success', [True, False])
def test_refresh_accepted_after_capture_consumption_finishes_through_real_redis_protocol(monkeypatch, tmp_path, success):
    import asyncio
    import sys
    from uuid import uuid4
    from test_login_service_worker import worker, fake_server, MemoryStorage, task_body, enqueue
    from test_login_service_daily_runtime import CHILD_PROGRAM, RecordingPool, QR, AUTHORIZED, LOGGED_IN, COMPLETED, emit, gate, eventually
    async def scenario():
        runtime = worker(fake_server(), MemoryStorage(), emulator_driver='ldplayer',
                         ldplayer_console_path=tmp_path / 'console.exe', ldplayer_pool_indices=(0,),
                         ldplayer_state_dir=tmp_path / 'state', daily_timeout_seconds=30)
        client = runtime._redis_queue._client
        body = task_body() | {'loginTimeoutSeconds': 600}
        session_key, _, _ = await enqueue(client, runtime, body, session_fields={'userId': USER})
        await client.set(f'{runtime.config.redis_queue_key}:users:{USER}:active', body['id'], ex=600)
        record = await runtime._redis_queue.pop()
        runtime._tasks[record.id] = record
        pool = RecordingPool(record.id)
        runtime._manager = pool
        handoff_gate = tmp_path / 'captured-and-refresh-accepted'
        ending = [emit(AUTHORIZED), emit(LOGGED_IN), emit(COMPLETED)] if success else [
            emit({'type': 'error', 'code': 'login_failed'})]
        async def run_child(current):
            return await runtime._run_child(current, [sys.executable, '-u', '-c', CHILD_PROGRAM],
                {'plan': [emit(QR), gate(handoff_gate), *ending]}, daily=True)
        monkeypatch.setattr(runtime, '_run_ldplayer', run_child)
        monkeypatch.setattr('naruto_daily.login_service.runtime.FAILED_INSTANCE_HOLD_SECONDS', 0)
        work = asyncio.create_task(runtime._process_record(record))
        try:
            await eventually(lambda: record.qr_published)
            # Run the actual capture loop's consumption boundary, then accept a
            # website refresh while the game URL handoff is still in progress.
            boundary = SessionBoundary(module())
            capture = boundary.setup(monkeypatch)
            capture.prepare()
            authorization = capture.wait(publish=lambda _: pytest.fail('No refresh existed before consume'))
            assert authorization.callback_url
            request_id = str(uuid4())
            refresh_key = f'{runtime.config.redis_queue_key}:refresh:{record.id}'
            website = json.loads(await client.get(session_key))
            website.update(status='preparing_qr', refreshRequestId=request_id)
            await client.set(session_key, json.dumps(website), keepttl=True)
            await client.set(refresh_key, json.dumps({'taskId': record.id, 'requestId': request_id}), ex=600)
            handoff_gate.touch()
            await asyncio.wait_for(work, 5)
            final = json.loads(await client.get(session_key))
            assert await client.get(refresh_key) is None
            assert 'refreshRequestId' not in final
            assert runtime.storage.put_ids == [record.id]
            assert pool.closed == [record.id]
            if success:
                assert final['loginAuthorized'] is True
                assert record.daily_status == 'completed'
            else:
                assert final['status'] == 'failed'
                assert not final.get('loginAuthorized')
                assert record.daily_status == 'failed'
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('helper,interruption', [(True, 'admin'), (True, 'cancel'), (True, 'buffered'),
    (True, 'timeout'), (True, 'shutdown'), (True, 'success'), (True, 'failure'),
    (False, 'admin'), (False, 'success')])
def test_parent_retains_capture_attempt_on_every_child_exit(monkeypatch, tmp_path, helper, interruption):
    import asyncio
    import sys
    from test_login_service_worker import worker, fake_server, MemoryStorage, task_body, enqueue
    from test_login_service_daily_runtime import (CHILD_PROGRAM, RecordingPool, emit, gate, eventually,
                                                 QR, AUTHORIZED, LOGGED_IN, COMPLETED)
    async def scenario():
        runtime = worker(fake_server(), MemoryStorage(), emulator_driver='ldplayer',
            database_url=DATABASE, ldplayer_console_path=tmp_path / 'console.exe',
            ldplayer_pool_indices=(0,), ldplayer_state_dir=tmp_path / 'state')
        client = runtime._redis_queue._client
        body = task_body(ttl=1 if interruption == 'timeout' else 60)
        await enqueue(client, runtime, body, session_fields={'userId': USER})
        record = await runtime._redis_queue.pop()
        runtime._tasks[record.id] = record
        runtime._manager = RecordingPool(record.id)
        boundary = DatabaseBoundary()
        monkeypatch.setattr(module().psycopg, 'connect', boundary.connect)
        started = emit({'type': 'capture_started'}) if helper else emit({'type': 'stage', 'status': 'opening_game'})
        ending = ([emit(QR), emit(AUTHORIZED), emit(LOGGED_IN), emit(COMPLETED)] if interruption == 'success'
                  else [emit({'type': 'error', 'code': 'login_failed'})] if interruption == 'failure'
                  else [gate(tmp_path / 'never')])
        message = {'captureUserId': USER, 'captureAttemptId': record.attempt_id,
                   'plan': [started, emit({'type': 'stage', 'status': 'preparing_qr'}), *ending]}
        program = CHILD_PROGRAM
        if interruption == 'buffered':
            # The parent is handling the prior stage while the child flushes its
            # legacy marker. Cancellation must not drain it to revoke anything.
            marker = tmp_path / 'marker-flushed'
            message['marker'] = str(marker)
            program = '''
import json, pathlib, sys, time
message = json.loads(sys.stdin.readline())
print(json.dumps({'type': 'stage', 'status': 'opening_game'}), flush=True)
print(json.dumps({'type': 'capture_started'}), flush=True)
pathlib.Path(message['marker']).touch()
time.sleep(60)
'''
            original_set_status = runtime._set_status
            async def blocked_status(record, status):
                await original_set_status(record, status)
                if status == 'opening_game':
                    await asyncio.Event().wait()
            monkeypatch.setattr(runtime, '_set_status', blocked_status)
        work = asyncio.create_task(runtime._run_child(record, [sys.executable, '-u', '-c', program], message, daily=True))
        try:
            if interruption not in ('success', 'failure', 'timeout'):
                if interruption == 'buffered':
                    await eventually(lambda: record.status == 'opening_game' and marker.exists() or work.done())
                else:
                    await eventually(lambda: record.status == 'preparing_qr' or work.done())
                assert not work.done(), 'Private capture marker must be accepted without public state mutation'
                if interruption == 'admin':
                    runtime._on_run_interrupted(record.id, record.attempt_id)
                elif interruption == 'shutdown':
                    runtime._workers = [work]
                    await asyncio.wait_for(runtime.stop(), 3)
                else:
                    work.cancel()
            results = await asyncio.wait_for(asyncio.gather(work, return_exceptions=True), 3)
            assert runtime._children[record.id].poll() is not None
            assert boundary.connections == [] and boundary.queries == []
            if interruption in ('cancel', 'buffered'):
                assert isinstance(results[0], asyncio.CancelledError)
            elif interruption == 'success':
                assert results == [b'\x89PNG\r\n\x1a\nfixture']
                assert record.daily_status == 'completed'
            elif interruption == 'failure':
                assert getattr(results[0], 'code', None) == 'login_failed'
            elif interruption == 'timeout':
                assert isinstance(results[0], TimeoutError)
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await runtime.stop()
    asyncio.run(scenario())
