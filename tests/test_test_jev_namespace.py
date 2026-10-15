"""Exercise production/test isolation with in-memory Redis/OSS and fake children."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.common import environment
from naruto_daily.common.namespace import isolate_oss_prefix, isolate_redis_queue_key
from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service.models import HttpError, parse_queue_task
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.refresh import RefreshRequests
from naruto_daily.login_service.run_logs import RedisRunReporter
from naruto_daily.login_service.runtime import LoginRuntime
from naruto_daily.login_service.storage import AliyunObjectStorage
from naruto_daily.multimodal import gateway
from oss_fakes import FakeBucket, OSS_ENV


@pytest.mark.parametrize("raw,expected", [
    ("naruto:login:tasks", "naruto:login-test-jev:tasks"),
    ("naruto:login-test-jev:tasks", "naruto:login-test-jev:tasks"),
    ("custom:login:tasks", "custom-test-jev:login:tasks"),
    ("tasks", "tasks-test-jev"),
    ("{login}:tasks", "{login}-test-jev:tasks"),
])
def test_queue_namespace_is_mandatory_and_idempotent(raw, expected):
    assert isolate_redis_queue_key(raw) == expected
    assert isolate_redis_queue_key(expected) == expected
    config = load_config(OSS_ENV | {"REDIS_QUEUE_KEY": raw})
    assert config.redis_queue_key == expected
    assert replace(config).redis_queue_key == expected
    assert ServiceConfig(redis_queue_key=raw).redis_queue_key == expected


def test_normalized_queue_still_obeys_length_limit():
    with pytest.raises(ValueError, match="REDIS_QUEUE_KEY"):
        load_config(OSS_ENV | {"REDIS_QUEUE_KEY": "a" * 128})


@pytest.mark.parametrize("raw,expected", [
    ("login", "login-test-jev"), ("/login/test/", "login-test-jev/test"),
    ("custom/qr", "custom-test-jev/qr"), ("login-test-jev", "login-test-jev"),
])
def test_oss_namespace_is_mandatory_and_idempotent(raw, expected):
    assert isolate_oss_prefix(raw) == expected
    assert isolate_oss_prefix(expected) == expected
    assert ServiceConfig(key_prefix=raw).key_prefix == expected
    assert load_config(OSS_ENV | {"OSS_KEY_PREFIX": raw}).key_prefix == expected


def test_shared_cloud_configuration_cannot_restore_production_names(monkeypatch, tmp_path):
    from naruto_daily.common import cloudflare_kv

    monkeypatch.setattr(environment, "_initialized", False)
    monkeypatch.setattr(environment.os, "environ", {})
    (tmp_path / ".env").write_text("CLOUDFLARE_API_TOKEN=test-token\n", encoding="utf-8")
    monkeypatch.setattr(cloudflare_kv, "fetch_environment", lambda _: OSS_ENV | {
        "REDIS_URL": "redis://shared.invalid/0", "REDIS_QUEUE_KEY": "naruto:login:tasks",
        "OSS_KEY_PREFIX": "login", "QQ_BOT_APP_ID": "1900000001",
        "QQ_BOT_APP_SECRET": "production-secret", "QQ_BOT_GROUP_OPENID": "production-group",
        "DATABASE_URL": "postgresql://production.invalid/site",
    })
    environment.initialize_environment()
    config = load_config()
    assert config.redis_queue_key == "naruto:login-test-jev:tasks"
    assert config.key_prefix == "login-test-jev"
    assert config.qq_bot_app_id == config.qq_bot_app_secret == config.qq_bot_group_openid == ""
    assert config.database_url is None
    runtime = LoginRuntime(config, storage=object())
    assert runtime._qq_notifier is runtime._user_name_resolver is None


def test_local_state_is_separate_even_with_explicit_shared_absolute_directory(tmp_path):
    for raw in ("relative-state", str(tmp_path / "shared-state")):
        config = load_config(OSS_ENV | {"LDPLAYER_STATE_DIR": raw})
        assert config.ldplayer_state_dir.name == "test-jev"
        assert replace(config).ldplayer_state_dir == config.ldplayer_state_dir
    assert ServiceConfig(ldplayer_state_dir=tmp_path).ldplayer_state_dir == tmp_path / "test-jev"
    assert load_config(OSS_ENV | {"LDPLAYER_STATE_DIR": ""}).ldplayer_state_dir == ServiceConfig.ldplayer_state_dir


def _body(config, **changes):
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    return {
        "version": 1, "id": task_id,
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "objectKey": f"{config.key_prefix}/{task_id}/qr.png",
    } | changes


async def _enqueue(client, prefix, body, **extra):
    session = {key: body[key] for key in ("id", "createdAt", "expiresAt")}
    session.update(status="queued", simulation=True, tokenHash="ab" * 32, **extra)
    await client.set(f"{prefix}:sessions:{body['id']}", json.dumps(session), px=60_000)
    await client.lpush(prefix, json.dumps(body))
    return session


def test_claim_refresh_logs_failure_cleanup_never_touch_production_namespace(monkeypatch):
    async def scenario():
        fakeredis = pytest.importorskip("fakeredis.aioredis")
        pytest.importorskip("lupa")
        client = fakeredis.FakeRedis(decode_responses=True)
        config = load_config(OSS_ENV | {"REDIS_URL": "redis://shared.invalid/0",
                                        "REDIS_QUEUE_KEY": "naruto:login:tasks", "OSS_KEY_PREFIX": "login"})
        queue = RedisTaskQueue(config, client=client)
        reporter = RedisRunReporter(config, queue)
        prefix, production = config.redis_queue_key, "naruto:login:tasks"
        body = _body(config, loginTimeoutSeconds=600)
        task_id, user_id = body["id"], str(uuid4())
        try:
            # Identical UUIDs across the two deployments must be harmless.
            await _enqueue(client, production, body | {"objectKey": f"login/{task_id}/qr.png"}, userId=user_id)
            await client.set(f"{production}:users:{user_id}:active", task_id, ex=600)
            await client.set(f"{production}:refresh:{task_id}", "production refresh", ex=600)
            await client.set(f"{production}:runs:{task_id}", "production run", ex=600)
            await client.xadd(f"{production}:logs:{task_id}", {"json": "production log"})
            before = {key: await client.dump(key) for key in await client.keys(f"{production}*")}

            await _enqueue(client, prefix, body, userId=user_id)
            await client.set(f"{prefix}:users:{user_id}:active", task_id, ex=600)
            record = await queue.pop()
            assert record is not None and record.id == task_id
            assert await client.llen(prefix) == 0
            assert await queue.set_status(record, "qr_ready")

            request_id = str(uuid4())
            await client.set(f"{prefix}:refresh:{task_id}", json.dumps({"taskId": task_id, "requestId": request_id}))
            read_keys = []
            reader = RefreshRequests("unused", production, task_id, client=SimpleNamespace(
                get=lambda key: read_keys.append(key) or json.dumps({"taskId": task_id, "requestId": request_id}),
            ))
            assert reader.poll() == request_id
            assert read_keys == [f"{prefix}:refresh:{task_id}"]
            assert await queue.pending_refresh(record, request_id)

            reporter.start()
            reporter.emit(record, status="waiting_scan")
            await reporter.flush(task_id)
            assert json.loads(await client.get(f"{prefix}:runs:{task_id}"))["status"] == "waiting_scan"
            assert await client.xlen(f"{prefix}:logs:{task_id}") == 1
            record.daily_error_code = "worker_stopped"
            assert await queue.finish_failed(record)
            assert await client.get(f"{prefix}:users:{user_id}:active") is None
            assert await client.get(f"{prefix}:refresh:{task_id}") is None
            assert before == {key: await client.dump(key) for key in await client.keys(f"{production}*")}
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_rejected_test_tasks_report_only_in_test_namespace():
    async def scenario():
        fakeredis = pytest.importorskip("fakeredis.aioredis")
        pytest.importorskip("lupa")
        client = fakeredis.FakeRedis(decode_responses=True)
        config = ServiceConfig(redis_url="redis://unused.invalid", redis_queue_key="naruto:login:tasks")
        body = _body(config, version=999)
        production = f"naruto:login:tasks:sessions:{body['id']}"
        try:
            await client.set(production, "production session")
            await _enqueue(client, config.redis_queue_key, body)
            assert await RedisTaskQueue(config, client=client).pop() is None
            assert await client.get(production) == "production session"
            assert json.loads(await client.get(f"{config.redis_queue_key}:runs:{body['id']}"))["status"] == "failed"
            assert await client.xlen(f"{config.redis_queue_key}:logs:{body['id']}") == 1
            assert await client.keys("naruto:login:tasks:logs:*") == []
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_oss_envelope_and_operations_have_matching_isolated_prefix():
    async def scenario():
        config = load_config(OSS_ENV | {"OSS_KEY_PREFIX": "login"})
        body = _body(config)
        assert parse_queue_task(body, config.key_prefix).id == body["id"]
        with pytest.raises(HttpError):
            parse_queue_task(body | {"objectKey": f"login/{body['id']}/qr.png"}, config.key_prefix)
        bucket = FakeBucket()
        storage = AliyunObjectStorage(config.key_prefix, config, bucket=bucket)
        production_key = f"login/{body['id']}/qr.png"
        bucket.objects[production_key] = (b"production qr", {})
        await storage.put(body["id"], b"test qr", body["expiresAt"])
        assert bucket.objects[body["objectKey"]][0] == b"test qr"
        await storage.delete(body["id"])
        assert body["objectKey"] not in bucket.objects
        assert bucket.objects[production_key][0] == b"production qr"
    asyncio.run(scenario())


def test_child_receives_the_same_isolated_refresh_key_and_state_directory(monkeypatch, tmp_path):
    config = ServiceConfig(redis_queue_key="naruto:login:tasks", ldplayer_state_dir=tmp_path,
                           ldplayer_console_path=Path("unused.exe"))
    record = parse_queue_task(_body(config, loginTimeoutSeconds=600), config.key_prefix)
    runtime = LoginRuntime(config, storage=object())
    messages = []

    async def child(record, command, message, **kwargs):
        messages.append(message)
        return b"png"

    monkeypatch.setattr(runtime, "_run_child", child)
    asyncio.run(runtime._run_ldplayer(record))
    assert messages[0]["refreshQueueKey"] == "naruto:login-test-jev:tasks"
    assert Path(messages[0]["stateDir"]) == tmp_path / "test-jev"


def test_model_route_counter_has_its_own_namespace(configured_environment):
    router = gateway.MultimodalGateway(gateway.load_gateway_settings({
        "YUWAN_API_KEY": "fake-key", "YUWAN_MODEL": "fake-model", "REDIS_URL": "redis://unused.invalid/0",
    }))
    router.select()
    assert configured_environment.keys == ["naruto:multimodal-test-jev:route:sequence"]
