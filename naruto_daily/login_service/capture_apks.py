"""Validate private APK downloads and publish only verified cache entries.

Deadlines are Unix timestamps, matching the login worker's task deadline.
Signed URLs are intentionally absent from representations and public errors.
"""

from dataclasses import dataclass, field
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
from time import monotonic, sleep, time
from urllib.parse import SplitResult, urlsplit


CAPTURE_PACKAGE = "com.huy.authcapture"
LOGIN_PACKAGE = "com.tencent.tim"
_PACKAGES = frozenset((CAPTURE_PACKAGE, LOGIN_PACKAGE))
_DOWNLOAD_TIMEOUT = 30.0
_READ_TIMEOUT = 5.0
_MAX_BYTES = 8 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_HEADER_NAMES = {
    "authorization": "Authorization",
    "date": "Date",
    "x-oss-security-token": "x-oss-security-token",
}
_MAX_HEADER_BYTES = 16 * 1024
_CACHE_READ_RETRY_TIMEOUT = 0.25


class ApkDownloadError(RuntimeError):
    """An APK failure with a message safe for worker logs."""


@dataclass(frozen=True)
class ApkDownload:
    package: str
    url: str = field(repr=False)
    sha256: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def _validated_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, dict) or len(headers) > len(_HEADER_NAMES):
        raise ApkDownloadError("Invalid APK request headers")
    normalized = {}
    for key, value in headers.items():
        name = _HEADER_NAMES.get(key.lower()) if isinstance(key, str) else None
        if name is None or name in normalized:
            raise ApkDownloadError("Invalid APK request headers")
        if (not isinstance(value, str) or not value.strip()
                or len(value) > _MAX_HEADER_BYTES
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
            raise ApkDownloadError("Invalid APK request headers")
        try:
            value.encode("latin-1")  # Match http.client's header encoding.
        except UnicodeEncodeError:
            raise ApkDownloadError("Invalid APK request headers") from None
        normalized[name] = value
    return normalized


def _validated(package: object, url: object, sha256: object, headers: object) -> ApkDownload:
    if not isinstance(package, str) or package not in _PACKAGES:
        raise ApkDownloadError("Unsupported APK package")
    if not isinstance(sha256, str) or re.fullmatch(r"[a-fA-F0-9]{64}", sha256) is None:
        raise ApkDownloadError("Invalid APK SHA256 digest")
    if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ApkDownloadError("Invalid APK HTTPS URL")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.fragment and "#" not in url)
        parsed.port  # Validate ports before the URL reaches a transport.
    except (ValueError, TypeError):
        raise ApkDownloadError("Invalid APK HTTPS URL") from None
    if not valid:
        raise ApkDownloadError("Invalid APK HTTPS URL")
    return ApkDownload(package, url, sha256.lower(), _validated_headers(headers))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ApkDownloadError("Duplicate APK configuration field")
        result[key] = value
    return result


def parse_downloads(raw: str | None) -> dict[str, ApkDownload]:
    """Read the parent process's package-keyed JSON without exposing its URLs."""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        raise ApkDownloadError("Invalid APK download configuration")
    try:
        decoded = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, ApkDownloadError, RecursionError):
        raise ApkDownloadError("Invalid APK download configuration") from None
    if not isinstance(decoded, dict):
        raise ApkDownloadError("Invalid APK download configuration")
    downloads = {}
    for package, entry in decoded.items():
        if (not isinstance(entry, dict) or not {"url", "sha256"} <= set(entry)
                or set(entry) - {"url", "sha256", "headers"}):
            raise ApkDownloadError("Invalid APK download configuration")
        downloads[package] = _validated(package, entry["url"], entry["sha256"], entry.get("headers", {}))
    return downloads


def _remaining(deadline: float, stop: float) -> float:
    remaining = min(deadline - time(), stop - monotonic())
    if remaining <= 0:
        raise ApkDownloadError("APK download deadline expired")
    return remaining


def _cache_matches(path: Path, sha256: str, deadline: float, stop: float) -> bool:
    retry_until = monotonic() + _CACHE_READ_RETRY_TIMEOUT
    while True:
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as stream:
                while True:
                    _remaining(deadline, stop)
                    chunk = stream.read(_CHUNK_BYTES)
                    if not chunk:
                        return digest.hexdigest() == sha256
                    size += len(chunk)
                    if size > _MAX_BYTES:
                        return False
                    digest.update(chunk)
        except FileNotFoundError:
            return False
        except PermissionError:
            # A concurrent Windows replacement can temporarily deny readers.
            # Retry only that error, briefly, within both download deadlines.
            remaining = _remaining(deadline, stop)
            retry_remaining = retry_until - monotonic()
            if retry_remaining <= 0:
                raise
            sleep(min(0.01, remaining, retry_remaining))


def _fetch(parsed: SplitResult, headers: dict[str, str], deadline: float, stop: float) -> bytes:
    """Bound even DNS/header waits; the network thread never accesses the cache.

    Socket read timeouts alone allow a peer to drip header bytes forever, and
    system DNS calls do not honor socket timeouts. A daemon worker plus the
    caller's total-budget wait covers both. Response bodies are limited to 8 MiB.
    """
    cancelled = threading.Event()
    state: dict[str, object] = {}

    def remaining() -> float:
        if cancelled.is_set():
            raise ApkDownloadError("APK download deadline expired")
        return min(_READ_TIMEOUT, _remaining(deadline, stop))

    def receive() -> None:
        connection = None
        response = None
        try:
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=remaining())
            state["connection"] = connection
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={"Accept-Encoding": "identity", **headers})
            state["socket"] = connection.sock
            if connection.sock is not None:
                connection.sock.settimeout(remaining())
            response = connection.getresponse()
            remaining()
            if response.status != 200:
                # Keep only the numeric status: server bodies can repeat the
                # signed URL, request headers or other sensitive request data.
                state["http_status"] = int(response.status)
                raise ApkDownloadError("APK server rejected download")
            header = response.getheader("Content-Length")
            expected = None
            if header is not None:
                if not re.fullmatch(r"[0-9]+", header):
                    raise ApkDownloadError("Invalid APK content length")
                expected = int(header)
                if expected > _MAX_BYTES:
                    raise ApkDownloadError("APK download exceeds size limit")
            body = bytearray()
            while True:
                timeout = remaining()
                active_socket = state.get("socket")
                if active_socket is not None:
                    active_socket.settimeout(timeout)
                # read1 returns after one buffered/socket read, unlike read(n),
                # so the total budget is rechecked even for a slow-drip body.
                chunk = response.read1(min(_CHUNK_BYTES, _MAX_BYTES + 1 - len(body)))
                remaining()
                if not chunk:
                    break
                if len(body) + len(chunk) > _MAX_BYTES:
                    raise ApkDownloadError("APK download exceeds size limit")
                body.extend(chunk)
                # HTTPResponse closes the underlying socket as soon as an
                # exact Content-Length is consumed. Do not touch it again.
                if expected is not None and len(body) == expected:
                    break
            if expected is not None and len(body) != expected:
                raise ApkDownloadError("APK download was truncated")
            state["body"] = bytes(body)
        except Exception:
            # Never forward a transport exception, repr, or exception chain:
            # http.client and TLS errors may contain a signed request target.
            state["failed"] = True
        finally:
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    worker = threading.Thread(target=receive, name="apk-download", daemon=True)
    worker.start()
    try:
        worker.join(timeout=_remaining(deadline, stop))
        _remaining(deadline, stop)
        if worker.is_alive():
            raise ApkDownloadError("APK download deadline expired")
        if state.get("failed") or "body" not in state:
            if "http_status" in state:
                raise ApkDownloadError(f"APK server rejected download (HTTP {state['http_status']})")
            raise ApkDownloadError("APK download failed")
        return state["body"]
    finally:
        cancelled.set()
        if worker.is_alive():
            connection = state.get("connection")
            active_socket = state.get("socket") or getattr(connection, "sock", None)
            if active_socket is not None:
                # shutdown interrupts a buffered read without waiting on its
                # internal lock. The worker closes its own response afterwards.
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass


def download_apk(spec: ApkDownload, cache_dir: Path, deadline: float) -> Path:
    """Return a verified cache path within the task's remaining time budget."""
    part: Path | None = None
    try:
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            raise ApkDownloadError("Invalid APK download deadline")
        stop = monotonic() + min(_DOWNLOAD_TIMEOUT, deadline - time())
        _remaining(deadline, stop)
        spec = _validated(spec.package, spec.url, spec.sha256, spec.headers)
        destination = Path(cache_dir) / f"{spec.sha256}.apk"
        if _cache_matches(destination, spec.sha256, deadline, stop):
            _remaining(deadline, stop)
            return destination
        body = _fetch(urlsplit(spec.url), spec.headers, deadline, stop)
        _remaining(deadline, stop)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent,
                                         prefix=f"{spec.sha256}-", suffix=".part", delete=False) as stream:
            part = Path(stream.name)
            digest = hashlib.sha256()
            for offset in range(0, len(body), _CHUNK_BYTES):
                _remaining(deadline, stop)
                chunk = body[offset:offset + _CHUNK_BYTES]
                stream.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != spec.sha256:
            raise ApkDownloadError("APK SHA256 verification failed")
        _remaining(deadline, stop)
        try:
            os.replace(part, destination)
        except OSError:
            # Windows can reject the second simultaneous replacement while
            # the winner's destination handle is closing. Reuse it only after
            # verifying its complete content; never delete another writer's file.
            if not _cache_matches(destination, spec.sha256, deadline, stop):
                raise ApkDownloadError("APK cache publication failed") from None
        else:
            part = None
        _remaining(deadline, stop)
        return destination
    except ApkDownloadError as error:
        raise error from None
    except Exception:
        raise ApkDownloadError("APK download or cache operation failed") from None
    finally:
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
