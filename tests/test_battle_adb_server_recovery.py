"""丰饶普通并发点击只恢复 ADB server，不重放失败点击。"""
from collections import Counter
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from naruto_daily.battle import run_battle, run_battle_without_first_skill
from naruto_daily.common.adb_client import AdbError, AdbServerConnectionError as ServerConnectionError
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, PhaseDeadlineExceeded, current_execution,
    execution_phase_scope, execution_scope,
)
from test_battle import Clock, battle_config


class RecoveryClient:
    def __init__(self, clock, *, buttons=(2, 3, 4), failures=None,
                 ensure_error=None, ensure_seconds=0, slow_button=None):
        self.clock = clock
        self.failures = failures or {}
        self.ensure_error = ensure_error
        self.ensure_seconds = ensure_seconds
        self.slow_button = slow_button
        self.lock = Lock()
        self.barrier = Barrier(len(buttons), action=lambda: clock.advance(1), timeout=3)
        self.calls = []
        self.finished = []
        self.frames = []
        self.ensure_calls = []
        self.active = 0
        self.per_button = Counter()

    def screenshot(self):
        with self.lock:
            assert self.active == 0, '检查结算前必须结束本批全部点击'
            self.frames.append(self.clock())
            return str(len(self.frames)).encode()

    def tap(self, x, y):
        return self._tap(x, y, 'ordinary')

    def battle_tap(self, x, y):
        return self._tap(x, y, 'battle')

    def _tap(self, x, y, route):
        with self.lock:
            self.per_button[x] += 1
            batch = self.per_button[x]
            self.calls.append((batch, x, y, route))
            self.active += 1
        try:
            self.barrier.wait()
            if (batch, x) == (1, self.slow_button):
                Event().wait(.03)
            error = self.failures.get((batch, x))
            if error is not None:
                raise error
        finally:
            with self.lock:
                self.active -= 1
                self.finished.append((batch, x))

    def ensure_battle_adb_server(self):
        with self.lock:
            assert self.active == 0, '必须排空本批点击，再检查或启动 server'
            budget = current_execution()
            self.ensure_calls.append((tuple(self.finished), budget))
        self.clock.advance(self.ensure_seconds)
        if self.ensure_error is not None:
            raise self.ensure_error


def run_until_frame(client, clock, *, frame=2, runner=run_battle_without_first_skill, waits=None):
    def sleep(seconds):
        if waits is not None:
            waits.append(seconds)
        clock.advance(seconds)

    return runner(
        client, dict(battle_config(), tap_interval_seconds=.5), clock=clock, sleep=sleep,
        check_interval_seconds=2,
        template_matcher=lambda png, *args: ((50, 50), .99) if int(png) >= frame else (None, .1),
    )


@pytest.mark.parametrize('legacy_client', [False, True])
def test_one_failed_tap_recovers_after_batch_without_replaying_it(legacy_client):
    clock, waits = Clock(), []
    client = RecoveryClient(clock, failures={(1, 2): ServerConnectionError('cannot connect to daemon')},
                            slow_button=4)
    operated_client = (SimpleNamespace(
        tap=client.tap, screenshot=client.screenshot,
        ensure_battle_adb_server=client.ensure_battle_adb_server,
    ) if legacy_client else client)

    summary = run_until_frame(operated_client, clock, waits=waits)

    assert Counter((batch, x) for batch, x, _, _ in client.calls) == Counter({
        (1, 2): 1, (1, 3): 1, (1, 4): 1, (2, 2): 1, (2, 3): 1, (2, 4): 1,
    })
    assert {route for _, _, _, route in client.calls} == {'ordinary' if legacy_client else 'battle'}
    assert len(client.ensure_calls) == 1
    assert sorted(client.ensure_calls[0][0]) == [(1, 2), (1, 3), (1, 4)]
    assert summary.taps == 5, '失败点击不计数，下一批三个按钮各执行一次'
    assert summary.end_target == 'result'
    assert waits == [.5], '连接恢复后仍执行原有批次间隔'
    assert client.frames == [0, 2.5]


def test_entire_failed_batch_only_ensures_server_once_and_counts_next_batch():
    clock = Clock()
    client = RecoveryClient(clock, failures={
        (1, x): ServerConnectionError('failed to start daemon') for x in (2, 3, 4)
    })

    summary = run_until_frame(client, clock)

    assert len(client.ensure_calls) == 1
    assert Counter((batch, x) for batch, x, _, _ in client.calls) == Counter({
        (1, 2): 1, (1, 3): 1, (1, 4): 1, (2, 2): 1, (2, 3): 1, (2, 4): 1,
    })
    assert summary.taps == 3


def test_recovery_time_consumes_existing_phase_and_daily_budgets():
    clock = Clock()
    client = RecoveryClient(clock, ensure_seconds=7,
                            failures={(1, 2): ServerConnectionError('cannot connect to daemon')})
    with execution_scope(3000, clock=clock, sleep=clock.advance) as parent:
        with execution_phase_scope(180) as phase:
            run_until_frame(client, clock)
            assert current_execution() is phase
            assert phase.deadline == 180
            assert phase.remaining() == 172
            assert client.ensure_calls[0][1] is phase
        assert current_execution() is parent
        assert parent.deadline == 3000
        assert parent.remaining() == 2992
    assert len(client.calls) == 3


def test_other_ordinary_battle_does_not_recover_server_errors():
    clock = Clock()
    error = ServerConnectionError('cannot connect to daemon')
    client = RecoveryClient(clock, buttons=(1, 2, 3, 4), failures={(1, 2): error})

    with pytest.raises(ServerConnectionError) as caught:
        run_until_frame(client, clock, runner=run_battle)

    assert caught.value is error
    assert client.ensure_calls == []
    assert len(client.calls) == 4
    assert {route for _, _, _, route in client.calls} == {'ordinary'}
    assert client.active == 0


@pytest.mark.parametrize('error', [
    AdbError('device offline'), PhaseDeadlineExceeded('phase expired'),
    DailyDeadlineExceeded('daily expired'), RuntimeError('unexpected tap failure'),
])
def test_mixed_server_and_other_failure_preserves_other_error(error):
    clock = Clock()
    client = RecoveryClient(clock, failures={
        (1, 2): ServerConnectionError('cannot connect to daemon'), (1, 3): error,
    })

    with pytest.raises(type(error)) as caught:
        run_until_frame(client, clock)

    assert caught.value is error
    assert client.ensure_calls == []
    assert len(client.calls) == 3
    assert sorted(client.finished) == [(1, 2), (1, 3), (1, 4)]


def test_server_check_failure_propagates_without_replaying_a_tap():
    clock = Clock()
    error = AdbError('start-server failed')
    client = RecoveryClient(clock, ensure_error=error,
                            failures={(1, 2): ServerConnectionError('cannot connect to daemon')})

    with pytest.raises(AdbError) as caught:
        run_until_frame(client, clock)

    assert caught.value is error
    assert len(client.ensure_calls) == 1
    assert len(client.calls) == 3
    assert client.active == 0


@pytest.mark.parametrize('daily_seconds, recovery_seconds, expected_type', [
    (3000, 179, PhaseDeadlineExceeded), (2, 1, DailyDeadlineExceeded),
])
def test_deadline_reached_during_recovery_blocks_the_next_batch(
        daily_seconds, recovery_seconds, expected_type):
    clock = Clock()
    client = RecoveryClient(clock, ensure_seconds=recovery_seconds,
                            failures={(1, 2): ServerConnectionError('cannot connect to daemon')})

    with pytest.raises(expected_type) as caught:
        with execution_scope(daily_seconds, clock=clock, sleep=clock.advance):
            with execution_phase_scope(180):
                run_until_frame(client, clock)

    assert type(caught.value) is expected_type
    assert len(client.ensure_calls) == 1
    assert len(client.calls) == 3
    assert client.active == 0


def test_client_without_server_check_keeps_the_connection_error():
    clock = Clock()
    error = ServerConnectionError('cannot connect to daemon')
    client = RecoveryClient(clock, failures={(1, 2): error})
    legacy_client = SimpleNamespace(tap=client.tap, screenshot=client.screenshot)

    with pytest.raises(ServerConnectionError) as caught:
        run_until_frame(legacy_client, clock)

    assert caught.value is error
    assert client.ensure_calls == []
    assert len(client.calls) == 3
