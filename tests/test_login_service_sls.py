"""Offline direct SLS producer tests; no Redis or cloud calls."""

import asyncio
from dataclasses import replace
import json
import os
import threading
from uuid import uuid4

import pytest

from naruto_daily.common import environment
from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service import sls_logs
from naruto_daily.login_service.sls_logs import SlsSdkWriter
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
        self.batches.append([dict(row) for row in records])
        if self.fail:
            raise RuntimeError("private SDK error credential=fixture-sls-secret")


def producer(tmp_path, **options):
    config = replace(load_config(SLS_ENV), ldplayer_state_dir=tmp_path)
    writer = Writer()
    publisher = getattr(sls_logs, "SlsLogPublisher", None)
    assert publisher is not None, "日志应从内存直接上报，而不是读取 Redis 流"
    return writer, publisher(config, writer=writer, interval=0.01, **options)


def event(message="正常任务节点", **fields):
    return {"time": "2026-10-20T09:00:00.000Z", "level": "INFO", "message": message, **fields}


def events(writer):
    return [row for batch in writer.batches for row in batch if row["kind"] == "event"]


def test_emit_only_enqueues_and_close_uploads_without_redis_or_disk(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path)
        files_before = list(tmp_path.iterdir())
        task_id = str(uuid4())
        first = publisher.emit(task_id, event("任务开始"))
        last = publisher.emit(task_id, event("任务完成", actionIndex=1, actionTotal=1))
        assert writer.batches == []
        assert first != last
        publisher.finish(task_id)
        publisher.start()
        await publisher.close()
        rows = events(writer)
        assert [row["id"] for row in rows] == [first, last]
        assert all(row.get("firstId") == first for row in rows)
        assert [row["message"] for row in rows] == ["任务开始", "任务完成"]
        assert all(row["environment"] == "test" for row in rows)
        marker = writer.batches[-1][-1]
        assert marker["firstId"] == first and marker["throughId"] == last
        assert marker["entryCount"] == "2" and marker["truncated"] == "false"
        assert list(tmp_path.iterdir()) == files_before
        assert publisher.emit(task_id, event()) is None
    asyncio.run(scenario())


def test_failed_or_ambiguous_batch_retries_identical_ids_and_marker(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path, retry_interval=0.01)
        writer.fail = True
        task_id = str(uuid4())
        publisher.emit(task_id, event())
        publisher.start()
        while not writer.batches:
            await asyncio.sleep(0.001)
        publisher.emit(task_id, event("第二条"))
        writer.fail = False
        await publisher.close()
        assert writer.batches[0] == writer.batches[1]
        assert writer.batches[-1][-1]["entryCount"] == "2"
    asyncio.run(scenario())


def test_overflow_keeps_latest_and_marks_gap_in_checkpoint(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path, buffer_limit=3, batch_size=2)
        task_id = str(uuid4())
        ids = [publisher.emit(task_id, event(str(i))) for i in range(8)]
        publisher.finish(task_id)
        publisher.start()
        await publisher.close()
        assert [row["message"] for row in events(writer)] == ["5", "6", "7"]
        assert writer.batches[-1][-1]["throughId"] == ids[-1]
        assert writer.batches[-1][-1]["entryCount"] == "3"
        assert writer.batches[-1][-1]["truncated"] == "true"
    asyncio.run(scenario())


def test_public_fields_redaction_and_bounded_message(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path)
        task_id = str(uuid4())
        publisher.emit(task_id, event("token=private fixture-sls-secret", username="must-not-upload",
                                       accessToken="must-not-upload", actionIndex=2, actionTotal=3))
        publisher.emit(task_id, event("中文" * 2000))
        publisher.start()
        await publisher.close()
        rows = events(writer)
        assert rows[0]["message"] == "token=[REDACTED] [REDACTED]"
        assert rows[0]["actionIndex"] == "2" and rows[0]["actionTotal"] == "3"
        assert len(rows[1]["message"]) <= 1000
        assert "must-not-upload" not in json.dumps(writer.batches)
    asyncio.run(scenario())


def test_slow_upload_does_not_block_emit_or_bounded_shutdown(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path)
        entered = asyncio.Event()
        async def slow(records):
            entered.set()
            await asyncio.Event().wait()
        writer.put = slow
        publisher.emit(str(uuid4()), event())
        publisher.start()
        await asyncio.wait_for(entered.wait(), 0.5)
        assert publisher.emit(str(uuid4()), event("执行继续"))
        await asyncio.wait_for(publisher.close(timeout=0.02), 0.5)
    asyncio.run(scenario())


def test_failed_uploads_back_off_and_never_log_sdk_secrets(tmp_path, caplog):
    async def scenario():
        writer, publisher = producer(tmp_path, retry_interval=0.05)
        writer.fail = True
        publisher.emit(str(uuid4()), event())
        publisher.start()
        await asyncio.sleep(0.12)
        await publisher.close(timeout=0.01)
        assert 2 <= len(writer.batches) <= 4
    asyncio.run(scenario())
    assert "fixture-sls-secret" not in caplog.text


def test_ids_stay_ordered_if_clock_moves_backwards_and_are_distinct_between_producers(tmp_path, monkeypatch):
    writer, first = producer(tmp_path)
    _, second = producer(tmp_path)
    monkeypatch.setattr(sls_logs.time, "time", lambda: 1800000000.0)
    task_id = str(uuid4())
    ids = [first.emit(task_id, event()), first.emit(task_id, event())]
    monkeypatch.setattr(sls_logs.time, "time", lambda: 1700000000.0)
    ids.append(first.emit(task_id, event()))
    assert [tuple(map(int, i.split('-'))) for i in ids] == sorted(tuple(map(int, i.split('-'))) for i in ids)
    assert second.emit(task_id, event()) not in ids
    assert all(int(i.split('-')[1]) < 2**53 for i in ids)


def test_new_owner_observes_old_cursor_before_generating_logs(tmp_path, monkeypatch):
    _, publisher = producer(tmp_path)
    observe = getattr(publisher, "observe_cursor", None)
    assert observe is not None, "重新领取任务必须接续旧游标"
    monkeypatch.setattr(sls_logs.time, "time", lambda: 1700000000.0)
    observe("1800000000000-9007199254740990")
    item_id = publisher.emit(str(uuid4()), event())
    assert tuple(map(int, item_id.split('-'))) > (1800000000000, 9007199254740990)
    for bad in (None, "", "-1-0", "10-x", "999999999999999999999-0"):
        observe(bad)


def test_reclaimed_task_marks_unknown_previous_uploads_as_truncated(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path)
        task_id = str(uuid4())
        publisher.observe_cursor("1800000000000-1", task_id=task_id)
        publisher.emit(task_id, event())
        await publisher.close()
        assert writer.batches[-1][-1]["truncated"] == "true"
    asyncio.run(scenario())


def test_byte_bound_and_invalid_events_do_not_escape_logger(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path, byte_limit=1024)
        task_id = str(uuid4())
        assert publisher.emit(task_id, event("文" * 1000)) is None
        assert publisher.emit("not-a-task", event()) is None
        assert publisher.emit(task_id, event(level={})) is None
        assert publisher.emit(task_id, event(actionIndex=3, actionTotal=2)) is None
        assert publisher.emit(task_id, event(time="not-a-dateZ")) is None
        assert publisher.emit(task_id, event("tail"))
        await publisher.close()
        assert [row["message"] for row in events(writer)] == ["tail"]
        assert writer.batches[-1][-1]["truncated"] == "true"
    asyncio.run(scenario())


def test_inflight_events_survive_overflow_and_completed_task_can_be_reused(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path, buffer_limit=4, batch_size=2)
        entered, release = asyncio.Event(), asyncio.Event()
        put = writer.put
        async def hold_first(records):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await put(records)
        writer.put = hold_first
        first, second = str(uuid4()), str(uuid4())
        publisher.emit(first, event("first-a"))
        publisher.emit(second, event("second-a"))
        publisher.start()
        await asyncio.wait_for(entered.wait(), 0.5)
        for i in range(5):
            publisher.emit(first, event(f"first-{i}"))
        publisher.finish(first)
        publisher.finish(second)
        release.set()
        while len(events(writer)) < 4:
            await asyncio.sleep(0.001)
        assert [row["message"] for row in events(writer)] == ["first-a", "second-a", "first-3", "first-4"]
        restarted_id = publisher.emit(first, event("reclaimed"))
        publisher.finish(first)
        await publisher.close()
        marker = writer.batches[-1][-1]
        assert marker["firstId"] == restarted_id and marker["entryCount"] == "1"
        assert events(writer)[-1]["message"] == "reclaimed"
    asyncio.run(scenario())


def test_overflow_after_batch_started_publishes_corrected_gap_marker(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path, byte_limit=1024)
        entered, release = asyncio.Event(), asyncio.Event()
        put = writer.put
        async def hold_first(records):
            await put(records)
            if not entered.is_set():
                entered.set()
                await release.wait()
        writer.put = hold_first
        task_id = str(uuid4())
        last = publisher.emit(task_id, event("accepted"))
        publisher.start()
        await asyncio.wait_for(entered.wait(), 0.5)
        assert publisher.emit(task_id, event("文" * 1000)) is None
        publisher.finish(task_id)
        release.set()
        await publisher.close()
        assert events(writer)[0]["id"] == last
        marker = writer.batches[-1][-1]
        assert marker["throughId"] == last and marker["entryCount"] == "1"
        assert marker["truncated"] == "true"
    asyncio.run(scenario())


def test_other_tasks_cannot_evict_an_accepted_final_log(tmp_path):
    async def scenario():
        writer, publisher = producer(tmp_path, buffer_limit=3, batch_size=1)
        entered, release = asyncio.Event(), asyncio.Event()
        put = writer.put
        async def hold_first(records):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await put(records)
        writer.put = hold_first
        first, second = str(uuid4()), str(uuid4())
        publisher.emit(first, event("first-start"))
        publisher.start()
        await asyncio.wait_for(entered.wait(), 0.5)
        tail = publisher.emit(first, event("first-finish"))
        publisher.finish(first)
        publisher.emit(second, event("second-start"))
        publisher.emit(second, event("second-finish"))
        release.set()
        await publisher.close()
        assert tail in {row["id"] for row in events(writer)}
    asyncio.run(scenario())


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
