import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
import subprocess
from urllib.request import Request

import pytest

from naruto_daily.common import execution
from naruto_daily.common.adb_client import AdbClient, AdbError, CommandResult
from naruto_daily.multimodal.transport import MultimodalRequestError, request_with_retry


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_phase_limits_stage_timeout_and_restores_original_daily_budget():
    clock = Clock()
    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep) as parent:
        clock.sleep(100)
        phase_scope = getattr(execution, 'execution_phase_scope', execution.execution_scope)
        with phase_scope(180) as phase:
            assert execution.stage_timeout(9999) == 180
            assert phase is not parent
            phase.sleep(20)
            assert execution.stage_timeout(9999) == 160
            with execution.execution_scope(9999):
                assert execution.current_execution() is phase
        assert execution.current_execution() is parent
        assert parent.deadline == 3000
        assert parent.remaining() == 2880
    assert execution.current_execution() is None


def test_phase_can_expire_without_a_daily_budget_and_cleans_context():
    clock = Clock()
    with pytest.raises(execution.PhaseDeadlineExceeded):
        with execution.execution_phase_scope(180, clock=clock, sleep=clock.sleep):
            execution.current_execution().sleep(200)
    assert clock.now == 180
    assert execution.current_execution() is None


def test_phase_timeout_restores_daily_budget_before_recovery_input():
    clock, commands = Clock(), []
    client = AdbClient(Path('offline-adb'),
                       run_command=lambda cmd: commands.append(cmd) or CommandResult(0, '', ''))
    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep) as parent:
        with pytest.raises(execution.PhaseDeadlineExceeded):
            with execution.execution_phase_scope(180):
                execution.current_execution().sleep(180)
        assert execution.current_execution() is parent
        client.back()
        assert parent.remaining() == 2820
    assert commands == [['offline-adb', 'shell', 'input', 'keyevent', '4']]


@pytest.mark.parametrize('daily_seconds', [20, 180])
def test_daily_timeout_precedes_phase_timeout(daily_seconds):
    clock = Clock()
    with pytest.raises(execution.DailyDeadlineExceeded) as caught:
        with execution.execution_scope(daily_seconds, clock=clock, sleep=clock.sleep):
            with execution.execution_phase_scope(180) as phase:
                assert phase.deadline == daily_seconds
                phase.sleep(180)
    assert type(caught.value) is execution.DailyDeadlineExceeded
    assert clock.now == daily_seconds
    assert execution.current_execution() is None


def test_phase_uses_parent_clock_and_sleep_even_if_different_ones_are_supplied():
    clock = Clock()

    def unwanted(*args):
        raise AssertionError('局部阶段必须沿用日常时钟和等待实现')

    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
        with execution.execution_phase_scope(180, clock=unwanted, sleep=unwanted) as phase:
            phase.sleep(3)
            assert phase.remaining() == 177
    assert clock.now == 3


def test_phase_scope_rejects_a_late_success_on_exit():
    clock = Clock()
    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
        with pytest.raises(execution.PhaseDeadlineExceeded):
            with execution.execution_phase_scope(180):
                clock.now = 181


def test_phase_scope_preserves_an_original_non_timeout_error():
    clock = Clock()
    original = RuntimeError('设备连接失败')
    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep) as parent:
        with pytest.raises(RuntimeError) as caught:
            with execution.execution_phase_scope(180):
                clock.now = 181
                raise original
        assert caught.value is original
        assert execution.current_execution() is parent


def test_observation_retry_wait_stops_at_phase_deadline():
    clock, observations = Clock(), []

    def observe():
        observations.append(clock.now)
        raise MultimodalRequestError('模型暂不可用', retryable=True)

    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep) as parent:
        with pytest.raises(execution.PhaseDeadlineExceeded):
            with execution.execution_phase_scope(15):
                execution.retry_observation(observe)
        assert execution.current_execution() is parent
        assert parent.remaining() == 2985
    assert observations == [0, 10]


def test_phase_budget_follows_copied_context_into_a_tap_thread():
    clock, commands = Clock(), []
    client = AdbClient(Path('offline-adb'),
                       run_command=lambda cmd: commands.append(cmd) or CommandResult(0, '', ''))
    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
        with pytest.raises(execution.PhaseDeadlineExceeded):
            with execution.execution_phase_scope(180), ThreadPoolExecutor(max_workers=1) as pool:
                clock.now = 180
                pool.submit(copy_context().run, client.tap, 10, 20).result()
    assert commands == []


@pytest.mark.parametrize('daily_seconds, expected_type', [
    (3000, 'PhaseDeadlineExceeded'),
    (20, 'DailyDeadlineExceeded'),
    (180, 'DailyDeadlineExceeded'),
])
@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
def test_adb_subprocess_timeout_uses_the_effective_deadline_type(
        monkeypatch, daily_seconds, expected_type, operation):
    clock, timeouts = Clock(), []

    def run(command, **kwargs):
        timeouts.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', run)
    with execution.execution_scope(daily_seconds, clock=clock, sleep=clock.sleep):
        with pytest.raises(execution.DailyDeadlineExceeded) as caught:
            with execution.execution_phase_scope(180):
                client = AdbClient(Path('offline-adb'))
                client.tap(10, 20) if operation == 'tap' else client.screenshot()
        assert type(caught.value) is getattr(execution, expected_type)
    assert timeouts == [min(180, daily_seconds)]


def test_model_retries_stop_at_phase_deadline_without_using_the_daily_budget():
    clock, timeouts = Clock(), []

    def open_request(request, *, timeout):
        timeouts.append(timeout)
        clock.sleep(timeout)
        raise TimeoutError('模型接口超时')

    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
        with pytest.raises(execution.PhaseDeadlineExceeded):
            with execution.execution_phase_scope(45):
                request_with_retry(Request('https://offline.invalid'), request_timeout=30,
                                   timeout_seconds=2000, open_request=open_request,
                                   refresh_request=lambda: Request('https://offline.invalid'))
    assert timeouts == [30, 5]
    assert clock.now == 45


def test_model_refresh_phase_timeout_is_not_wrapped_as_a_request_failure():
    clock = Clock()

    def open_request(request, *, timeout):
        raise TimeoutError('模型接口超时')

    def refresh_request():
        clock.now = 180
        execution.check_execution()

    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
        with pytest.raises(execution.PhaseDeadlineExceeded):
            with execution.execution_phase_scope(180):
                request_with_retry(Request('https://offline.invalid'), request_timeout=30,
                                   open_request=open_request, refresh_request=refresh_request)


def test_async_model_request_does_not_retry_a_phase_timeout(monkeypatch):
    from naruto_daily.multimodal.transport import ModelAsyncClient, httpx
    clock = Clock()

    async def send(client, request, **kwargs):
        clock.now = 180
        execution.check_execution()

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)

    async def run():
        with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
            with execution.execution_phase_scope(180):
                async with ModelAsyncClient(request_timeout=30) as client:
                    await client.send(httpx.Request('POST', 'https://offline.invalid'))

    with pytest.raises(execution.PhaseDeadlineExceeded):
        asyncio.run(run())


def _client_returning_at_phase_deadline(monkeypatch, clock, runner_kind, returncode):
    def run_command(command):
        clock.now = 180
        return CommandResult(returncode, '', 'device offline' if returncode else '')

    def run_binary(command):
        clock.now = 180
        return returncode, b'png' if not returncode else b'', b'device offline' if returncode else b''

    if runner_kind == 'injected':
        return AdbClient(Path('offline-adb'), run_command=run_command, run_binary=run_binary)

    def run(command, **kwargs):
        if kwargs.get('text'):
            result = run_command(command)
            return subprocess.CompletedProcess(command, result.returncode, result.stdout, result.stderr)
        return subprocess.CompletedProcess(command, *run_binary(command))

    monkeypatch.setattr(subprocess, 'run', run)
    return AdbClient(Path('offline-adb'))


@pytest.mark.parametrize('runner_kind', ['default', 'injected'])
@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
def test_known_adb_failure_is_not_replaced_by_a_phase_timeout(monkeypatch, runner_kind, operation):
    clock = Clock()
    client = _client_returning_at_phase_deadline(monkeypatch, clock, runner_kind, returncode=1)
    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
        with pytest.raises(AdbError, match='device offline'):
            with execution.execution_phase_scope(180):
                client.tap(10, 20) if operation == 'tap' else client.screenshot()


@pytest.mark.parametrize('runner_kind', ['default', 'injected'])
@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
def test_successful_adb_result_after_phase_deadline_is_rejected(monkeypatch, runner_kind, operation):
    clock = Clock()
    client = _client_returning_at_phase_deadline(monkeypatch, clock, runner_kind, returncode=0)
    with execution.execution_scope(3000, clock=clock, sleep=clock.sleep):
        with pytest.raises(execution.PhaseDeadlineExceeded):
            with execution.execution_phase_scope(180):
                client.tap(10, 20) if operation == 'tap' else client.screenshot()


@pytest.mark.parametrize('runner_kind', ['default', 'injected'])
@pytest.mark.parametrize('operation', ['tap', 'screenshot'])
def test_daily_deadline_still_precedes_a_known_adb_failure(monkeypatch, runner_kind, operation):
    clock = Clock()
    client = _client_returning_at_phase_deadline(monkeypatch, clock, runner_kind, returncode=1)
    with pytest.raises(execution.DailyDeadlineExceeded) as caught:
        with execution.execution_scope(180, clock=clock, sleep=clock.sleep):
            with execution.execution_phase_scope(180):
                client.tap(10, 20) if operation == 'tap' else client.screenshot()
    assert type(caught.value) is execution.DailyDeadlineExceeded
