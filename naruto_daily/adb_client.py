from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class AdbError(RuntimeError):
    """ADB 命令失败或返回内容无法使用。"""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]
BinaryRunner = Callable[[Sequence[str]], tuple[int, bytes, bytes]]


def _run_subprocess(command: Sequence[str]) -> CommandResult:
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _run_subprocess_binary(command: Sequence[str]) -> tuple[int, bytes, bytes]:
    completed = subprocess.run(command, capture_output=True)
    return completed.returncode, completed.stdout, completed.stderr


class AdbClient:
    def __init__(
        self,
        adb_path: Path,
        serial: str | None = None,
        run_command: CommandRunner = _run_subprocess,
        run_binary: BinaryRunner = _run_subprocess_binary,
    ):
        self.adb_path = Path(adb_path)
        self.serial = serial
        self._run_command = run_command
        self._run_binary = run_binary

    def _command(self, *arguments: str, use_serial: bool = True) -> CommandResult:
        command = [str(self.adb_path)]
        if use_serial and self.serial:
            command.extend(["-s", self.serial])
        command.extend(arguments)
        result = self._run_command(command)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise AdbError(f"ADB 命令执行失败：{detail}")
        return result

    def devices(self) -> list[str]:
        output = self._command("devices", use_serial=False).stdout
        devices = []
        for line in output.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "device":
                devices.append(fields[0])
        return devices

    def screen_size(self) -> tuple[int, int]:
        output = self._command("shell", "wm", "size").stdout
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if not match:
            match = re.search(r"(\d+)x(\d+)", output)
        if not match:
            raise AdbError(f"无法解析屏幕尺寸：{output.strip()!r}")
        return int(match.group(1)), int(match.group(2))

    def tap(self, x: int, y: int) -> None:
        self._command("shell", "input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self._command("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def back(self) -> None:
        self._command("shell", "input", "keyevent", "4")

    def screenshot(self) -> bytes:
        command = [str(self.adb_path)]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(["exec-out", "screencap", "-p"])
        returncode, stdout, stderr = self._run_binary(command)
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip() or f"退出码 {returncode}"
            raise AdbError(f"ADB 截图失败：{detail}")
        if not stdout:
            raise AdbError("ADB 截图失败：返回内容为空")
        return stdout
