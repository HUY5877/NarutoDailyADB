"""A completed scan closes QR refresh before the game finishes logging in."""

import asyncio
from io import StringIO
import json
import sys
from uuid import uuid4

import pytest

from naruto_daily.login_service import ldplayer_worker
from naruto_daily.login_service.queue import RedisQueueError
from test_login_service_daily_runtime import (
    AUTHORIZED, CHILD_PROGRAM, COMPLETED, LOGGED_IN, QR, RecordingPool,
    emit, eventually, gate,
)
from test_login_service_ldplayer_worker import PNG, daily_dependencies, request
from test_login_service_qq_capture import ATTEMPT, DATABASE, USER
from test_login_service_worker import MemoryStorage, enqueue, fake_server, task_body, worker


def test_worker_notifies_completed_scan_before_opening_game(monkeypatch, tmp_path):
    calls = []
    output = StringIO()
    dependencies = list(daily_dependencies(calls))

    class Manager(dependencies[0]):
        def capture_apps_installed(self, instance, deadline):
            return True

    class Capture:
        def __init__(self, *args, **kwargs): pass
        def prepare(self): return PNG
        def wait(self, **kwargs): return object()
        def prepare_login(self):
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            assert events[-1] == {'type': 'scan_captured'}
            assert not any(event['type'] == 'authorized' for event in events)
            raise RuntimeError('game launch test failure')

    class Daily:
        def __init__(self, **kwargs): pass

    dependencies[0] = Manager
    monkeypatch.setattr(ldplayer_worker, '_load_dependencies', lambda: tuple(dependencies))
    monkeypatch.setattr(ldplayer_worker, '_load_capture_session', lambda: Capture)
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: Daily)
    code = ldplayer_worker.main(
        input_stream=StringIO(json.dumps(request(
            captureUserId=USER, captureAttemptId=ATTEMPT,
            dailyConfigPath=str(tmp_path / 'daily.json'), dailyTimeoutSeconds=600,
        )) + '\n'), output_stream=output, clock=lambda: 1_800_000_000., use_watchdog=False,
    )
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert code == 1
    assert {'type': 'scan_captured'} in events
    assert {'type': 'authorized'} not in events
    assert events[-1] == {'type': 'error', 'code': 'login_failed'}
    assert 'launch' not in calls


@pytest.mark.parametrize('success', [True, False])
@pytest.mark.parametrize('redis_unavailable', [False, True])
def test_scan_progress_reaches_website_while_game_is_still_opening(
    monkeypatch, tmp_path, success, redis_unavailable,
):
    async def scenario():
        runtime = worker(fake_server(), MemoryStorage(), emulator_driver='ldplayer',
            database_url=DATABASE, ldplayer_console_path=tmp_path / 'console.exe',
            ldplayer_pool_indices=(0,), ldplayer_state_dir=tmp_path / 'state', daily_timeout_seconds=30)
        runtime._user_name_resolver = None
        client = runtime._redis_queue._client
        body = task_body() | {'loginTimeoutSeconds': 600}
        session_key, _, _ = await enqueue(client, runtime, body, session_fields={'userId': USER})
        await client.set(f'{runtime.config.redis_queue_key}:users:{USER}:active', body['id'], ex=600)
        record = await runtime._redis_queue.pop()
        runtime._tasks[record.id] = record
        runtime._manager = pool = RecordingPool(record.id)
        deadline = record.expires_at_ms
        scanned, entered_game = tmp_path / 'scanned', tmp_path / 'entered-game'
        ending = ([emit(AUTHORIZED), emit(LOGGED_IN), emit(COMPLETED)] if success else
                  [emit({'type': 'error', 'code': 'login_failed'})])

        async def run_child(current):
            return await runtime._run_child(current, [sys.executable, '-u', '-c', CHILD_PROGRAM], {
                'captureUserId': USER, 'captureAttemptId': record.attempt_id,
                'plan': [emit(QR), gate(scanned), emit({'type': 'scan_captured'}), gate(entered_game), *ending],
            }, daily=True)

        monkeypatch.setattr(runtime, '_run_ldplayer', run_child)
        monkeypatch.setattr('naruto_daily.login_service.runtime.FAILED_INSTANCE_HOLD_SECONDS', 0)
        if redis_unavailable:
            async def unavailable(_):
                raise RedisQueueError('temporary test outage')
            monkeypatch.setattr(runtime._redis_queue, 'mark_scan_captured', unavailable, raising=False)
        runtime._run_reporter.start()
        work = asyncio.create_task(runtime._process_record(record))
        try:
            await eventually(lambda: record.qr_published)
            # A click already in flight must be cleared without replacing the QR.
            request_id = str(uuid4())
            refresh_key = f'{runtime.config.redis_queue_key}:refresh:{record.id}'
            session = json.loads(await client.get(session_key))
            session.update(status='preparing_qr', refreshRequestId=request_id)
            await client.set(session_key, json.dumps(session), keepttl=True)
            await client.set(refresh_key, json.dumps({'taskId': record.id, 'requestId': request_id}), ex=600)
            scanned.touch()
            await eventually(lambda: record.daily_status == 'opening_game' or work.done())
            assert record.daily_status == 'opening_game'
            assert not work.done()
            assert record.authorized_at is None and record.daily_started_at is None
            assert record.expires_at_ms == deadline and record.execution_deadline is None
            assert runtime.storage.put_ids == [record.id]
            assert await runtime._run_reporter.flush(record.id)
            run = json.loads(await client.get(f'{runtime.config.redis_queue_key}:runs:{record.id}'))
            assert run['scanCapturedAt'].endswith('Z')
            assert run['status'] == 'opening_game' and 'authorizedAt' not in run
            session = json.loads(await client.get(session_key))
            assert not session.get('loginAuthorized')
            if not redis_unavailable:
                assert session['scanCaptured'] is True and session['status'] == 'qr_ready'
                assert 'refreshRequestId' not in session and await client.get(refresh_key) is None
            entered_game.touch()
            await asyncio.wait_for(work, 5)
            assert record.daily_status == ('completed' if success else 'failed')
            assert (record.authorized_at is not None) == success
            assert runtime.storage.put_ids == [record.id]
            assert await client.get(refresh_key) is None
            assert pool.closed == [record.id]
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['before_qr', 'unowned', 'duplicate', 'after_authorized', 'qr_after_scan'])
def test_scan_protocol_rejects_out_of_order_or_unowned_notifications(monkeypatch, tmp_path, case):
    async def scenario():
        runtime = worker(fake_server(), MemoryStorage(), emulator_driver='ldplayer',
            database_url=DATABASE, ldplayer_state_dir=tmp_path / 'state')
        client = runtime._redis_queue._client
        await enqueue(client, runtime, task_body(), session_fields={'userId': USER})
        record = await runtime._redis_queue.pop()
        scanned = {'type': 'scan_captured'}
        plans = {
            'before_qr': [scanned],
            'unowned': [QR, scanned],
            'duplicate': [QR, scanned, scanned],
            'after_authorized': [QR, AUTHORIZED, scanned],
            'qr_after_scan': [QR, scanned, QR],
        }
        message = {'captureUserId': USER, 'captureAttemptId': record.attempt_id,
                   'plan': [emit(event) for event in plans[case]]}
        if case == 'unowned':
            message.pop('captureAttemptId')
        try:
            with pytest.raises(ValueError):
                await runtime._run_child(record, [sys.executable, '-u', '-c', CHILD_PROGRAM], message, daily=True)
            if case in ('before_qr', 'unowned', 'after_authorized'):
                assert record.scan_captured_at is None
            assert runtime.storage.put_ids == ([] if case == 'before_qr' else [record.id])
        finally:
            await runtime.stop()
    asyncio.run(scenario())
