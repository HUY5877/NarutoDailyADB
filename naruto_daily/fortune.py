from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class FortuneSummary:
    """记录成功执行的免费招财次数。"""

    clicks: int


def _match(client, target: dict, template_matcher: Callable, asset_root: Path):
    template_path = Path(asset_root) / target["template"]
    roi = (target["left"], target["top"], target["right"], target["bottom"])
    return template_matcher(client.screenshot(), template_path, roi, float(target["threshold"]))


def handle_coin_fortune(
    client,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> FortuneSummary:
    """只识别并点击配置区域内的免费招财按钮。"""

    target = action["free_button"]
    timeout_seconds = float(action["timeout_seconds"])
    poll_seconds = float(action["poll_seconds"])
    pre_click_wait = float(action.get("pre_click_wait_seconds", 3))
    post_click_wait = float(action.get("post_click_wait_seconds", 3))
    required_clicks = int(action["clicks"])
    completed_clicks = 0
    max_missing_checks = 3

    for click_number in range(1, required_clicks + 1):
        elapsed = 0.0
        missing_checks = 0
        while True:
            center, score = _match(client, target, template_matcher, asset_root)
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
                elapsed += pre_click_wait
                confirmed_center, confirmed_score = _match(
                    client, target, template_matcher, asset_root
                )
                if confirmed_center is None:
                    missing_checks = 1
                    LOGGER.info(
                        "等待后%s已消失，相似度 %.3f，不点击旧坐标并继续识别",
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

            if elapsed >= timeout_seconds:
                raise VisualError(
                    f"等待第 {click_number}/{required_clicks} 次{target['name']}超时，"
                    f"最后相似度 {score:.3f}"
                )
            delay = min(poll_seconds, timeout_seconds - elapsed)
            LOGGER.info(
                "等待第 %d/%d 次%s，当前相似度 %.3f，%.2f 秒后重试",
                click_number,
                required_clicks,
                target["name"],
                score,
                delay,
            )
            sleep(delay)
            elapsed += delay

    return FortuneSummary(clicks=completed_clicks)
