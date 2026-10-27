import subprocess
from contextlib import nullcontext

import pytest

from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, PhaseDeadlineExceeded, current_execution,
    execution_phase_scope, execution_scope,
)
from test_login_service_daily import session_for


def operate(client, operation):
    if operation == 'tap':
        client.tap(10, 20)
    else:
        client.screenshot()


@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
@pytest.mark.parametrize('daily_seconds, expected_type', [
    (3000, PhaseDeadlineExceeded), (178, DailyDeadlineExceeded),
    (180, DailyDeadlineExceeded),
])
def test_service_command_stops_at_nearest_deadline_and_restores_parent(
        tmp_path, operation, daily_seconds, expected_type):
    session, clock, _ = session_for(tmp_path)
    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs['timeout'])
        clock.sleep(kwargs['timeout'])
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    session._run_subprocess = run
    deadline = clock() + daily_seconds
    session._set_deadline(deadline)
    with pytest.raises(expected_type) as caught:
        with execution_scope(deadline, clock=clock, sleep=clock.sleep) as parent:
            try:
                with execution_phase_scope(180):
                    clock.sleep(175)
                    operate(session.client, operation)
            except PhaseDeadlineExceeded:
                assert current_execution() is parent
                session._run_subprocess = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, '', '')
                session.client.back()
                assert parent.remaining() == 2820
                raise
    assert type(caught.value) is expected_type
    assert current_execution() is None
    assert session._deadline == deadline
    assert timeouts == [min(180, daily_seconds) - 175]


@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
def test_ordinary_service_command_timeout_is_not_a_phase_timeout(tmp_path, operation):
    session, clock, _ = session_for(tmp_path)
    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs['timeout'])
        clock.sleep(kwargs['timeout'])
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    session._run_subprocess = run
    session._set_deadline(clock() + 3000)
    with execution_scope(session._deadline, clock=clock, sleep=clock.sleep):
        with pytest.raises(subprocess.TimeoutExpired):
            with execution_phase_scope(180):
                operate(session.client, operation)
    assert timeouts == [20]


@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
@pytest.mark.parametrize('returncode, expected_type', [(0, PhaseDeadlineExceeded), (1, AdbError)])
def test_service_late_result_preserves_explicit_adb_failure(tmp_path, operation, returncode, expected_type):
    session, clock, _ = session_for(tmp_path)

    def run(command, **kwargs):
        clock.sleep(5)
        if kwargs.get('text'):
            return subprocess.CompletedProcess(command, returncode, '', 'device offline')
        return subprocess.CompletedProcess(command, returncode, b'png', b'device offline')

    session._run_subprocess = run
    session._set_deadline(clock() + 3000)
    with execution_scope(session._deadline, clock=clock, sleep=clock.sleep):
        with pytest.raises(expected_type) as caught:
            with execution_phase_scope(180):
                clock.sleep(175)
                operate(session.client, operation)
        assert type(caught.value) is expected_type


@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
@pytest.mark.parametrize('daily_scope', [False, True])
def test_service_timeout_outside_abundance_keeps_original_exception(tmp_path, operation, daily_scope):
    session, clock, _ = session_for(tmp_path)
    deadline = clock() + 2
    session._set_deadline(deadline)
    original_error = subprocess.TimeoutExpired('offline-adb', 2)

    def run(command, **kwargs):
        assert kwargs['timeout'] == 2
        clock.sleep(2)
        raise original_error

    session._run_subprocess = run
    scope = execution_scope(deadline, clock=clock, sleep=clock.sleep) if daily_scope else nullcontext()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        with scope:
            operate(session.client, operation)
    assert caught.value is original_error
