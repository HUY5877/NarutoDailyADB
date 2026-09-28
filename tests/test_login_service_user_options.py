"""Check per-user daily options across queue parsing and child dispatch."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import ServiceConfig
from naruto_daily.login_service.models import (
    HttpError, TaskRecord, TaskValidationError, parse_queue_task, parse_submission,
)
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.runtime import LoginRuntime
from naruto_daily.login_service.user_options import parse_user_options


INVALID_OPTIONS = [
    None, [], "false", False, 0,
    {"equipmentStamina": None}, {"equipmentStamina": "false"},
    {"equipmentStamina": 0}, {"equipmentStamina": 1},
    {"equipmentStamina": []}, {"equipmentStamina": {}},
    {"unknownOption": True}, {"equipmentStamina": False, "unknownOption": True},
]


def queue_body():
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    return {
        "version": 1,
        "id": task_id,
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "objectKey": f"login/{task_id}/qr.png",
    }


def parse_http(body):
    return parse_submission(body | {"accessToken": "ab" * 32})


def parse_queue(body):
    return parse_queue_task(body, "login")


def test_user_options_defaults_are_independent():
    first = parse_user_options()
    second = parse_user_options({})
    assert first == second == {"equipmentStamina": True}
    first["equipmentStamina"] = False
    assert second == parse_user_options() == {"equipmentStamina": True}


@pytest.mark.parametrize("value", INVALID_OPTIONS)
def test_user_options_parser_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_user_options(value)


@pytest.mark.parametrize("parse", [parse_queue, parse_http])
@pytest.mark.parametrize("options", [{}, {"userOptions": {}}])
def test_legacy_tasks_default_to_equipment_stamina_enabled(parse, options):
    assert parse(queue_body() | options).user_options == {"equipmentStamina": True}


@pytest.mark.parametrize("parse", [parse_queue, parse_http])
@pytest.mark.parametrize("enabled", [True, False])
def test_explicit_options_are_preserved_without_sharing_input_or_records(parse, enabled):
    supplied = {"equipmentStamina": enabled}
    body = queue_body() | {"userOptions": supplied}
    first, second = parse(body), parse(body)
    assert first.user_options == second.user_options == supplied
    assert first.user_options is not supplied
    assert first.user_options is not second.user_options
    first.user_options["equipmentStamina"] = not enabled
    assert supplied == second.user_options == {"equipmentStamina": enabled}


def test_task_record_defaults_do_not_share_mutable_options():
    body = queue_body()
    args = (body["id"], "", body["createdAt"], body["expiresAt"], 0)
    first, second = TaskRecord(*args), TaskRecord(*args)
    assert first.user_options == second.user_options == {"equipmentStamina": True}
    first.user_options["equipmentStamina"] = False
    assert second.user_options == {"equipmentStamina": True}


@pytest.mark.parametrize("value", INVALID_OPTIONS)
def test_queue_rejects_invalid_options_as_invalid_payload(value):
    with pytest.raises(TaskValidationError) as exc:
        parse_queue(queue_body() | {"userOptions": value})
    assert exc.value.status == 400
    assert exc.value.code == "invalid_task_payload"


@pytest.mark.parametrize("value", INVALID_OPTIONS)
def test_http_submission_rejects_invalid_options(value):
    with pytest.raises(HttpError) as exc:
        parse_http(queue_body() | {"userOptions": value})
    assert exc.value.status == 400


@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_passes_complete_options_to_daily_child(monkeypatch, enabled):
    record = parse_queue(queue_body() | {"userOptions": {"equipmentStamina": enabled}})
    config = ServiceConfig(ldplayer_console_path=Path("unused-dnconsole.exe"))
    # The runtime never starts: injected storage and child transport keep this
    # dispatch contract test entirely in memory, without devices or services.
    runtime = LoginRuntime(config, storage=object())
    captured = []

    async def run_child(task, command, message, *, daily=False):
        captured.append((task, command, json.loads(json.dumps(message)), daily))
        return b"test-png"

    monkeypatch.setattr(runtime, "_run_child", run_child)
    assert asyncio.run(runtime._run_ldplayer(record)) == b"test-png"
    assert len(captured) == 1
    task, command, message, daily = captured[0]
    assert task is record
    assert command[-2:] == ["-m", "naruto_daily.login_service.ldplayer_worker"]
    assert daily is True
    assert message["id"] == record.id
    assert message["userOptions"] == {"equipmentStamina": enabled}


def test_redis_claim_and_requeue_keep_disabled_equipment_stamina():
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    pytest.importorskip("lupa")

    async def scenario():
        client = fakeredis.FakeRedis(decode_responses=True)
        try:
            config = ServiceConfig(redis_url="redis://unused.invalid", redis_queue_key="test:options:tasks")
            queue = RedisTaskQueue(config, client=client)
            next_worker = RedisTaskQueue(config, client=client)
            body = queue_body() | {"userOptions": {"equipmentStamina": False}}
            session = {
                "id": body["id"], "status": "queued", "createdAt": body["createdAt"],
                "expiresAt": body["expiresAt"], "simulation": True, "tokenHash": "ab" * 32,
            }
            payload = json.dumps(body)
            await client.set(f"{config.redis_queue_key}:sessions:{body['id']}", json.dumps(session), px=60_000)
            await client.lpush(config.redis_queue_key, payload)
            record = await queue.pop()
            assert record is not None
            assert record.user_options == {"equipmentStamina": False}
            assert await queue.requeue(record)
            assert await client.lrange(config.redis_queue_key, 0, -1) == [payload]
            reclaimed = await next_worker.pop()
            assert reclaimed is not None
            assert reclaimed.user_options == {"equipmentStamina": False}
        finally:
            await client.aclose()

    asyncio.run(scenario())
