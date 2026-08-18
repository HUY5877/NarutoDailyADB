from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .adb_client import AdbClient
from .visual import find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class PopupCleanupSummary:
    rounds: int
    clicks: int


def cleanup_popups(
    client: AdbClient,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> PopupCleanupSummary:
    """按配置轮询可选弹窗，识别成功后点击模板中心。"""
    idle_count = 0
    click_count = 0
    completed_rounds = 0
    wait_seconds = float(action["wait_seconds"])

    for round_number in range(1, int(action["rounds"]) + 1):
        completed_rounds = round_number
        round_clicked = False
        LOGGER.info("开始第 %d 轮弹窗清理", round_number)
        for target in action["targets"]:
            template_path = Path(asset_root) / target["template"]
            roi = (target["left"], target["top"], target["right"], target["bottom"])
            center, score = template_matcher(
                client.screenshot(), template_path, roi, float(target["threshold"])
            )
            if center is None:
                LOGGER.info(
                    "第 %d 轮：未发现%s，相似度 %.3f，阈值 %.3f",
                    round_number,
                    target["name"],
                    score,
                    float(target["threshold"]),
                )
                continue
            LOGGER.info(
                "第 %d 轮：发现%s，相似度 %.3f，点击坐标 (%d, %d)",
                round_number,
                target["name"],
                score,
                center[0],
                center[1],
            )
            client.tap(*center)
            click_count += 1
            round_clicked = True
            if wait_seconds:
                LOGGER.info("等待弹窗切换 %.2f 秒", wait_seconds)
                sleep(wait_seconds)

        if round_clicked:
            idle_count = 0
            continue
        idle_count += 1
        if idle_count >= int(action["idle_rounds"]):
            LOGGER.info("连续 %d 轮没有发现可处理弹窗，结束清理", idle_count)
            break

    LOGGER.info("弹窗清理结束：执行 %d 轮，点击 %d 次", completed_rounds, click_count)
    return PopupCleanupSummary(completed_rounds, click_count)
