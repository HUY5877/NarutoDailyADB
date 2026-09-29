import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from naruto_daily.common.adb_client import AdbClient, CommandResult
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, action_with_execution_budget, current_execution,
    execution_scope, retry_observation, stage_timeout,
)
from naruto_daily.multimodal.transport import MultimodalRequestError


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_phases_share_deadline_and_nested_scope_cannot_restart_it():
    clock = Clock()
    with execution_scope(1500, clock=clock, sleep=clock.sleep) as budget:
        clock.sleep(400)
        with execution_scope(9999, clock=clock):
            assert current_execution() is budget
            assert stage_timeout(20) == 1100
        assert budget.remaining() == 1100
    with execution_scope(1500, clock=clock):
        assert stage_timeout(30) == 1100
    assert current_execution() is None


def test_retry_observation_exceeds_three_failures_without_replaying_actions():
    clock = Clock()
    frames = []

    def observe():
        frames.append(len(frames) + 1)
        if len(frames) <= 5:
            raise MultimodalRequestError('timed out', retryable=True)
        return frames[-1]

    with execution_scope(1500, clock=clock, sleep=clock.sleep):
        assert retry_observation(observe) == 6
    assert frames == [1, 2, 3, 4, 5, 6]
    assert clock.now == 50


def test_retry_wait_stops_at_total_deadline_before_another_observation():
    clock = Clock()
    frames = []

    def observe():
        frames.append('fresh')
        raise MultimodalRequestError('timed out', retryable=True)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(5, clock=clock, sleep=clock.sleep):
        retry_observation(observe)
    assert frames == ['fresh']
    assert clock.now == 5


def test_late_observation_result_is_rejected():
    clock = Clock()

    def observe():
        clock.now = 1501
        return (800, 500)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(1500, clock=clock):
        retry_observation(observe)


def test_action_legacy_timeouts_are_replaced_without_changing_input_intervals_or_config():
    action = {'timeout_seconds': 30, 'sweep_timeout_seconds': 120,
              'multimodal': {'timeout_seconds': 60, 'max_calls': 3},
              'battle': {'start_timeout_seconds': 60, 'timeout_seconds': 300,
                         'tap_interval_seconds': .15, 'check_interval_seconds': 18}}
    saved = deepcopy(action)
    with execution_scope(1500, clock=lambda: 500):
        result = action_with_execution_budget(action)
    assert action == saved
    assert result['timeout_seconds'] == result['sweep_timeout_seconds'] == 1000
    assert result['battle']['start_timeout_seconds'] == result['battle']['timeout_seconds'] == 1000
    assert result['battle']['tap_interval_seconds'] == .15
    assert result['battle']['check_interval_seconds'] == 18
    assert result['multimodal'] == {'timeout_seconds': 30, 'max_calls': 3}


def test_deadline_blocks_adb_input_and_cleans_context():
    clock = Clock()
    commands = []
    client = AdbClient(Path('offline-adb'), run_command=lambda cmd: commands.append(cmd) or CommandResult(0, '', ''))
    with pytest.raises(DailyDeadlineExceeded), execution_scope(10, clock=clock):
        clock.now = 10
        client.tap(10, 20)
    assert commands == []
    assert current_execution() is None


def test_default_adb_runner_bounds_subprocess_wait(monkeypatch):
    import subprocess
    seen = []

    def run(cmd, **kwargs):
        seen.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(DailyDeadlineExceeded), execution_scope(1500, clock=lambda: 1498):
        AdbClient(Path('offline-adb')).screenshot()
    assert seen == [2]


def test_navigation_deadline_cancels_inflight_work_without_a_completion_probe():
    import time
    from naruto_daily.multimodal.agent.completion_watch import CompletionWatch
    cancelled = []

    async def work():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    async def run():
        with execution_scope(time.monotonic() + .02):
            await CompletionWatch(None, None).run(work())

    with pytest.raises(DailyDeadlineExceeded):
        asyncio.run(run())
    assert cancelled == [True]


def test_navigation_with_probe_stops_sdk_before_bounded_cleanup(monkeypatch):
    import time
    from naruto_daily.multimodal.agent import completion_watch
    monkeypatch.setattr(completion_watch, 'CLEANUP_GRACE_SECONDS', .01)
    events = []

    async def work():
        try:
            await asyncio.sleep(60)
        finally:
            events.append('cleanup')
            await asyncio.sleep(60)

    async def run():
        watch = completion_watch.CompletionWatch(None, lambda _: None,
                                                on_complete=lambda: events.append('stop'))
        with execution_scope(time.monotonic() + .02):
            await watch.run(work())

    started = time.monotonic()
    with pytest.raises(DailyDeadlineExceeded):
        asyncio.run(run())
    assert time.monotonic() - started < 1
    assert events[:2] == ['stop', 'cleanup']
