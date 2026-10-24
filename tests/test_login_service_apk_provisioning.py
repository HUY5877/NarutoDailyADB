"""QQ missing-app preparation remains optional and precedes capture dispatch."""
import json
from pathlib import Path

import pytest

from naruto_daily.login_service import capture_apks, ldplayer_worker
from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerOwnershipError

CAPTURE, LOGIN = 'com.huy.authcapture', 'com.tencent.tim'
PACKAGES = {CAPTURE, LOGIN}
DOWNLOADS = json.dumps({name: {'url': 'https://bucket.example.test/app.apk?Signature=secret-fixture',
                              'sha256': digest * 64} for name, digest in [(CAPTURE, 'a'), (LOGIN, 'b')]})


@pytest.mark.parametrize('installed', [set(), {CAPTURE}, {LOGIN}, PACKAGES])
def test_only_missing_apps_are_downloaded_in_capture_first_order(monkeypatch, tmp_path, installed):
    existing, calls = set(installed), []
    class Manager:
        def capture_apps_installed(self, *_): return PACKAGES <= existing
        def missing_capture_apps(self, *_): return PACKAGES - existing
        def install_missing_app(self, instance, apk, package, deadline):
            assert instance == 'owned' and deadline == 100
            calls.append(('install', package))
            existing.add(package)
    def download(spec, cache_dir, deadline):
        assert cache_dir == tmp_path / 'apk-cache' and deadline == 100
        calls.append(('download', spec.package))
        return tmp_path / (spec.package + '.apk')
    monkeypatch.setenv('QQ_APK_DOWNLOADS', DOWNLOADS)
    monkeypatch.setattr(capture_apks, 'download_apk', download)
    assert ldplayer_worker._capture_apps_ready(Manager(), 'owned', 100, tmp_path, lambda: 0)
    assert calls == [item for package in (CAPTURE, LOGIN) if package not in installed
                     for item in [('download', package), ('install', package)]]


@pytest.mark.parametrize('failure', ['configuration', 'probe', 'download', 'install', 'postcheck'])
def test_provisioning_failures_return_false_for_legacy_dispatch_without_secret_logs(monkeypatch, tmp_path, caplog, failure):
    verified = []
    class Manager:
        def capture_apps_installed(self, *_): return False
        def missing_capture_apps(self, *_):
            if failure == 'probe': raise LDPlayerError('secret-fixture')
            return PACKAGES
        def install_missing_app(self, *_):
            if failure == 'install': raise LDPlayerError('secret-fixture')
        def verify_capture_instance(self, instance, deadline):
            verified.append((instance, deadline))
    def download(*_):
        if failure == 'download': raise capture_apks.ApkDownloadError('secret-fixture')
        return tmp_path / 'fixture.apk'
    monkeypatch.setenv('QQ_APK_DOWNLOADS', 'secret-fixture' if failure == 'configuration' else DOWNLOADS)
    monkeypatch.setattr(capture_apks, 'download_apk', download)
    assert not ldplayer_worker._capture_apps_ready(Manager(), 'owned', 100, tmp_path, lambda: 0)
    assert 'secret-fixture' not in caplog.text and '原 QQ 扫码' in caplog.text
    assert verified == [('owned', 100)]


def test_failed_download_cannot_fall_back_after_live_instance_changes(monkeypatch, tmp_path):
    live_serial = ['emulator-5570']
    class Manager:
        def capture_apps_installed(self, *_): return False
        def missing_capture_apps(self, *_): return PACKAGES
        def verify_capture_instance(self, *_):
            if live_serial[0] != 'emulator-5570':
                raise LDPlayerOwnershipError('Live serial changed during download')
    def download(*_):
        live_serial[0] = 'emulator-9999'
        raise capture_apks.ApkDownloadError('Download failed')
    monkeypatch.setenv('QQ_APK_DOWNLOADS', DOWNLOADS)
    monkeypatch.setattr(capture_apks, 'download_apk', download)
    with pytest.raises(LDPlayerOwnershipError):
        ldplayer_worker._capture_apps_ready(Manager(), 'owned', 100, tmp_path, lambda: 0)


@pytest.mark.parametrize('failure', ['ownership', 'deadline'])
def test_provisioning_failure_cannot_bypass_ownership_or_deadline(monkeypatch, tmp_path, failure):
    class Manager:
        def capture_apps_installed(self, *_): return False
        def missing_capture_apps(self, *_):
            if failure == 'ownership': raise LDPlayerOwnershipError('binding changed')
            raise LDPlayerError('operation failed')
    monkeypatch.setenv('QQ_APK_DOWNLOADS', DOWNLOADS)
    with pytest.raises(LDPlayerOwnershipError if failure == 'ownership' else TimeoutError):
        ldplayer_worker._capture_apps_ready(Manager(), 'owned', 100, tmp_path, lambda: 101 if failure == 'deadline' else 0)


@pytest.mark.parametrize('channel,mode', [('android_qq', 'daily'), ('ios_qq', 'arena_weekly'),
                                         ('android_wechat', 'daily'), ('ios_wechat', 'daily'), ('android_qq', 'qr_only')])
@pytest.mark.parametrize('provisioned', [True, False])
def test_worker_dispatches_after_provisioning_and_preserves_wechat_and_qr_only(monkeypatch, tmp_path, channel, mode, provisioned):
    from test_login_service_ldplayer_worker import daily_dependencies, invoke, request, PNG
    calls = []
    uses_capture = mode != 'qr_only' and channel in ('android_qq', 'ios_qq') and provisioned
    def prepare(manager, instance, deadline, state_dir, clock):
        assert instance == 'bound-instance' and state_dir == tmp_path
        assert calls[:2] == ['allocate', 'start']
        calls.append('provision')
        return provisioned
    monkeypatch.setattr(ldplayer_worker, '_capture_apps_ready', prepare, raising=False)
    class Capture:
        def __init__(self, *args, **kwargs): assert uses_capture
        def prepare(self): calls.append('capture'); return PNG
        def wait(self, **kwargs): return 'synthetic'
        def prepare_login(self): pass
        def inject(self, _): pass
        def close(self): pass
    class Daily:
        def __init__(self, **kwargs): pass
        def wait_for_authorization(self, _): pass
        def finish_login(self, _): pass
        def run_daily(self, _, **kwargs):
            if mode == 'arena_weekly': kwargs['on_arena_started'](1_800_001_000.0)
            return 1
    monkeypatch.setattr(ldplayer_worker, '_load_capture_session', lambda: Capture)
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: Daily)
    changes = dict(stateDir=str(tmp_path), loginType=channel)
    if mode != 'qr_only': changes.update(dailyConfigPath=str(tmp_path / 'daily.json'), dailyTimeoutSeconds=600)
    if mode == 'arena_weekly': changes['userOptions'] = {'mainTask': mode}
    code, events = invoke(monkeypatch, request(**changes), daily_dependencies(calls))
    assert code == 0
    assert ('provision' in calls) == (mode != 'qr_only' and channel in ('android_qq', 'ios_qq'))
    assert ('capture' in calls) == uses_capture
    assert ('qr' in calls) != uses_capture
    assert any(event['type'] == 'qr' for event in events)
