from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class NavigationSummary:
    """记录为找到目标卡片执行的横向滑动次数。"""

    swipes: int


def navigate_to_card(
    client,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> NavigationSummary:
    """横向查找指定任务卡片，并点击该卡片下方对应的前往按钮。"""

    name = action["name"]
    template_path = Path(asset_root) / action["template"]
    roi = (action["left"], action["top"], action["right"], action["bottom"])
    threshold = float(action["threshold"])
    max_swipes = action["max_swipes"]

    for swipe_count in range(max_swipes + 1):
        center, score = template_matcher(client.screenshot(), template_path, roi, threshold)
        if center is not None:
            click_x = center[0] + action["click_offset_x"]
            click_y = center[1] + action["click_offset_y"]
            LOGGER.info(
                "发现%s，相似度 %.3f，点击对应的立刻前往：(%d, %d)",
                name,
                score,
                click_x,
                click_y,
            )
            client.tap(click_x, click_y)
            return NavigationSummary(swipes=swipe_count)

        if swipe_count == max_swipes:
            raise VisualError(f"滑动 {max_swipes} 次后仍未找到{name}，最后相似度 {score:.3f}")

        LOGGER.info(
            "当前页面未找到%s，相似度 %.3f，执行第 %d/%d 次横向滑动",
            name,
            score,
            swipe_count + 1,
            max_swipes,
        )
        client.swipe(
            action["swipe_x1"],
            action["swipe_y1"],
            action["swipe_x2"],
            action["swipe_y2"],
            action["swipe_duration_ms"],
        )
        sleep(float(action["wait_seconds"]))

    raise AssertionError("横向查找循环不应运行到此处")
