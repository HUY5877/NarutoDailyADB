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

import cv2
import numpy as np

from naruto_daily.common.adb_client import AdbClient, CommandResult
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import find_template_in_png
from naruto_daily.battle import run_duel_battle
from naruto_daily.config import ConfigError, load_settings
from naruto_daily.runner import ActionRunner
from naruto_daily.tasks import build_all, build_task
from .user_options import ARENA_WEEKLY_SECONDS, UserOptions, parse_user_options


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
        selected_settings = self.settings
        skipped_tasks = {}
        weekly = self.user_options.get('mainTask') == 'arena_weekly'
        if not weekly and not self.user_options["equipmentStamina"]:
            skipped_tasks['equipment_upgrade'] = []
            LOGGER.info("已按用户设置跳过装备栏体力刷取（扫荡、合成、装备和进阶）")
        skip_secret = self.user_options['secretRealmMode'] == 'skip'
        if not weekly and skip_secret:
            # Remove the entire route, including entry, room handling and its return navigation.
            skipped_tasks.update(secret_realm=[], secret_realm_farm=[])
            LOGGER.info('已按用户设置跳过整个秘境探险线路，继续其余日常')
        if skipped_tasks:
            selected_settings = replace(self.settings, tasks={**self.settings.tasks, **skipped_tasks})
        if weekly:
            entry = self.settings.tasks.get('arena_weekly')
            if not entry:
                raise ConfigError('缺少决斗场周胜 tasks.arena_weekly 入口动作，请更新配置文件')
            actions = [*self._login_actions, *deepcopy(entry)]
        elif task_name == "all":
            actions = build_all(selected_settings)
        elif task_name == "secret_realm_farm":
            actions = list(self._login_actions) if skip_secret else build_task(selected_settings, task_name)
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
                            asset_root=self.asset_root, user_options=self.user_options)

    def wait_for_authorization(self, deadline: float, *, on_wait=None) -> None:
        """只以开始游戏按钮确认扫码授权，按钮操作留给登录收尾。"""
        with execution_scope(deadline, clock=self._clock, sleep=self._sleep_impl):
            self._authorized = False
            self._logged_in = False
            self._set_deadline(deadline)
            if not self._login_actions or self._login_actions[0]["type"] != "finish_login_authorization":
                raise ConfigError("扫码登录必须先确认授权并开始游戏")
            action = self._login_actions[0]
            target = action["start_game"]
            images = ImageRecognition(self.client, self._match, self.asset_root, self._sleep)
            LOGGER.info("等待手机扫码确认；识别到开始游戏按钮才结束二维码等待")
            def observed():
                self._remaining()
                png = self.client.screenshot()
                frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
                # 微信二维码、过期提示和加载页保持竖屏，不能套用游戏横屏检测区域。
                # 继续交给刷新回调和原截止时间；恢复横屏后再确认扫码授权。
                if frame is not None and frame.shape[0] > frame.shape[1]:
                    return False
                center, _ = images.match(png, target, timeout_seconds=self._remaining())
                if center is not None:
                    self._remaining()
                    self._authorized = True
                    LOGGER.info("已确认扫码授权，识别到开始游戏按钮，立即结束二维码等待")
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
            actions = deepcopy(self._login_actions)
            start = actions[0]
            # 扫码收尾只点击开始游戏；保存账号登录继续使用原授权流程。
            actions[0] = {
                **start["start_game"], "type": "required_template_tap",
                "timeout_seconds": self._remaining(), "poll_seconds": start["poll_seconds"],
                "pre_click_wait_seconds": start.get("pre_click_wait_seconds", 3),
                "post_click_wait_seconds": start.get("post_click_wait_seconds", 3),
            }
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

    def run_daily(self, deadline: float, *, on_arena_started=None) -> int:
        """执行所选线路的登录后动作，默认完整日常，始终使用同一设备。"""
        with execution_scope(deadline, clock=self._clock, sleep=self._sleep_impl):
            if not self._logged_in:
                raise RuntimeError("必须先完成扫码登录并确认主界面")
            self._set_deadline(deadline)
            summary = self._runner().run(self._daily_actions)
            self._remaining()
        if self.user_options.get('mainTask') != 'arena_weekly':
            return summary.action_count
        # 登录和决斗场入口不占用周胜的 40 分钟；离开旧预算后再开始战斗预算。
        battle_deadline = self._clock() + ARENA_WEEKLY_SECONDS
        self._set_deadline(battle_deadline)
        if on_arena_started is not None:
            on_arena_started(battle_deadline)
        LOGGER.info('开始决斗场周胜战斗，从本次模块调用起计时 40 分钟，到时结束执行进程')
        battle = dict(timeout_seconds=ARENA_WEEKLY_SECONDS, tap_interval_seconds=0, end_targets=[])
        try:
            with execution_scope(battle_deadline, clock=self._clock, sleep=self._sleep_impl):
                while True:
                    run_duel_battle(self.client, battle, template_matcher=self._match,
                                    asset_root=self.asset_root, sleep=self._sleep, clock=self._clock)
        except (TimeoutError, subprocess.TimeoutExpired):
            if self._clock() < battle_deadline:
                raise
        LOGGER.info('决斗场周胜已执行满 40 分钟，结束本次任务')
        return summary.action_count
