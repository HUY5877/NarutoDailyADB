from pathlib import Path

import pytest

from naruto_daily.common.adb_client import AdbClient, AdbError, CommandResult


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
