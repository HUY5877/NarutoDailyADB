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
    pre_click_wait = float(action.get("pre_click_wait_seconds", 3))
    post_click_wait = float(action.get("post_click_wait_seconds", 3))

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
                "第 %d 轮：发现%s，相似度 %.3f，点击前等待 %.2f 秒",
                round_number,
                target["name"],
                score,
                pre_click_wait,
            )
            sleep(pre_click_wait)
            confirmed_center, confirmed_score = template_matcher(
                client.screenshot(), template_path, roi, float(target["threshold"])
            )
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
            continue
        idle_count += 1
        if idle_count >= int(action["idle_rounds"]):
            LOGGER.info("连续 %d 轮没有发现可处理弹窗，结束清理", idle_count)
            break

    LOGGER.info("弹窗清理结束：执行 %d 轮，点击 %d 次", completed_rounds, click_count)
    return PopupCleanupSummary(completed_rounds, click_count)


def return_to_main(
    client: AdbClient,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> PopupCleanupSummary:
    """逐层关闭带叉界面，并在识别到主界面后立即停止。"""

    idle_count = 0
    click_count = 0
    completed_rounds = 0
    main_target = action["main_page_marker"]
    main_template_path = Path(asset_root) / main_target["template"]
    main_roi = (
        main_target["left"], main_target["top"],
        main_target["right"], main_target["bottom"],
    )
    main_threshold = float(main_target["threshold"])
    close_target = action["close_target"]
    close_template_path = Path(asset_root) / close_target["template"]
    close_roi = (
        close_target["left"], close_target["top"],
        close_target["right"], close_target["bottom"],
    )
    close_threshold = float(close_target["threshold"])
    pre_click_wait = float(action.get("pre_click_wait_seconds", 3))
    post_click_wait = float(action.get("post_click_wait_seconds", 3))
    poll_seconds = float(action["poll_seconds"])

    for round_number in range(1, int(action["max_rounds"]) + 1):
        completed_rounds = round_number
        screenshot = client.screenshot()
        main_center, main_score = template_matcher(
            screenshot,
            main_template_path,
            main_roi,
            main_threshold,
        )
        if main_center is not None:
            LOGGER.info(
                "回主界面第 %d 轮：发现%s，相似度 %.3f，确认已处于主界面",
                round_number,
                main_target["name"],
                main_score,
            )
            break

        center, score = template_matcher(
            screenshot,
            close_template_path,
            close_roi,
            close_threshold,
        )
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
                LOGGER.info("连续 %d 轮没有发现关闭按钮，已回到主界面", idle_count)
                break
            sleep(poll_seconds)
            continue

        LOGGER.info(
            "回主界面第 %d 轮：发现关闭按钮，相似度 %.3f，点击前等待 %.2f 秒",
            round_number,
            score,
            pre_click_wait,
        )
        sleep(pre_click_wait)
        confirmed_screenshot = client.screenshot()
        confirmed_main_center, confirmed_main_score = template_matcher(
            confirmed_screenshot,
            main_template_path,
            main_roi,
            main_threshold,
        )
        if confirmed_main_center is not None:
            LOGGER.info(
                "点击前发现%s，相似度 %.3f，确认已处于主界面，取消点击",
                main_target["name"],
                confirmed_main_score,
            )
            break

        confirmed_center, confirmed_score = template_matcher(
            confirmed_screenshot,
            close_template_path,
            close_roi,
            close_threshold,
        )
        if confirmed_center is None:
            LOGGER.info(
                "等待后关闭按钮已消失，相似度 %.3f，本轮不点击旧坐标",
                confirmed_score,
            )
            idle_count += 1
            if idle_count >= int(action["idle_rounds"]):
                LOGGER.info("连续 %d 轮没有发现关闭按钮，已回到主界面", idle_count)
                break
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
        LOGGER.info("已达到最多 %d 轮回主界面检查", int(action["max_rounds"]))

    LOGGER.info("回主界面结束：检查 %d 轮，点击关闭 %d 次", completed_rounds, click_count)
    return PopupCleanupSummary(completed_rounds, click_count)
