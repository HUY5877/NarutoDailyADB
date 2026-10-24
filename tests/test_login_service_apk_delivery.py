"""Private APK release configuration must not widen child credentials."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
from urllib.parse import unquote, urlsplit

import pytest

from oss_fakes import OSS_ENV
from naruto_daily.common import environment
from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service.storage import AliyunObjectStorage


PREFIX = 'releases/huy-android/2026-10-24-qq-auto'
RELEASE = {'QQ_APK_RELEASE_PREFIX': PREFIX, 'QQ_CAPTURE_APK_SHA256': 'a' * 64,
           'QQ_LOGIN_APK_SHA256': 'b' * 64}
PACKAGES = ('com.huy.authcapture', 'com.tencent.tim')


def test_release_configuration_is_optional_and_requires_complete_pinned_pair():
    config = load_config(OSS_ENV | RELEASE)
    assert getattr(config, 'qq_apk_release_prefix', None) == PREFIX
    assert config.qq_capture_apk_sha256 == 'a' * 64
    assert config.qq_login_apk_sha256 == 'b' * 64
    old = load_config(OSS_ENV)
    assert old.qq_apk_release_prefix is None


@pytest.mark.parametrize('values', [
    {'QQ_APK_RELEASE_PREFIX': PREFIX},
    RELEASE | {'QQ_LOGIN_APK_SHA256': ''},
    RELEASE | {'QQ_CAPTURE_APK_SHA256': 'bad'},
    RELEASE | {'QQ_APK_RELEASE_PREFIX': 'login/anything'},
    RELEASE | {'QQ_APK_RELEASE_PREFIX': PREFIX + '/../evil'},
    RELEASE | {'QQ_APK_RELEASE_PREFIX': 'https://example.test/release'},
])
def test_invalid_release_config_cannot_choose_arbitrary_objects(values):
    with pytest.raises(environment.EnvironmentConfigError, match='QQ_'):
        load_config(OSS_ENV | values)


def test_apk_configuration_comes_only_from_selected_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    local = tmp_path / '.env'
    local.write_text('\n'.join(f'{key}=stale-local' for key in RELEASE), encoding='utf8')
    os.environ.update(dict.fromkeys(RELEASE, 'stale-process'))
    remote = dict(RELEASE)
    monkeypatch.setattr('naruto_daily.common.cloudflare_kv.fetch_environment', lambda _: dict(remote))
    environment.initialize_environment(local)
    assert {key: os.environ[key] for key in RELEASE} == RELEASE
    monkeypatch.setattr(environment, '_initialized', False)
    remote.clear()
    environment.initialize_environment(local)
    assert all(key not in os.environ for key in RELEASE)
    assert all(key not in environment._read_local_dotenv(local) for key in RELEASE)


@pytest.mark.parametrize('token', ['', 'fixture-session-token'])
def test_private_apks_use_object_scoped_authorization_headers_without_network(monkeypatch, token):
    import oss2
    date = 'Sun, 25 Oct 2026 00:00:00 GMT'
    monkeypatch.setattr(oss2.utils, 'http_date', lambda: date)
    auth = (oss2.StsAuth('fixture-id', 'fixture-secret', token) if token
            else oss2.Auth('fixture-id', 'fixture-secret'))
    bucket = oss2.Bucket(auth, 'https://oss-cn-shanghai.aliyuncs.com', 'fixture-bucket')
    def no_network(*args, **kwargs): pytest.fail('Signing must not access OSS')
    monkeypatch.setattr(bucket.session, 'do_request', no_network)
    storage = AliyunObjectStorage('login', ServiceConfig(), bucket=bucket)
    requests = asyncio.run(storage.apk_download_requests(PREFIX))
    assert set(requests) == set(PACKAGES)
    for package, filename in zip(PACKAGES, ('HUYCapture.apk', 'HUYLogin.apk')):
        request = requests[package]
        url = urlsplit(request['url'])
        assert url.scheme == 'https' and url.hostname == 'fixture-bucket.oss-cn-shanghai.aliyuncs.com'
        assert unquote(url.path) == f'/{PREFIX}/{filename}' and not url.query
        headers = request['headers']
        assert set(headers) == ({'date', 'authorization', 'x-oss-security-token'} if token else {'date', 'authorization'})
        assert headers['date'] == date
        canonical = f'GET\n\n\n{date}\n'
        if token:
            assert headers['x-oss-security-token'] == token
            canonical += f'x-oss-security-token:{token}\n'
        canonical += f'/fixture-bucket/{PREFIX}/{filename}'
        signature = base64.b64encode(hmac.new(b'fixture-secret', canonical.encode(), hashlib.sha1).digest()).decode()
        assert headers['authorization'] == 'OSS fixture-id:' + signature
        assert 'fixture-secret' not in json.dumps(request)
    assert storage._known_expiries == {}
    with pytest.raises(ValueError):
        asyncio.run(storage.apk_download_requests('login/../other'))


@pytest.mark.parametrize('channel,daily,enabled', [
    ('android_qq', True, True), ('ios_qq', True, True),
    ('android_wechat', True, False), ('ios_wechat', True, False), ('android_qq', False, False),
])
def test_child_gets_only_short_lived_downloads_for_qq(monkeypatch, channel, daily, enabled):
    from naruto_daily.login_service.runtime import LoginRuntime
    from naruto_daily.login_service.models import parse_submission
    from test_login_service_runtime import MemoryStorage, submission
    calls, seen = [], {}
    class Storage(MemoryStorage):
        async def apk_download_requests(self, prefix):
            calls.append(prefix)
            return dict.fromkeys(PACKAGES, {'url': 'https://bucket.example.test/app.apk',
                                           'headers': {'authorization': 'OSS fixture-id:signature', 'date': 'fixture-date'}})
    config = load_config(OSS_ENV | RELEASE)
    runtime = LoginRuntime(config, Storage())
    record = parse_submission(submission() | {'loginType': channel})
    class SpawnCaptured(Exception): pass
    def popen(*args, **kwargs):
        seen.update(kwargs['env'])
        raise SpawnCaptured
    monkeypatch.setenv('QQ_APK_DOWNLOADS', 'stale-download-url')
    monkeypatch.setenv('OSS_ACCESS_KEY_SECRET', 'ambient-secret')
    monkeypatch.setattr('naruto_daily.login_service.runtime.subprocess.Popen', popen)
    with pytest.raises(SpawnCaptured):
        asyncio.run(runtime._run_child(record, [], {}, daily=daily))
    assert calls == ([PREFIX] if enabled else [])
    assert 'OSS_ACCESS_KEY_SECRET' not in seen and 'CLOUDFLARE_API_TOKEN' not in seen
    if enabled:
        data = json.loads(seen['QQ_APK_DOWNLOADS'])
        assert data[PACKAGES[0]]['sha256'] == 'a' * 64
        assert data[PACKAGES[1]]['sha256'] == 'b' * 64
        assert data[PACKAGES[0]]['headers']['authorization'] == 'OSS fixture-id:signature'
    else:
        assert 'QQ_APK_DOWNLOADS' not in seen


def test_url_signing_failure_keeps_child_legacy_path_and_does_not_log_secrets(monkeypatch, caplog):
    from naruto_daily.login_service.runtime import LoginRuntime
    from naruto_daily.login_service.models import parse_submission
    from test_login_service_runtime import MemoryStorage, submission
    class Storage(MemoryStorage):
        async def apk_download_requests(self, prefix):
            raise RuntimeError('https://private.example.test/?Signature=SECRET')
    runtime = LoginRuntime(load_config(OSS_ENV | RELEASE), Storage())
    seen = {}
    class SpawnCaptured(Exception): pass
    def popen(*args, **kwargs):
        seen.update(kwargs['env'])
        raise SpawnCaptured
    monkeypatch.setattr('naruto_daily.login_service.runtime.subprocess.Popen', popen)
    with pytest.raises(SpawnCaptured):
        asyncio.run(runtime._run_child(parse_submission(submission()), [], {}, daily=True))
    assert 'QQ_APK_DOWNLOADS' not in seen
    assert 'SECRET' not in caplog.text and '继续' in caplog.text


@pytest.mark.parametrize('state', ['interrupted', 'stopping', 'expired'])
@pytest.mark.parametrize('signing_fails', [False, True])
def test_signing_wait_cannot_spawn_child_after_task_becomes_inactive(monkeypatch, state, signing_fails):
    from naruto_daily.login_service.runtime import LoginRuntime, _TaskInterrupted, _WorkerStopping, _TaskExpired
    from naruto_daily.login_service.models import parse_submission
    from test_login_service_runtime import MemoryStorage, submission
    record = parse_submission(submission())
    class Storage(MemoryStorage):
        async def apk_download_requests(self, prefix):
            await asyncio.sleep(0)
            if state == 'interrupted': record._admin_interrupted = True
            elif state == 'stopping': runtime._stopping = True
            else: record.expires_at_ms = 1
            if signing_fails: raise RuntimeError('signing failed')
            return dict.fromkeys(PACKAGES, {'url': 'https://bucket.example.test/app.apk', 'headers': {}})
    runtime = LoginRuntime(load_config(OSS_ENV | RELEASE), Storage())
    def forbidden_spawn(*args, **kwargs):
        pytest.fail('Inactive task must not spawn an emulator child')
    monkeypatch.setattr('naruto_daily.login_service.runtime.subprocess.Popen', forbidden_spawn)
    expected = {'interrupted': _TaskInterrupted, 'stopping': _WorkerStopping, 'expired': _TaskExpired}[state]
    with pytest.raises(expected):
        asyncio.run(runtime._run_child(record, [], {}, daily=True))
