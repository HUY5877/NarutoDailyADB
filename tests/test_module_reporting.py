import pytest

from naruto_daily import runner
from naruto_daily.common import module_reporting as reporting
from naruto_daily.runner import ActionRunner
from test_runner import RecordingClient


def test_each_return_to_main_invocation_has_its_own_boundary(monkeypatch):
    events, calls = [], []

    def returning(*args, **kwargs):
        assert events[-1] == {'type': 'module', 'state': 'started', 'name': 'return_to_main'}
        calls.append('return')

    monkeypatch.setattr(runner, 'return_to_main', returning)
    with reporting.module_reporting(events.append):
        summary = ActionRunner(RecordingClient()).run([{'type': 'return_to_main'}] * 2)
    assert calls == ['return', 'return']
    assert summary.action_count == 2
    assert [event['state'] for event in events] == ['started', 'finished'] * 2


def test_secret_modules_are_exempt_but_nested_return_is_reported(monkeypatch):
    events = []

    def entering(*args, **kwargs):
        with reporting.module_scope('return_to_main'):
            assert events[-1]['name'] == 'return_to_main'
        return True

    monkeypatch.setattr(runner, 'enter_secret_realm', entering)
    monkeypatch.setattr(runner, 'handle_secret_realm', lambda *args, **kwargs: None)
    with reporting.module_reporting(events.append):
        ActionRunner(RecordingClient()).run([
            {'type': 'enter_secret_realm'}, {'type': 'handle_secret_realm'}, {'type': 'back'},
        ])
    assert events == [
        {'type': 'module', 'state': 'started', 'name': 'return_to_main'},
        {'type': 'module', 'state': 'finished', 'name': 'return_to_main'},
    ]


def test_module_end_is_reported_on_failure_and_reporting_is_task_scoped():
    events = []
    with reporting.module_reporting(events.append):
        with pytest.raises(RuntimeError, match='module failed'):
            with reporting.module_scope('handle_free_recruit'):
                raise RuntimeError('module failed')
    with reporting.module_scope('back'):
        pass
    assert [event['state'] for event in events] == ['started', 'finished']


def test_nested_return_is_reported_without_duplicate_same_module():
    events = []
    with reporting.module_reporting(events.append):
        with reporting.module_scope('prepare_arena_matchmaking'):
            with reporting.module_scope('return_to_main'):
                with reporting.module_scope('return_to_main'):
                    pass
    assert [(event['state'], event['name']) for event in events] == [
        ('started', 'prepare_arena_matchmaking'), ('started', 'return_to_main'),
        ('finished', 'return_to_main'), ('finished', 'prepare_arena_matchmaking'),
    ]


def test_internal_return_function_reports_even_without_runner(monkeypatch):
    from naruto_daily.return_to_main import handler
    events = []

    def unavailable(*args, **kwargs):
        assert events[-1]['name'] == 'return_to_main'
        raise RuntimeError('device unavailable')

    monkeypatch.setattr(handler, 'ImageRecognition', unavailable)
    with reporting.module_reporting(events.append):
        with pytest.raises(RuntimeError, match='device unavailable'):
            handler.return_to_main(None, {})
    assert [event['state'] for event in events] == ['started', 'finished']


def test_reporting_failure_and_dry_run_do_not_change_execution():
    def broken(event):
        raise OSError('pipe unavailable')

    client = RecordingClient()
    with reporting.module_reporting(broken):
        assert ActionRunner(client).run([{'type': 'back'}]).action_count == 1
    assert client.calls == [('back',)]
    events = []
    with reporting.module_reporting(events.append):
        ActionRunner(client, dry_run=True).run([{'type': 'back'}])
    assert events == []
