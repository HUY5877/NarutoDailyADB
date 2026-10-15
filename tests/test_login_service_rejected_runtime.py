"""Rejected Redis jobs report failure before reserving an emulator pool slot."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import ServiceConfig
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.runtime import LoginRuntime


def test_validation_failure_is_visible_without_reserving_an_instance_and_consumer_continues(tmp_path):
    async def scenario():
        fakeredis = pytest.importorskip("fakeredis.aioredis")
        pytest.importorskip("lupa")
        client = fakeredis.FakeRedis(decode_responses=True)
        config = ServiceConfig(
            redis_url="redis://unused.invalid/0", redis_queue_key="test:rejected-runtime",
            emulator_driver="ldplayer", ldplayer_console_path=tmp_path / "unused.exe",
            ldplayer_pool_indices=(0, 1, 2),
        )
        queue = RedisTaskQueue(config, client=client)
        # Neither storage nor a real emulator is touched by this admission test.
        runtime = LoginRuntime(config, storage=object(), redis_queue=queue)
        allocations, processed = [], []

        class Pool:
            def has_capacity(self):
                return True

            def allocate(self, task_id, _deadline):
                allocations.append(task_id)

        runtime._manager = Pool()

        async def process(record):
            processed.append(record.id)
            runtime._stopping = True

        runtime._process_record = process
        now = datetime.now(timezone.utc)

        async def enqueue(version):
            task_id = str(uuid4())
            payload = {
                "version": version, "id": task_id,
                "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "expiresAt": (now + timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "objectKey": f"login-test-jev/{task_id}/qr.png",
            }
            session = {key: payload[key] for key in ("id", "createdAt", "expiresAt")}
            session.update(status="queued", simulation=True, tokenHash="ab" * 32)
            await client.set(f"{config.redis_queue_key}:sessions:{task_id}", json.dumps(session), px=60_000)
            await client.lpush(config.redis_queue_key, json.dumps(payload))
            return task_id

        try:
            rejected, accepted = await enqueue(2), await enqueue(1)
            await asyncio.wait_for(runtime._consume_redis(), timeout=5)
            assert allocations == processed == [accepted]
            assert rejected not in runtime._tasks
            assert runtime._children == {}
            prefix = config.redis_queue_key
            session = json.loads(await client.get(f"{prefix}:sessions:{rejected}"))
            run = json.loads(await client.get(f"{prefix}:runs:{rejected}"))
            logs = await client.xrange(f"{prefix}:logs:{rejected}")
            assert session["status"] == run["status"] == "failed"
            assert session["errorCode"] == run["errorCode"] == "invalid_task_payload"
            assert len(logs) == 1
            event = json.loads(logs[0][1]["json"])
            assert event["level"] == "ERROR"
            assert event["message"] == session["error"]
        finally:
            await queue.close()

    asyncio.run(scenario())
