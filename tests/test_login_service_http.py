"""Regression coverage for the HTTP contract consumed by the TypeScript client."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import secrets
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from oss_fakes import OSS_ENV, FakeBucket

from naruto_daily.login_service.app import create_app
from naruto_daily.login_service.config import load_config
from naruto_daily.login_service.runtime import LoginRuntime
from naruto_daily.login_service.storage import AliyunObjectStorage


def iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def task(ttl_ms=10_000):
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid4()),
        "accessToken": secrets.token_hex(32),
        "createdAt": iso(now),
        "expiresAt": iso(now + timedelta(milliseconds=ttl_ms)),
    }


@contextmanager
def service(tmp_path, **overrides):
    config = replace(
        load_config(OSS_ENV),
        **{
            "ldplayer_state_dir": tmp_path,
            "step_delay_ms": 5,
            "sweep_interval_ms": 20,
            **overrides,
        },
    )
    storage = AliyunObjectStorage(config.key_prefix, config, bucket=FakeBucket())
    runtime = LoginRuntime(config, storage)
    with TestClient(create_app(runtime)) as client:
        client.headers.update({"X-Service-Token": config.service_token})
        yield client, runtime


def get_task(client, submission, token=None):
    return client.get(
        f"/internal/tasks/{submission['id']}",
        headers={"X-Session-Token": submission["accessToken"] if token is None else token},
    )


def wait_for(read, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read()
        if predicate(last):
            return last
        time.sleep(0.025)
    raise AssertionError(f"Timed out waiting for expected state; last={last!r}")


def wait_for_status(client, submission, status):
    return wait_for(
        lambda: get_task(client, submission).json(),
        lambda view: view.get("status") == status,
    )


def test_authenticated_submission_dedup_and_png_object_contract(tmp_path):
    with service(tmp_path) as (client, runtime):
        submission = task()
        rejected = client.post(
            "/internal/tasks", json=submission, headers={"X-Service-Token": "wrong"},
        )
        assert rejected.status_code == 401
        assert rejected.headers["cache-control"] == "no-store"
        for _ in range(2):
            accepted = client.post("/internal/tasks", json=submission)
            assert accepted.status_code == 202
            assert accepted.json() == {"id": submission["id"], "status": "queued"}
        assert runtime.stats["tasks"] == 1
        conflict = {**submission, "accessToken": secrets.token_hex(32)}
        assert client.post("/internal/tasks", json=conflict).status_code == 409
        assert get_task(client, submission, "wrong").status_code == 404
        assert client.get(f"/internal/tasks/{submission['id']}").status_code == 404

        ready = wait_for_status(client, submission, "qr_ready")
        assert ready["simulation"] is True
        assert ready["id"] == submission["id"]
        assert ready["createdAt"] == submission["createdAt"]
        assert ready["expiresAt"] == submission["expiresAt"]
        assert "accessToken" not in ready
        assert submission["accessToken"] not in str(ready)

        path = f"/internal/objects/{submission['id']}/qr.png"
        head = client.head(path)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-type"] == "image/png"
        assert head.headers["cache-control"] == "no-store"
        assert head.headers["x-expires-at"] == submission["expiresAt"]
        assert client.get(path, headers={"X-Service-Token": "wrong"}).status_code == 401
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(response.content) == int(head.headers["content-length"])
        assert response.content == runtime.storage._bucket.objects[f"login/{submission['id']}/qr.png"][0]
        with Image.open(BytesIO(response.content)) as png:
            assert png.format == "PNG"
            assert png.width == png.height
            assert png.width >= 300
            png.verify()
        wait_for(lambda: runtime.stats["active"], lambda count: count == 0)


def test_health_is_public_and_internal_endpoints_require_service_token(tmp_path):
    with service(tmp_path) as (client, _runtime):
        client.headers.pop("X-Service-Token")
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok", "simulation": True, "storage": "oss",
            "active": 0, "queued": 0, "tasks": 0,
        }
        submission = task()
        assert client.post("/internal/tasks", json=submission).status_code == 401
        assert get_task(client, submission).status_code == 401
        path = f"/internal/objects/{submission['id']}/qr.png"
        assert client.head(path).status_code == 401
        assert client.get(path).status_code == 401


def test_invalid_payloads_json_and_oversized_bodies_are_rejected(tmp_path):
    with service(tmp_path) as (client, _runtime):
        valid = task()
        now = datetime.now(timezone.utc)
        invalid = [
            None, [], "not-an-object", {},
            {**valid, "id": "../other"},
            {**valid, "id": "not-a-uuid"},
            {**valid, "accessToken": "short"},
            {**valid, "accessToken": "z" * 64},
            {**valid, "expiresAt": iso(now - timedelta(seconds=1))},
            {**valid, "expiresAt": iso(now + timedelta(minutes=11))},
            {**valid, "createdAt": iso(now + timedelta(seconds=10))},
            {**valid, "createdAt": iso(now - timedelta(minutes=11))},
            {**valid, "createdAt": "not-a-date"},
            {**valid, "createdAt": "2026-13-01T00:00:00Z"},
        ]
        for payload in invalid:
            response = client.post("/internal/tasks", json=payload)
            assert response.status_code == 400, (payload, response.text)
        headers = {"Content-Type": "application/json"}
        assert client.post("/internal/tasks", content="{", headers=headers).status_code == 400
        assert client.post("/internal/tasks", content=b"\xff", headers=headers).status_code == 400
        assert client.post("/internal/tasks", content="x" * 8193, headers=headers).status_code == 413
        assert client.get("/internal/objects/not-a-uuid/qr.png").status_code == 404


def test_ready_qr_expires_and_retained_task_is_eventually_removed(tmp_path):
    with service(tmp_path, retention_ms=200) as (client, runtime):
        submission = task(ttl_ms=2000)
        assert client.post("/internal/tasks", json=submission).status_code == 202
        wait_for_status(client, submission, "qr_ready")
        path = f"/internal/objects/{submission['id']}/qr.png"
        assert client.get(path).status_code == 200
        wait_for_status(client, submission, "expired")
        assert client.get(path).status_code == 404
        assert client.head(path).status_code == 404
        wait_for(lambda: runtime.stats["tasks"], lambda count: count == 0)
        assert get_task(client, submission).status_code == 404
        assert f"login/{submission['id']}/qr.png" not in runtime.storage._bucket.objects


def test_ldplayer_mode_rejects_missing_console_configuration_at_startup(tmp_path):
    with pytest.raises(ValueError, match="LDPLAYER_CONSOLE_PATH"):
        with service(tmp_path, emulator_driver="ldplayer"):
            pass


def test_worker_storage_failures_do_not_expose_secrets(tmp_path, monkeypatch):
    secret = "credential-value-not-safe-to-return"

    async def broken_put(*_args, **_kwargs):
        raise RuntimeError(f"OSS_ACCESS_KEY_SECRET={secret}")

    with service(tmp_path) as (client, runtime):
        monkeypatch.setattr(runtime.storage, "put", broken_put)
        submission = task()
        assert client.post("/internal/tasks", json=submission).status_code == 202
        failed = wait_for_status(client, submission, "failed")
        assert failed.get("error")
        assert secret not in str(failed)
        assert submission["accessToken"] not in str(failed)
        assert client.get(f"/internal/objects/{submission['id']}/qr.png").status_code == 404


def test_object_read_errors_are_sanitized(tmp_path, monkeypatch):
    secret = "storage-secret-from-sdk-error"

    async def broken_get(*_args, **_kwargs):
        raise RuntimeError(f"OSS_ACCESS_KEY_SECRET={secret}")

    with service(tmp_path) as (client, runtime):
        submission = task()
        assert client.post("/internal/tasks", json=submission).status_code == 202
        wait_for_status(client, submission, "qr_ready")
        monkeypatch.setattr(runtime.storage, "get", broken_get)
        response = client.get(f"/internal/objects/{submission['id']}/qr.png")
        assert response.status_code == 500
        assert response.json() == {"error": "Internal service error"}
        assert response.headers["cache-control"] == "no-store"
        assert secret not in response.text
