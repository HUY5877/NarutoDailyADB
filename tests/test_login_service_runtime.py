"""Verify the Python queue/worker behavior without Redis, OSS, or an emulator."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
import secrets
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import PROJECT_ROOT, ServiceConfig, load_config
from naruto_daily.login_service.models import HttpError, MAX_TTL_MS, equal_secret, parse_submission
from naruto_daily.login_service.queue import MockRedisQueue, SAFE_DRIVER_ERRORS
from naruto_daily.login_service.runtime import LoginRuntime


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.put_ids = []
        self.sweep_count = 0
        self.delete_failures = 0
        self.fail_put = False

    async def initialize(self):
        pass

    async def put(self, task_id, png, expires_at):
        if self.fail_put:
            raise RuntimeError("Secret storage SDK detail must not escape")
        self.objects[task_id] = (png, expires_at)
        self.put_ids.append(task_id)

    async def delete(self, task_id):
        if self.delete_failures:
            self.delete_failures -= 1
            raise OSError("temporary failure")
        self.objects.pop(task_id, None)

    async def sweep(self):
        self.sweep_count += 1


def submission(ttl=5):
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid4()), "accessToken": secrets.token_hex(32),
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_config_defaults_and_project_relative_storage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = load_config({})
    assert config.port == 4320
    assert config.concurrency == 2
    assert config.max_queue_size == 100
    assert config.max_tasks == 500
    assert config.local_storage_dir == PROJECT_ROOT / ".data/login-service/objects"
    assert load_config({"LOCAL_STORAGE_DIR": "relative-objects"}).local_storage_dir == PROJECT_ROOT / "relative-objects"
    assert load_config({"LOCAL_STORAGE_DIR": str(tmp_path)}).local_storage_dir == tmp_path
    assert config.ldplayer_console_path is None
    assert config.ldplayer_template == ""
    assert config.ldplayer_pool_indices == ()
    assert config.ldplayer_state_dir == PROJECT_ROOT / ".data/login-service/ldplayer"
    assert config.ldplayer_package == "com.tencent.KiHan"
    assert config.ldplayer_command_timeout_seconds == 20


def test_ldplayer_config_resolves_paths_and_redacts_oss_secrets(tmp_path):
    console = tmp_path / "dnconsole.exe"
    console.write_bytes(b"")
    config = load_config({
        "REDIS_URL": "redis://localhost/0",
        "EMULATOR_DRIVER": "ldplayer",
        "LDPLAYER_CONSOLE_PATH": str(console),
        "LDPLAYER_TEMPLATE": "clean-login-template",
        "LDPLAYER_STATE_DIR": "relative-state",
        "LDPLAYER_PACKAGE": "example.game",
        "LDPLAYER_COMMAND_TIMEOUT_SECONDS": "31",
        "OSS_ACCESS_KEY_SECRET": "private-oss-secret",
        "OSS_STS_TOKEN": "private-sts-token",
    })
    assert config.ldplayer_console_path == console
    assert config.ldplayer_template == "clean-login-template"
    assert config.ldplayer_state_dir == PROJECT_ROOT / "relative-state"
    assert config.ldplayer_package == "example.game"
    assert config.ldplayer_command_timeout_seconds == 31
    assert "private-oss-secret" not in repr(config)
    assert "private-sts-token" not in repr(config)


def test_ldplayer_pool_config_does_not_require_clone_template(tmp_path):
    console = tmp_path / "dnconsole.exe"
    console.write_bytes(b"")
    config = load_config({
        "REDIS_URL": "redis://localhost/0",
        "EMULATOR_DRIVER": "ldplayer",
        "LDPLAYER_CONSOLE_PATH": str(console),
        "LDPLAYER_POOL_INDICES": "0, 2",
    })
    assert config.ldplayer_pool_indices == (0, 2)
    assert config.ldplayer_template == ""


def test_ldplayer_pool_requires_redis_worker_mode(tmp_path):
    console = tmp_path / "dnconsole.exe"
    console.write_bytes(b"")
    with pytest.raises(ValueError, match="REDIS_URL"):
        load_config({
            "EMULATOR_DRIVER": "ldplayer", "LDPLAYER_CONSOLE_PATH": str(console),
            "LDPLAYER_POOL_INDICES": "0,2",
        })


@pytest.mark.parametrize("env", [
    {"EMULATOR_DRIVER": "ldplayer"},
    {"EMULATOR_DRIVER": "ldplayer", "LDPLAYER_CONSOLE_PATH": "missing.exe", "LDPLAYER_TEMPLATE": "clean"},
    {"EMULATOR_DRIVER": "ldplayer", "LDPLAYER_CONSOLE_PATH": __file__, "LDPLAYER_TEMPLATE": ""},
])
def test_ldplayer_config_rejects_missing_console_or_template(env):
    with pytest.raises(ValueError, match="LDPLAYER"):
        load_config(env)


@pytest.mark.parametrize("env", [
    {"HOST": "0.0.0.0"}, {"SERVICE_TOKEN": "short"}, {"PORT": "not-a-number"},
    {"PORT": "-1"}, {"WORKER_CONCURRENCY": "0"}, {"MAX_TASKS": "1.2"},
    {"STORAGE_DRIVER": "bad"}, {"EMULATOR_DRIVER": "bad"}, {"OSS_KEY_PREFIX": "../unsafe"},
    {"STORAGE_DRIVER": "oss"}, {"LDPLAYER_COMMAND_TIMEOUT_SECONDS": "0"},
    {"LDPLAYER_POOL_INDICES": "0,0"}, {"LDPLAYER_POOL_INDICES": "0,00"},
    {"LDPLAYER_POOL_INDICES": "-1,2"},
    {"LDPLAYER_POOL_INDICES": "0,"}, {"LDPLAYER_POOL_INDICES": "zero,2"},
])
def test_config_rejects_invalid_values(env):
    with pytest.raises(ValueError):
        load_config(env)


def test_config_allows_explicit_oss_and_non_loopback_credentials():
    config = load_config({
        "HOST": "0.0.0.0", "SERVICE_TOKEN": "unique-token-for-test", "STORAGE_DRIVER": "oss",
        "OSS_REGION": "oss-cn-hangzhou", "OSS_BUCKET": "example", "OSS_ACCESS_KEY_ID": "test-id",
        "OSS_ACCESS_KEY_SECRET": "test-secret", "OSS_STS_TOKEN": "test-sts", "OSS_KEY_PREFIX": "/login/test/",
    })
    assert config.key_prefix == "login/test"
    assert config.oss_sts_token == "test-sts"


@pytest.mark.parametrize("changes", [
    {"id": "../path"}, {"accessToken": "short"}, {"createdAt": "bad"},
    {"createdAt": "2026-02-30T10:00:00Z"}, {"expiresAt": "2100-01-01T00:00:00Z"},
    {"expiresAt": "2000-01-01T00:00:00Z"}, {"expiresAt": "2100-01-01T00:00:00+00:00"},
])
def test_submission_rejects_malformed_or_expired_input(changes):
    with pytest.raises(HttpError) as exc:
        parse_submission(submission() | changes)
    assert exc.value.status == 400


def test_submission_ttl_and_secret_validation():
    with pytest.raises(HttpError):
        parse_submission(submission(MAX_TTL_MS / 1000 + 1))
    with pytest.raises(HttpError):
        parse_submission([])
    body = submission()
    body["id"] = body["id"].upper()
    record = parse_submission(body)
    assert record.id == body["id"].lower()
    assert equal_secret(body["accessToken"], record.access_token)
    assert not equal_secret(None, record.access_token)
    assert not equal_secret("\u4e2d\u6587", record.access_token)
    assert "accessToken" not in record.to_view()


def test_mock_redis_fifo_capacity_waiters_and_close():
    async def scenario():
        queue = MockRedisQueue(2)
        assert queue.lpush("first")
        assert queue.lpush("second")
        assert not queue.lpush("full")
        assert await queue.brpop() == "first"
        queue.remove(lambda item: item == "second")
        waiter = asyncio.create_task(queue.brpop())
        await asyncio.sleep(0)
        assert queue.lpush("direct")
        assert await waiter == "direct"
        cancelled = asyncio.create_task(queue.brpop())
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        assert queue.lpush("after-cancel")
        assert await queue.brpop() == "after-cancel"
        pending = asyncio.create_task(queue.brpop())
        await asyncio.sleep(0)
        queue.close()
        assert await pending is None
        assert await queue.brpop() is None
        assert not queue.lpush("closed")
    asyncio.run(scenario())


def test_runtime_rejects_unstarted_and_stopped_service():
    async def scenario():
        runtime = LoginRuntime(ServiceConfig(), MemoryStorage())
        with pytest.raises(HttpError) as exc:
            runtime.submit(submission())
        assert exc.value.status == 503
        await runtime.start()
        await runtime.start()
        await runtime.stop()
        await runtime.stop()
        with pytest.raises(HttpError) as exc:
            runtime.submit(submission())
        assert exc.value.status == 503
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            await runtime.start()
    asyncio.run(scenario())


def test_submission_is_idempotent_and_session_protected():
    async def scenario():
        runtime = LoginRuntime(ServiceConfig(), MemoryStorage())
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            assert runtime.submit(body) == task_id
            assert runtime.stats == {"queued": 1, "active": 0, "tasks": 1}
            with pytest.raises(HttpError) as exc:
                runtime.submit(body | {"accessToken": secrets.token_hex(32)})
            assert exc.value.status == 409
            view = runtime.get_task(task_id.upper(), body["accessToken"])
            assert view["status"] == "queued"
            assert view["simulation"] is True
            assert "accessToken" not in view
            assert runtime.get_task(task_id, None) is None
            assert runtime.get_task(task_id, secrets.token_hex(32)) is None
            assert runtime.get_task("../bad", body["accessToken"]) is None
            assert not runtime.object_available(task_id)
        finally:
            await runtime.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("config", [ServiceConfig(max_queue_size=1), ServiceConfig(max_tasks=1)])
def test_runtime_enforces_queue_and_task_capacity(config):
    async def scenario():
        runtime = LoginRuntime(config, MemoryStorage())
        await runtime.start()
        try:
            runtime.submit(submission())
            with pytest.raises(HttpError) as exc:
                runtime.submit(submission())
            assert exc.value.status == 429
            assert runtime.stats["tasks"] == 1
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_child_process_generates_png_and_exposes_stages():
    pytest.importorskip("qrcode")
    from PIL import Image

    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=60), storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            observed = {"queued"}
            pids = set()
            async with asyncio.timeout(5):
                while True:
                    view = runtime.get_task(task_id, body["accessToken"])
                    observed.add(view["status"])
                    pids.update(child.pid for child in runtime._children.values())
                    if view["status"] in {"qr_ready", "failed"}:
                        break
                    await asyncio.sleep(0.005)
            assert observed == {"queued", "starting_emulator", "opening_game", "preparing_qr", "qr_ready"}
            assert len(pids) == 1 and os.getpid() not in pids
            png, expires_at = storage.objects[task_id]
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert Image.open(BytesIO(png)).size == (360, 360)
            assert expires_at == body["expiresAt"]
            assert runtime.object_available(task_id)
            assert runtime.stats["active"] == 0
        finally:
            await runtime.stop()
        assert storage.objects == {}
    asyncio.run(scenario())


def test_each_job_gets_an_isolated_child_and_concurrency_is_bounded():
    pytest.importorskip("qrcode")

    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(concurrency=2, step_delay_ms=100), storage)
        await runtime.start()
        try:
            task_ids = [runtime.submit(submission()) for _ in range(4)]
            pids = set()
            async with asyncio.timeout(5):
                while len(storage.put_ids) < 4:
                    assert runtime.stats["active"] <= 2
                    pids.update(child.pid for child in runtime._children.values())
                    await asyncio.sleep(0.005)
            assert len(pids) == 4
            assert set(storage.put_ids) == set(task_ids)
            assert len({png for png, _ in storage.objects.values()}) == 4
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_expiry_kills_running_child_and_removes_retained_task():
    pytest.importorskip("qrcode")

    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=1000, retention_ms=200, sweep_interval_ms=10), storage)
        await runtime.start()
        try:
            body = submission(ttl=0.3)
            task_id = runtime.submit(body)
            await eventually(lambda: task_id in runtime._children)
            child = runtime._children[task_id]
            await eventually(lambda: (runtime.get_task(task_id, body["accessToken"]) or {}).get("status") == "expired")
            assert not runtime.object_available(task_id)
            await eventually(lambda: child.poll() is not None)
            await eventually(lambda: runtime.stats["tasks"] == 0)
            assert runtime.get_task(task_id, body["accessToken"]) is None
            assert storage.objects == {}
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_expired_queued_job_never_launches_child():
    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(ServiceConfig(), storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            runtime._tasks[task_id].expires_at_ms = 0
            assert runtime.get_task(task_id, body["accessToken"])["status"] == "expired"
            await asyncio.sleep(0)
            assert runtime.stats["active"] == 0
            assert runtime.stats["queued"] == 0
            assert storage.put_ids == []
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_shutdown_terminates_and_reaps_child():
    pytest.importorskip("qrcode")

    async def scenario():
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=5000), MemoryStorage())
        await runtime.start()
        task_id = runtime.submit(submission())
        await eventually(lambda: task_id in runtime._children)
        child = runtime._children[task_id]
        await asyncio.wait_for(runtime.stop(), 2)
        assert child.poll() is not None
        assert runtime.stats == {"queued": 0, "active": 0, "tasks": 0}
    asyncio.run(scenario())


def test_ldplayer_start_rejects_incomplete_direct_config():
    async def scenario():
        runtime = LoginRuntime(ServiceConfig(emulator_driver="ldplayer"), MemoryStorage())
        with pytest.raises(ValueError, match="LDPLAYER"):
            await runtime.start()
    asyncio.run(scenario())


def test_ldplayer_pool_start_rejects_local_http_mode(tmp_path):
    async def scenario():
        runtime = LoginRuntime(ServiceConfig(
            emulator_driver="ldplayer", ldplayer_console_path=tmp_path / "dnconsole.exe",
            ldplayer_pool_indices=(0, 2),
        ), MemoryStorage())
        runtime.config.ldplayer_console_path.write_bytes(b"")
        with pytest.raises(ValueError, match="REDIS_URL"):
            await runtime.start()
    asyncio.run(scenario())


class _OpenBytesIO(BytesIO):
    def close(self):
        pass


class _ProtocolChild:
    pid = 98765

    def __init__(self, events, exit_code=0):
        self.stdin = _OpenBytesIO()
        self.stdout = _OpenBytesIO(b"".join(
            json.dumps(event).encode("utf-8") + b"\n" for event in events
        ))
        self.exit_code = exit_code
        self.killed = False

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed = True

    def wait(self):
        return self.exit_code


class _BlockingOutput:
    def __init__(self, child):
        self.child = child

    def readline(self, _limit):
        self.child.finished.wait()
        return b""

    def close(self):
        pass


class _BlockingChild:
    pid = 98766

    def __init__(self):
        import threading
        self.stdin = _OpenBytesIO()
        self.finished = threading.Event()
        self.stdout = _BlockingOutput(self)

    def poll(self):
        return -9 if self.finished.is_set() else None

    def kill(self):
        self.finished.set()

    def wait(self):
        self.finished.wait()
        return -9


def _ldplayer_config(tmp_path):
    console = tmp_path / "dnconsole.exe"
    console.write_bytes(b"")
    return ServiceConfig(
        emulator_driver="ldplayer", ldplayer_console_path=console,
        ldplayer_template="clean-template", ldplayer_state_dir=tmp_path / "state",
        ldplayer_package="example.game", ldplayer_command_timeout_seconds=23,
    )


@pytest.mark.parametrize('daily', [False, True])
def test_child_receives_configured_routing_redis_only_for_daily_tasks(tmp_path, monkeypatch, daily):
    config = replace(_ldplayer_config(tmp_path), redis_url='redis://configured:private@test.invalid/2')
    runtime = LoginRuntime(config, MemoryStorage())
    monkeypatch.setenv('REDIS_URL', 'redis://unrelated.invalid/0')
    monkeypatch.setenv('YUWAN_API_KEY', 'private-yuwan')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'private-deepseek')
    spawned = {}

    class CapturedSpawn(Exception):
        pass

    def capture(command, **kwargs):
        spawned.update(kwargs)
        raise CapturedSpawn

    monkeypatch.setattr('naruto_daily.login_service.runtime.subprocess.Popen', capture)
    with pytest.raises(CapturedSpawn):
        asyncio.run(runtime._run_child(parse_submission(submission()), ['worker'], {'type': 'start'}, daily=daily))
    assert spawned['env'].get('REDIS_URL') == (config.redis_url if daily else None)
    assert 'YUWAN_API_KEY' not in spawned['env']
    assert 'DEEPSEEK_API_KEY' not in spawned['env']


def test_ldplayer_worker_protocol_is_secret_free_and_completed_instance_is_released(monkeypatch, tmp_path):
    png = b"\x89PNG\r\n\x1a\nfixture"
    child = _ProtocolChild([
        {"type": "stage", "status": "opening_game"},
        {"type": "stage", "status": "preparing_qr"},
        {"type": "qr", "png": __import__("base64").b64encode(png).decode("ascii")},
        {"type": "logged_in"},
        {"type": "daily_completed", "actionCount": 12},
    ])
    spawned = {}

    def popen(command, **kwargs):
        spawned.update(command=command, kwargs=kwargs)
        return child

    monkeypatch.setenv("REDIS_URL", "redis://user:private@host/0")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "private-oss-secret")
    for name in ("MULTIMODAL_API_KEY", "MULTIMODAL_BASE_URL", "MULTIMODAL_MODEL"):
        monkeypatch.setenv(name, "stale-service-value")
    windows_paths = {
        "SYSTEMDRIVE": "C:", "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD", "APPDATA": r"C:\Users\test\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\test\AppData\Local", "PROGRAMDATA": r"C:\ProgramData",
        "ALLUSERSPROFILE": r"C:\ProgramData", "PROGRAMFILES": r"C:\Program Files",
        "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
        "COMMONPROGRAMFILES": r"C:\Program Files\Common Files",
        "COMMONPROGRAMFILES(X86)": r"C:\Program Files (x86)\Common Files",
        "PUBLIC": r"C:\Users\Public", "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\test",
    }
    for name, value in windows_paths.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LOGIN_SERVICE_PRIVATE_SECRET", "must-not-enter-child")
    monkeypatch.setattr("naruto_daily.login_service.runtime.subprocess.Popen", popen)
    closed = []

    async def scenario():
        storage = MemoryStorage()
        runtime = LoginRuntime(_ldplayer_config(tmp_path), storage)
        monkeypatch.setattr(runtime, "_close_ldplayer", lambda task_id: closed.append(task_id))
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await eventually(lambda: runtime.get_task(task_id, body["accessToken"])["status"] == "qr_ready")
            assert storage.objects[task_id][0] == png
            await eventually(lambda: closed == [task_id])
            assert runtime._tasks[task_id].daily_status == "completed"
            message = json.loads(child.stdin.getvalue())
            assert message == {
                "type": "start", "id": task_id, "expiresAt": body["expiresAt"],
                "consolePath": str(tmp_path / "dnconsole.exe"),
                "template": "clean-template", "poolIndices": [],
                "stateDir": str(tmp_path / "state"),
                "package": "example.game", "commandTimeout": 23,
                "dailyConfigPath": str(runtime.config.daily_config_path),
                "dailyTimeoutSeconds": runtime.config.daily_timeout_seconds,
                "userOptions": {"equipmentStamina": True},
            }
            assert spawned["command"][-2:] == ["-m", "naruto_daily.login_service.ldplayer_worker"]
            assert "REDIS_URL" not in spawned["kwargs"]["env"]
            assert "OSS_ACCESS_KEY_SECRET" not in spawned["kwargs"]["env"]
            assert "LOGIN_SERVICE_PRIVATE_SECRET" not in spawned["kwargs"]["env"]
            for name in ("MULTIMODAL_API_KEY", "MULTIMODAL_BASE_URL", "MULTIMODAL_MODEL"):
                assert name not in spawned["kwargs"]["env"]
            assert {name: spawned["kwargs"]["env"][name] for name in windows_paths} == windows_paths
        finally:
            await runtime.stop()
    asyncio.run(scenario())
    assert len(closed) == 1


def test_ldplayer_worker_error_is_whitelisted_and_instance_is_closed(monkeypatch, tmp_path):
    monkeypatch.setattr("naruto_daily.login_service.runtime.FAILED_INSTANCE_HOLD_SECONDS", 0)
    child = _ProtocolChild([{
        "type": "error", "code": "qr_not_found",
        "message": "redis://user:password@private-host should never escape",
    }])
    monkeypatch.setattr("naruto_daily.login_service.runtime.subprocess.Popen", lambda *a, **k: child)
    closed = []

    async def scenario():
        runtime = LoginRuntime(_ldplayer_config(tmp_path), MemoryStorage())
        monkeypatch.setattr(runtime, "_close_ldplayer", lambda task_id: closed.append(task_id))
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await eventually(lambda: runtime.get_task(task_id, body["accessToken"])["status"] == "failed")
            view = runtime.get_task(task_id, body["accessToken"])
            assert view["error"] == SAFE_DRIVER_ERRORS["qr_not_found"]
            assert "private-host" not in json.dumps(view)
            await eventually(lambda: closed == [task_id])
            assert closed == [task_id]
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_ldplayer_worker_shutdown_closes_unfinished_instance(monkeypatch, tmp_path):
    child = _BlockingChild()
    monkeypatch.setattr("naruto_daily.login_service.runtime.subprocess.Popen", lambda *a, **k: child)
    closed = []

    async def scenario():
        runtime = LoginRuntime(_ldplayer_config(tmp_path), MemoryStorage())
        monkeypatch.setattr(runtime, "_close_ldplayer", lambda task_id: closed.append(task_id))
        await runtime.start()
        task_id = runtime.submit(submission())
        await eventually(lambda: task_id in runtime._children)
        await asyncio.wait_for(runtime.stop(), 2)
        assert child.poll() is not None
        assert closed == [task_id]
    asyncio.run(scenario())


def test_ldplayer_worker_expiry_closes_unfinished_instance(monkeypatch, tmp_path):
    monkeypatch.setattr("naruto_daily.login_service.runtime.FAILED_INSTANCE_HOLD_SECONDS", 0)
    child = _BlockingChild()
    monkeypatch.setattr("naruto_daily.login_service.runtime.subprocess.Popen", lambda *a, **k: child)
    closed = []

    async def scenario():
        runtime = LoginRuntime(_ldplayer_config(tmp_path), MemoryStorage())
        monkeypatch.setattr(runtime, "_close_ldplayer", lambda task_id: closed.append(task_id))
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await eventually(lambda: task_id in runtime._children)
            runtime._tasks[task_id].expires_at_ms = 0
            assert runtime.get_task(task_id, body["accessToken"])["status"] == "expired"
            await eventually(lambda: closed == [task_id])
            assert child.poll() is not None
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_storage_failure_is_redacted_and_sweep_retries_cleanup():
    pytest.importorskip("qrcode")

    async def scenario():
        storage = MemoryStorage()
        storage.fail_put = True
        storage.delete_failures = 1
        config = ServiceConfig(step_delay_ms=0, sweep_interval_ms=10, storage_sweep_interval_ms=10)
        runtime = LoginRuntime(config, storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await eventually(lambda: runtime.get_task(task_id, body["accessToken"])["status"] == "failed")
            assert "Secret" not in runtime.get_task(task_id, body["accessToken"])["error"]
            await eventually(lambda: task_id not in runtime._pending_deletes)
            assert storage.delete_failures == 0
            assert storage.sweep_count > 0
        finally:
            await runtime.stop()
    asyncio.run(scenario())


def test_expiry_during_upload_removes_late_object():
    pytest.importorskip("qrcode")

    async def scenario():
        upload_started = asyncio.Event()
        finish_upload = asyncio.Event()

        class DelayedStorage(MemoryStorage):
            async def put(self, task_id, png, expires_at):
                upload_started.set()
                await finish_upload.wait()
                await super().put(task_id, png, expires_at)

        storage = DelayedStorage()
        runtime = LoginRuntime(ServiceConfig(step_delay_ms=0, sweep_interval_ms=10), storage)
        await runtime.start()
        try:
            body = submission()
            task_id = runtime.submit(body)
            await asyncio.wait_for(upload_started.wait(), 5)
            runtime._tasks[task_id].expires_at_ms = 0
            assert runtime.get_task(task_id, body["accessToken"])["status"] == "expired"
            await eventually(lambda: task_id not in runtime._pending_deletes)
            finish_upload.set()
            await eventually(lambda: runtime.stats["active"] == 0)
            assert not runtime.object_available(task_id)
            assert storage.objects == {}
        finally:
            finish_upload.set()
            await runtime.stop()
    asyncio.run(scenario())
