"""在扫码任务绑定的设备中复用已有登录收尾和日常动作。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import logging
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Sequence

from naruto_daily.common.adb_client import AdbClient, CommandResult
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import find_template_in_png
from naruto_daily.config import ConfigError, load_settings
from naruto_daily.runner import ActionRunner
from naruto_daily.tasks import build_all, build_task
from .user_options import UserOptions, parse_user_options


LOGGER = logging.getLogger("naruto_daily")


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
        task_name: str = "all",
        user_options: UserOptions | None = None,
    ):
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("日常任务必须指定已绑定的 ADB serial")
        if (isinstance(command_timeout, bool) or not isinstance(command_timeout, (int, float))
                or not math.isfinite(command_timeout) or command_timeout <= 0):
            raise ValueError("ADB 命令超时必须是有限正数")
        self.config_path = Path(config_path).resolve()
        self.asset_root = self.config_path.parent
        self.settings = load_settings(self.config_path, skip_account_login=True)
        self.user_options = parse_user_options({} if user_options is None else user_options)
        self._login_actions = deepcopy(self.settings.tasks["login"])
        if task_name == "all":
            selected_settings = self.settings
            if not self.user_options["equipmentStamina"]:
                # Skip the whole equipment route, including menu navigation and
                # material synthesis/upgrade; all other task groups keep their order.
                selected_settings = replace(self.settings, tasks={**self.settings.tasks, "equipment_upgrade": []})
                LOGGER.info("已按用户设置跳过装备栏体力刷取（扫荡、合成、装备和进阶）")
            actions = build_all(selected_settings)
        elif task_name == "secret_realm_farm":
            actions = build_task(self.settings, task_name)
        else:
            raise ConfigError(f"不支持的扫码后任务：{task_name}")
        self._daily_actions = actions[len(self._login_actions):]
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
        self._authorized = False
        self._already_in_game = False
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
            raise DailyDeadlineExceeded("日常任务已超过截止时间")
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

    def wait_for_authorization(self, deadline: float, *, on_wait=None) -> None:
        """观察扫码后的明确页面，立即交接；按钮操作留给登录收尾。"""
        with execution_scope(deadline, clock=self._clock, sleep=self._sleep_impl):
            self._authorized = False
            self._already_in_game = False
            self._logged_in = False
            self._set_deadline(deadline)
            if not self._login_actions or self._login_actions[0]["type"] != "finish_login_authorization":
                raise ConfigError("扫码登录必须先确认授权并开始游戏")
            action = self._login_actions[0]
            targets = (action["start_game"], action["second_authorization"], self._main_marker)
            images = ImageRecognition(self.client, self._match, self.asset_root, self._sleep)
            LOGGER.info("等待手机扫码确认；识别到授权后的页面即结束二维码等待")
            def observed():
                self._remaining()
                png = self.client.screenshot()
                for index, target in enumerate(targets):
                    if index == 2 and on_wait is not None and target.get('main_menu'):
                        from .qr_navigation import pre_login_image
                        # Known QR/login pages cannot be the game home screen.
                        # Keep Redis polling responsive instead of issuing a model
                        # request on every observation of a waiting QR code.
                        if pre_login_image(png):
                            return False
                    center, _ = images.match(png, target, timeout_seconds=self._remaining())
                    if center is not None:
                        self._remaining()
                        self._authorized = True
                        self._already_in_game = index == 2
                        LOGGER.info("已确认扫码授权，识别到%s，立即结束二维码等待", target.get("name", "游戏主界面"))
                        return True
                return False

            while True:
                if observed():
                    return
                if on_wait is not None:
                    on_wait(observed)
                    if self._authorized:
                        return
                # A missing QR alone is not evidence: loading, cancellation and
                # network errors must not be mistaken for successful authorization.
                self._sleep(min(0.5, self._remaining()))

    def finish_login(self, deadline: float) -> None:
        """授权后沿用日常总截止时间等待加载、清弹窗并确认主界面。"""
        with execution_scope(deadline, clock=self._clock, sleep=self._sleep_impl):
            if not self._authorized:
                raise RuntimeError("必须先完成扫码授权")
            self._logged_in = False
            self._set_deadline(deadline)
            actions = deepcopy(self._login_actions[1:] if self._already_in_game else self._login_actions)
            if not self._already_in_game:
                actions[0]["timeout_seconds"] = self._remaining()
            LOGGER.info("开始扫码登录收尾，执行登录收尾动作；外围剩余时间 %.2f 秒", max(0.0, deadline - self._clock()))
            self._runner().run(actions)
            LOGGER.info("登录收尾动作已完成，额外复核主界面；外围剩余时间 %.2f 秒", max(0.0, deadline - self._clock()))
            images = ImageRecognition(self.client, self._match, self.asset_root, self._sleep)
            images.wait_for(self._main_marker, timeout_seconds=self._remaining(), poll_seconds=1)
            self._remaining()
            self._logged_in = True

    def wait_for_login(self, deadline: float) -> None:
        """兼容原调用者，在同一预算内完成授权和登录收尾。"""
        self.wait_for_authorization(deadline)
        self.finish_login(deadline)

    def run_daily(self, deadline: float) -> int:
        """执行所选线路的登录后动作，默认完整日常，始终使用同一设备。"""
        with execution_scope(deadline, clock=self._clock, sleep=self._sleep_impl):
            if not self._logged_in:
                raise RuntimeError("必须先完成扫码登录并确认主界面")
            self._set_deadline(deadline)
            summary = self._runner().run(self._daily_actions)
            self._remaining()
            return summary.action_count
