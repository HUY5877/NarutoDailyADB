"""在扫码任务绑定的设备中复用已有登录收尾和日常动作。"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Sequence

from naruto_daily.common.adb_client import AdbClient, CommandResult
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.config import ConfigError, load_settings
from naruto_daily.runner import ActionRunner
from naruto_daily.tasks import build_all


def _templates(value):
    if isinstance(value, dict):
        template = value.get("template")
        if isinstance(template, str):
            yield template
        yield from value.get("alternate_templates", [])
        for child in value.values():
            yield from _templates(child)
    elif isinstance(value, list):
        for child in value:
            yield from _templates(child)


class DailySession:
    def __init__(
        self, config_path: Path, adb_path: Path, serial: str, command_timeout: float,
        *, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        run_subprocess: Callable = subprocess.run,
        template_matcher: Callable = find_template_in_png,
    ):
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("日常任务必须指定已绑定的 ADB serial")
        if (isinstance(command_timeout, bool) or not isinstance(command_timeout, (int, float))
                or not math.isfinite(command_timeout) or command_timeout <= 0):
            raise ValueError("ADB 命令超时必须是有限正数")
        self.config_path = Path(config_path).resolve()
        self.asset_root = self.config_path.parent
        self.settings = load_settings(self.config_path, skip_account_login=True)
        self._login_actions = deepcopy(self.settings.tasks["login"])
        self._daily_actions = build_all(self.settings)[len(self._login_actions):]
        main_actions = [action for action in self._login_actions if action["type"] == "return_to_main"]
        if not main_actions:
            raise ConfigError("扫码登录收尾必须包含 return_to_main 以确认进入主界面")
        self._main_marker = main_actions[-1]["main_page_marker"]
        for template in set(_templates([*self._login_actions, *self._daily_actions])):
            if not (self.asset_root / template).is_file():
                raise ConfigError(f"日常任务模板不存在：{template}")
        self._clock = clock
        self._sleep_impl = sleep
        self._run_subprocess = run_subprocess
        self._template_matcher = template_matcher
        self._command_timeout = float(command_timeout)
        self._deadline: float | None = None
        self._logged_in = False
        self.client = AdbClient(Path(adb_path), serial=serial,
                                run_command=self._run_text, run_binary=self._run_binary)

    def _set_deadline(self, deadline: float) -> None:
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            raise ValueError("日常任务截止时间必须是有限时间戳")
        self._deadline = float(deadline)
        self._remaining()

    def _remaining(self) -> float:
        if self._deadline is None:
            raise RuntimeError("日常任务尚未设置截止时间")
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("日常任务已超过截止时间")
        return remaining

    def _run(self, command: Sequence[str], *, text: bool):
        timeout = min(self._command_timeout, self._remaining())
        kwargs = dict(capture_output=True, timeout=timeout,
                      creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if text:
            kwargs.update(text=True, encoding="utf-8", errors="replace")
        result = self._run_subprocess(command, **kwargs)
        self._remaining()
        return result

    def _run_text(self, command: Sequence[str]) -> CommandResult:
        result = self._run(command, text=True)
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def _run_binary(self, command: Sequence[str]) -> tuple[int, bytes, bytes]:
        result = self._run(command, text=False)
        return result.returncode, result.stdout, result.stderr

    def _sleep(self, seconds: float) -> None:
        remaining = self._remaining()
        self._sleep_impl(min(float(seconds), remaining))
        self._remaining()

    def _match(self, *args, **kwargs):
        self._remaining()
        result = self._template_matcher(*args, **kwargs)
        self._remaining()
        return result

    def _runner(self) -> ActionRunner:
        return ActionRunner(self.client, sleep=self._sleep, template_matcher=self._match,
                            asset_root=self.asset_root)

    def wait_for_login(self, deadline: float) -> None:
        """等待扫码授权、开始游戏、清弹窗，确认同一设备已进入主界面。"""
        self._logged_in = False
        self._set_deadline(deadline)
        actions = deepcopy(self._login_actions)
        actions[0]["timeout_seconds"] = self._remaining()
        self._runner().run(actions)
        images = ImageRecognition(self.client, self._match, self.asset_root, self._sleep)
        center, _ = images.find(self._main_marker)
        if center is None:
            raise VisualError("扫码登录后未能确认游戏主界面")
        self._remaining()
        self._logged_in = True

    def run_daily(self, deadline: float) -> int:
        """复用原 run all 的日常顺序，始终操作已经完成扫码的同一设备。"""
        if not self._logged_in:
            raise RuntimeError("必须先完成扫码登录并确认主界面")
        self._set_deadline(deadline)
        summary = self._runner().run(self._daily_actions)
        self._remaining()
        return summary.action_count
