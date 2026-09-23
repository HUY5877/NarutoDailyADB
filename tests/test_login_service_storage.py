import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.login_service.storage import AliyunObjectStorage, LocalObjectStorage, create_storage


def expiry(seconds=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def test_local_storage_round_trip_and_delete(tmp_path):
    async def scenario():
        storage = LocalObjectStorage(tmp_path, "login/test")
        await storage.initialize()
        id = str(uuid4())
        expires_at = expiry()
        assert await storage.head(id) is None
        await storage.put(id, b"png bytes", expires_at)
        assert (tmp_path / "login" / "test" / id / "qr.png").read_bytes() == b"png bytes"
        assert json.loads((tmp_path / "login" / "test" / id / "metadata.json").read_text()) == {"expiresAt": expires_at}
        metadata = await storage.head(id)
        assert metadata.content_length == 9
        assert metadata.content_type == "image/png"
        assert metadata.expires_at == expires_at
        assert metadata.body is None
        assert (await storage.get(id)).body == b"png bytes"
        assert not list(tmp_path.rglob("*.tmp"))
        await storage.delete(id)
        await storage.delete(id)
        assert await storage.get(id) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("id", ["../outside", "..\\outside", "", "f" * 36, "00000000-0000-0000-0000-000000000000"])
def test_local_storage_rejects_invalid_ids(tmp_path, id):
    async def scenario():
        storage = LocalObjectStorage(tmp_path)
        for operation in (storage.head(id), storage.get(id), storage.delete(id), storage.put(id, b"png", expiry())):
            with pytest.raises(ValueError, match="Invalid object id"):
                await operation

    asyncio.run(scenario())


@pytest.mark.parametrize("prefix", ["../escape", "a/../escape", "a\\escape", "", "/absolute"])
def test_local_storage_rejects_unsafe_prefix(tmp_path, prefix):
    with pytest.raises(ValueError, match="OSS_KEY_PREFIX"):
        LocalObjectStorage(tmp_path, prefix)


def test_local_storage_hides_expired_and_cleans_corrupt_metadata(tmp_path):
    async def scenario():
        storage = LocalObjectStorage(tmp_path)
        await storage.initialize()
        expired_id = str(uuid4())
        await storage.put(expired_id, b"old png", expiry(-1))
        assert await storage.head(expired_id) is None
        assert await storage.get(expired_id) is None
        corrupt_ids = []
        for raw_metadata in ["null", "[1]", "{}", "{", '{"expiresAt":"not-a-date"}']:
            id = str(uuid4())
            corrupt_ids.append(id)
            directory = storage.root / id
            directory.mkdir()
            (directory / "qr.png").write_bytes(b"corrupt")
            (directory / "metadata.json").write_text(raw_metadata)
            assert await storage.head(id) is None
            assert await storage.get(id) is None
        await storage.sweep()
        assert not (storage.root / expired_id).exists()
        assert all(not (storage.root / id).exists() for id in corrupt_ids)

    asyncio.run(scenario())


def test_local_storage_cleans_old_orphans_preserving_recent_writes(tmp_path):
    async def scenario():
        storage = LocalObjectStorage(tmp_path)
        storage.root.mkdir(parents=True)
        old = storage.root / str(uuid4())
        recent = storage.root / str(uuid4())
        other = storage.root / "unrelated-directory"
        for directory in [old, recent, other]:
            directory.mkdir()
            (directory / "partial.tmp").write_bytes(b"partial")
        os.utime(old, (time.time() - 601, time.time() - 601))
        await storage.initialize()
        assert not old.exists()
        assert recent.exists()
        assert other.exists()

    asyncio.run(scenario())


def test_local_storage_rejects_symlink_escape_before_delete(tmp_path):
    storage = LocalObjectStorage(tmp_path / "objects")
    storage.root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    keep = external / "keep.txt"
    keep.write_text("keep")
    id = str(uuid4())
    try:
        (storage.root / id).symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this Windows account")
    with pytest.raises(ValueError, match="escapes storage root"):
        asyncio.run(storage.delete(id))
    assert keep.read_text() == "keep"


def test_cancelled_write_stays_serialized_with_delete(tmp_path, monkeypatch):
    async def scenario():
        storage = LocalObjectStorage(tmp_path)
        await storage.initialize()
        id = str(uuid4())
        started = threading.Event()
        resume = threading.Event()
        original_replace = Path.replace

        def delayed_replace(path, target):
            if path.name.startswith("metadata.json."):
                started.set()
                assert resume.wait(timeout=5)
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", delayed_replace)
        write = asyncio.create_task(storage.put(id, b"png", expiry()))
        assert await asyncio.to_thread(started.wait, 5)
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        deletion = asyncio.create_task(storage.delete(id))
        await asyncio.sleep(0.02)
        assert not deletion.done()
        resume.set()
        await deletion
        assert not (storage.root / id).exists()

    asyncio.run(scenario())


class MissingObject(Exception):
    status = 404


class FakeResult:
    def __init__(self, headers, body=b""):
        self.headers = headers
        self.body = body
        self.closed = False

    def read(self):
        return self.body

    def close(self):
        self.closed = True


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.last_result = None

    def put_object(self, key, body, headers):
        self.objects[key] = (body, dict(headers, **{"Content-Length": str(len(body))}))

    def head_object(self, key):
        if key not in self.objects:
            raise MissingObject()
        body, headers = self.objects[key]
        return FakeResult(headers, body)

    def get_object(self, key):
        self.last_result = self.head_object(key)
        return self.last_result

    def delete_object(self, key):
        self.objects.pop(key, None)


def test_oss_contract_and_expiry():
    async def scenario():
        bucket = FakeBucket()
        storage = AliyunObjectStorage("login/test", SimpleNamespace(), bucket=bucket)
        await storage.initialize()
        id = str(uuid4())
        assert await storage.head(id) is None
        assert await storage.get(id) is None
        expires_at = expiry()
        await storage.put(id, b"png", expires_at)
        key = f"login/test/{id}/qr.png"
        headers = bucket.objects[key][1]
        assert headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "no-store"
        assert headers["x-oss-object-acl"] == "private"
        assert headers["x-oss-meta-expires-at"] == expires_at
        assert (await storage.head(id)).content_length == 3
        assert (await storage.get(id)).body == b"png"
        assert bucket.last_result.closed
        await storage.put(id, b"expired", expiry(-1))
        assert await storage.head(id) is None
        assert await storage.get(id) is None
        assert bucket.last_result.closed
        await storage.sweep()
        assert key not in bucket.objects
        await storage.delete(id)
        with pytest.raises(ValueError, match="Invalid object id"):
            await storage.put("../bad", b"png", expiry())

    asyncio.run(scenario())


@pytest.mark.parametrize("sts_token", [None, "temporary-token"])
def test_create_storage_loads_sdk_lazily_with_https(tmp_path, monkeypatch, sts_token):
    captured = {}

    def auth(*arguments):
        captured["auth"] = arguments
        return "auth"

    def bucket(*arguments, **kwargs):
        captured["bucket"] = (arguments, kwargs)
        return FakeBucket()

    sdk = SimpleNamespace(Auth=auth, StsAuth=auth, Bucket=bucket)
    monkeypatch.setattr("naruto_daily.login_service.storage.importlib.import_module", lambda name: sdk)
    config = SimpleNamespace(
        storage_driver="local", local_storage_dir=tmp_path, key_prefix="login",
        oss_region="oss-cn-hangzhou", oss_bucket="bucket", oss_access_key_id="id",
        oss_access_key_secret="secret", oss_sts_token=sts_token,
    )
    assert isinstance(create_storage(config), LocalObjectStorage)
    assert not captured
    config.storage_driver = "oss"
    storage = create_storage(config)
    assert not captured
    asyncio.run(storage.initialize())
    assert captured["bucket"] == (("auth", "https://oss-cn-hangzhou.aliyuncs.com", "bucket"), {"connect_timeout": 10})
    assert captured["auth"] == (("id", "secret", sts_token) if sts_token else ("id", "secret"))
