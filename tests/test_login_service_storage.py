import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.login_service.storage import AliyunObjectStorage, create_storage
from oss_fakes import FakeBucket


PNG = b"\x89PNG\r\n\x1a\nexception-fixture"


class ObjectAlreadyExists(Exception):
    status = 409
    code = "FileAlreadyExists"


class ConditionalBucket(FakeBucket):
    def __init__(self):
        super().__init__()
        self.put_keys = []

    def put_object(self, key, body, headers):
        self.put_keys.append(key)
        if headers.get("x-oss-forbid-overwrite") == "true" and key in self.objects:
            raise ObjectAlreadyExists()
        super().put_object(key, body, headers)


def expiry(seconds=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("id", ["../outside", "..\\outside", "", "f" * 36, "00000000-0000-0000-0000-000000000000"])
def test_oss_rejects_invalid_ids(id):
    async def scenario():
        storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=FakeBucket())
        for operation in (storage.head(id), storage.get(id), storage.delete(id), storage.put(id, b"png", expiry()),
                          storage.put_exception(id, 1, PNG)):
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


def test_exception_images_are_private_and_survive_qr_cleanup():
    async def scenario():
        bucket = FakeBucket()
        storage = AliyunObjectStorage("login/test", SimpleNamespace(), bucket=bucket)
        task_id = str(uuid4())

        assert await storage.put_exception(task_id, 1, PNG) == 1
        key = f"login/test/exceptions/{task_id}_1.png"
        body, headers = bucket.objects[key]
        assert body == PNG
        assert headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "no-store"
        assert headers["x-oss-object-acl"] == "private"
        assert headers["x-oss-forbid-overwrite"] == "true"
        assert "x-oss-meta-expires-at" not in headers

        await storage.put(task_id, b"expired qr", expiry(-1))
        await storage.sweep()
        await storage.delete(task_id)
        assert bucket.objects == {key: (body, headers)}

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, -1, True, False, 1.0, "1", None])
def test_exception_upload_rejects_invalid_counts(count):
    bucket = FakeBucket()
    storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
    with pytest.raises(ValueError, match="count"):
        asyncio.run(storage.put_exception(str(uuid4()), count, PNG))
    assert not bucket.objects


@pytest.mark.parametrize("case", ["empty", "wrong_signature", "not_bytes", "oversize"])
def test_exception_upload_rejects_invalid_images(case):
    png = {
        "empty": b"", "wrong_signature": b"not png", "not_bytes": "png",
        "oversize": PNG + bytes(16 * 1024 * 1024 + 1 - len(PNG)),
    }[case]
    bucket = FakeBucket()
    storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
    with pytest.raises(ValueError, match="PNG"):
        asyncio.run(storage.put_exception(str(uuid4()), 1, png))
    assert not bucket.objects


def test_exception_upload_accepts_the_image_size_limit():
    png = PNG + bytes(16 * 1024 * 1024 - len(PNG))
    bucket = FakeBucket()
    storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
    assert asyncio.run(storage.put_exception(str(uuid4()), 1, png)) == 1
    assert next(iter(bucket.objects.values()))[0] == png


def test_exception_upload_advances_counter_without_overwriting_previous_attempts():
    async def scenario():
        bucket = ConditionalBucket()
        task_id = str(uuid4())
        first = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
        assert await first.put_exception(task_id, 1, PNG + b"first") == 1
        assert await first.put_exception(task_id, 2, PNG + b"second") == 2

        restarted = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
        assert await restarted.put_exception(task_id, 1, PNG + b"third") == 3
        assert [bucket.objects[f"login/exceptions/{task_id}_{count}.png"][0]
                for count in (1, 2, 3)] == [PNG + suffix for suffix in (b"first", b"second", b"third")]

    asyncio.run(scenario())


def test_exception_counter_conflicts_have_a_bounded_retry_limit():
    async def scenario():
        bucket = ConditionalBucket()
        task_id = str(uuid4())
        for count in range(1, 101):
            bucket.objects[f"login/exceptions/{task_id}_{count}.png"] = (PNG, {})
        storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
        with pytest.raises(RuntimeError, match="counter"):
            await storage.put_exception(task_id, 1, PNG)
        assert len(bucket.put_keys) == 100
        assert len(bucket.objects) == 100

    asyncio.run(scenario())


@pytest.mark.parametrize("status,code", [(403, "AccessDenied"), (409, "OtherConflict"), (500, "FileAlreadyExists")])
def test_exception_upload_propagates_non_collision_errors(status, code):
    class FailingBucket(FakeBucket):
        calls = 0

        def put_object(self, key, body, headers):
            self.calls += 1
            raise error

    error = RuntimeError("storage unavailable")
    error.status, error.code = status, code
    bucket = FailingBucket()
    storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(storage.put_exception(str(uuid4()), 1, PNG))
    assert caught.value is error
    assert bucket.calls == 1


def test_slow_exception_upload_does_not_block_qr_upload(monkeypatch):
    async def scenario():
        bucket = FakeBucket()
        storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
        task_id = str(uuid4())
        started, resume = threading.Event(), threading.Event()
        original_put = bucket.put_object

        def delayed_exception(key, *args, **kwargs):
            if "/exceptions/" in key:
                started.set()
                assert resume.wait(timeout=5)
            return original_put(key, *args, **kwargs)

        monkeypatch.setattr(bucket, "put_object", delayed_exception)
        diagnostic = asyncio.create_task(storage.put_exception(task_id, 1, PNG))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.wait_for(storage.put(task_id, b"qr", expiry()), timeout=1)
            assert bucket.objects[f"login/{task_id}/qr.png"][0] == b"qr"
        finally:
            resume.set()
            await diagnostic

    asyncio.run(scenario())


def test_exception_upload_completes_when_default_executor_is_fully_occupied():
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        started, release = threading.Event(), threading.Event()

        def blocked_pipe_read():
            started.set()
            assert release.wait(timeout=5)

        occupied = loop.run_in_executor(None, blocked_pipe_read)
        bucket = ConditionalBucket()
        task_id = str(uuid4())
        key = f"login/exceptions/{task_id}"
        bucket.objects[f"{key}_1.png"] = (PNG + b"old", {})
        storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            count = await asyncio.wait_for(storage.put_exception(task_id, 1, PNG), timeout=1)
            assert count == 2
            assert bucket.objects[f"{key}_2.png"][0] == PNG
            assert not occupied.done()
        finally:
            release.set()
            await occupied

    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("close_loop", [False, True])
def test_cancelled_exception_upload_finishes_quietly(monkeypatch, capsys, fails, close_loop):
    started, release = threading.Event(), threading.Event()
    upload_threads, thread_errors = [], []

    class DelayedBucket(FakeBucket):
        def put_object(self, key, body, headers):
            upload_threads.append(threading.current_thread())
            started.set()
            assert release.wait(timeout=5)
            if fails:
                raise RuntimeError("private-storage-secret")
            super().put_object(key, body, headers)

    monkeypatch.setattr(threading, "excepthook", thread_errors.append)
    bucket = DelayedBucket()
    storage = AliyunObjectStorage("login", SimpleNamespace(), bucket=bucket)
    loop = asyncio.new_event_loop()
    loop_errors = []
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    async def cancel_upload():
        task = asyncio.create_task(storage.put_exception(str(uuid4()), 1, PNG))
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.001)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        loop.run_until_complete(cancel_upload())
        assert upload_threads[0].daemon
    finally:
        if close_loop:
            loop.close()
        release.set()
        for thread in upload_threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        if not loop.is_closed():
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    assert not thread_errors
    assert not loop_errors
    assert "private-storage-secret" not in capsys.readouterr().err
