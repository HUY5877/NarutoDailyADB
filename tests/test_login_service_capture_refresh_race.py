"""A completed scan must survive a refresh arriving at either consume boundary."""

from datetime import datetime, timezone
import sqlite3

import pytest

from naruto_daily.login_service import qq_capture as capture


USER = '11111111-1111-4111-8111-111111111111'
TASK = '22222222-2222-4222-8222-222222222222'
ATTEMPT = 'a' * 32
CALLBACK = 'auth://callback?access_token=fixture-only'
REQUEST = '33333333-3333-4333-8333-333333333333'


class CaptureDatabase:
    """Execute store statements locally, controlling the upload interleaving.

    Only PostgreSQL placeholders and clock functions are adapted. WHERE clauses
    and UPDATE effects are executed as written by the production store.
    """

    def __init__(self, clock):
        self.clock = clock
        self.after_empty_consume = None
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute("ATTACH DATABASE ':memory:' AS naruto_login")
        self.connection.execute('''
            CREATE TABLE naruto_login.qq_capture_requests (
                task_id TEXT, owner_id TEXT, attempt_id TEXT, capture_id TEXT,
                capture_token_hash TEXT, login_type TEXT, expires_at REAL,
                status TEXT DEFAULT 'pending', callback_url TEXT, captured_at REAL,
                authorization_expires_at REAL, authorization_hash TEXT, updated_at REAL,
                UNIQUE(task_id, attempt_id)
            )
        ''')
        self.connection.create_function('clock_timestamp', 0, lambda: self.clock[0])
        self.connection.create_function('now', 0, lambda: self.clock[0])

    def query(self, query, params):
        is_consume = "SET status = 'consumed'" in query
        params = tuple(value.timestamp() if isinstance(value, datetime) else value for value in params)
        row = self.connection.execute(query.replace('%s', '?'), params).fetchone()
        if is_consume:
            if row is not None:
                return row[0], datetime.fromtimestamp(row[1], timezone.utc) if row[1] is not None else None
            if self.after_empty_consume is not None:
                upload, self.after_empty_consume = self.after_empty_consume, None
                upload()
        return row

    def upload(self, **changes):
        fields = dict(status='completed', callback_url=CALLBACK, captured_at=100.,
                      authorization_expires_at=101.9)
        fields.update(changes)
        assignments = ', '.join(f'{column} = ?' for column in fields)
        self.connection.execute(f'UPDATE naruto_login.qq_capture_requests SET {assignments}', tuple(fields.values()))

    def saved(self):
        return self.connection.execute('''
            SELECT capture_id, status, callback_url FROM naruto_login.qq_capture_requests
        ''').fetchone()


class RefreshRequest:
    def __init__(self):
        self.polled = 0

    def poll(self):
        self.polled += 1
        return REQUEST if self.polled == 1 else None


@pytest.fixture
def session_boundary(monkeypatch):
    clock = [100.]
    database = CaptureDatabase(clock)
    app_calls = []
    complete_on_refresh = [False]

    class Provider:
        def __init__(self, device, authority):
            self.kind = 'capture' if authority == 'com.huy.authcapture.automation' else 'login'
            self.current = None

        def call(self, method, argument=None):
            if method == 'capabilities':
                return {'protocol': 1, 'kind': self.kind}
            app_calls.append(method)
            if method in ('begin', 'refresh'):
                self.current = argument['captureId']
                if method == 'refresh' and complete_on_refresh[0]:
                    database.upload()
            return {'captureId': self.current, 'state': 'qr_ready', 'qrRevision': 1}

        def qr(self, capture_id):
            assert capture_id == self.current
            return b'\x89PNG\r\n\x1a\nfixture'

    monkeypatch.setattr(capture.CaptureStore, '_query', lambda self, query, params: database.query(query, params))
    monkeypatch.setattr(capture, 'AutomationProvider', Provider)
    monkeypatch.setattr(capture.psycopg, 'connect', lambda *args, **kwargs: pytest.fail('No real database access'))
    device = type('Device', (), {'deadline': 102., 'login_type': 'android_qq'})()
    session = capture.QQCaptureSession(device, user_id=USER, task_id=TASK, attempt_id=ATTEMPT,
        database_url='postgresql://fixture.invalid/local', api_base='https://example.test',
        clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    session.prepare()
    yield session, database, clock, app_calls, complete_on_refresh
    database.connection.close()


def test_completed_scan_is_consumed_before_a_pending_refresh(session_boundary):
    session, database, _, app_calls, _ = session_boundary
    original = session.current
    database.upload()
    refresh, published = RefreshRequest(), []

    result = session.wait(refresh_requests=refresh, publish=published.append)

    assert result.callback_url == CALLBACK
    assert session.current == original
    assert database.saved() == (original.capture_id, 'consumed', CALLBACK)
    assert refresh.polled == 0
    assert 'refresh' not in app_calls
    assert published == []


def test_upload_after_empty_consume_survives_atomic_refresh(session_boundary):
    session, database, _, app_calls, _ = session_boundary
    original = session.current
    database.after_empty_consume = database.upload
    published = []

    result = session.wait(refresh_requests=RefreshRequest(), publish=published.append)

    assert result.callback_url == CALLBACK
    assert session.current == original
    assert database.saved() == (original.capture_id, 'consumed', CALLBACK)
    assert 'refresh' not in app_calls
    assert published == []


def test_store_refresh_never_overwrites_a_completed_upload(session_boundary):
    session, database, _, _, _ = session_boundary
    original = session.current
    database.upload()

    with pytest.raises(capture.CaptureError):
        session.store.begin(previous=original)

    assert database.saved() == (original.capture_id, 'completed', CALLBACK)


def test_pending_scan_still_refreshes_and_publishes_the_new_qr(session_boundary):
    session, database, clock, app_calls, complete_on_refresh = session_boundary
    original = session.current
    complete_on_refresh[0] = True
    published = []

    result = session.wait(refresh_requests=RefreshRequest(), publish=published.append)

    assert result.callback_url == CALLBACK
    assert session.current != original
    assert database.saved() == (session.current.capture_id, 'consumed', CALLBACK)
    assert app_calls.count('refresh') == 1
    assert published == [
        {'type': 'refreshing', 'requestId': REQUEST},
        {'type': 'qr', 'requestId': REQUEST, 'png': 'iVBORw0KGgpmaXh0dXJl'},
    ]
    assert session.device.deadline == 102.
    assert clock[0] < 102.


@pytest.mark.parametrize('invalid', [
    {'authorization_expires_at': 99.}, {'expires_at': 99.},
    {'attempt_id': 'b' * 32}, {'owner_id': TASK}, {'task_id': USER},
    {'capture_id': USER}, {'callback_url': ''}, {'captured_at': None},
])
def test_refresh_conflict_never_accepts_an_invalid_or_other_attempt_upload(session_boundary, invalid):
    session, database, clock, app_calls, _ = session_boundary
    original = session.current
    database.after_empty_consume = lambda: database.upload(**invalid)
    published = []

    with pytest.raises(capture.CaptureError):
        session.wait(refresh_requests=RefreshRequest(), publish=published.append)

    assert session.current == original
    assert 'refresh' not in app_calls
    assert published == []
    assert clock[0] == 100.


def test_pending_refresh_does_not_extend_the_task_deadline(session_boundary):
    session, _, clock, _, _ = session_boundary
    published = []

    with pytest.raises(TimeoutError):
        session.wait(refresh_requests=RefreshRequest(), publish=published.append)

    assert clock[0] == 102.
    assert session.device.deadline == 102.
    assert [event['type'] for event in published] == ['refreshing', 'qr']
