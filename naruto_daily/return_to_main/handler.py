from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from naruto_daily.common.adb_client import AdbClient
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.results import PopupCleanupSummary


LOGGER = logging.getLogger("naruto_daily")


def return_to_main(
    client: AdbClient,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> PopupCleanupSummary:
    """逐层关闭带叉界面，并在识别到主界面后立即停止。"""

    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    idle_count = 0
    click_count = 0
    completed_rounds = 0
    main_target = action["main_page_marker"]
    close_target = action["close_target"]
    pre_click_wait = float(action.get("pre_click_wait_seconds", 1))
    post_click_wait = float(action.get("post_click_wait_seconds", 1))
    poll_seconds = float(action["poll_seconds"])

    for round_number in range(1, int(action["max_rounds"]) + 1):
        completed_rounds = round_number
        LOGGER.info('[回主界面 第%d/%d轮] 先找主界面标记，找到即结束；否则找关闭按钮并复查点击；都未找到则等待，连续空闲 %d 轮后停止',
                    round_number, action['max_rounds'], action['idle_rounds'])
        page, matches = images.either([main_target, close_target])
        main_center, main_score = matches[0]
        if main_center is not None:
            LOGGER.info(
                "回主界面第 %d 轮：发现%s，相似度 %.3f，确认已处于主界面",
                round_number,
                main_target["name"],
                main_score,
            )
            break

        center, score = matches[1]
        if center is None:
            idle_count += 1
            LOGGER.info(
                "回主界面第 %d 轮：未发现关闭按钮，相似度 %.3f，连续空闲 %d/%d 轮",
                round_number,
                score,
                idle_count,
                int(action["idle_rounds"]),
            )
            if idle_count >= int(action["idle_rounds"]):
                message = (
                    f"连续 {idle_count} 轮既未发现主界面标记，也未发现关闭按钮，"
                    "未能确认已经返回主界面"
                )
                LOGGER.error(message)
                raise VisualError(message)
            LOGGER.info('本轮未找到主界面或关闭按钮，等待 %.2f 秒后进入下一轮检查（如有剩余轮次）', poll_seconds)
            sleep(poll_seconds)
            continue

        LOGGER.info(
            "回主界面第 %d 轮：发现关闭按钮，相似度 %.3f，点击前等待 %.2f 秒",
            round_number,
            score,
            pre_click_wait,
        )
        sleep(pre_click_wait)
        page, matches = images.either([main_target, close_target])
        confirmed_main_center, confirmed_main_score = matches[0]
        if confirmed_main_center is not None:
            LOGGER.info(
                "点击前发现%s，相似度 %.3f，确认已处于主界面，取消点击",
                main_target["name"],
                confirmed_main_score,
            )
            break

        confirmed_center, confirmed_score = matches[1]
        if confirmed_center is None:
            LOGGER.info(
                "等待后关闭按钮已消失，相似度 %.3f，本轮不点击旧坐标",
                confirmed_score,
            )
            idle_count += 1
            if idle_count >= int(action["idle_rounds"]):
                message = (
                    f"连续 {idle_count} 轮既未发现主界面标记，也未发现关闭按钮，"
                    "未能确认已经返回主界面"
                )
                LOGGER.error(message)
                raise VisualError(message)
            LOGGER.info('本轮取消关闭点击，等待 %.2f 秒后进入下一轮检查（如有剩余轮次）', poll_seconds)
            sleep(poll_seconds)
            continue

        LOGGER.info(
            "再次确认关闭按钮，相似度 %.3f，点击坐标：(%d, %d)",
            confirmed_score,
            confirmed_center[0],
            confirmed_center[1],
        )
        client.tap(*confirmed_center)
        click_count += 1
        idle_count = 0
        LOGGER.info("关闭一层界面后等待 %.2f 秒", post_click_wait)
        sleep(post_click_wait)
    else:
        message = (
            f"已达到最多 {int(action['max_rounds'])} 轮回主界面检查，"
            "未能确认已经返回主界面"
        )
        LOGGER.error(message)
        raise VisualError(message)

    LOGGER.info("回主界面结束：检查 %d 轮，点击关闭 %d 次", completed_rounds, click_count)
    return PopupCleanupSummary(completed_rounds, click_count)
