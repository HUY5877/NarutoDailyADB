"""Private Aliyun OSS storage for login QR and exception images."""

from __future__ import annotations

import asyncio
import importlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .config import ServiceConfig


_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_PREFIX_PATTERN = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_EXCEPTION_PNG_BYTES = 16 * 1024 * 1024
_MAX_EXCEPTION_PUT_ATTEMPTS = 100


@dataclass(frozen=True)
class StoredObject:
    content_length: int
    content_type: str
    expires_at: str
    body: bytes | None = None


class ObjectStorage(Protocol):
    async def initialize(self) -> None: ...
    async def put(self, id: str, png: bytes, expires_at: str) -> None: ...
    async def put_exception(self, id: str, count: int, png: bytes) -> int: ...
    async def head(self, id: str) -> StoredObject | None: ...
    async def get(self, id: str) -> StoredObject | None: ...
    async def delete(self, id: str) -> None: ...
    async def sweep(self) -> None: ...
    async def apk_download_requests(self, release_prefix: str) -> dict[str, dict[str, Any]]: ...


def _safe_id(id: str) -> None:
    if not isinstance(id, str) or _UUID_PATTERN.fullmatch(id) is None:
        raise ValueError("Invalid object id")


def _safe_prefix(prefix: str) -> str:
    if _PREFIX_PATTERN.fullmatch(prefix) is None:
        raise ValueError("Invalid OSS_KEY_PREFIX")
    return prefix


def _expiry(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _expired(value: Any) -> bool:
    timestamp = _expiry(value)
    return timestamp is None or timestamp <= time.time()


def _missing(error: Exception) -> bool:
    return (
        isinstance(error, FileNotFoundError)
        or getattr(error, "status", None) == 404
        or getattr(error, "code", None) == "NoSuchKey"
    )


class AliyunObjectStorage:
    def __init__(self, prefix: str, config: ServiceConfig, *, bucket: Any = None) -> None:
        self.prefix = _safe_prefix(prefix)
        self._config = config
        self._bucket = bucket
        self._known_expiries: dict[str, float] = {}
        self._lock = threading.RLock()

    def _key(self, id: str) -> str:
        _safe_id(id)
        return f"{self.prefix}/{id}/qr.png"

    def _client(self) -> Any:
        if self._bucket is None:
            try:
                oss2 = importlib.import_module("oss2")
            except ImportError as error:
                raise RuntimeError("Run pip install -e '.[login-service]' to install the OSS SDK") from error
            config = self._config
            if config.oss_sts_token:
                auth = oss2.StsAuth(config.oss_access_key_id, config.oss_access_key_secret, config.oss_sts_token)
            else:
                auth = oss2.Auth(config.oss_access_key_id, config.oss_access_key_secret)
            region = config.oss_region
            if not re.fullmatch(r"(?:oss-)?[a-z0-9]+(?:-[a-z0-9]+)+", region):
                raise ValueError("Invalid OSS_REGION")
            if not region.startswith("oss-"):
                region = f"oss-{region}"
            self._bucket = oss2.Bucket(auth, f"https://{region}.aliyuncs.com", config.oss_bucket, connect_timeout=10)
        return self._bucket

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        with self._lock:
            self._client()

    async def apk_download_requests(self, release_prefix: str) -> dict[str, dict[str, Any]]:
        """Sign two fixed GET requests without passing OSS signing secrets to a child."""
        if not re.fullmatch(r'releases/huy-android/[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', release_prefix):
            raise ValueError('Invalid QQ APK release prefix')
        return await asyncio.to_thread(self._apk_download_requests, release_prefix)

    def _apk_download_requests(self, release_prefix: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            bucket = self._client()
            oss2 = importlib.import_module('oss2')
            downloads = {}
            for package, filename in (('com.huy.authcapture', 'HUYCapture.apk'),
                                      ('com.tencent.tim', 'HUYLogin.apk')):
                key = f'{release_prefix}/{filename}'
                # Default OSS public endpoints reject APK query-signed URLs.
                # Use the SDK's own request signer, as Bucket._do does, without
                # issuing I/O. Date limits reuse; method/resource bind this GET.
                request = oss2.http.Request('GET', bucket._make_url(bucket.bucket_name, key))
                bucket.auth._sign_request(request, bucket.bucket_name, key)
                downloads[package] = {
                    'url': request.url,
                    'headers': {name.lower(): value for name, value in request.headers.items()
                                if name.lower() in ('authorization', 'date', 'x-oss-security-token')},
                }
            return downloads

    async def put(self, id: str, png: bytes, expires_at: str) -> None:
        await asyncio.to_thread(self._put, id, png, expires_at)

    def _put(self, id: str, png: bytes, expires_at: str) -> None:
        with self._lock:
            key = self._key(id)
            timestamp = _expiry(expires_at)
            if timestamp is None:
                raise ValueError("Invalid object expiry")
            self._client().put_object(key, png, headers={
                "Content-Type": "image/png",
                "Cache-Control": "no-store",
                "x-oss-object-acl": "private",
                "x-oss-meta-expires-at": expires_at,
            })
            self._known_expiries[id] = timestamp

    async def put_exception(self, id: str, count: int, png: bytes) -> int:
        """Keep a private diagnostic independently of the QR image's lifetime."""
        _safe_id(id)
        if type(count) is not int or count <= 0:
            raise ValueError("Invalid exception image count")
        if (not isinstance(png, bytes) or not png.startswith(_PNG_SIGNATURE)
                or len(png) > _MAX_EXCEPTION_PNG_BYTES):
            raise ValueError("Invalid exception PNG")
        # Blocking child pipe readers may occupy every default executor thread.
        # Diagnostics use their own daemon so uploads can start immediately.
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def settle(actual_count: int | None, error: Exception | None) -> None:
            if result.done():
                return
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(actual_count)

        def send() -> None:
            actual_count, failure = None, None
            try:
                actual_count = self._put_exception(id, count, png)
            except Exception as error:
                # Deliver errors only to the caller; an uncaught thread error
                # could expose SDK credentials through stderr.
                failure = error
            try:
                loop.call_soon_threadsafe(settle, actual_count, failure)
            except RuntimeError:
                # The task can be cancelled and its event loop closed while
                # the SDK finishes an already running upload.
                pass

        threading.Thread(target=send, name="oss-exception-upload", daemon=True).start()
        return await result

    def _put_exception(self, id: str, count: int, png: bytes) -> int:
        with self._lock:
            bucket = self._client()
        # Diagnostic I/O must not hold the QR lifecycle lock. OSS conditional
        # writes also protect earlier images from another attempt or worker.
        for candidate in range(count, count + _MAX_EXCEPTION_PUT_ATTEMPTS):
            key = f"{self.prefix}/exceptions/{id}_{candidate}.png"
            try:
                bucket.put_object(key, png, headers={
                    "Content-Type": "image/png",
                    "Cache-Control": "no-store",
                    "x-oss-object-acl": "private",
                    "x-oss-forbid-overwrite": "true",
                })
            except Exception as error:
                if (getattr(error, "status", None) != 409
                        or getattr(error, "code", None) != "FileAlreadyExists"):
                    raise
            else:
                return candidate
        raise RuntimeError("Exception image counter conflicts exhausted")

    @staticmethod
    def _metadata(result: Any) -> StoredObject | None:
        headers = {str(key).lower(): value for key, value in result.headers.items()}
        expires_at = headers.get("x-oss-meta-expires-at")
        if _expired(expires_at):
            return None
        try:
            content_length = int(headers["content-length"])
        except (KeyError, ValueError, TypeError):
            return None
        if content_length < 0:
            return None
        return StoredObject(content_length, "image/png", expires_at)

    async def head(self, id: str) -> StoredObject | None:
        return await asyncio.to_thread(self._head, id)

    def _head(self, id: str) -> StoredObject | None:
        with self._lock:
            key = self._key(id)
            try:
                return self._metadata(self._client().head_object(key))
            except Exception as error:
                if _missing(error):
                    return None
                raise

    async def get(self, id: str) -> StoredObject | None:
        return await asyncio.to_thread(self._get, id)

    def _get(self, id: str) -> StoredObject | None:
        with self._lock:
            key = self._key(id)
            try:
                result = self._client().get_object(key)
                try:
                    metadata = self._metadata(result)
                    if metadata is None:
                        return None
                    body = result.read()
                    if _expired(metadata.expires_at):
                        return None
                    return StoredObject(len(body), metadata.content_type, metadata.expires_at, body)
                finally:
                    result.close()
            except Exception as error:
                if _missing(error):
                    return None
                raise

    async def delete(self, id: str) -> None:
        await asyncio.to_thread(self._delete, id)

    def _delete(self, id: str) -> None:
        with self._lock:
            key = self._key(id)
            try:
                self._client().delete_object(key)
            except Exception as error:
                if not _missing(error):
                    raise
            self._known_expiries.pop(id, None)

    async def sweep(self) -> None:
        await asyncio.to_thread(self._sweep)

    def _sweep(self) -> None:
        with self._lock:
            for id, expires_at in list(self._known_expiries.items()):
                if expires_at <= time.time():
                    self._delete(id)


def create_storage(config: ServiceConfig) -> ObjectStorage:
    return AliyunObjectStorage(config.key_prefix, config)
