import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.login_service.storage import AliyunObjectStorage, create_storage
from oss_fakes import FakeBucket


def expiry(seconds=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("id", ["../outside", "..\\outside", "", "f" * 36, "00000000-0000-0000-0000-000000000000"])
def test_oss_rejects_invalid_ids(id):
    async def scenario():
        storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=FakeBucket())
        for operation in (storage.head(id), storage.get(id), storage.delete(id), storage.put(id, b"png", expiry())):
            with pytest.raises(ValueError, match="Invalid object id"):
                await operation

    asyncio.run(scenario())


@pytest.mark.parametrize("prefix", ["../escape", "a/../escape", "a\\escape", "", "/absolute"])
def test_oss_rejects_unsafe_prefix(prefix):
    with pytest.raises(ValueError, match="OSS_KEY_PREFIX"):
        AliyunObjectStorage(prefix, SimpleNamespace(), bucket=FakeBucket())


def test_cancelled_oss_write_stays_serialized_with_delete(monkeypatch):
    async def scenario():
        bucket = FakeBucket()
        storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
        id = str(uuid4())
        started = threading.Event()
        resume = threading.Event()
        original_put = bucket.put_object

        def delayed_put(*args, **kwargs):
            started.set()
            assert resume.wait(timeout=5)
            return original_put(*args, **kwargs)

        monkeypatch.setattr(bucket, "put_object", delayed_put)
        write = asyncio.create_task(storage.put(id, b"png", expiry()))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            write.cancel()
            with pytest.raises(asyncio.CancelledError):
                await write
            deletion = asyncio.create_task(storage.delete(id))
            await asyncio.sleep(0.02)
            assert not deletion.done()
        finally:
            resume.set()
        await deletion
        assert f"login/{id}/qr.png" not in bucket.objects

    asyncio.run(scenario())


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
def test_create_storage_loads_sdk_lazily_with_https(monkeypatch, sts_token):
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
        key_prefix="login",
        oss_region="oss-cn-hangzhou", oss_bucket="bucket", oss_access_key_id="id",
        oss_access_key_secret="secret", oss_sts_token=sts_token,
    )
    storage = create_storage(config)
    assert isinstance(storage, AliyunObjectStorage)
    assert not captured
    asyncio.run(storage.initialize())
    assert captured["bucket"] == (("auth", "https://oss-cn-hangzhou.aliyuncs.com", "bucket"), {"connect_timeout": 10})
    assert captured["auth"] == (("id", "secret", sts_token) if sts_token else ("id", "secret"))
