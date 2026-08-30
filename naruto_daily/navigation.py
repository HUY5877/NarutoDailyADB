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
    button_template_path = Path(asset_root) / action["button_template"]
    roi = (action["left"], action["top"], action["right"], action["bottom"])
    threshold = float(action["threshold"])
    max_swipes = action["max_swipes"]

    for swipe_count in range(max_swipes + 1):
        screenshot = client.screenshot()
        center, score = template_matcher(screenshot, template_path, roi, threshold)
        if center is not None:
            LOGGER.info(
                "发现%s，相似度 %.3f，文字中心：(%d, %d)",
                name,
                score,
                center[0],
                center[1],
            )
            button_roi = (
                max(0, center[0] + action["button_left_offset"]),
                max(0, center[1] + action["button_top_offset"]),
                min(action["right"], center[0] + action["button_right_offset"]),
                center[1] + action["button_bottom_offset"],
            )
            button_center, button_score = template_matcher(
                screenshot,
                button_template_path,
                button_roi,
                float(action["button_threshold"]),
            )
            if button_center is None:
                raise VisualError(
                    f"找到{name}，但未找到它下方的立刻前往，最后相似度 {button_score:.3f}"
                )
            LOGGER.info(
                "发现对应的立刻前往，相似度 %.3f，点击坐标：(%d, %d)",
                button_score,
                button_center[0],
                button_center[1],
            )
            client.tap(*button_center)
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
