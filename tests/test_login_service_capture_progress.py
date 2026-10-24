"""Capture delivery is visible before game authorization, without extending login time."""

import asyncio
import json
import sys
import time

import pytest

from test_login_service_worker import worker, fake_server, MemoryStorage, task_body, enqueue
from test_login_service_daily_runtime import CHILD_PROGRAM, QR, AUTHORIZED, LOGGED_IN, COMPLETED, emit, gate, eventually
from test_login_service_qq_capture import USER, DATABASE


async def capture_runtime(tmp_path, monkeypatch, plan):
    runtime = worker(fake_server(), MemoryStorage(), emulator_driver='ldplayer', database_url=DATABASE,
                     ldplayer_state_dir=tmp_path / 'state', daily_timeout_seconds=30)
    client = runtime._redis_queue._client
    body = task_body() | {'loginTimeoutSeconds': 600}
    session_key, _, _ = await enqueue(client, runtime, body, session_fields={'userId': USER})
    await client.set(f'{runtime.config.redis_queue_key}:users:{USER}:active', body['id'], ex=600)
    record = await runtime._redis_queue.pop()
    runtime._tasks[record.id] = record
    runtime._run_reporter.start()
    async def revoke(*_):
        pass  # No live PostgreSQL is used by this protocol test.
    monkeypatch.setattr(runtime, '_revoke_capture_attempt', revoke)
    message = {'captureUserId': USER, 'captureAttemptId': record.attempt_id, 'plan': plan}
    return runtime, record, client, session_key, message


def test_capture_received_publishes_before_game_and_does_not_authorize_or_extend_deadline(tmp_path, monkeypatch):
    async def scenario():
        login_gate = tmp_path / 'allow-game-login'
        plan = [emit({'type': 'capture_started'}), emit(QR), emit({'type': 'capture_received'}),
                gate(login_gate), emit(AUTHORIZED), emit(LOGGED_IN), emit(COMPLETED)]
        runtime, record, client, session_key, message = await capture_runtime(tmp_path, monkeypatch, plan)
        original_expiry = record.expires_at_ms
        work = asyncio.create_task(runtime._run_child(record, [sys.executable, '-u', '-c', CHILD_PROGRAM], message, daily=True))
        try:
            await eventually(lambda: getattr(record, 'scan_captured_at', None) is not None or work.done())
            assert not work.done(), 'Scan receipt must be published while the game is still gated'
            await eventually(lambda: getattr(record, 'scan_captured_at', None) is not None)
            await runtime._run_reporter.flush(record.id)
            run = json.loads(await client.get(f'{runtime.config.redis_queue_key}:runs:{record.id}'))
            assert run['scanCapturedAt']
            assert run['status'] == 'waiting_scan'
            assert run.get('authorizedAt') is None and run.get('startedAt') is None
            assert record.authorized_at is None and record.daily_started_at is None
            assert record.execution_deadline is None and record.expires_at_ms == original_expiry
            session = json.loads(await client.get(session_key))
            assert session['scanCaptured'] is True and not session.get('loginAuthorized')
            local = json.loads((runtime.config.ldplayer_state_dir / 'runs' / f'{record.id}.json').read_text())
            assert local['scanCapturedAt'] == record.scan_captured_at
            assert local['deadline'] == original_expiry / 1000
            captured = record.scan_captured_at
            login_gate.touch()
            await asyncio.wait_for(work, 5)
            assert record.daily_status == 'completed'
            assert record.scan_captured_at == captured and record.authorized_at >= captured
        finally:
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await runtime.stop()
    asyncio.run(scenario())


def test_capture_received_does_not_extend_original_login_timeout(tmp_path, monkeypatch):
    async def scenario():
        plan = [emit({'type': 'capture_started'}), emit(QR), emit({'type': 'capture_received'}), gate(tmp_path / 'never')]
        runtime, record, _, _, message = await capture_runtime(tmp_path, monkeypatch, plan)
        record.expires_at_ms = (time.time() + 0.8) * 1000
        try:
            with pytest.raises(TimeoutError):
                await runtime._run_child(record, [sys.executable, '-u', '-c', CHILD_PROGRAM], message, daily=True)
            assert record.scan_captured_at is not None
            assert record.authorized_at is None and record.execution_deadline is None
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('events', [
    [QR, {'type': 'capture_received'}],
    [{'type': 'capture_started'}, {'type': 'capture_received'}],
    [{'type': 'capture_started'}, QR, {'type': 'capture_received'}, {'type': 'capture_received'}],
    [{'type': 'capture_started'}, QR, {'type': 'capture_received'}, QR],
    [{'type': 'capture_started'}, QR, {'type': 'capture_received'}, {'type': 'refreshing', 'requestId': USER}],
])
def test_unexpected_capture_or_refresh_after_capture_is_rejected(tmp_path, monkeypatch, events):
    async def scenario():
        runtime, record, _, _, message = await capture_runtime(tmp_path, monkeypatch, [emit(event) for event in events])
        try:
            with pytest.raises(ValueError):
                await runtime._run_child(record, [sys.executable, '-u', '-c', CHILD_PROGRAM], message, daily=True)
        finally:
            await runtime.stop()
    asyncio.run(scenario())
