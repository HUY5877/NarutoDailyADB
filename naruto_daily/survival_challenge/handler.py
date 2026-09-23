"""生存挑战：进入奖励卡片后执行扫荡、等待和按模型读取重置次数。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.prompts import SURVIVAL_RESET_COUNT


LOGGER = logging.getLogger("naruto_daily")
TRANSFER_DISMISS = (960, 540)
BUTTON_TIMEOUT_SECONDS = 20.0


def _wait_and_click(client, images: ImageRecognition, target: dict, *, deadline: float,
                    poll_seconds: float, post_click_wait: float,
                    elapsed: Callable[[], float], skip_if: dict | None = None) -> bool:
    def probe(png):
        # 先确认前景的扫荡提示，不能点击弹窗后方变暗的“准备就绪”。
        if skip_if is not None:
            center, score = images.match(png, skip_if)
            if center is not None:
                return None, score, True
        center, score = images.match(png, target)
        return center, score, False

    deadline = min(deadline, elapsed() + BUTTON_TIMEOUT_SECONDS)
    round_number = 0
    score = 0.0
    while elapsed() < deadline:
        round_number += 1
        remaining = deadline - elapsed()
        LOGGER.info("[生存挑战：%s 第%d轮] 等待按钮，剩余 %.2f 秒",
                    target['name'], round_number, remaining)
        name = target['name'] if skip_if is None else f"{target['name']} / {skip_if['name']}"
        center, score, skip = images.check(
            probe, lambda result: result[0] is not None or result[2], name,
            deadline=images.elapsed + remaining,
        )
        # ADB 截图和模板计算也计入本步骤预算，迟到的匹配结果不能触发点击。
        if elapsed() >= deadline:
            break
        if skip:
            LOGGER.info("已出现%s，名单已确认，跳过准备就绪和名单确认", skip_if['name'])
            return False
        if center is not None:
            LOGGER.info("确认%s，相似度 %.3f，点击坐标：(%d, %d)",
                        target["name"], score, *center)
            client.tap(*center)
            if post_click_wait:
                images.sleep(post_click_wait)
            return True
        LOGGER.info("未找到%s，相似度 %.3f，阈值 %.3f",
                    target['name'], score, target['threshold'])
        remaining = deadline - elapsed()
        if remaining > 0:
            images.sleep(min(poll_seconds, remaining))
    raise VisualError(f"等待生存挑战按钮{target['name']}超时，最后相似度 {score:.3f}，未继续点击")


def _dismiss_transfer_if_present(client, images: ImageRecognition, target: dict) -> None:
    center, score = images.match(client.screenshot(), target)
    if center is None:
        LOGGER.info("未发现包含“传送”的提示界面，继续生存挑战")
        return
    LOGGER.info("发现“传送”提示界面，相似度 %.3f，点击任意位置退出", score)
    client.tap(*TRANSFER_DISMISS)
    images.sleep(1)


def _read_reset_count(model: MultimodalClient, client, *, timeout_seconds: float) -> int:
    result = model.complete_json(client.screenshot(), SURVIVAL_RESET_COUNT,
                                 timeout_seconds=timeout_seconds)
    count = result.get("reset_count") if isinstance(result, dict) else None
    if type(count) is not int or count < 0:
        raise VisualError("大模型未能确认生存挑战重置次数，已停止")
    LOGGER.info("大模型识别生存挑战重置次数：%d", count)
    return count


def handle_survival_challenge(
    client, action: dict, *, model: MultimodalClient | None = None,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."), sleep: Callable[[float], None] = time.sleep,
) -> None:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    started = time.monotonic()

    def elapsed() -> float:
        # 实际运行计入 ADB/识别/模型耗时；测试中的替代 sleep 仍推进等待预算。
        return max(images.elapsed, time.monotonic() - started)

    if model is None:
        model = MultimodalClient(action["multimodal"], asset_root=asset_root)
    deadline = float(action["timeout_seconds"])
    post_click_wait = float(action.get("post_click_wait_seconds", 1))
    poll_seconds = float(action["poll_seconds"])

    _dismiss_transfer_if_present(client, images, action["transfer_marker"])
    for round_number in range(1, int(action["max_rounds"]) + 1):
        LOGGER.info("[生存挑战第%d轮] 开始扫荡→按当前页面处理名单/扫荡确认→等待%d秒",
                    round_number, int(action["wait_seconds"]))
        _wait_and_click(client, images, action['start_sweep'], deadline=deadline,
                        poll_seconds=poll_seconds, post_click_wait=post_click_wait, elapsed=elapsed)
        roster_open = _wait_and_click(
            client, images, action['ready'], deadline=deadline, poll_seconds=poll_seconds,
            post_click_wait=post_click_wait, elapsed=elapsed, skip_if=action.get('sweep_prompt'),
        )
        if roster_open:
            _wait_and_click(client, images, action['confirm_first'], deadline=deadline,
                            poll_seconds=poll_seconds, post_click_wait=post_click_wait, elapsed=elapsed)
        _wait_and_click(client, images, action['confirm_second'], deadline=deadline,
                        poll_seconds=poll_seconds, post_click_wait=post_click_wait, elapsed=elapsed)
        if elapsed() + float(action["wait_seconds"]) >= deadline:
            raise VisualError("生存挑战等待扫荡完成超时")
        images.sleep(float(action["wait_seconds"]))
        remaining = deadline - elapsed()
        if remaining <= 0:
            raise VisualError("生存挑战等待扫荡完成超时")
        count = _read_reset_count(model, client, timeout_seconds=remaining)
        if elapsed() >= deadline:
            raise VisualError("生存挑战重置次数识别超时，未继续点击")
        if count == 0:
            LOGGER.info("生存挑战重置次数为0，结束扫荡循环")
            return
        LOGGER.info("生存挑战重置次数为%d，大于0，点击重置并确认后继续", count)
        _wait_and_click(client, images, action["reset"], deadline=deadline,
                        poll_seconds=poll_seconds, post_click_wait=post_click_wait,
                        elapsed=elapsed)
        _wait_and_click(client, images, action["reset_confirm"], deadline=deadline,
                        poll_seconds=poll_seconds, post_click_wait=post_click_wait,
                        elapsed=elapsed)
    raise VisualError(f"生存挑战达到最多 {action['max_rounds']} 轮，未能结束")
