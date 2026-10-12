"""End-of-task QQ messages follow real worker outcomes and independent session TTLs."""

import asyncio
import json
import logging
import time

import pytest

from naruto_daily.login_service import runtime as runtime_module
from naruto_daily.login_service.queue import RedisQueueError, RedisTaskQueue
from naruto_daily.login_service.run_logs import RedisRunReporter
from test_login_service_daily_runtime import (
    AUTHORIZED, COMPLETED, LOGGED_IN, QR, emit, eventually, gate, make_runtime,
    record_with_ttl, run_report, start_processing,
)
from test_login_service_qq_runtime import ready_runtime
from test_login_service_worker import enqueue, fake_client, fake_server


USER_ID = '11111111-1111-4111-8111-111111111111'
PRIVATE_DETAILS = 'private-qq-secret postgresql://private-db-password@private-db-host/db'


@pytest.fixture(autouse=True)
def no_failure_inspection_delay(monkeypatch):
    monkeypatch.setattr(runtime_module, 'FAILED_INSTANCE_HOLD_SECONDS', 0)


class Names:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.requested = []

    async def resolve(self, user_id):
        self.requested.append(user_id)
        if self.fail:
            raise RuntimeError(PRIVATE_DETAILS)
        return '雪花'


class Notifications:
    def __init__(self, *, fail=False, wait=None):
        self.fail, self.wait = fail, wait
        self.qr_messages = []
        self.attempts = []
        self.messages = []

    async def notify_qr_ready(self, task_id, user_name, *, refreshed=False, expires_at_ms=None):
        self.qr_messages.append((task_id, user_name, refreshed))

    async def notify_task_finished(self, task_id, user_name, *, status, error_code=None):
        message = (task_id, user_name, status, error_code)
        self.attempts.append(message)
        if self.wait is not None:
            await self.wait.wait()
        if self.fail:
            raise RuntimeError(PRIVATE_DETAILS)
        self.messages.append(message)

    async def close(self):
        pass


def enable_notifications(runtime, queue, *, names_fail=False, notify_fail=False, wait=None):
    names = Names(fail=names_fail)
    notifications = Notifications(fail=notify_fail, wait=wait)
    runtime._user_name_resolver, runtime._qq_notifier = names, notifications

    async def identity(record):
        return USER_ID if record.expires_at_ms > time.time() * 1000 else ''

    queue.notification_user_id = identity
    return names, notifications


async def drain_notifications(runtime):
    if runtime._notification_tasks:
        await asyncio.wait_for(asyncio.gather(*list(runtime._notification_tasks)), 3)


def test_completion_waits_for_successful_exit_and_instance_cleanup(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        exit_gate = tmp_path / 'allow-exit'
        completion_read, cleanup_started, cleanup_release = (asyncio.Event() for _ in range(3))
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit(COMPLETED),
            emit({'type': 'log', 'message': 'completion received'}), gate(exit_gate),
        ])
        names, notifications = enable_notifications(runtime, queue)
        monkeypatch.setattr(runtime, '_accept_log', lambda *_: completion_read.set())
        cleanup = runtime._cleanup_ldplayer

        async def wait_to_cleanup(task_id):
            cleanup_started.set()
            await cleanup_release.wait()
            return await cleanup(task_id)

        monkeypatch.setattr(runtime, '_cleanup_ldplayer', wait_to_cleanup)
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(completion_read.wait(), 3)
            child = runtime._children[record.id]
            assert child.poll() is None and not work.done()
            assert notifications.attempts == []
            exit_gate.touch()
            await asyncio.wait_for(cleanup_started.wait(), 3)
            assert child.poll() == 0
            assert pool.closed == [] and notifications.attempts == []
            cleanup_release.set()
            await asyncio.wait_for(work, 3)
            await drain_notifications(runtime)
            assert pool.closed == [record.id]
            assert record.id not in runtime._children
            assert notifications.messages == [(record.id, '雪花', 'completed', None)]
            assert names.requested == [USER_ID]
            assert record.username == '雪花'
        finally:
            cleanup_release.set()
            await runtime.stop()
    asyncio.run(scenario())


def test_completion_signal_followed_by_nonzero_exit_only_notifies_failure(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit(COMPLETED), {'exit': 7},
        ])
        _, notifications = enable_notifications(runtime, queue)
        try:
            await asyncio.wait_for(runtime._process_record(record), 3)
            await drain_notifications(runtime)
            assert record.daily_status == 'failed'
            assert pool.closed == [record.id]
            assert notifications.messages == [(record.id, '雪花', 'failed', 'protocol_error')]
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage,code', [
    ('before_child', 'unexpected_error'),
    ('before_qr', 'emulator_start_failed'),
    ('after_qr', 'login_failed'),
    ('daily', 'daily_failed'),
    ('bad_protocol', 'protocol_error'),
])
def test_failures_before_and_after_qr_notify_once_with_safe_codes(monkeypatch, tmp_path, stage, code, caplog):
    async def scenario():
        record = record_with_ttl()
        events = [] if stage in {'before_child', 'before_qr'} else [QR]
        if stage == 'daily':
            events.append(LOGGED_IN)
        events.append({'type': 'unknown'} if stage == 'bad_protocol' else {
            'type': 'error', 'code': code, 'message': PRIVATE_DETAILS,
        })
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [emit(event) for event in events])
        _, notifications = enable_notifications(runtime, queue)
        if stage == 'before_child':
            async def fail_start(_):
                raise RuntimeError(PRIVATE_DETAILS)
            monkeypatch.setattr(runtime, '_run_ldplayer', fail_start)
        try:
            await asyncio.wait_for(runtime._process_record(record), 3)
            await drain_notifications(runtime)
            assert pool.closed == [record.id]
            assert notifications.messages == [(record.id, '雪花', 'failed', code)]
            assert not queue.requeued
        finally:
            await runtime.stop()
    asyncio.run(scenario())
    assert 'private-qq-secret' not in caplog.text and 'private-db-password' not in caplog.text


@pytest.mark.parametrize('events,ttl,budget,code', [
    ([QR], .8, 5, 'login_timeout'),
    ([QR, AUTHORIZED], 5, .15, 'login_finish_timeout'),
    ([QR, LOGGED_IN], 5, .15, 'daily_timeout'),
])
def test_scan_login_finishing_and_daily_timeout_each_notify_once(monkeypatch, tmp_path, events, ttl, budget, code):
    async def scenario():
        record = record_with_ttl(ttl)
        runtime, _, queue, pool = make_runtime(
            tmp_path, monkeypatch, record,
            [emit(event) for event in events] + [gate(tmp_path / 'never-finish')], daily_timeout=budget,
        )
        _, notifications = enable_notifications(runtime, queue)
        work = start_processing(runtime, record)
        try:
            await asyncio.wait_for(work, 3)
            await drain_notifications(runtime)
            assert run_report(runtime, record)['errorCode'] == code
            assert pool.closed == [record.id] and not queue.requeued
            assert notifications.messages == [(record.id, '雪花', 'failed', code)]
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['completed', 'failed'])
@pytest.mark.parametrize('session_loss', ['deleted', 'expired'])
def test_verified_username_survives_redis_session_deletion_or_expiry(monkeypatch, tmp_path, outcome, session_loss):
    async def scenario():
        record = record_with_ttl()
        finish_gate = tmp_path / 'finish-after-session-loss'
        last_event = COMPLETED if outcome == 'completed' else {'type': 'error', 'code': 'daily_failed'}
        runtime, _, _, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), gate(finish_gate), emit(last_event),
        ])
        client = fake_client(fake_server())
        queue = RedisTaskQueue(runtime.config, client=client)
        runtime._redis_queue = queue
        names, notifications = Names(), Notifications()
        runtime._user_name_resolver, runtime._qq_notifier = names, notifications
        body = {
            'version': 1, 'id': record.id, 'createdAt': record.created_at, 'expiresAt': record.expires_at,
            'objectKey': f'{runtime.config.key_prefix}/{record.id}/qr.png',
        }
        key, _, _ = await enqueue(client, runtime, body, session_fields={'userId': USER_ID})
        record = await queue.pop()
        runtime._tasks[record.id] = record
        runtime._run_reporter = RedisRunReporter(runtime.config, queue)
        runtime._run_reporter.start()
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.daily_started_at is not None and bool(notifications.qr_messages))
            await runtime._run_reporter.flush(record.id)
            run_key = f'{runtime.config.redis_queue_key}:runs:{record.id}'
            assert json.loads(await client.get(run_key))['username'] == '雪花'
            assert await queue.notification_user_id(record) == USER_ID
            if session_loss == 'deleted':
                await client.delete(key)
            else:
                await client.pexpire(key, 1)
                async with asyncio.timeout(3):
                    while await client.exists(key):
                        await asyncio.sleep(.01)
            assert await queue.notification_user_id(record) == ''
            assert USER_ID not in repr(record) and USER_ID not in str(record.to_view())
            finish_gate.touch()
            await asyncio.wait_for(work, 3)
            await drain_notifications(runtime)
            await runtime._run_reporter.flush(record.id)
            error_code = None if outcome == 'completed' else 'daily_failed'
            assert notifications.messages == [(record.id, '雪花', outcome, error_code)]
            assert names.requested == [USER_ID]
            report = json.loads(await client.get(run_key))
            assert report['status'] == outcome and report['username'] == '雪花'
            assert pool.closed == [record.id]
            assert await client.get(key) is None, 'Notification must never recreate an expired login session'
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_foreign_worker_session_identity_cannot_notify(monkeypatch):
    async def scenario():
        runtime, record, names, _, _ = await ready_runtime()
        notifications = Notifications()
        runtime._qq_notifier = notifications
        runtime._redis_queue.owner = 'different-worker'
        try:
            await runtime._process_record(record)
            await drain_notifications(runtime)
            assert names.requested == []
            assert notifications.attempts == [] and notifications.qr_messages == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('action,stage,cleanup_fails', [
    ('stop', 'before_qr', False),
    ('stop', 'waiting_scan', False),
    ('stop', 'running_daily', False),
    ('stop', 'running_daily', True),
    ('cancel', 'before_qr', False),
    ('cancel', 'running_daily', False),
    ('cancel', 'running_daily', True),
])
def test_shutdown_or_cancellation_never_notifies_even_if_cleanup_fails(monkeypatch, tmp_path, action, stage, cleanup_fails):
    async def scenario():
        record = record_with_ttl()
        events = [] if stage == 'before_qr' else [QR]
        if stage == 'running_daily':
            events.append(LOGGED_IN)
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record,
            [emit(event) for event in events] + [gate(tmp_path / 'wait-for-stop')])
        _, notifications = enable_notifications(runtime, queue)
        if cleanup_fails:
            def cannot_close(_):
                raise RuntimeError(PRIVATE_DETAILS)
            monkeypatch.setattr(pool, 'close', cannot_close)
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.id in runtime._children if stage == 'before_qr'
                             else record.daily_status == stage)
            child = runtime._children[record.id]
            if action == 'stop':
                await asyncio.wait_for(runtime.stop(), 3)
            else:
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(work, 3)
                await drain_notifications(runtime)
            assert child.poll() is not None
            assert notifications.attempts == []
            if cleanup_fails:
                assert record.daily_error_code == 'instance_cleanup_failed'
            else:
                assert record.daily_status == 'interrupted'
            assert queue.requeued == ([record.id] if stage == 'before_qr' else [])
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_cleanup_failure_overrides_completed_result_before_notification(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit(COMPLETED),
        ])
        _, notifications = enable_notifications(runtime, queue)

        def cannot_close(_):
            raise RuntimeError(PRIVATE_DETAILS)

        monkeypatch.setattr(pool, 'close', cannot_close)
        try:
            await asyncio.wait_for(runtime._process_record(record), 3)
            await drain_notifications(runtime)
            assert run_report(runtime, record)['errorCode'] == 'instance_cleanup_failed'
            assert notifications.messages == [(record.id, '雪花', 'failed', 'instance_cleanup_failed')]
            assert not queue.failed_sessions
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['identity', 'database', 'qq'])
def test_notification_dependencies_cannot_fail_the_task_or_leak_secrets(monkeypatch, tmp_path, failure, caplog):
    async def scenario():
        record = record_with_ttl()
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit(COMPLETED),
        ])
        _, notifications = enable_notifications(runtime, queue,
            names_fail=failure == 'database', notify_fail=failure == 'qq')
        if failure == 'identity':
            async def unavailable(_):
                raise RuntimeError(PRIVATE_DETAILS)
            queue.notification_user_id = unavailable
        try:
            await asyncio.wait_for(runtime._process_record(record), 3)
            await drain_notifications(runtime)
            assert record.daily_status == 'completed'
            assert pool.closed == [record.id] and not queue.requeued
            assert notifications.messages == []
            assert len(notifications.attempts) == (1 if failure == 'qq' else 0)
        finally:
            await runtime.stop()
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert 'QQ' in caplog.text and 'RuntimeError' in caplog.text
    assert 'private-qq-secret' not in caplog.text
    assert 'private-db-password' not in caplog.text and 'private-db-host' not in caplog.text


def test_pending_failure_session_retry_and_repeated_finish_do_not_repeat_message(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit({'type': 'error', 'code': 'daily_failed'}),
        ])
        _, notifications = enable_notifications(runtime, queue)
        finish, attempts = queue.finish_failed, []

        async def unavailable_once(current):
            attempts.append(current.id)
            if len(attempts) == 1:
                raise RedisQueueError('Redis unavailable')
            return await finish(current)

        queue.finish_failed = unavailable_once
        try:
            await asyncio.wait_for(runtime._process_record(record), 3)
            await drain_notifications(runtime)
            assert record.id in runtime._pending_session_failures
            await runtime._sweep()
            await runtime._sweep()
            runtime._notify_task_finished(record, 'failed')
            runtime._notify_task_finished(record, 'failed')
            await drain_notifications(runtime)
            assert attempts == [record.id, record.id]
            assert queue.failed_sessions == [record.id]
            assert pool.closed == [record.id]
            assert not runtime._pending_session_failures
            assert notifications.messages == [(record.id, '雪花', 'failed', 'daily_failed')]
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_slow_terminal_notification_does_not_hold_the_consumer(monkeypatch, tmp_path):
    async def scenario():
        record = record_with_ttl()
        runtime, _, queue, pool = make_runtime(tmp_path, monkeypatch, record, [
            emit(QR), emit(LOGGED_IN), emit(COMPLETED),
        ])
        release = asyncio.Event()
        _, notifications = enable_notifications(runtime, queue, wait=release)
        try:
            await asyncio.wait_for(runtime._process_record(record), 3)
            await eventually(lambda: bool(notifications.attempts))
            assert pool.closed == [record.id] and pool.has_capacity()
            assert notifications.messages == []
            release.set()
            await drain_notifications(runtime)
            assert notifications.messages == [(record.id, '雪花', 'completed', None)]
        finally:
            release.set()
            await runtime.stop()
    asyncio.run(scenario())


def test_mock_qr_publication_does_not_announce_daily_completion(monkeypatch):
    async def scenario():
        runtime, record, _, _, _ = await ready_runtime()
        notifications = Notifications()
        runtime._qq_notifier = notifications

        async def emulator(_):
            return b'mock-png'

        monkeypatch.setattr(runtime, '_run_mock', emulator)
        try:
            await runtime._process_record(record)
            await drain_notifications(runtime)
            assert record.qr_published and record.daily_started_at is None
            assert notifications.qr_messages == [(record.id, '雪花', False)]
            assert notifications.attempts == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())
