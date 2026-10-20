"""Offline SLS migration tests: actual Redis Lua semantics, no cloud calls."""

import asyncio
from dataclasses import replace
import json
import os
import sqlite3
import threading
from uuid import uuid4

import pytest

from naruto_daily.common import environment
from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.sls_logs import Checkpoint, Checkpoints, SlsLogMirror, SlsSdkWriter
from oss_fakes import OSS_ENV


SLS_ENV = {**OSS_ENV, "REDIS_URL": "redis://fixture.invalid/0", "SLS_ENABLED": "true",
           "APP_ENV": "test", "SLS_ENDPOINT": "https://cn-shanghai.log.aliyuncs.com",
           "SLS_PROJECT": "naruto-test", "SLS_LOGSTORE": "task-events",
           "SLS_ACCESS_KEY_ID": "fixture-sls-id", "SLS_ACCESS_KEY_SECRET": "fixture-sls-secret"}


class Writer:
    def __init__(self):
        self.batches = []
        self.fail = False

    async def put(self, records):
        self.batches.append(records)
        if self.fail:
            raise RuntimeError("private SDK error credential=fixture-sls-secret")


def fixture(tmp_path, **changes):
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    pytest.importorskip("lupa")
    redis = fakeredis.FakeRedis(decode_responses=True)
    config = replace(load_config(SLS_ENV), ldplayer_state_dir=tmp_path, **changes)
    queue = RedisTaskQueue(config, client=redis)
    writer = Writer()
    mirror = SlsLogMirror(config, queue, writer=writer)
    return redis, config, writer, mirror


async def append(redis, config, task_id, number, message="正常任务节点", **fields):
    event = {"time": "2026-10-20T09:00:00.000Z", "level": "INFO", "message": message, **fields}
    return await redis.xadd(f"{config.redis_queue_key}:logs:{task_id}",
                           {"json": json.dumps(event)}, id=f"1800000000000-{number}")


def test_scan_mirrors_submission_and_rejection_without_a_running_task(tmp_path):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        first, rejected = str(uuid4()), str(uuid4())
        await append(redis, config, first, 1, "登录申请已提交")
        await append(redis, config, rejected, 1, "拒绝无效任务", level="ERROR")
        await redis.xadd("another:environment:logs:" + str(uuid4()), {"json": "{}"})
        try:
            await mirror.sync_once()
            events = [row for batch in writer.batches for row in batch if row["kind"] == "event"]
            assert {row["taskId"] for row in events} == {first, rejected}
            assert all(row["environment"] == "test" for row in events)
            assert all(row["id"] == "1800000000000-1" for row in events)
            assert all(row["streamMs"] == "1800000000000" and row["streamSeq"] == "1" for row in events)
            assert all(batch[-1]["entryCount"] == "1" for batch in writer.batches)
            await mirror.sync_once()
            assert len(writer.batches) == 2
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_bounded_batches_checkpoints_and_restart_resume(tmp_path):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        task_id = str(uuid4())
        for index in range(1, 206):
            await append(redis, config, task_id, index, actionIndex=index, actionTotal=205)
        try:
            assert await mirror.sync_task(task_id)
            assert len(writer.batches[0]) == 201
            assert writer.batches[0][-1]["entryCount"] == "200"
            assert writer.batches[0][-1]["throughId"] == "1800000000000-200"
            restarted = SlsLogMirror(config, mirror.queue, writer=writer)
            assert await restarted.sync_task(task_id)
            marker = writer.batches[-1][-1]
            assert marker["entryCount"] == "205" and marker["firstId"] == "1800000000000-1"
            assert marker["truncated"] == "false"
            assert len(writer.batches[-1]) == 6
            assert not await restarted.sync_task(task_id)
            assert (tmp_path / "sls/checkpoints.sqlite3").is_file()
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_failed_or_ambiguous_upload_replays_identical_ids_and_count(tmp_path):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        task_id = str(uuid4())
        await append(redis, config, task_id, 1)
        try:
            writer.fail = True
            with pytest.raises(RuntimeError):
                await mirror.sync_task(task_id)
            assert mirror.checkpoints.get(task_id) == Checkpoint()
            writer.fail = False
            await mirror.sync_task(task_id)
            assert writer.batches[0] == writer.batches[1]
            assert mirror.checkpoints.get(task_id).entry_count == 1
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_trim_corruption_recreation_and_expiry_are_explicit(tmp_path):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        task_id = str(uuid4())
        key = f"{config.redis_queue_key}:logs:{task_id}"
        for index in range(1, 4):
            await append(redis, config, task_id, index)
        await redis.xtrim(key, maxlen=2, approximate=False)
        await redis.xadd(key, {"json": "broken"}, id="1800000000000-4")
        try:
            await mirror.sync_task(task_id)
            marker = writer.batches[-1][-1]
            assert marker["truncated"] == "true" and marker["entryCount"] == "2"
            assert marker["firstId"] == "1800000000000-2" and marker["throughId"] == "1800000000000-4"
            await redis.delete(key)
            await append(redis, config, task_id, 10, "重新建立的流")
            await mirror.sync_task(task_id)
            assert writer.batches[-1][-1]["entryCount"] == "1"
            assert writer.batches[-1][-1]["firstId"] == "1800000000000-10"
            await redis.delete(key)
            assert not await mirror.sync_task(task_id)
            assert mirror.checkpoints.get(task_id) == Checkpoint()
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_only_public_fields_and_redacted_message_reach_sls(tmp_path):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        task_id = str(uuid4())
        await append(redis, config, task_id, 1, "token=private fixture-sls-secret",
                     accessToken="must-not-upload", username="must-not-upload", actionIndex=2, actionTotal=3)
        try:
            await mirror.sync_task(task_id)
            event = writer.batches[0][0]
            assert event["message"] == "token=[REDACTED] [REDACTED]"
            assert event["actionIndex"] == "2" and event["actionTotal"] == "3"
            assert "must-not-upload" not in json.dumps(writer.batches)
            content = (tmp_path / "sls/checkpoints.sqlite3").read_bytes()
            assert b"fixture-sls-secret" not in content and b"private" not in content
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("new_entries, expected_truncated", [(1, False), (3, False), (4, True)])
def test_redis_trimming_mirrored_entries_does_not_claim_sls_lost_them(tmp_path, new_entries, expected_truncated):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        task_id = str(uuid4())
        key = f"{config.redis_queue_key}:logs:{task_id}"
        try:
            for index in range(1, 4):
                await append(redis, config, task_id, index)
            await mirror.sync_task(task_id)
            for index in range(4, 4 + new_entries):
                await append(redis, config, task_id, index)
            await redis.xtrim(key, maxlen=3, approximate=False)
            await mirror.sync_task(task_id)
            marker = writer.batches[-1][-1]
            assert marker["truncated"] == str(expected_truncated).lower()
            assert marker["entryCount"] == str(3 + min(new_entries, 3))
            assert marker["firstId"] == "1800000000000-1"
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_recreated_stream_with_same_added_count_resets_cursor_scope(tmp_path):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        task_id = str(uuid4())
        key = f"{config.redis_queue_key}:logs:{task_id}"
        try:
            for index in range(1, 4):
                await append(redis, config, task_id, index)
            await mirror.sync_task(task_id)
            await redis.delete(key)
            for index in range(10, 13):
                await append(redis, config, task_id, index)
            await mirror.sync_task(task_id)
            marker = writer.batches[-1][-1]
            assert marker["firstId"] == "1800000000000-10"
            assert marker["entryCount"] == "3" and marker["truncated"] == "false"
        finally:
            await redis.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"app_env": "prod"}, {"redis_queue_key": "other:tasks"},
    {"sls_endpoint": "https://cn-hangzhou.log.aliyuncs.com"},
    {"sls_project": "other-project"}, {"sls_logstore": "other-store"},
])
def test_checkpoints_are_scoped_to_selected_environment_and_destination(tmp_path, change):
    config = replace(load_config(SLS_ENV), ldplayer_state_dir=tmp_path)
    first = Checkpoints(config)
    task_id = str(uuid4())
    first.save(task_id, Checkpoint("1800000000000-1", "1800000000000-1", 1))
    assert Checkpoints(replace(config, **change)).get(task_id) == Checkpoint()
    assert first.get(task_id).entry_count == 1


def test_checkpoint_cleanup_and_no_busy_loop_or_shutdown_wait(tmp_path, caplog):
    async def scenario():
        redis, config, writer, mirror = fixture(tmp_path)
        task_id = str(uuid4())
        mirror.checkpoints.save(task_id, Checkpoint())
        with sqlite3.connect(mirror.checkpoints.path) as database:
            database.execute("UPDATE checkpoints SET updated_at=0")
        mirror.checkpoints.cleanup()
        with sqlite3.connect(mirror.checkpoints.path) as database:
            assert database.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
        calls = []
        async def broken_scan():
            calls.append(1)
            raise RuntimeError("credential=fixture-sls-secret")
        mirror.sync_once = broken_scan
        mirror.interval = 0.05
        mirror.start()
        await asyncio.sleep(0.13)
        await asyncio.wait_for(mirror.close(), 0.5)
        assert 2 <= len(calls) <= 4
        await redis.aclose()
    asyncio.run(scenario())
    assert "fixture-sls-secret" not in caplog.text
    assert caplog.text.count("SLS 日志同步暂不可用") == 1


def test_sdk_work_does_not_block_event_loop_or_cancellation():
    entered, release = threading.Event(), threading.Event()
    writer = object.__new__(SlsSdkWriter)
    def slow_put(records):
        entered.set()
        release.wait(5)
    writer._put = slow_put
    async def scenario():
        task = asyncio.create_task(writer.put([]))
        while not entered.is_set():
            await asyncio.sleep(0)
        marker = []
        await asyncio.sleep(0)
        marker.append("game continues")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert marker == ["game continues"]
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_official_sdk_builds_compressed_batch_with_original_time(monkeypatch):
    pytest.importorskip("aliyun.log")
    writer = SlsSdkWriter(load_config(SLS_ENV))
    requests = []
    monkeypatch.setattr(writer.client, "put_logs", requests.append)
    fields = {"kind": "event", "taskId": str(uuid4()), "environment": "test",
              "id": "1800000000123-0", "streamMs": "1800000000123", "streamSeq": "0",
              "time": "2026-10-20T09:00:00.000Z", "level": "INFO", "message": "测试"}
    writer._put([fields])
    request = requests[0]
    assert request.get_project() == "naruto-test" and request.get_logstore() == "task-events"
    assert request.get_compress() is True
    item = request.get_log_items()[0]
    assert item.get_time() == 1800000000
    assert item.get_time_nano_part() == 123_000_000
    assert dict(item.get_contents()) == fields
    assert writer.client.http_type == "https://" and writer.client.timeout == 5


@pytest.mark.parametrize("change", [
    {"SLS_ENABLED": "yes"}, {"SLS_ENDPOINT": "http://cn-shanghai.log.aliyuncs.com"},
    {"SLS_ENDPOINT": "https://credential@cn-shanghai.log.aliyuncs.com"},
    {"SLS_ENDPOINT": "https://cn-shanghai.log.aliyuncs.com/path"},
    {"SLS_PROJECT": ""}, {"SLS_LOGSTORE": "bad space"},
    {"SLS_ACCESS_KEY_ID": ""}, {"SLS_ACCESS_KEY_SECRET": ""},
    {"SLS_STS_TOKEN": "bad\ntoken"}, {"REDIS_URL": ""}, {"APP_ENV": "unknown"},
])
def test_enabled_configuration_is_strict_and_never_reports_secret_values(change):
    with pytest.raises(ValueError) as caught:
        load_config({**SLS_ENV, **change})
    assert "fixture-sls-secret" not in str(caught.value)


def test_disabled_default_and_cloud_only_configuration(tmp_path):
    assert not load_config(OSS_ENV).sls_enabled
    assert not ServiceConfig().sls_enabled
    config = load_config(SLS_ENV)
    assert config.app_env == "test"
    assert "fixture-sls-secret" not in repr(config) and "fixture-sls-id" not in repr(config)
    local = tmp_path / ".env"
    local.write_text("SLS_ENABLED=true\nSLS_ACCESS_KEY_SECRET=local-secret\n", encoding="utf-8")
    assert environment._read_local_dotenv(local) == {}
    assert all(name in environment.CLOUD_ENV_NAMES for name in SLS_ENV if name.startswith("SLS_"))


def test_cloud_sls_configuration_clears_stale_values_and_never_enters_child(monkeypatch, tmp_path):
    from naruto_daily.common import cloudflare_kv
    names = [name for name in environment.CLOUD_ENV_NAMES if name.startswith("SLS_")]
    monkeypatch.setattr(environment, "_initialized", False)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.update({name: "stale-secret" for name in names})
    local = tmp_path / ".env"
    local.write_text("SLS_ENABLED=true\nSLS_ACCESS_KEY_SECRET=local-secret\nAPP_ENV=test\n", encoding="utf-8")
    remote = {name: value for name, value in SLS_ENV.items() if name != "APP_ENV"}
    monkeypatch.setattr(cloudflare_kv, "fetch_environment", lambda _: dict(remote))
    environment.initialize_environment(local)
    assert load_config().sls_enabled
    assert load_config().app_env == "test"
    assert all(name not in environment.model_environment_for_child() for name in names)
    environment._initialized = False
    remote.clear()
    remote.update(OSS_ENV)
    environment.initialize_environment(local)
    assert all(name not in os.environ for name in names)
    assert not load_config().sls_enabled


def test_runtime_starts_and_stops_mirror_only_when_enabled(monkeypatch, tmp_path):
    from naruto_daily.login_service import sls_logs
    from naruto_daily.login_service.runtime import LoginRuntime
    from test_login_service_runtime import MemoryStorage

    calls = []
    class Mirror:
        def __init__(self, config, queue):
            calls.append(("create", config.app_env))
        def start(self):
            calls.append("start")
        async def close(self):
            calls.append("close")
    monkeypatch.setattr(sls_logs, "SlsLogMirror", Mirror)

    async def scenario():
        for enabled in (False, True):
            redis, config, _, mirror = fixture(tmp_path, sls_enabled=enabled)
            runtime = LoginRuntime(config, MemoryStorage(), redis_queue=mirror.queue)
            try:
                await runtime.start()
                await runtime.stop()
                assert calls == ([("create", "test"), "start", "close"] if enabled else [])
            finally:
                await runtime.stop()
                await redis.aclose()
    asyncio.run(scenario())
