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


def cleanup_popups(
    client: AdbClient,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> PopupCleanupSummary:
    """按配置轮询可选弹窗，识别成功后点击模板中心。"""
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    idle_count = 0
    click_count = 0
    completed_rounds = 0
    wait_seconds = float(action["wait_seconds"])
    pre_click_wait = float(action.get("pre_click_wait_seconds", 1))
    post_click_wait = float(action.get("post_click_wait_seconds", 1))

    for round_number in range(1, int(action["rounds"]) + 1):
        completed_rounds = round_number
        round_clicked = False
        LOGGER.info('[弹窗清理 第%d/%d轮] 依次检查：%s；找到则复查并点击，未找到则检查下一项；连续 %d 轮未点击时结束',
                    round_number, action['rounds'], '、'.join(t['name'] for t in action['targets']), action['idle_rounds'])
        for target in action["targets"]:
            center, score = images.find(target)
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
                "第 %d 轮：发现%s，相似度 %.3f，点击前等待 %.2f 秒",
                round_number,
                target["name"],
                score,
                pre_click_wait,
            )
            sleep(pre_click_wait)
            confirmed_center, confirmed_score = images.find(target)
            if confirmed_center is None:
                LOGGER.info(
                    "第 %d 轮：等待后%s已消失，相似度 %.3f，不点击旧坐标",
                    round_number,
                    target["name"],
                    confirmed_score,
                )
                if wait_seconds:
                    sleep(wait_seconds)
                continue
            LOGGER.info(
                "第 %d 轮：再次确认%s，相似度 %.3f，点击坐标 (%d, %d)",
                round_number,
                target["name"],
                confirmed_score,
                confirmed_center[0],
                confirmed_center[1],
            )
            client.tap(*confirmed_center)
            click_count += 1
            round_clicked = True
            LOGGER.info("等待弹窗切换 %.2f 秒", post_click_wait)
            sleep(post_click_wait)

        if round_clicked:
            idle_count = 0
            LOGGER.info('本轮已处理弹窗；还有轮次额度则重新检查整组弹窗')
            continue
        idle_count += 1
        if idle_count >= int(action["idle_rounds"]):
            LOGGER.info("连续 %d 轮没有发现可处理弹窗，结束清理", idle_count)
            break
        LOGGER.info('本轮未点击弹窗，连续空闲 %d/%d 轮；还有轮次额度则继续检查', idle_count, action['idle_rounds'])

    LOGGER.info("弹窗清理结束：执行 %d 轮，点击 %d 次", completed_rounds, click_count)
    return PopupCleanupSummary(completed_rounds, click_count)
