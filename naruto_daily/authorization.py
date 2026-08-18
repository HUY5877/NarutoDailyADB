from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .adb_client import AdbClient
from .visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class AuthorizationSummary:
    used_second_authorization: bool


def _match_target(client, target: dict, template_matcher: Callable, asset_root: Path):
    template_path = Path(asset_root) / target["template"]
    roi = (target["left"], target["top"], target["right"], target["bottom"])
    return template_matcher(client.screenshot(), template_path, roi, float(target["threshold"]))


def finish_login_authorization(
    client: AdbClient,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> AuthorizationSummary:
    """兼容一次授权和两次授权，并最终点击开始游戏。"""
    timeout_seconds = float(action["timeout_seconds"])
    poll_seconds = float(action["poll_seconds"])
    elapsed = 0.0
    used_second_authorization = False

    while True:
        start_center, start_score = _match_target(
            client, action["start_game"], template_matcher, Path(asset_root)
        )
        if start_center is not None:
            if used_second_authorization:
                LOGGER.info("第二次授权完成，发现开始游戏按钮，相似度 %.3f", start_score)
            else:
                LOGGER.info("无需第二次授权，直接发现开始游戏按钮，相似度 %.3f", start_score)
            LOGGER.info("点击开始游戏坐标：(%d, %d)", start_center[0], start_center[1])
            client.tap(*start_center)
            return AuthorizationSummary(used_second_authorization)

        second_score = 0.0
        if not used_second_authorization:
            second_center, second_score = _match_target(
                client, action["second_authorization"], template_matcher, Path(asset_root)
            )
            if second_center is not None:
                LOGGER.info("发现第二次授权页面，相似度 %.3f", second_score)
                click_x = action["second_authorization"]["click_x"]
                click_y = action["second_authorization"]["click_y"]
                LOGGER.info("点击第二次授权同意坐标：(%d, %d)", click_x, click_y)
                client.tap(click_x, click_y)
                used_second_authorization = True

        if elapsed >= timeout_seconds:
            raise VisualError(
                f"等待授权完成页面超时，开始游戏相似度 {start_score:.3f}，第二次授权页面相似度 {second_score:.3f}"
            )
        delay = min(poll_seconds, timeout_seconds - elapsed)
        LOGGER.info("等待授权页面加载，%.2f 秒后重试", delay)
        sleep(delay)
        elapsed += delay
