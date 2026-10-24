from pathlib import Path

import pytest

from naruto_daily.common.adb_client import AdbClient, AdbError, CommandResult
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        return self.results.pop(0)


def test_devices_returns_only_online_serials():
    runner = FakeRunner([CommandResult(0, "List of devices attached\nemulator-5554\tdevice\noffline-1\toffline\n", "")])
    client = AdbClient(Path("adb.exe"), run_command=runner)

    assert client.devices() == ["emulator-5554"]
    assert runner.commands == [["adb.exe", "devices"]]


def test_screen_size_prefers_physical_size():
    runner = FakeRunner([CommandResult(0, "Physical size: 1280x720\nOverride size: 960x540\n", "")])
    client = AdbClient(Path("adb.exe"), serial="emulator-5554", run_command=runner)

    assert client.screen_size() == (1280, 720)
    assert runner.commands == [["adb.exe", "-s", "emulator-5554", "shell", "wm", "size"]]


def test_input_commands_include_selected_serial():
    runner = FakeRunner([CommandResult(0, "", ""), CommandResult(0, "", ""), CommandResult(0, "", "")])
    client = AdbClient(Path("adb.exe"), serial="127.0.0.1:5555", run_command=runner)

    client.tap(10, 20)
    client.swipe(1, 2, 3, 4, 500)
    client.back()

    assert runner.commands == [
        ["adb.exe", "-s", "127.0.0.1:5555", "shell", "input", "tap", "10", "20"],
        ["adb.exe", "-s", "127.0.0.1:5555", "shell", "input", "swipe", "1", "2", "3", "4", "500"],
        ["adb.exe", "-s", "127.0.0.1:5555", "shell", "input", "keyevent", "4"],
    ]


def test_nonzero_exit_raises_adb_error():
    runner = FakeRunner([CommandResult(1, "", "device unavailable")])
    client = AdbClient(Path("adb.exe"), run_command=runner)

    with pytest.raises(AdbError, match="device unavailable"):
        client.devices()


def test_unparseable_screen_size_raises_adb_error():
    runner = FakeRunner([CommandResult(0, "unknown", "")])
    client = AdbClient(Path("adb.exe"), run_command=runner)

    with pytest.raises(AdbError, match="屏幕尺寸"):
        client.screen_size()


def test_screenshot_uses_binary_exec_out():
    binary_commands = []

    def run_binary(command):
        binary_commands.append(command)
        return 0, b"png-bytes", b""

    client = AdbClient(Path("adb.exe"), serial="emulator-5554", run_binary=run_binary)

    assert client.screenshot() == b"png-bytes"
    assert binary_commands == [["adb.exe", "-s", "emulator-5554", "exec-out", "screencap", "-p"]]


class ScreenshotClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize('empty_frames', [1, 2])
def test_screenshot_recovers_from_empty_frames_on_the_same_device(empty_frames):
    clock = ScreenshotClock()
    runner = FakeRunner([(0, b'', b'')] * empty_frames + [(0, b'fresh-frame', b'')])
    client = AdbClient(Path('adb.exe'), serial='emulator-5554', run_binary=runner)

    with execution_scope(5, clock=clock, sleep=clock.sleep):
        assert client.screenshot() == b'fresh-frame'

    assert runner.commands == [
        ['adb.exe', '-s', 'emulator-5554', 'exec-out', 'screencap', '-p']
    ] * (empty_frames + 1)
    assert 0 < clock() < 5


def test_screenshot_stops_after_three_empty_frames():
    clock = ScreenshotClock()
    runner = FakeRunner([(0, b'', b'')] * 3)
    client = AdbClient(Path('adb.exe'), run_binary=runner)

    with execution_scope(5, clock=clock, sleep=clock.sleep):
        with pytest.raises(AdbError, match='返回内容为空'):
            client.screenshot()

    assert len(runner.commands) == 3
    assert clock() < 5


def test_screenshot_does_not_retry_a_disconnected_device():
    runner = FakeRunner([(1, b'', b'error: device offline')])
    client = AdbClient(Path('adb.exe'), run_binary=runner)

    with pytest.raises(AdbError, match='device offline'):
        client.screenshot()

    assert len(runner.commands) == 1


def test_screenshot_empty_retry_does_not_outlive_execution_deadline():
    clock = ScreenshotClock()
    runner = FakeRunner([(0, b'', b'')])
    client = AdbClient(Path('adb.exe'), run_binary=runner)

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(.1, clock=clock, sleep=clock.sleep):
            client.screenshot()

    assert clock() == pytest.approx(.1)
    assert len(runner.commands) == 1


def test_screenshot_empty_retry_also_works_without_execution_scope(monkeypatch):
    waits = []
    monkeypatch.setattr('time.sleep', waits.append)
    runner = FakeRunner([(0, b'', b''), (0, b'fresh-frame', b'')])
    client = AdbClient(Path('adb.exe'), run_binary=runner)

    assert client.screenshot() == b'fresh-frame'
    assert len(waits) == 1 and waits[0] > 0
