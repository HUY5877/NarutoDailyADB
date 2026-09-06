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
class NavigationSummary:
    """记录横向滑动次数以及目标任务是否已经完成。"""

    swipes: int
    completed: bool


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
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    max_swipes = action["max_swipes"]

    def scan_card(png):
        center, score = images.match(png, action)
        if center is None:
            return center, score, None, 0.0, None, 0.0
        region = dict(
            left=max(0, center[0] + action["button_left_offset"]),
            top=max(0, center[1] + action["button_top_offset"]),
            right=min(action["right"], center[0] + action["button_right_offset"]),
            bottom=center[1] + action["button_bottom_offset"],
        )
        completed, completed_score = images.match(png, dict(region,
            template=action["completed_template"], threshold=action["completed_threshold"]))
        if completed is not None:
            return center, score, completed, completed_score, None, 0.0
        button, button_score = images.match(png, dict(region,
            template=action["button_template"], threshold=action["button_threshold"]))
        return center, score, completed, completed_score, button, button_score

    def check_card():
        # 每次补查都重新定位文字、已完成标记及该卡片的按钮。
        return images.check(scan_card, lambda r: r[2] is not None or r[4] is not None, name)

    swipe_count = 0
    alignment_swipe_used = False
    round_number = 0
    while True:
        round_number += 1
        LOGGER.info('[查找%s 第%d轮] 定位卡片→检查已完成标记或前往按钮；未找到则滑动，达到上限后跳过或报错；已滑动 %d 次，常规上限 %d 次',
                    name, round_number, swipe_count, max_swipes)
        center, score, completed_center, completed_score, button_center, button_score = check_card()
        if center is not None:
            LOGGER.info(
                "发现%s，相似度 %.3f，文字中心：(%d, %d)",
                name,
                score,
                center[0],
                center[1],
            )
            if completed_center is not None:
                LOGGER.info(
                    "今日%s已完成，相似度 %.3f，跳过执行",
                    name,
                    completed_score,
                )
                return NavigationSummary(swipes=swipe_count, completed=True)

            if button_center is None:
                if not alignment_swipe_used:
                    LOGGER.info(
                        "找到%s文字，但按钮和已完成标记都不完整，额外左滑一次后重新判断",
                        name,
                    )
                    client.swipe(
                        action["swipe_x1"],
                        action["swipe_y1"],
                        action["swipe_x2"],
                        action["swipe_y2"],
                        action["swipe_duration_ms"],
                    )
                    swipe_count += 1
                    alignment_swipe_used = True
                    sleep(float(action["wait_seconds"]))
                    continue
                if action.get("completed_skip_type"):
                    LOGGER.info(
                        "额外左滑后仍未找到可执行按钮，跳过%s这个可选任务",
                        name,
                    )
                    return NavigationSummary(swipes=swipe_count, completed=True)
                raise VisualError(
                    f"找到{name}，但未找到它下方的立刻前往，最后相似度 {button_score:.3f}"
                )
            LOGGER.info(
                "发现对应的立刻前往，相似度 %.3f，点击前等待 %.2f 秒",
                button_score,
                float(action.get("pre_click_wait_seconds", 1)),
            )
            sleep(float(action.get("pre_click_wait_seconds", 1)))
            _, _, completed_center, completed_score, confirmed_center, confirmed_score = check_card()
            if completed_center is not None:
                LOGGER.info("今日%s已完成，相似度 %.3f，取消点击并跳过执行", name, completed_score)
                return NavigationSummary(swipes=swipe_count, completed=True)
            if confirmed_center is None:
                raise VisualError(
                    f"找到{name}的立刻前往，但点击前复查及补查均未确认，"
                    f"最后相似度 {confirmed_score:.3f}"
                )
            LOGGER.info(
                "再次确认对应的立刻前往，相似度 %.3f，点击坐标：(%d, %d)",
                confirmed_score,
                confirmed_center[0],
                confirmed_center[1],
            )
            client.tap(*confirmed_center)
            LOGGER.info(
                "点击立刻前往后等待页面响应 %.2f 秒",
                float(action.get("post_click_wait_seconds", 1)),
            )
            sleep(float(action.get("post_click_wait_seconds", 1)))
            return NavigationSummary(swipes=swipe_count, completed=False)

        if alignment_swipe_used:
            if action.get("completed_skip_type"):
                LOGGER.info(
                    "额外左滑后未再找到%s，跳过这个可选任务",
                    name,
                )
                return NavigationSummary(swipes=swipe_count, completed=True)
            raise VisualError(f"额外左滑后未再找到{name}，最后相似度 {score:.3f}")

        if swipe_count == max_swipes:
            if action.get("completed_skip_type"):
                LOGGER.info(
                    "滑动 %d 次后仍未找到%s，跳过这个可选任务",
                    max_swipes,
                    name,
                )
                return NavigationSummary(swipes=swipe_count, completed=True)
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
        swipe_count += 1
        sleep(float(action["wait_seconds"]))


def required_template_tap(client, action, *, template_matcher=find_template_in_png,
                          asset_root=Path("."), sleep=time.sleep):
    """等待、复查并点击必需按钮，供任务执行器和入口流程共用。"""
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    timeout_seconds = float(action["timeout_seconds"])
    poll_seconds = float(action["poll_seconds"])
    pre_click_wait_seconds = float(action.get("pre_click_wait_seconds", 1))
    post_click_wait_seconds = float(action.get("post_click_wait_seconds", 1))
    deadline = images.elapsed + timeout_seconds
    round_number = 0
    while True:
        round_number += 1
        LOGGER.info('[等待点击%s 第%d轮] 查找按钮→等待后复查→点击后结束；未确认则轮询，超时停止；剩余等待 %.2f 秒',
                    action['name'], round_number, max(0, deadline - images.elapsed))
        center, score = images.find(action, deadline=deadline)
        if center is not None:
            LOGGER.info(
                "发现%s，相似度 %.3f，点击前等待 %.2f 秒",
                action["name"], score, pre_click_wait_seconds,
            )
            images.sleep(pre_click_wait_seconds)
            confirmed_center, confirmed_score = images.find(action, deadline=deadline)
            if confirmed_center is None:
                LOGGER.info(
                    "等待后%s已消失，相似度 %.3f，继续识别",
                    action["name"], confirmed_score,
                )
            else:
                LOGGER.info(
                    "再次确认%s，相似度 %.3f，点击坐标 (%d, %d)",
                    action["name"], confirmed_score, confirmed_center[0], confirmed_center[1],
                )
                client.tap(*confirmed_center)
                LOGGER.info(
                    "点击完成，等待 %.2f 秒让页面响应",
                    post_click_wait_seconds,
                )
                images.sleep(post_click_wait_seconds)
                break
        if images.elapsed >= deadline:
            raise VisualError(f"等待{action['name']}超时，最后相似度 {score:.3f}")
        delay = min(poll_seconds, deadline - images.elapsed)
        LOGGER.info(
            "等待%s，当前相似度 %.3f，%.2f 秒后重试",
            action["name"], score, delay,
        )
        images.sleep(delay)
