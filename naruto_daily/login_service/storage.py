"""Local object storage and an optional private Aliyun OSS adapter."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from .config import ServiceConfig


_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_PREFIX_PATTERN = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*")
_ORPHAN_GRACE_SECONDS = 10 * 60


@dataclass(frozen=True)
class StoredObject:
    content_length: int
    content_type: str
    expires_at: str
    body: bytes | None = None


class ObjectStorage(Protocol):
    async def initialize(self) -> None: ...
    async def put(self, id: str, png: bytes, expires_at: str) -> None: ...
    async def head(self, id: str) -> StoredObject | None: ...
    async def get(self, id: str) -> StoredObject | None: ...
    async def delete(self, id: str) -> None: ...
    async def sweep(self) -> None: ...


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


class LocalObjectStorage:
    def __init__(self, directory: str | Path, prefix: str = "login") -> None:
        base = Path(directory).resolve()
        self.root = base.joinpath(*_safe_prefix(prefix).split("/")).resolve()
        if not self.root.is_relative_to(base) or self.root == base:
            raise ValueError("Object prefix escapes storage directory")
        # Keep the lock inside the worker thread: cancelling an async caller must
        # not let a sweep/delete race with an unfinished filesystem write.
        self._lock = threading.RLock()

    def _directory(self, id: str) -> Path:
        _safe_id(id)
        directory = (self.root / id).resolve()
        if self.root.resolve() != self.root or directory != self.root / id:
            raise ValueError("Object directory escapes storage root")
        return directory

    @staticmethod
    def _file(directory: Path, name: str) -> Path:
        path = directory / name
        if path.resolve() != path:
            raise ValueError("Object file escapes task directory")
        return path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._sweep()

    async def put(self, id: str, png: bytes, expires_at: str) -> None:
        await asyncio.to_thread(self._put, id, png, expires_at)

    def _put(self, id: str, png: bytes, expires_at: str) -> None:
        with self._lock:
            directory = self._directory(id)
            if _expiry(expires_at) is None:
                raise ValueError("Invalid object expiry")
            directory.mkdir(parents=True, exist_ok=True)
            image = self._file(directory, "qr.png")
            metadata = self._file(directory, "metadata.json")
            nonce = uuid4().hex
            temporary_image = directory / f"qr.png.{nonce}.tmp"
            temporary_metadata = directory / f"metadata.json.{nonce}.tmp"
            try:
                with temporary_image.open("xb") as stream:
                    stream.write(png)
                with temporary_metadata.open("x", encoding="utf-8") as stream:
                    json.dump({"expiresAt": expires_at}, stream)
                temporary_metadata.replace(metadata)
                temporary_image.replace(image)
            finally:
                temporary_image.unlink(missing_ok=True)
                temporary_metadata.unlink(missing_ok=True)

    async def head(self, id: str) -> StoredObject | None:
        return await asyncio.to_thread(self._head, id)

    def _head(self, id: str) -> StoredObject | None:
        with self._lock:
            directory = self._directory(id)
            try:
                metadata = json.loads(self._file(directory, "metadata.json").read_text(encoding="utf-8"))
                if not isinstance(metadata, dict) or _expired(metadata.get("expiresAt")):
                    return None
                image = self._file(directory, "qr.png")
                if not image.is_file():
                    return None
                return StoredObject(image.stat().st_size, "image/png", metadata["expiresAt"])
            except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
                return None

    async def get(self, id: str) -> StoredObject | None:
        return await asyncio.to_thread(self._get, id)

    def _get(self, id: str) -> StoredObject | None:
        with self._lock:
            metadata = self._head(id)
            if metadata is None:
                return None
            try:
                body = self._file(self._directory(id), "qr.png").read_bytes()
            except FileNotFoundError:
                return None
            if _expired(metadata.expires_at):
                return None
            return StoredObject(len(body), metadata.content_type, metadata.expires_at, body)

    async def delete(self, id: str) -> None:
        await asyncio.to_thread(self._delete, id)

    def _delete(self, id: str) -> None:
        with self._lock:
            # Validate the resolved absolute path immediately before recursion.
            directory = self._directory(id)
            if not directory.is_relative_to(self.root) or directory == self.root:
                raise ValueError("Object directory escapes storage root")
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass

    async def sweep(self) -> None:
        await asyncio.to_thread(self._sweep)

    def _sweep(self) -> None:
        with self._lock:
            if not self.root.exists():
                return
            for entry in self.root.iterdir():
                if not entry.is_dir() or not _UUID_PATTERN.fullmatch(entry.name):
                    continue
                directory = self._directory(entry.name)
                try:
                    metadata = json.loads(self._file(directory, "metadata.json").read_text(encoding="utf-8"))
                except FileNotFoundError:
                    # A recently created orphan can belong to another producer.
                    try:
                        aged = time.time() - directory.stat().st_mtime > _ORPHAN_GRACE_SECONDS
                    except FileNotFoundError:
                        continue
                    if aged:
                        self._delete(entry.name)
                    continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._delete(entry.name)
                    continue
                if not isinstance(metadata, dict) or _expired(metadata.get("expiresAt")):
                    self._delete(entry.name)


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
                raise RuntimeError("Run pip install -e '.[login-service]' to use STORAGE_DRIVER=oss") from error
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
    if config.storage_driver == "oss":
        return AliyunObjectStorage(config.key_prefix, config)
    if config.storage_driver == "local":
        return LocalObjectStorage(config.local_storage_dir, config.key_prefix)
    raise ValueError("STORAGE_DRIVER must be local or oss")
