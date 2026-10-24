"""Exercise APK validation, bounded downloads and atomic cache publication offline."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
from io import BytesIO
import json
from pathlib import Path
import threading
import time
import traceback

import pytest


CAPTURE_PACKAGE = "com.huy.authcapture"
LOGIN_PACKAGE = "com.tencent.tim"
SIGNED_URL = "https://private.example.test/v1/capture.apk?OSSAccessKeyId=private-key&Signature=private-signature"
APK = b"verified apk bytes"
DIGEST = hashlib.sha256(APK).hexdigest()
SIGNED_HEADERS = {
    "Authorization": "OSS private-access-key:private-header-signature",
    "Date": "Sun, 25 Oct 2026 10:00:00 GMT",
    "x-oss-security-token": "private-security-token",
}


def test_download_cache_api_is_available():
    assert importlib.util.find_spec("naruto_daily.login_service.capture_apks") is not None


@pytest.fixture
def module():
    try:
        return importlib.import_module("naruto_daily.login_service.capture_apks")
    except ModuleNotFoundError:
        pytest.fail("APK download and cache module has not been implemented")


@pytest.fixture
def cache_dir(tmp_path):
    path = tmp_path / "apk-cache"
    path.mkdir()
    return path


def spec(module, *, body=APK, url=SIGNED_URL):
    return module.ApkDownload(CAPTURE_PACKAGE, url, hashlib.sha256(body).hexdigest())


class Response(BytesIO):
    def __init__(self, body=APK, *, status=200, length=None, before_read=None):
        super().__init__(body)
        self.status = status
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.before_read = before_read

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size=-1):
        if self.before_read:
            self.before_read()
        return super().read1(size)


class Socket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def shutdown(self, how):
        pass

    def close(self):
        pass


class Transport:
    """Replace only HTTPS I/O; cache files, hashes and publication remain real."""

    def __init__(self, response=None, *, failure=None, before_response=None):
        self.response = response or Response()
        self.failure = failure
        self.before_response = before_response
        self.requests = []
        self.request_headers = []
        self.connections = []

    def __call__(self, host, port=None, *, timeout, **kwargs):
        owner = self

        class Connection:
            def __init__(self):
                self.sock = Socket()
                self.timeout = timeout

            def request(self, method, target, **kwargs):
                owner.requests.append((host, port, method, target))
                owner.request_headers.append(kwargs.get("headers", {}))
                if owner.failure:
                    raise owner.failure

            def getresponse(self):
                if owner.before_response:
                    owner.before_response()
                return owner.response() if callable(owner.response) else owner.response

            def close(self):
                pass

        connection = Connection()
        self.connections.append(connection)
        return connection


def install_transport(monkeypatch, module, transport):
    monkeypatch.setattr(module.http.client, "HTTPSConnection", transport)


@pytest.mark.parametrize("raw", [None, "", "{}"])
def test_absent_configuration_disables_downloads(module, raw):
    assert module.parse_downloads(raw) == {}


def test_parses_both_packages_and_preserves_signed_query_without_repr_leak(module):
    parsed = module.parse_downloads(json.dumps({
        CAPTURE_PACKAGE: {"url": SIGNED_URL, "sha256": DIGEST.upper()},
        LOGIN_PACKAGE: {"url": "https://private.example.test/login.apk", "sha256": DIGEST},
    }))
    assert set(parsed) == {CAPTURE_PACKAGE, LOGIN_PACKAGE}
    download = parsed[CAPTURE_PACKAGE]
    assert download.url == SIGNED_URL
    assert download.package == CAPTURE_PACKAGE
    assert download.sha256 == DIGEST
    assert download.headers == {}
    assert "private" not in repr(download)
    with pytest.raises(FrozenInstanceError):
        download.sha256 = "0" * 64


def test_parses_only_allowed_signed_headers_case_insensitively_and_hides_repr(module):
    parsed = module.parse_downloads(json.dumps({CAPTURE_PACKAGE: {
        "url": SIGNED_URL,
        "sha256": DIGEST,
        "headers": {"authorization": SIGNED_HEADERS["Authorization"],
                    "dATE": SIGNED_HEADERS["Date"],
                    "X-OSS-SECURITY-TOKEN": SIGNED_HEADERS["x-oss-security-token"]},
    }}))
    download = parsed[CAPTURE_PACKAGE]
    assert download.headers == SIGNED_HEADERS
    assert "private" not in repr(download)
    assert "Authorization" not in repr(download)


@pytest.mark.parametrize("headers", [
    None, [], True,
    {"Host": "private-host"}, {"Cookie": "private-cookie"},
    {"Accept-Encoding": "gzip"}, {"X-Custom": "private-header-signature"},
    {"Authorization": 123}, {"Authorization": ""}, {"Date": "   "},
    {" Authorization": "private-header-signature"},
    {"Authorization": "first", "authorization": "private-header-signature"},
    {"Authorization": "a", "Date": "b", "x-oss-security-token": "c", "date": "d"},
    {"Authorization": "private-header-signature\r\nHost: attacker.test"},
    {"Authorization": "private-header-signature\x00"},
    {"Authorization": "private-header-signature\x1f"},
    {"Authorization": "private-header-signature\x7f"},
    {"Authorization": "private-header-signature\t"},
    {"Authorization": "private-header-signature\x85"},
    {"Authorization": "x" * (16 * 1024 + 1)},
])
def test_rejects_unsafe_signed_headers_without_echoing_them(module, headers):
    raw = json.dumps({CAPTURE_PACKAGE: {"url": SIGNED_URL, "sha256": DIGEST, "headers": headers}})
    with pytest.raises(module.ApkDownloadError) as caught:
        module.parse_downloads(raw)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-header-signature" not in rendered
    assert "private-cookie" not in rendered
    assert "private-host" not in rendered


@pytest.mark.parametrize("raw", [
    "not json private-signature", "[]", "null", "42", " ",
    json.dumps({"unexpected.package": {"url": SIGNED_URL, "sha256": DIGEST}}),
    json.dumps({CAPTURE_PACKAGE: None}),
    json.dumps({CAPTURE_PACKAGE: {"url": SIGNED_URL}}),
    json.dumps({CAPTURE_PACKAGE: {"url": SIGNED_URL, "sha256": "x" * 64}}),
    json.dumps({CAPTURE_PACKAGE: {"url": SIGNED_URL, "sha256": "a" * 63}}),
    json.dumps({CAPTURE_PACKAGE: {"url": SIGNED_URL, "sha256": 123}}),
    json.dumps({CAPTURE_PACKAGE: {"url": SIGNED_URL, "sha256": DIGEST, "secret": "private-signature"}}),
    '{"com.huy.authcapture": {}, "com.huy.authcapture": {}}',
])
def test_rejects_invalid_configuration_without_echoing_input(module, raw):
    with pytest.raises(module.ApkDownloadError) as caught:
        module.parse_downloads(raw)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-signature" not in rendered
    assert SIGNED_URL not in rendered


@pytest.mark.parametrize("url", [
    "http://private.example.test/a.apk", "ftp://private.example.test/a.apk",
    "https:///a.apk", "https://user:private-signature@example.test/a.apk",
    "https://user@example.test/a.apk", SIGNED_URL + "#fragment",
    "https://example.test:bad/a.apk", "https://example.test:99999/a.apk",
    "https://example.test/a.apk\r\nX-Secret: private-signature", 42, None,
])
def test_rejects_unsafe_urls(module, url):
    with pytest.raises(module.ApkDownloadError):
        module.parse_downloads(json.dumps({CAPTURE_PACKAGE: {"url": url, "sha256": DIGEST}}))


def test_downloads_signed_https_and_reuses_hash_verified_cache(module, monkeypatch, cache_dir):
    transport = Transport(Response(length=len(APK)))
    install_transport(monkeypatch, module, transport)
    download = spec(module)
    first = module.download_apk(download, cache_dir, time.time() + 10)
    second = module.download_apk(download, cache_dir, time.time() + 10)
    assert first == second == cache_dir / f"{DIGEST}.apk"
    assert first.read_bytes() == APK
    assert transport.requests == [("private.example.test", None, "GET", "/v1/capture.apk?OSSAccessKeyId=private-key&Signature=private-signature")]
    assert list(cache_dir.glob("*.part")) == []


def test_corrupt_cache_is_replaced_only_by_verified_download(module, monkeypatch, cache_dir):
    destination = cache_dir / f"{DIGEST}.apk"
    destination.write_bytes(b"corrupt")
    install_transport(monkeypatch, module, Transport())
    assert module.download_apk(spec(module), cache_dir, time.time() + 10) == destination
    assert destination.read_bytes() == APK


@pytest.mark.parametrize("response", [
    lambda: Response(b"wrong apk"),
    lambda: Response(APK, length=len(APK) + 10),
    lambda: Response(APK, length=-1),
    lambda: Response(APK, length="invalid"),
    lambda: Response(APK, length=8 * 1024 * 1024 + 1),
    lambda: Response(b"x" * (8 * 1024 * 1024 + 1)),
])
def test_bad_download_never_publishes_or_deletes_other_downloads(module, monkeypatch, cache_dir, response):
    other = cache_dir / "another-download.part"
    other.write_bytes(b"belongs to another downloader")
    install_transport(monkeypatch, module, Transport(response))
    with pytest.raises(module.ApkDownloadError):
        module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert list(cache_dir.iterdir()) == [other]
    assert other.read_bytes() == b"belongs to another downloader"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 403, 404, 500])
def test_non_success_responses_are_rejected_without_following_redirects(module, monkeypatch, cache_dir, status):
    transport = Transport(Response(status=status))
    install_transport(monkeypatch, module, transport)
    with pytest.raises(module.ApkDownloadError):
        module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert len(transport.requests) == 1
    assert list(cache_dir.iterdir()) == []


def test_oss_apk_policy_rejection_reports_status_without_echoing_sensitive_body(module, monkeypatch, cache_dir):
    body = ("<Error><Code>ApkDownloadForbidden</Code><Request>" + SIGNED_URL + "</Request></Error>").encode()
    transport = Transport(Response(body, status=400))
    install_transport(monkeypatch, module, transport)
    with pytest.raises(module.ApkDownloadError, match="HTTP 400") as caught:
        module.download_apk(spec(module), cache_dir, time.time() + 10)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-signature" not in rendered
    assert "OSSAccessKeyId" not in rendered
    assert list(cache_dir.iterdir()) == []
    assert len(transport.requests) == 1


def test_network_errors_are_sanitized_in_message_and_traceback(module, monkeypatch, cache_dir):
    install_transport(monkeypatch, module, Transport(failure=OSError(SIGNED_URL)))
    with pytest.raises(module.ApkDownloadError) as caught:
        module.download_apk(spec(module), cache_dir, time.time() + 10)
    rendered = "".join(traceback.format_exception(caught.value))
    assert SIGNED_URL not in rendered
    assert "private-signature" not in rendered
    assert caught.value.__cause__ is None
    assert list(cache_dir.iterdir()) == []


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf"), True, "secret"])
def test_invalid_or_expired_deadline_does_not_download(module, monkeypatch, cache_dir, deadline):
    transport = Transport()
    install_transport(monkeypatch, module, transport)
    with pytest.raises(module.ApkDownloadError):
        module.download_apk(spec(module), cache_dir, deadline)
    assert transport.requests == []


def test_deadline_applies_to_cache_hits(module, monkeypatch, cache_dir):
    (cache_dir / f"{DIGEST}.apk").write_bytes(APK)
    with pytest.raises(module.ApkDownloadError):
        module.download_apk(spec(module), cache_dir, time.time() - 1)


def test_blocked_connection_cannot_overrun_task_deadline_or_publish_later(module, monkeypatch, cache_dir):
    release = threading.Event()
    transport = Transport(before_response=lambda: release.wait(2))
    install_transport(monkeypatch, module, transport)
    started = time.monotonic()
    try:
        with pytest.raises(module.ApkDownloadError):
            module.download_apk(spec(module), cache_dir, time.time() + 0.08)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
    assert list(cache_dir.iterdir()) == []


def test_total_download_budget_is_capped_even_with_long_task_deadline(module, monkeypatch, cache_dir):
    monkeypatch.setattr(module, "_DOWNLOAD_TIMEOUT", 0.08)
    release = threading.Event()
    install_transport(monkeypatch, module, Transport(before_response=lambda: release.wait(2)))
    started = time.monotonic()
    try:
        with pytest.raises(module.ApkDownloadError):
            module.download_apk(spec(module), cache_dir, time.time() + 600)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_slow_drip_http_cannot_keep_download_alive(module, monkeypatch, cache_dir, phase):
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                if phase == "body":
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                else:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                while not stopped.wait(0.01):
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    # Keep stdlib HTTP parsing and real sockets, bypassing only TLS for this local fixture.
    monkeypatch.setattr(module.http.client, "HTTPSConnection", lambda host, port=None, *, timeout, **kwargs:
                        HTTPConnection("127.0.0.1", server.server_port, timeout=timeout))
    started = time.monotonic()
    try:
        with pytest.raises(module.ApkDownloadError):
            module.download_apk(spec(module), cache_dir, time.time() + 0.12)
        assert 0.08 <= time.monotonic() - started < 0.6
        assert list(cache_dir.iterdir()) == []
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


@pytest.mark.parametrize("use_signed_headers", [False, True])
def test_real_http_parser_downloads_complete_response(module, monkeypatch, cache_dir, use_signed_headers):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Length", str(len(APK)))
            self.end_headers()
            self.wfile.write(APK)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    monkeypatch.setattr(module.http.client, "HTTPSConnection", lambda host, port=None, *, timeout, **kwargs:
                        HTTPConnection("127.0.0.1", server.server_port, timeout=timeout))
    try:
        download = (module.ApkDownload(CAPTURE_PACKAGE, SIGNED_URL, DIGEST, SIGNED_HEADERS)
                    if use_signed_headers else spec(module))
        result = module.download_apk(download, cache_dir, time.time() + 10)
        assert result.read_bytes() == APK
        assert received[0]["Accept-Encoding"] == "identity"
        if use_signed_headers:
            assert {key: received[0][key] for key in SIGNED_HEADERS} == SIGNED_HEADERS
        else:
            assert "Authorization" not in received[0]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def test_signed_headers_are_not_forwarded_after_redirect(module, monkeypatch, cache_dir):
    response = Response(status=302)
    response.headers["Location"] = "https://different.example.test/steal"
    transport = Transport(response)
    install_transport(monkeypatch, module, transport)
    download = module.ApkDownload(CAPTURE_PACKAGE, SIGNED_URL, DIGEST, SIGNED_HEADERS)
    with pytest.raises(module.ApkDownloadError, match="HTTP 302"):
        module.download_apk(download, cache_dir, time.time() + 10)
    assert len(transport.requests) == 1
    assert transport.requests[0][0] == "private.example.test"
    assert transport.request_headers == [{"Accept-Encoding": "identity", **SIGNED_HEADERS}]


def test_direct_specs_validate_headers_before_any_request(module, monkeypatch, cache_dir):
    transport = Transport()
    install_transport(monkeypatch, module, transport)
    download = module.ApkDownload(CAPTURE_PACKAGE, SIGNED_URL, DIGEST, {"Host": "attacker.test"})
    with pytest.raises(module.ApkDownloadError):
        module.download_apk(download, cache_dir, time.time() + 10)
    assert transport.requests == []


def test_signed_header_transport_errors_are_sanitized(module, monkeypatch, cache_dir):
    transport = Transport(failure=OSError(SIGNED_HEADERS["Authorization"]))
    install_transport(monkeypatch, module, transport)
    download = module.ApkDownload(CAPTURE_PACKAGE, SIGNED_URL, DIGEST, SIGNED_HEADERS)
    with pytest.raises(module.ApkDownloadError) as caught:
        module.download_apk(download, cache_dir, time.time() + 10)
    assert "private" not in repr(download)
    assert "private-header-signature" not in "".join(traceback.format_exception(caught.value))


def test_concurrent_downloads_publish_verified_unique_parts_and_reuse_result(module, monkeypatch, cache_dir):
    barrier = threading.Barrier(2)
    transport = Transport(lambda: Response(), before_response=lambda: barrier.wait(timeout=2))
    install_transport(monkeypatch, module, transport)
    original_replace = module.os.replace
    sources = []

    def checked_replace(source, destination):
        source = Path(source)
        assert source.suffix == ".part"
        assert source.read_bytes() == APK
        sources.append(source)
        original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", checked_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(module.download_apk, spec(module), cache_dir, time.time() + 10) for _ in range(2)]
        failures = [future.exception(timeout=3) for future in futures]
        assert not any(failures), [
            {"context_type": type(error.__context__).__name__,
             "errno": getattr(error.__context__, "errno", None),
             "winerror": getattr(error.__context__, "winerror", None),
             "frames": [(Path(frame.filename).name, frame.lineno, frame.name)
                        for frame in traceback.extract_tb(error.__context__.__traceback__)]
                       if error.__context__ else []}
            for error in failures if error
        ]
        paths = [future.result(timeout=3) for future in futures]
    assert paths[0] == paths[1] == cache_dir / f"{DIGEST}.apk"
    assert paths[0].read_bytes() == APK
    assert len(sources) == len(set(sources)) == 2
    assert list(cache_dir.glob("*.part")) == []
    assert module.download_apk(spec(module), cache_dir, time.time() + 10) == paths[0]
    assert len(transport.requests) == 2


def test_publish_error_is_sanitized_and_cleans_own_part(module, monkeypatch, cache_dir):
    install_transport(monkeypatch, module, Transport())

    def fail_replace(*args):
        raise OSError(SIGNED_URL)

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(module.ApkDownloadError) as caught:
        module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert "private-signature" not in "".join(traceback.format_exception(caught.value))
    assert list(cache_dir.iterdir()) == []


def test_windows_publish_race_reuses_only_verified_winner(module, monkeypatch, cache_dir):
    install_transport(monkeypatch, module, Transport())

    def winner_already_published(source, destination):
        Path(destination).write_bytes(APK)
        raise PermissionError("Windows destination handle is still closing")

    monkeypatch.setattr(module.os, "replace", winner_already_published)
    result = module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert result.read_bytes() == APK
    assert list(cache_dir.glob("*.part")) == []


def test_temporarily_locked_cache_is_reverified_and_reused(module, monkeypatch, cache_dir):
    destination = cache_dir / f"{DIGEST}.apk"
    destination.write_bytes(APK)
    original_open = Path.open
    attempts = []

    def briefly_locked(path, *args, **kwargs):
        if path == destination:
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError(13, "Permission denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", briefly_locked)
    transport = Transport()
    install_transport(monkeypatch, module, transport)
    result = module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert result == destination
    assert len(attempts) == 2
    assert transport.requests == []


@pytest.mark.parametrize("limit", ["task", "download"])
def test_permanently_locked_cache_respects_deadline(module, monkeypatch, cache_dir, limit):
    destination = cache_dir / f"{DIGEST}.apk"
    destination.write_bytes(APK)
    original_open = Path.open
    attempts = []

    def locked(path, *args, **kwargs):
        if path == destination:
            attempts.append(path)
            raise PermissionError(13, SIGNED_URL)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    transport = Transport()
    install_transport(monkeypatch, module, transport)
    if limit == "download":
        monkeypatch.setattr(module, "_DOWNLOAD_TIMEOUT", 0.06)
    started = time.monotonic()
    with pytest.raises(module.ApkDownloadError, match="deadline") as caught:
        module.download_apk(spec(module), cache_dir, time.time() + (0.06 if limit == "task" else 10))
    assert 0.04 <= time.monotonic() - started < 0.5
    assert len(attempts) >= 2
    assert transport.requests == []
    assert "private-signature" not in "".join(traceback.format_exception(caught.value))


def test_permanently_locked_cache_has_short_retry_limit(module, monkeypatch, cache_dir):
    destination = cache_dir / f"{DIGEST}.apk"
    destination.write_bytes(APK)
    original_open = Path.open
    attempts = []

    def locked(path, *args, **kwargs):
        if path == destination:
            attempts.append(path)
            raise PermissionError(13, "Permission denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    started = time.monotonic()
    with pytest.raises(module.ApkDownloadError):
        module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert 0.1 <= time.monotonic() - started < 0.6
    assert len(attempts) >= 2


def test_other_cache_io_errors_are_not_retried(module, monkeypatch, cache_dir):
    destination = cache_dir / f"{DIGEST}.apk"
    original_open = Path.open
    attempts = []

    def broken_disk(path, *args, **kwargs):
        if path == destination:
            attempts.append(path)
            raise OSError(5, "Input/output error")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", broken_disk)
    transport = Transport()
    install_transport(monkeypatch, module, transport)
    with pytest.raises(module.ApkDownloadError):
        module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert len(attempts) == 1
    assert transport.requests == []


def test_publish_race_retries_reading_temporarily_locked_winner(module, monkeypatch, cache_dir):
    destination = cache_dir / f"{DIGEST}.apk"
    original_open = Path.open
    published = False
    reads = []

    def winner_already_published(source, target):
        nonlocal published
        Path(target).write_bytes(APK)
        published = True
        raise PermissionError(13, "Permission denied")

    def briefly_locked(path, *args, **kwargs):
        if path == destination and published:
            reads.append(path)
            if len(reads) == 1:
                raise PermissionError(13, "Permission denied")
        return original_open(path, *args, **kwargs)

    install_transport(monkeypatch, module, Transport())
    monkeypatch.setattr(module.os, "replace", winner_already_published)
    monkeypatch.setattr(Path, "open", briefly_locked)
    result = module.download_apk(spec(module), cache_dir, time.time() + 10)
    assert result == destination
    assert len(reads) == 2
    assert list(cache_dir.glob("*.part")) == []
