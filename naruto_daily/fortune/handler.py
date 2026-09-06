from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class FortuneSummary:
    """记录成功执行的免费招财次数。"""

    clicks: int


def handle_coin_fortune(
    client,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> FortuneSummary:
    """只识别并点击配置区域内的免费招财按钮。"""

    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    sleep = images.sleep
    target = action["free_button"]
    timeout_seconds = float(action["timeout_seconds"])
    poll_seconds = float(action["poll_seconds"])
    pre_click_wait = float(action.get("pre_click_wait_seconds", 1))
    post_click_wait = float(action.get("post_click_wait_seconds", 1))
    required_clicks = int(action["clicks"])
    completed_clicks = 0
    max_missing_checks = 3

    for click_number in range(1, required_clicks + 1):
        LOGGER.info('[免费招财 第%d/%d次] 查找并复查免费按钮，成功点击一次后进入下一次；连续 %d 轮未确认则结束招财',
                    click_number, required_clicks, max_missing_checks)
        deadline = images.elapsed + timeout_seconds
        missing_checks = 0
        round_number = 0
        while True:
            round_number += 1
            LOGGER.info('[招财第%d次·检查第%d轮] 识别免费按钮→等待后复查→点击；连续未确认 %d/%d 轮，剩余等待 %.2f 秒',
                        click_number, round_number, missing_checks, max_missing_checks, max(0, deadline - images.elapsed))
            center, score = images.find(target, deadline=deadline)
            if center is not None:
                missing_checks = 0
                LOGGER.info(
                    "发现第 %d/%d 次%s，相似度 %.3f，点击前等待 %.2f 秒",
                    click_number,
                    required_clicks,
                    target["name"],
                    score,
                    pre_click_wait,
                )
                sleep(pre_click_wait)
                confirmed_center, confirmed_score = images.find(target, deadline=deadline)
                if confirmed_center is None:
                    missing_checks = 1
                    LOGGER.info(
                        "等待后%s未确认（含补查），相似度 %.3f，不点击旧坐标并继续识别",
                        target["name"],
                        confirmed_score,
                    )
                else:
                    LOGGER.info(
                        "再次确认%s，相似度 %.3f，点击坐标：(%d, %d)",
                        target["name"],
                        confirmed_score,
                        confirmed_center[0],
                        confirmed_center[1],
                    )
                    client.tap(*confirmed_center)
                    completed_clicks += 1
                    LOGGER.info(
                        "完成第 %d/%d 次免费招财，等待页面响应 %.2f 秒",
                        completed_clicks,
                        required_clicks,
                        post_click_wait,
                    )
                    sleep(post_click_wait)
                    break
            else:
                missing_checks += 1

            if missing_checks >= max_missing_checks:
                LOGGER.info(
                    "连续 %d 次未发现%s，结束招财并交给后续动作返回主界面",
                    max_missing_checks,
                    target["name"],
                )
                return FortuneSummary(clicks=completed_clicks)

            if images.elapsed >= deadline:
                raise VisualError(
                    f"等待第 {click_number}/{required_clicks} 次{target['name']}超时，"
                    f"最后相似度 {score:.3f}"
                )
            delay = min(poll_seconds, deadline - images.elapsed)
            LOGGER.info(
                "等待第 %d/%d 次%s，当前相似度 %.3f，%.2f 秒后重试",
                click_number,
                required_clicks,
                target["name"],
                score,
                delay,
            )
            sleep(delay)

    return FortuneSummary(clicks=completed_clicks)
