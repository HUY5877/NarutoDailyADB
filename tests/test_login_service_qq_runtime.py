"""QR notifications must follow publication and never interfere with login."""

import asyncio
import logging

import pytest

from naruto_daily.login_service.config import load_config
from naruto_daily.login_service.runtime import LoginRuntime
from oss_fakes import OSS_ENV
from test_login_service_worker import MemoryStorage, worker, fake_server, enqueue, task_body


QQ_ENV = {
    'TEST_JEV_QQ_BOT_APP_ID': '1900000001', 'TEST_JEV_QQ_BOT_APP_SECRET': 'private-qq-secret',
    'TEST_JEV_QQ_BOT_GROUP_OPENID': 'A' * 32, 'TEST_JEV_DATABASE_URL': 'postgresql://user:private-db@db.invalid/naruto',
}


def test_qq_configuration_requires_complete_credentials_and_database():
    config = load_config(OSS_ENV | QQ_ENV)
    assert config.qq_bot_app_id == '1900000001'
    assert config.qq_bot_group_openid == 'A' * 32
    assert config.database_url == QQ_ENV['TEST_JEV_DATABASE_URL']
    assert 'private-qq-secret' not in repr(config)
    assert 'private-db' not in repr(config)
    assert not load_config(OSS_ENV).qq_bot_app_id
    for key in QQ_ENV:
        with pytest.raises(ValueError, match=key):
            load_config(OSS_ENV | {name: value for name, value in QQ_ENV.items() if name != key})


class Names:
    def __init__(self):
        self.requested = []

    async def resolve(self, user_id):
        self.requested.append(user_id)
        return '雪花'


class Notifications:
    def __init__(self, storage, *, fail=False, gate=None):
        self.storage, self.fail, self.gate = storage, fail, gate
        self.messages = []

    async def notify_qr_ready(self, task_id, user_name, *, refreshed=False, expires_at_ms=None):
        assert task_id in self.storage.objects
        if self.gate is not None:
            await self.gate.wait()
        self.messages.append((task_id, user_name, refreshed))
        if self.fail:
            raise RuntimeError('private-qq-secret private-db')

    async def close(self):
        pass


async def ready_runtime(*, fail_put=False, fail_notify=False, gate=None):
    storage = MemoryStorage(fail_put=fail_put)
    runtime = worker(fake_server(), storage)
    names, notifications = Names(), Notifications(storage, fail=fail_notify, gate=gate)
    runtime = LoginRuntime(runtime.config, storage, redis_queue=runtime._redis_queue,
                           qq_notifier=notifications, user_name_resolver=names)
    body = task_body()
    user_id = '11111111-1111-4111-8111-111111111111'
    await enqueue(runtime._redis_queue._client, runtime, body, session_fields={'userId': user_id})
    record = await runtime._redis_queue.pop()
    return runtime, record, names, notifications, user_id


def test_successful_publication_sends_database_name_once():
    async def scenario():
        runtime, record, names, notifications, user_id = await ready_runtime()
        try:
            await runtime._publish_qr(record, b'png')
            await runtime._publish_qr(record, b'png')
            await runtime.stop()
            assert notifications.messages == [(record.id, '雪花', False)]
            assert names.requested == [user_id]
            assert record.status == 'qr_ready'
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_failed_upload_never_sends_notification():
    async def scenario():
        runtime, record, names, notifications, _ = await ready_runtime(fail_put=True)
        try:
            with pytest.raises(RuntimeError):
                await runtime._publish_qr(record, b'png')
            await runtime.stop()
            assert notifications.messages == []
            assert names.requested == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_qq_failure_does_not_fail_published_qr_or_expose_secrets(caplog):
    async def scenario():
        runtime, record, _, notifications, _ = await ready_runtime(fail_notify=True)
        try:
            await runtime._publish_qr(record, b'png')
            await runtime.stop()
            assert record.status == 'qr_ready' and record.qr_published
            assert record.id in runtime.storage.objects
            assert notifications.messages == [(record.id, '雪花', False)]
        finally:
            await runtime.stop()
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert 'QQ' in caplog.text
    assert 'private-qq-secret' not in caplog.text and 'private-db' not in caplog.text


def test_slow_notification_does_not_delay_qr_publication():
    async def scenario():
        gate = asyncio.Event()
        runtime, record, _, notifications, _ = await ready_runtime(gate=gate)
        try:
            await asyncio.wait_for(runtime._publish_qr(record, b'png'), .5)
            assert record.status == 'qr_ready'
            gate.set()
            await runtime.stop()
            assert notifications.messages == [(record.id, '雪花', False)]
        finally:
            gate.set()
            await runtime.stop()
    asyncio.run(scenario())


def test_mock_publication_also_notifies(monkeypatch):
    async def scenario():
        runtime, record, _, notifications, _ = await ready_runtime()
        async def emulator(record):
            return b'mock-png'
        monkeypatch.setattr(runtime, '_run_mock', emulator)
        try:
            await runtime._process_record(record)
            await runtime.stop()
            assert notifications.messages == [(record.id, '雪花', False)]
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_notification_identity_is_owner_checked():
    async def scenario():
        runtime, record, _, _, user_id = await ready_runtime()
        try:
            queue = runtime._redis_queue
            assert await queue.notification_user_id(record) == user_id
            queue.owner = 'different-worker'
            assert await queue.notification_user_id(record) == ''
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_each_completed_refresh_notifies_once():
    async def scenario():
        runtime, record, _, notifications, _ = await ready_runtime()
        try:
            await runtime._publish_qr(record, b'first-png')
            client = runtime._redis_queue._client
            import json
            key = runtime._redis_queue._session_key(record)
            session = json.loads(await client.get(key))
            request_id = '22222222-2222-4222-8222-222222222222'
            session['refreshRequestId'] = request_id
            await client.set(key, json.dumps(session), keepttl=True)
            await client.set(f'{runtime.config.redis_queue_key}:refresh:{record.id}',
                             json.dumps({'taskId': record.id, 'requestId': request_id}), ex=60)
            await runtime._publish_qr(record, b'refreshed-png', refresh_request_id=request_id)
            await runtime._publish_qr(record, b'refreshed-png', refresh_request_id=request_id)
            await runtime.stop()
            assert sorted(notifications.messages) == [(record.id, '雪花', False), (record.id, '雪花', True)]
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_database_failure_preserves_qr_and_never_sends_a_fake_name(caplog):
    async def scenario():
        runtime, record, names, notifications, _ = await ready_runtime()
        async def failed_lookup(user_id):
            raise RuntimeError('private-db')
        names.resolve = failed_lookup
        try:
            await runtime._publish_qr(record, b'png')
            await runtime.stop()
            assert record.status == 'qr_ready'
            assert notifications.messages == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())
    assert 'private-db' not in caplog.text


def test_qr_that_expires_during_name_lookup_is_not_announced():
    async def scenario():
        runtime, record, names, notifications, _ = await ready_runtime()
        async def lookup(user_id):
            record.expires_at_ms = 0
            return '雪花'
        names.resolve = lookup
        try:
            await runtime._publish_qr(record, b'png')
            await runtime.stop()
            assert notifications.messages == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())
