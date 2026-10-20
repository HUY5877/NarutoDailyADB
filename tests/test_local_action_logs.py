"""Local input diagnostics retain actual commands without publishing them."""

from datetime import datetime, timezone
import io
import logging
from pathlib import Path
import subprocess
from types import SimpleNamespace
import time

import pytest

from naruto_daily.common.adb_client import AdbClient, AdbError, CommandResult
from naruto_daily.login_service import log_events, qr_navigation


@pytest.mark.parametrize('device', ['daily', 'login'])
def test_each_tap_and_back_has_local_time_coordinates_and_result(monkeypatch, device):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=b'')
    monkeypatch.setattr(qr_navigation.subprocess, 'run', run)
    if device == 'daily':
        client = AdbClient(Path('adb.exe'), 'emulator-5554',
                           run_command=lambda command: (commands.append(command) or CommandResult(0, '', '')))
    else:
        client = qr_navigation.LoginDevice(Path('adb.exe'), 'emulator-5554', time.time() + 30)
    public, terminal = [], io.StringIO()
    before = datetime.now(timezone.utc)
    with log_events.forward_task_logs(public.append, diagnostic_stream=terminal):
        client.tap(123, 456)
        client.back()
    after = datetime.now(timezone.utc)
    text = terminal.getvalue()
    assert '[ADB操作开始]' in text and '[ADB操作完成]' in text
    assert all(line.startswith(log_events.FILE_ONLY_PREFIX) for line in text.splitlines())
    assert '点击 x=123 y=456' in text and '返回 keycode=4' in text
    assert 'serial=emulator-5554' in text and 'elapsed_ms=' in text
    for line in text.splitlines():
        instant = datetime.fromisoformat(line.split('time=')[1].split()[0])
        assert before <= instant <= after
    assert len(commands) == 2
    assert public == []


def test_local_file_keeps_inputs_while_terminal_keeps_other_backend_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    terminal = io.StringIO()
    handler = logging.StreamHandler(terminal)
    original_filters = handler.filters[:]
    root = logging.getLogger()
    root.addHandler(handler)
    client = AdbClient(Path('adb.exe'), 'emulator-5554',
                       run_command=lambda _: CommandResult(0, '', ''))
    try:
        with log_events.local_log_file(tmp_path):
            client.tap(123, 456)
            logging.getLogger('naruto_daily').info('普通动作进度')
            logging.getLogger('naruto_daily').warning('模型后台诊断', extra={'backend_only': True})
        assert handler.filters == original_filters
        content = (tmp_path / 'logs' / 'worker.log').read_text(encoding='utf-8')
        assert '[ADB操作开始]' in content and '[ADB操作完成]' in content
        assert '点击 x=123 y=456' in content
        assert '[ADB操作' not in terminal.getvalue()
        for message in ('普通动作进度', '模型后台诊断'):
            assert message in content and message in terminal.getvalue()
    finally:
        root.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize('device', ['daily', 'login'])
def test_failed_tap_is_logged_without_success_or_private_device_output(monkeypatch, device):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('private-device-command', 1)
    monkeypatch.setattr(qr_navigation.subprocess, 'run', fail)
    if device == 'daily':
        client = AdbClient(Path('adb.exe'), 'emulator-5554',
                           run_command=lambda _: CommandResult(1, '', 'private-device-output'))
        error = AdbError
    else:
        client = qr_navigation.LoginDevice(Path('adb.exe'), 'emulator-5554', time.time() + 30)
        error = qr_navigation.QRNavigationError
    public, terminal = [], io.StringIO()
    with log_events.forward_task_logs(public.append, diagnostic_stream=terminal):
        with pytest.raises(error):
            client.tap(123, 456)
    text = terminal.getvalue()
    assert '[ADB操作失败]' in text and '点击 x=123 y=456' in text
    assert '[ADB操作完成]' not in text
    assert 'private-device-' not in text
    assert public == []


def test_swipe_logs_both_ends_and_duration_without_logging_read_commands():
    client = AdbClient(Path('adb.exe'), 'emulator-5556',
                       run_command=lambda _: CommandResult(0, 'Physical size: 1920x1080', ''))
    terminal = io.StringIO()
    with log_events.forward_task_logs(lambda _: pytest.fail('input logs are private'), diagnostic_stream=terminal):
        client.swipe(10, 20, 30, 40, 500)
        after_swipe = terminal.getvalue()
        assert client.screen_size() == (1920, 1080)
    assert '滑动 x1=10 y1=20 x2=30 y2=40 duration_ms=500' in after_swipe
    assert terminal.getvalue() == after_swipe


def test_local_file_appends_redacts_and_removes_its_handler(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv('JEV_API_KEY', 'private-model-key')
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'private-cloudflare-token')
    original_handlers = logging.getLogger().handlers[:]
    path = tmp_path / 'logs' / 'worker.log'
    for message in ['first', 'second']:
        with log_events.local_log_file(tmp_path):
            logging.getLogger('naruto_daily').info(
                'Task example | %s 点击 x=123 y=456 private-model-key private-cloudflare-token', message)
        assert logging.getLogger().handlers == original_handlers
    content = path.read_text(encoding='utf-8')
    assert 'first 点击 x=123 y=456' in content and 'second 点击 x=123 y=456' in content
    assert 'Task example' in content and '[REDACTED]' in content
    assert 'private-model-key' not in content and 'private-cloudflare-token' not in content


def test_local_file_rotates_with_bounded_history(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(log_events, 'LOCAL_LOG_MAX_BYTES', 300, raising=False)
    monkeypatch.setattr(log_events, 'LOCAL_LOG_BACKUPS', 2, raising=False)
    with log_events.local_log_file(tmp_path):
        for index in range(20):
            logging.getLogger('naruto_daily').info('rotation-%02d %s', index, 'x' * 100)
    paths = sorted((tmp_path / 'logs').glob('worker.log*'))
    assert len(paths) == 3
    assert 'rotation-19' in (tmp_path / 'logs' / 'worker.log').read_text(encoding='utf-8')
    assert not any('rotation-00' in path.read_text(encoding='utf-8') for path in paths)


def test_rotation_failure_does_not_expose_raw_logs_or_interrupt_inputs(tmp_path, caplog, capsys, monkeypatch):
    caplog.set_level(logging.INFO)
    secret = 'private-model-key'
    monkeypatch.setenv('JEV_API_KEY', secret)
    commands = []
    client = AdbClient(Path('adb.exe'), 'emulator-5554',
                       run_command=lambda command: (commands.append(command) or CommandResult(0, '', '')))
    def fail_rotation():
        raise PermissionError('log file is locked')
    with log_events.local_log_file(tmp_path):
        handler = next(handler for handler in logging.getLogger().handlers
                       if isinstance(handler, log_events.RotatingFileHandler))
        monkeypatch.setattr(handler, 'shouldRollover', lambda record: True)
        monkeypatch.setattr(handler, 'doRollover', fail_rotation)
        logging.getLogger('naruto_daily').info('local diagnostic %s', secret)
        client.tap(123, 456)
    assert commands == [['adb.exe', '-s', 'emulator-5554', 'shell', 'input', 'tap', '123', '456']]
    assert secret not in capsys.readouterr().err
