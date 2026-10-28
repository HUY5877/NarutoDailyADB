from __future__ import annotations

import logging
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from .execution import (
    DailyDeadlineExceeded, PhaseDeadlineExceeded, check_execution, current_execution, current_phase_execution,
)
from .input_logging import log_adb_input
from .adb_server_lock import battle_server_lock


class AdbError(RuntimeError):
    """ADB 命令失败或返回内容无法使用。"""


class AdbServerConnectionError(AdbError):
    """战斗命令未连接到主机 ADB 服务，可以在本批输入结束后检查服务。"""


_SERVER_CONNECTION_ERRORS = (
    'cannot connect to daemon', 'cannot start server on remote host',
    'could not read ok from adb server', 'failed to start daemon', "adb server didn't ack",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]
BinaryRunner = Callable[[Sequence[str]], tuple[int, bytes, bytes]]


def _check_command_deadline(returncode: int) -> None:
    try:
        check_execution()
    except PhaseDeadlineExceeded:
        # Preserve a known ADB failure for the caller instead of treating it
        # as a recoverable phase timeout. The daily deadline still propagates.
        if returncode == 0:
            raise


def _run_subprocess(command: Sequence[str]) -> CommandResult:
    completed = _bounded_subprocess(command, text=True, encoding="utf-8", errors="replace")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _run_subprocess_binary(command: Sequence[str]) -> tuple[int, bytes, bytes]:
    completed = _bounded_subprocess(command)
    return completed.returncode, completed.stdout, completed.stderr


def _bounded_subprocess(command: Sequence[str], **kwargs):
    budget = current_execution()
    if budget is None:
        return subprocess.run(command, capture_output=True, **kwargs)
    try:
        result = subprocess.run(command, capture_output=True, timeout=budget.remaining(), **kwargs)
    except subprocess.TimeoutExpired as exc:
        if current_phase_execution() is None:
            raise DailyDeadlineExceeded("执行总时限已到，已终止 ADB 命令") from exc
        try:
            budget.raise_deadline_exceeded("执行时限已到，已终止 ADB 命令")
        except TimeoutError as deadline_error:
            raise deadline_error from exc
    _check_command_deadline(result.returncode)
    return result


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

    def _command(self, *arguments: str, use_serial: bool = True,
                 server_host: str | None = None) -> CommandResult:
        check_execution()
        command = [str(self.adb_path)]
        if server_host is not None:
            command.extend(['-H', server_host, '-P', '5037'])
        if use_serial and self.serial:
            command.extend(["-s", self.serial])
        command.extend(arguments)
        with log_adb_input(self.serial, arguments):
            result = self._run_command(command)
            _check_command_deadline(result.returncode)
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                if server_host == '127.0.0.1' and any(
                        message in detail.lower() for message in _SERVER_CONNECTION_ERRORS):
                    raise AdbServerConnectionError(f"ADB 战斗连接失败：{detail}")
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

    def battle_tap(self, x: int, y: int) -> None:
        # A numeric -H makes adb return on connection failure instead of
        # launching a second local server. Plain tap keeps its original path.
        self._command('shell', 'input', 'tap', str(x), str(y), server_host='127.0.0.1')

    def ensure_battle_adb_server(self) -> None:
        """只检查一次服务；能连接则复用，不能连接才显式启动，不重放点击。"""
        check_execution()
        budget = current_execution()
        with battle_server_lock():
            check_execution()
            timeout = min(1.0, budget.remaining()) if budget else 1.0
            try:
                connection = socket.create_connection(('127.0.0.1', 5037), timeout=timeout)
            except OSError:
                check_execution()
                logging.getLogger(__name__).warning('丰饶战斗的 ADB 5037 无法连接，启动一次服务')
                self._command('start-server', use_serial=False, server_host='localhost')
            else:
                connection.close()
                check_execution()
                logging.getLogger(__name__).warning('ADB 5037 仍可连接，跳过本批失败点击，继续下一批')
            check_execution()

    def battle_taps(self, points):
        """按调用方的战斗坐标创建高速通道，不支持时返回普通点击回退标记。"""
        from .fast_duel_tap import fast_duel_taps

        return fast_duel_taps(self, points)

    def duel_taps(self, points):
        """保留原决斗场调用入口。"""
        return self.battle_taps(points)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self._command("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def back(self) -> None:
        self._command("shell", "input", "keyevent", "4")

    def screenshot(self) -> bytes:
        check_execution()
        command = [str(self.adb_path)]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(["exec-out", "screencap", "-p"])
        # Activity 切回游戏时，screencap 可能成功退出却尚无画面。
        # 只重取空帧，不重放输入，也不延长调用方的执行截止时间。
        for attempt in range(3):
            check_execution()
            returncode, stdout, stderr = self._run_binary(command)
            _check_command_deadline(returncode)
            if returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip() or f"退出码 {returncode}"
                raise AdbError(f"ADB 截图失败：{detail}")
            if stdout:
                return stdout
            if attempt < 2:
                logging.getLogger(__name__).warning(
                    "ADB 截图暂时为空，0.5 秒后重取（第 %d 次重试）", attempt + 1,
                    extra={"backend_only": True},
                )
                budget = current_execution()
                if budget is not None:
                    budget.sleep(0.5)
                else:
                    time.sleep(0.5)
        raise AdbError("ADB 截图失败：返回内容为空（连续 3 次）")

    def ui_hierarchy(self) -> str:
        path = '/sdcard/naruto_daily_ui.xml'
        # 先去掉上一次转储，防止 dump 失败时读取旧页面的坐标。
        self._command('shell', 'rm', '-f', path)
        self._command('shell', 'uiautomator', 'dump', path)
        return self._command('shell', 'cat', path).stdout
