"""QQ mentions use scoped bindings and re-read them for each notification."""

import asyncio
import traceback

import pytest

from test_login_service_user_names import DatabaseBoundary, DATABASE_URL, USER_ID
from test_login_service_qq_notify import (
    QQTransport, TASK_ID, make_notifier, request_json, token_response,
)
from test_login_service_qq_runtime import ready_runtime


APP_ID = '1900000001'
GROUP = 'A' * 32
MEMBER = 'B' * 32


def test_binding_query_is_scoped_and_read_only(monkeypatch):
    from naruto_daily.login_service.qq_bindings import QQBindingResolver
    import naruto_daily.login_service.qq_bindings as module

    boundary = DatabaseBoundary((MEMBER,))
    monkeypatch.setattr(module.psycopg, 'connect', boundary.connect)
    resolver = QQBindingResolver(DATABASE_URL, APP_ID, GROUP)
    assert asyncio.run(resolver.resolve(USER_ID.upper())) == MEMBER
    _, options, _, connection = boundary.connections[0]
    query, values, read_only = connection.queries[0]
    assert values == (USER_ID, APP_ID, GROUP)
    assert 'user_id = %s' in query and 'bot_app_id = %s' in query and 'group_openid = %s' in query
    assert 'naruto_login.qq_bindings' in query
    assert 'is_invalid = false' in query
    assert read_only and connection.closed
    assert options['connect_timeout'] == 5 and 'statement_timeout=5000' in options['options']


@pytest.mark.parametrize('row', [None, (None,), ('',), ('<qqbot-at-everyone />',), ('x' * 129,), (123,)])
def test_missing_or_malformed_binding_cannot_create_a_mention(monkeypatch, row):
    from naruto_daily.login_service.qq_bindings import QQBindingResolver
    import naruto_daily.login_service.qq_bindings as module

    boundary = DatabaseBoundary(row)
    monkeypatch.setattr(module.psycopg, 'connect', boundary.connect)
    assert asyncio.run(QQBindingResolver(DATABASE_URL, APP_ID, GROUP).resolve(USER_ID)) == ''


def test_binding_errors_hide_connection_secrets(monkeypatch):
    from naruto_daily.login_service.qq_bindings import QQBindingResolver, QQBindingLookupError
    import naruto_daily.login_service.qq_bindings as module

    boundary = DatabaseBoundary(failure='execute')
    monkeypatch.setattr(module.psycopg, 'connect', boundary.connect)
    with pytest.raises(QQBindingLookupError) as caught:
        asyncio.run(QQBindingResolver(DATABASE_URL, APP_ID, GROUP).resolve(USER_ID))
    assert DATABASE_URL not in ''.join(traceback.format_exception(caught.value))
    assert boundary.connections[0][3].closed


def test_invalid_user_identity_never_queries_bindings(monkeypatch):
    from naruto_daily.login_service.qq_bindings import QQBindingResolver, QQBindingLookupError
    import naruto_daily.login_service.qq_bindings as module

    boundary = DatabaseBoundary((MEMBER,))
    monkeypatch.setattr(module.psycopg, 'connect', boundary.connect)
    with pytest.raises(QQBindingLookupError):
        asyncio.run(QQBindingResolver(DATABASE_URL, APP_ID, GROUP).resolve("' OR true --"))
    assert boundary.connections == []


@pytest.mark.parametrize('kind', ['qr', 'finished'])
def test_bound_notifications_use_real_markdown_mention(monkeypatch, kind):
    transport = QQTransport(token_response(), {'id': 'message'})
    _, notifier = make_notifier(monkeypatch, transport)
    if kind == 'qr':
        asyncio.run(notifier.notify_qr_ready(TASK_ID, '小鸣人', member_openid=MEMBER))
    else:
        asyncio.run(notifier.notify_task_finished(TASK_ID, '小鸣人', status='completed', member_openid=MEMBER))
    payload = request_json(transport.requests[-1][0])
    assert payload['msg_type'] == 2
    assert payload['markdown']['content'].startswith(f'<qqbot-at-user id="{MEMBER}" /> ')
    assert '小鸣人' in payload['markdown']['content']
    assert 'content' not in payload and 'msg_id' not in payload and 'event_id' not in payload


def test_bound_nickname_cannot_inject_tags_or_markdown_links(monkeypatch):
    transport = QQTransport(token_response(), {'id': 'message'})
    _, notifier = make_notifier(monkeypatch, transport)
    name = '<qqbot-at-everyone /> [管理员](https://example.invalid) **提示**'
    asyncio.run(notifier.notify_qr_ready(TASK_ID, name, member_openid=MEMBER))
    content = request_json(transport.requests[-1][0])['markdown']['content']
    assert content.count('<qqbot-') == 1
    assert '[管理员](' not in content and '**提示**' not in content


@pytest.mark.parametrize('member', ['a" /> <qqbot-at-everyone />', 'x' * 129, 123, None])
def test_invalid_mention_identity_is_rejected_before_http(monkeypatch, member):
    transport = QQTransport()
    module, notifier = make_notifier(monkeypatch, transport)
    with pytest.raises(module.QQNotifyError):
        asyncio.run(notifier.notify_qr_ready(TASK_ID, '小鸣人', member_openid=member))
    assert transport.requests == []


class Bindings:
    def __init__(self):
        self.member = MEMBER
        self.requested = []

    async def resolve(self, user_id):
        self.requested.append(user_id)
        return self.member


@pytest.mark.parametrize('final_member', ['', MEMBER])
@pytest.mark.parametrize('main_task,expected', [
    ('daily', '日常任务全部搞定啦！辛苦啦～'),
    ('arena_weekly', '决斗场周胜任务结束啦！辛苦啦～'),
])
def test_runtime_rereads_binding_after_session_expires_and_honors_unbind(monkeypatch, final_member, main_task, expected):
    async def scenario():
        runtime, record, _, _, user_id = await ready_runtime()
        record.user_options['mainTask'] = main_task
        bindings = Bindings()
        runtime._qq_binding_resolver = bindings
        transport = QQTransport(token_response(), {'id': 'qr'}, {'id': 'result'})
        _, runtime._qq_notifier = make_notifier(monkeypatch, transport)
        try:
            await runtime._publish_qr(record, b'png')
            await asyncio.gather(*list(runtime._notification_tasks))
            assert bindings.requested == [user_id]
            first = request_json(transport.requests[-1][0])
            assert first['markdown']['content'].startswith(f'<qqbot-at-user id="{MEMBER}" />')
            await runtime._redis_queue._client.delete(runtime._redis_queue._session_key(record))
            bindings.member = final_member
            record.daily_started_at = 1.0
            await runtime._send_task_notification(record, 'completed', None)
            assert bindings.requested == [user_id, user_id]
            final = request_json(transport.requests[-1][0])
            if final_member:
                assert final['msg_type'] == 2
                assert final['markdown']['content'].startswith(f'<qqbot-at-user id="{MEMBER}" />')
                assert expected in final['markdown']['content']
            else:
                assert final['msg_type'] == 0 and 'markdown' not in final
                assert expected in final['content']
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_binding_lookup_failure_preserves_plain_notification_without_leaking(monkeypatch, caplog):
    async def scenario():
        runtime, record, _, _, _ = await ready_runtime()
        class BrokenBinding:
            async def resolve(self, _user_id):
                raise RuntimeError(DATABASE_URL)
        runtime._qq_binding_resolver = BrokenBinding()
        transport = QQTransport(token_response(), {'id': 'qr'})
        _, runtime._qq_notifier = make_notifier(monkeypatch, transport)
        try:
            await runtime._publish_qr(record, b'png')
            await asyncio.gather(*list(runtime._notification_tasks))
            payload = request_json(transport.requests[-1][0])
            assert payload['msg_type'] == 0 and '雪花' in payload['content']
            assert record.status == 'qr_ready'
        finally:
            await runtime.stop()
    asyncio.run(scenario())
    assert 'QQ binding lookup failed' in caplog.text
    assert DATABASE_URL not in caplog.text
