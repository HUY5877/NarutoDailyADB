from pathlib import Path
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest

from naruto_daily.common.adb_client import AdbClient, AdbError, CommandResult
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_phase_scope, execution_scope
from test_battle import Clock


def test_battle_tap_uses_numeric_host_to_avoid_implicit_local_server_start():
    commands = []
    client = AdbClient(Path('adb.exe'), serial='emulator-5554',
                       run_command=lambda cmd: commands.append(cmd) or CommandResult(0, '', ''))
    getattr(client, 'battle_tap', client.tap)(10, 20)
    assert commands == [['adb.exe', '-H', '127.0.0.1', '-P', '5037', '-s', 'emulator-5554',
                         'shell', 'input', 'tap', '10', '20']]


@pytest.mark.parametrize('message', [
    'cannot connect to daemon at tcp:127.0.0.1:5037',
    '* cannot start server on remote host',
    'could not read ok from ADB Server; failed to start daemon',
])
def test_only_battle_tap_marks_explicit_server_connection_failure(message):
    client = AdbClient(Path('adb.exe'), run_command=lambda _: CommandResult(1, '', message))
    with pytest.raises(AdbError) as caught:
        getattr(client, 'battle_tap', client.tap)(10, 20)
    assert type(caught.value).__name__ == 'AdbServerConnectionError'
    with pytest.raises(AdbError) as ordinary:
        client.tap(10, 20)
    assert type(ordinary.value) is AdbError


@pytest.mark.parametrize('message', ['device offline', 'device unauthorized', 'device not found',
                                     'input: permission denied', 'cannot connect to device'])
def test_battle_does_not_misclassify_device_or_shell_errors(message):
    client = AdbClient(Path('adb.exe'), run_command=lambda _: CommandResult(1, '', message))
    with pytest.raises(AdbError) as caught:
        getattr(client, 'battle_tap', client.tap)(10, 20)
    assert type(caught.value) is AdbError


class ProbeSocket:
    def __init__(self, closed):
        self.closed = closed

    def close(self):
        self.closed.append(True)


def ensure_server(client):
    method = getattr(client, 'ensure_battle_adb_server', None)
    assert method is not None, '缺少先探测5037再决定是否启动的入口'
    method()


def test_reachable_server_is_closed_after_probe_and_no_command_is_started(monkeypatch):
    probes, closed, commands = [], [], []
    def connect(address, timeout):
        probes.append((address, timeout))
        return ProbeSocket(closed)
    monkeypatch.setattr('socket.create_connection', connect)
    client = AdbClient(Path('adb.exe'), run_command=lambda cmd: commands.append(cmd))
    ensure_server(client)
    assert probes == [(('127.0.0.1', 5037), 1.0)]
    assert closed == [True] and commands == []


def test_parallel_service_checks_only_start_one_server(monkeypatch):
    from threading import Event
    from time import sleep
    commands, closed = [], []
    up = Event()
    gate = Barrier(2, timeout=3)
    def connect(*args, **kwargs):
        if not up.is_set():
            raise ConnectionRefusedError('5037 unavailable')
        return ProbeSocket(closed)
    def run(command):
        commands.append(command)
        sleep(.03)
        up.set()
        return CommandResult(0, '', '')
    monkeypatch.setattr('socket.create_connection', connect)
    client = AdbClient(Path('adb.exe'), run_command=run)
    def check():
        gate.wait()
        ensure_server(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(check) for _ in range(2)]
        for future in futures:
            future.result()
    assert commands == [['adb.exe', '-H', 'localhost', '-P', '5037', 'start-server']]
    assert closed == [True]


def test_unreachable_server_is_started_once_without_replaying_a_tap(monkeypatch):
    commands = []
    def connect(*args, **kwargs):
        raise ConnectionRefusedError('5037 unavailable')
    monkeypatch.setattr('socket.create_connection', connect)
    client = AdbClient(Path('adb.exe'), serial='emulator-5554',
                       run_command=lambda cmd: commands.append(cmd) or CommandResult(0, '', ''))
    ensure_server(client)
    assert commands == [['adb.exe', '-H', 'localhost', '-P', '5037', 'start-server']]


def test_failed_explicit_server_start_is_not_retried(monkeypatch):
    commands = []
    def connect(*args, **kwargs):
        raise ConnectionRefusedError('5037 unavailable')
    monkeypatch.setattr('socket.create_connection', connect)
    client = AdbClient(Path('adb.exe'), run_command=lambda cmd: commands.append(cmd)
                       or CommandResult(1, '', 'cannot bind socket'))
    with pytest.raises(AdbError, match='cannot bind'):
        ensure_server(client)
    assert len(commands) == 1


def test_probe_is_bounded_by_the_original_phase_and_cannot_release_late_success(monkeypatch):
    clock, closed, commands = Clock(), [], []
    def connect(address, timeout):
        assert timeout == .25
        clock.advance(timeout)
        return ProbeSocket(closed)
    monkeypatch.setattr('socket.create_connection', connect)
    client = AdbClient(Path('adb.exe'), run_command=lambda cmd: commands.append(cmd))
    with execution_scope(3000, clock=clock, sleep=clock.advance) as parent:
        with pytest.raises(DailyDeadlineExceeded) as caught:
            with execution_phase_scope(.25):
                ensure_server(client)
        assert type(caught.value).__name__ == 'PhaseDeadlineExceeded'
        assert parent.remaining() == 2999.75
    assert closed == [True] and commands == []
