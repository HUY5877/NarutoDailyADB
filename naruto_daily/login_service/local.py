"""本地扫码刷秘境：只操作指定设备，不连接任务队列或上传二维码。"""
from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Callable

from .daily import DailySession
from .qr_navigation import LoginDevice, fetch_login_qr


LOGGER = logging.getLogger("naruto_daily")


def run_local_qr_secret(
    config_path: Path, adb_path: Path, serial: str, *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    session = DailySession(config_path, adb_path, serial, 20, task_name="secret_realm_farm",
                           sleep=sleep, clock=clock)
    LOGGER.info("本地扫码刷秘境，设备：%s；二维码直接显示在模拟器，不上传、不消费 Redis 任务", serial)
    LOGGER.info("正在打开 QQ 扫码页，请保持游戏处于登录界面")
    # 共用有 QQ / 无 QQ 的导航，只确认实际二维码已显示，不保存或发布图片。
    fetch_login_qr(LoginDevice(adb_path, serial, clock() + 120))
    LOGGER.info("二维码已显示，请在 180 秒内用手机 QQ 扫码并确认授权")
    session.wait_for_authorization(clock() + 180)
    LOGGER.info("扫码授权成功，正在进入游戏并处理登录弹窗")
    session.finish_login(clock() + 180)
    LOGGER.info("登录成功，开始独立秘境线路：忍者挑战 → 秘境探险 → 按挑战券循环")
    return session.run_daily(clock() + 7200)
