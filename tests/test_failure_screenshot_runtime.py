"""Failure diagnostics use real worker pipes and OSS writes; no emulator/network."""

import asyncio
import threading
import time

import pytest

from naruto_daily.login_service import runtime as runtime_module
from naruto_daily.login_service.storage import AliyunObjectStorage
from oss_fakes import FakeBucket
from test_login_service_daily_runtime import (
    AUTHORIZED, COMPLETED, LOGGED_IN, PNG, QR, emit, eventually, gate,
    make_runtime, record_with_ttl, start_processing,
)


FAILURE_PNG = PNG + b"task-failure"
MODULE_PNG = PNG + b"earlier-module-timeout"


@pytest.fixture(autouse=True)
def no_failure_hold(monkeypatch):
    monkeypatch.setattr(runtime_module, "FAILED_INSTANCE_HOLD_SECONDS", 0)


class AlreadyExists(Exception):
    status = 409
    code = "FileAlreadyExists"


class DiagnosticBucket(FakeBucket):
    """The external OSS boundary can stall/fail and honors conditional writes."""

    def __init__(self, *, block_upload=False, fail_upload=False):
        super().__init__()
        self.block_upload = block_upload
        self.fail_upload = fail_upload
        self.upload_started = threading.Event()
        self.upload_release = threading.Event()
        self.exception_puts = []

    def put_object(self, key, body, headers):
        if "/exceptions/" in key:
            self.exception_puts.append(key)
            if headers.get("x-oss-forbid-overwrite") == "true" and key in self.objects:
                raise AlreadyExists()
            self.upload_started.set()
            if self.block_upload and not self.upload_release.wait(8):
                raise TimeoutError("Test did not release diagnostic upload")
            if self.fail_upload:
                raise OSError("Test OSS upload is unavailable")
        super().put_object(key, body, headers)


def diagnostic_runtime(tmp_path, monkeypatch, record, plan, *, bucket=None, daily_timeout=5):
    runtime, _, queue, pool = make_runtime(
        tmp_path, monkeypatch, record, plan, daily_timeout=daily_timeout,
    )
    bucket = bucket or DiagnosticBucket()
    runtime.storage = AliyunObjectStorage("login/test", runtime.config, bucket=bucket)
    captures = []

    def capture(task_id, timeout_seconds=15):
        child = runtime._children.get(task_id)
        captures.append((task_id, timeout_seconds, child.poll() if child is not None else None))
        return FAILURE_PNG

    # This substitutes the only real-device boundary, keeping collection/upload real.
    monkeypatch.setattr(pool, "capture_task_screenshot", capture, raising=False)
    return runtime, queue, pool, bucket, captures


@pytest.mark.parametrize("failure", ["daily_failed", "protocol_before_qr"])
def test_failure_upload_finishes_before_cleanup_and_keeps_prior_module_image(
    tmp_path, monkeypatch, failure,
):
    async def scenario():
        record = record_with_ttl(10)
        release_failure = tmp_path / "release-failure"
        if failure == "daily_failed":
            plan = [emit(QR), emit(LOGGED_IN), gate(release_failure),
                    emit({"type": "error", "code": "daily_failed"})]
            expected_error = "daily_failed"
        else:
            plan = [emit({"type": "stage", "status": "preparing_qr"}),
                    gate(release_failure), emit({"type": "unsupported-event"})]
            expected_error = "protocol_error"
        # Remain alive after emitting failure, so the runtime must stop the child.
        plan.append(gate(tmp_path / "never-continue"))
        bucket = DiagnosticBucket(block_upload=True)
        runtime, queue, pool, _, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record, plan, bucket=bucket,
        )
        old_key = f"login/test/exceptions/{record.id}_1.png"
        new_key = f"login/test/exceptions/{record.id}_2.png"
        bucket.objects[old_key] = (MODULE_PNG, {"Content-Type": "image/png"})
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.id in runtime._children)
            child = runtime._children[record.id]
            release_failure.touch()
            await eventually(lambda: bucket.upload_started.is_set() or work.done())
            assert bucket.upload_started.is_set(), "Failure must upload a diagnostic before cleanup"
            assert captures == [(record.id, 15, child.returncode)]
            assert child.returncode is not None, "Stop the business child before diagnostic capture"
            assert pool.closed == []
            assert not work.done()
            assert new_key not in bucket.objects
            bucket.upload_release.set()
            await asyncio.wait_for(work, 5)
            assert record.daily_status == "failed"
            assert record.daily_error_code == expected_error
            assert pool.closed == [record.id]
            assert queue.requeued == []
            assert len(captures) == 1, "Inner and outer failure handlers must share one attempt"
            assert bucket.objects[old_key][0] == MODULE_PNG
            assert bucket.objects[new_key][0] == FAILURE_PNG
            assert bucket.objects[new_key][1]["x-oss-object-acl"] == "private"
            assert sorted(key for key in bucket.objects if "/exceptions/" in key) == [old_key, new_key]
        finally:
            bucket.upload_release.set()
            release_failure.touch()
            await runtime.stop()
            await asyncio.gather(work, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("broken_boundary", ["capture", "upload"])
def test_diagnostic_failure_preserves_original_error_and_cleans_up(
    tmp_path, monkeypatch, broken_boundary,
):
    async def scenario():
        record = record_with_ttl()
        plan = [emit(QR), emit(LOGGED_IN), emit({"type": "error", "code": "daily_failed"}),
                gate(tmp_path / "never-continue")]
        bucket = DiagnosticBucket(fail_upload=broken_boundary == "upload")
        runtime, queue, pool, _, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record, plan, bucket=bucket,
        )
        if broken_boundary == "capture":
            def fail_capture(task_id, timeout_seconds=15):
                captures.append((task_id, timeout_seconds))
                raise OSError("Test ADB screenshot is unavailable")
            monkeypatch.setattr(pool, "capture_task_screenshot", fail_capture)
        try:
            await asyncio.wait_for(runtime._process_record(record), 5)
            assert len(captures) == 1, "Even unsuccessful diagnostics must be attempted only once"
            assert record.daily_error_code == "daily_failed"
            assert record.daily_status == "failed"
            assert pool.closed == [record.id]
            assert not runtime._children
            assert queue.requeued == []
            assert not any("/exceptions/" in key for key in bucket.objects)
            assert bool(bucket.exception_puts) is (broken_boundary == "upload")
        finally:
            await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["login", "daily"])
def test_real_task_timeout_still_saves_failure_image(tmp_path, monkeypatch, phase):
    async def scenario():
        record = record_with_ttl(2 if phase == "login" else 5)
        plan = [emit(QR)]
        if phase == "daily":
            plan.extend([emit(AUTHORIZED), emit(LOGGED_IN)])
        plan.append(gate(tmp_path / "never-finish"))
        runtime, _, pool, bucket, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record, plan, daily_timeout=0.3,
        )
        try:
            await asyncio.wait_for(runtime._process_record(record), 5)
            assert record.daily_error_code == ("login_timeout" if phase == "login" else "daily_timeout")
            assert record.daily_status == "failed"
            assert pool.closed == [record.id]
            assert len(captures) == 1
            assert bucket.objects[f"login/test/exceptions/{record.id}_1.png"][0] == FAILURE_PNG
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_process_start_failure_uses_outer_diagnostic_fallback(tmp_path, monkeypatch):
    async def scenario():
        record = record_with_ttl()
        runtime, _, pool, bucket, captures = diagnostic_runtime(tmp_path, monkeypatch, record, [])

        async def missing_executable(current):
            # Exercise the real OS launch error before _run_child owns a child.
            return await runtime._run_child(
                current, [str(tmp_path / "missing-worker.exe")], {"plan": []}, daily=True,
            )

        monkeypatch.setattr(runtime, "_run_ldplayer", missing_executable)
        try:
            await asyncio.wait_for(runtime._process_record(record), 5)
            assert record.status == "failed"
            assert record.daily_error_code == "protocol_error"
            assert not runtime._children
            assert pool.closed == [record.id]
            assert len(captures) == 1
            assert bucket.objects[f"login/test/exceptions/{record.id}_1.png"][0] == FAILURE_PNG
        finally:
            await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("main_task", ["daily", "arena_weekly"])
def test_normal_completion_including_weekly_time_limit_takes_no_failure_image(
    tmp_path, monkeypatch, main_task,
):
    async def scenario():
        record = record_with_ttl(10)
        record.user_options["mainTask"] = main_task
        plan = [emit(QR), emit(AUTHORIZED), emit(LOGGED_IN)]
        if main_task == "arena_weekly":
            plan.extend([emit({"type": "arena_started", "executionDeadline": time.time() + 1}),
                         gate(tmp_path / "weekly-keeps-running")])
        else:
            plan.append(emit(COMPLETED))
        runtime, _, pool, bucket, captures = diagnostic_runtime(tmp_path, monkeypatch, record, plan)
        try:
            await asyncio.wait_for(runtime._process_record(record), 5)
            assert record.daily_status == "completed"
            assert record.daily_error_code is None
            assert pool.closed == [record.id]
            assert captures == []
            assert bucket.exception_puts == []
        finally:
            await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["cancel", "shutdown", "admin"])
def test_interrupted_task_takes_no_failure_image(tmp_path, monkeypatch, reason):
    async def scenario():
        record = record_with_ttl(10)
        runtime, _, pool, bucket, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record,
            [emit(QR), emit(LOGGED_IN), gate(tmp_path / "never-finish")],
        )
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: record.daily_status == "running_daily")
            if reason == "cancel":
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await work
            elif reason == "shutdown":
                await asyncio.wait_for(runtime.stop(), 5)
            else:
                runtime._on_run_interrupted(record.id, record.attempt_id)
                await asyncio.wait_for(work, 5)
            assert record.daily_status == "interrupted"
            assert record.daily_error_code == ("admin_interrupted" if reason == "admin" else "worker_stopped")
            assert pool.closed == [record.id]
            assert captures == []
            assert bucket.exception_puts == []
        finally:
            await runtime.stop()
            await asyncio.gather(work, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_site", ["child", "startup"])
def test_cancellation_during_failure_upload_still_releases_instance(
    tmp_path, monkeypatch, failure_site,
):
    async def scenario():
        record = record_with_ttl(10)
        bucket = DiagnosticBucket(block_upload=True)
        runtime, _, pool, _, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record,
            [emit(QR), emit(LOGGED_IN), emit({"type": "error", "code": "daily_failed"}),
             gate(tmp_path / "never-continue")], bucket=bucket,
        )
        if failure_site == "startup":
            async def missing_executable(current):
                return await runtime._run_child(
                    current, [str(tmp_path / "missing-worker.exe")], {"plan": []}, daily=True,
                )
            monkeypatch.setattr(runtime, "_run_ldplayer", missing_executable)
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: bucket.upload_started.is_set() or work.done())
            assert bucket.upload_started.is_set(), "Cancel while the failure upload is active"
            child = runtime._children.get(record.id)
            if failure_site == "child":
                assert child is not None and child.poll() is not None
            else:
                assert child is None
            assert pool.closed == []
            work.cancel()
            bucket.upload_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(work, 5)
            assert record.daily_status == "interrupted"
            assert pool.closed == [record.id]
            assert not runtime._children
            assert len(captures) == 1
        finally:
            bucket.upload_release.set()
            await runtime.stop()
            await asyncio.gather(work, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_code", ["daily_failed", "protocol_error"])
def test_failure_before_login_deadline_keeps_its_error_after_slow_upload(
    tmp_path, monkeypatch, failure_code,
):
    async def scenario():
        record = record_with_ttl(1.2)
        bucket = DiagnosticBucket(block_upload=True)
        event = ({"type": "error", "code": failure_code} if failure_code == "daily_failed"
                 else {"type": "unsupported-event"})
        runtime, _, pool, _, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record,
            [emit(QR), emit(event), gate(tmp_path / "never-continue")], bucket=bucket,
        )
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: bucket.upload_started.is_set() or work.done(), timeout=2)
            assert bucket.upload_started.is_set()
            assert record.qr_published
            assert time.time() * 1000 < record.expires_at_ms, "The actual failure precedes expiry"
            assert pool.closed == []
            await eventually(lambda: time.time() * 1000 > record.expires_at_ms + 20, timeout=2)
            bucket.upload_release.set()
            await asyncio.wait_for(work, 2)
            assert record.daily_status == "failed"
            assert record.daily_error_code == failure_code
            assert pool.closed == [record.id]
            assert len(captures) == 1
        finally:
            bucket.upload_release.set()
            await runtime.stop()
            await asyncio.gather(work, return_exceptions=True)

    asyncio.run(scenario())


def test_admin_interruption_during_failure_upload_wins_over_original_error(tmp_path, monkeypatch):
    async def scenario():
        record = record_with_ttl(10)
        bucket = DiagnosticBucket(block_upload=True)
        runtime, queue, pool, _, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record,
            [emit(QR), emit(LOGGED_IN), emit({"type": "error", "code": "daily_failed"}),
             gate(tmp_path / "never-continue")], bucket=bucket,
        )
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: bucket.upload_started.is_set() or work.done(), timeout=2)
            assert bucket.upload_started.is_set()
            runtime._on_run_interrupted(record.id, record.attempt_id)
            assert record.daily_status == "interrupted"
            assert pool.closed == []
            bucket.upload_release.set()
            await asyncio.wait_for(work, 2)
            assert record.daily_status == "interrupted"
            assert record.daily_error_code == "admin_interrupted"
            assert pool.closed == [record.id]
            assert queue.requeued == []
            assert len(captures) == 1
        finally:
            bucket.upload_release.set()
            await runtime.stop()
            await asyncio.gather(work, return_exceptions=True)

    asyncio.run(scenario())


def test_failure_upload_does_not_turn_stopped_module_into_module_timeout(tmp_path, monkeypatch):
    from naruto_daily.login_service import module_timeout

    monkeypatch.setattr(module_timeout, "MODULE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(module_timeout, "POLL_SECONDS", 0.01)
    module_captures = []

    def capture_module(adb_path, serial):
        module_captures.append((adb_path, serial))
        return MODULE_PNG

    monkeypatch.setattr(module_timeout, "capture_screenshot", capture_module)

    async def scenario():
        record = record_with_ttl(10)
        bucket = DiagnosticBucket(block_upload=True)
        runtime, _, pool, _, captures = diagnostic_runtime(
            tmp_path, monkeypatch, record,
            [emit(QR), emit(LOGGED_IN),
             emit({"type": "module", "state": "started", "name": "return_to_main",
                   "serial": "emulator-5570"}),
             emit({"type": "error", "code": "daily_failed"}),
             gate(tmp_path / "never-continue")], bucket=bucket,
        )
        work = start_processing(runtime, record)
        try:
            await eventually(lambda: bucket.upload_started.is_set() or work.done(), timeout=2)
            assert bucket.upload_started.is_set()
            assert module_captures == [], "The module has not timed out when its child stops"
            assert runtime._children[record.id].poll() is not None
            await asyncio.sleep(0.3)
            assert pool.closed == []
            bucket.upload_release.set()
            await asyncio.wait_for(work, 2)
            assert module_captures == [], "Diagnostic upload time is not module execution time"
            assert record.daily_error_code == "daily_failed"
            assert pool.closed == [record.id]
            assert len(captures) == 1
            assert len(bucket.exception_puts) == 1
        finally:
            bucket.upload_release.set()
            await runtime.stop()
            await asyncio.gather(work, return_exceptions=True)

    asyncio.run(scenario())
