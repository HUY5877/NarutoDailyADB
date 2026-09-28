from pathlib import Path

import pytest

from naruto_daily.login_service import local


@pytest.mark.parametrize('failure', [None, 'qr', 'authorization', 'finish'])
def test_local_scan_reuses_login_then_secret_on_same_device(monkeypatch, failure):
    events, now = [], [100.0]

    def step(name, deadline, elapsed):
        events.append((name, deadline))
        if name == failure:
            raise TimeoutError(name)
        now[0] += elapsed

    class Session:
        def __init__(self, config, adb, serial, timeout, **kwargs):
            assert (config, adb, serial, timeout) == (Path('test.json'), Path('adb.exe'), 'emulator-5554', 20)
            assert kwargs['task_name'] == 'secret_realm_farm'
        def wait_for_authorization(self, deadline): step('authorization', deadline, 60)
        def finish_login(self, deadline): step('finish', deadline, 20)
        def run_daily(self, deadline):
            step('secret', deadline, 0)
            return 3

    def device(adb, serial, deadline):
        assert (adb, serial) == (Path('adb.exe'), 'emulator-5554')
        return deadline

    def fetch(deadline):
        step('qr', deadline, 110)
        return b'QR stays local'

    monkeypatch.setattr(local, 'DailySession', Session)
    monkeypatch.setattr(local, 'LoginDevice', device)
    monkeypatch.setattr(local, 'fetch_login_qr', fetch)
    def run():
        return local.run_local_qr_secret(Path('test.json'), Path('adb.exe'), 'emulator-5554', clock=lambda: now[0])

    expected = [('qr', 220), ('authorization', 390), ('finish', 450), ('secret', 7490)]
    if failure:
        with pytest.raises(TimeoutError, match=failure):
            run()
        expected = expected[:next(i for i, (name, _) in enumerate(expected) if name == failure) + 1]
    else:
        assert run() == 3
    assert events == expected
