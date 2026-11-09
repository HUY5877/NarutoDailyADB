"""Reconcile asynchronous LDPlayer shutdown without touching live emulators."""

import subprocess
import time

import pytest

from naruto_daily.login_service import ldplayer
from test_login_service_ldplayer import Clock, TASK_ID, command_result
from test_login_service_pool_state import SECOND_TASK, fake_pool, read_state, state_path


@pytest.fixture
def bound_pool(tmp_path, monkeypatch):
    service, fake = fake_pool(tmp_path, monkeypatch, (4, 17))
    for values in fake.instances.values():
        values[1] = 1
    service.reset_pool()
    for task_id in (TASK_ID, SECOND_TASK):
        service.allocate(task_id, time.time() + 10)
        service.mark_ready(task_id)
    clock = Clock()
    monkeypatch.setattr(ldplayer, "time", clock)
    return service, fake, clock


def assert_lock_released(service, clock):
    other = ldplayer.LDPlayerManager(
        service.console_path, "", service.state_dir, pool_indices=service.pool_indices,
    )
    with other._exclusive(clock.time() + 1):
        pass


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_failed_quit_still_releases_stopped_instance_with_fresh_confirmation_budget(
    bound_pool, tmp_path, monkeypatch, caplog, failure,
):
    service, fake, clock = bound_pool
    other_slot = read_state(tmp_path)["bindings"]["17"]
    original_run = fake.__call__
    polls = 0
    closing = False

    def asynchronous_shutdown(args, **kwargs):
        nonlocal closing, polls
        command = tuple(map(str, args))
        if closing and command[1] == "list2":
            polls += 1
            if polls == 2:
                fake.instances["日常登录甲"][1] = 0
        result = original_run(args, **kwargs)
        if command[1] == "quit":
            closing = True
            fake.instances["日常登录甲"][1] = 1
            if failure == "timeout":
                clock.sleep(kwargs["timeout"])
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            result.returncode = 1
        return result

    monkeypatch.setattr(ldplayer.subprocess, "run", asynchronous_shutdown)

    service.close(TASK_ID)

    state = read_state(tmp_path)["bindings"]
    assert state["4"]["state"] == "idle" and state["4"]["taskId"] is None
    assert state["17"] == other_slot
    assert fake.instances["手机 - 备用乙"][1] == 1
    assert polls == 2
    assert fake.calls.count((fake.console, "quit", "--name", "日常登录甲")) == 1
    assert "instance close" in caplog.text
    assert ("exit code 1" if failure == "nonzero" else "timed out") in caplog.text
    assert_lock_released(service, clock)
    service.close(TASK_ID)  # Cleanup is safe to repeat after the binding is released.


@pytest.mark.parametrize("listing", ["nonzero", "invalid", "missing"])
def test_cleanup_retries_transient_confirmation_failure(bound_pool, tmp_path, monkeypatch, listing):
    service, fake, clock = bound_pool
    original_run = fake.__call__
    closing = False
    polls = 0

    def transient_listing(args, **kwargs):
        nonlocal closing, polls
        command = tuple(map(str, args))
        result = original_run(args, **kwargs)
        if command[1] == "quit":
            closing = True
        elif closing and command[1] == "list2":
            polls += 1
            if polls == 1:
                kwargs["stdout"].seek(0)
                kwargs["stdout"].truncate()
                return command_result(
                    kwargs, b"invalid" if listing == "invalid" else b"",
                    returncode=1 if listing == "nonzero" else 0,
                )
        return result

    monkeypatch.setattr(ldplayer.subprocess, "run", transient_listing)

    service.close(TASK_ID)

    assert read_state(tmp_path)["bindings"]["4"]["taskId"] is None
    assert polls == 2
    assert_lock_released(service, clock)


@pytest.mark.parametrize("final_state", ["running", "unknown"])
def test_failed_quit_keeps_owner_when_shutdown_cannot_be_confirmed(
    bound_pool, tmp_path, monkeypatch, final_state,
):
    service, fake, clock = bound_pool
    before = state_path(tmp_path).read_bytes()
    original_run = fake.__call__
    closing = False

    def unsuccessful_shutdown(args, **kwargs):
        nonlocal closing
        command = tuple(map(str, args))
        result = original_run(args, **kwargs)
        if command[1] == "quit":
            closing = True
            fake.instances["日常登录甲"][1] = 1
            result.returncode = 1
        elif closing and command[1] == "list2" and final_state == "unknown":
            result.returncode = 1
        return result

    monkeypatch.setattr(ldplayer.subprocess, "run", unsuccessful_shutdown)
    started = clock.time()

    with pytest.raises(ldplayer.LDPlayerError) as caught:
        service.close(TASK_ID)

    assert "cleanup" in str(caught.value).lower()
    assert ("still running" if final_state == "running" else "instance listing failed") in str(caught.value)
    assert isinstance(caught.value.__cause__, ldplayer.LDPlayerError)
    assert "instance close" in str(caught.value.__cause__)
    assert clock.time() - started == pytest.approx(service.command_timeout)
    assert state_path(tmp_path).read_bytes() == before
    assert_lock_released(service, clock)


def test_cleanup_save_failure_preserves_owner_and_releases_file_lock(bound_pool, tmp_path, monkeypatch):
    service, fake, clock = bound_pool
    before = state_path(tmp_path).read_bytes()

    def cannot_replace(*args):
        raise OSError("private filesystem error")

    monkeypatch.setattr(ldplayer.os, "replace", cannot_replace)

    with pytest.raises(ldplayer.LDPlayerError):
        service.close(TASK_ID)

    assert fake.instances["日常登录甲"][1] == 0
    assert state_path(tmp_path).read_bytes() == before
    assert_lock_released(service, clock)


def test_unexpected_quit_error_is_not_swallowed_and_releases_file_lock(bound_pool, tmp_path, monkeypatch):
    service, fake, clock = bound_pool
    before = state_path(tmp_path).read_bytes()
    original_run = fake.__call__
    closing = False
    confirmed = False

    def interrupted_shutdown(args, **kwargs):
        nonlocal closing, confirmed
        command = tuple(map(str, args))
        result = original_run(args, **kwargs)
        if command[1] == "quit":
            closing = True
            raise KeyboardInterrupt
        if closing and command[1] == "list2":
            confirmed = True
        return result

    monkeypatch.setattr(ldplayer.subprocess, "run", interrupted_shutdown)

    with pytest.raises(KeyboardInterrupt):
        service.close(TASK_ID)

    assert confirmed
    assert state_path(tmp_path).read_bytes() == before
    assert_lock_released(service, clock)
