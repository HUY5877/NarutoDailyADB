"""丰饶之间的挑战入口和结算处理，战斗操作复用通用模块。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from naruto_daily.battle import BattleSummary, run_battle
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png


LOGGER = logging.getLogger('naruto_daily')


def _click_confirmed(client, images: ImageRecognition, target: dict, timeout: float, pre_wait: float):
    deadline = images.elapsed + timeout
    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[丰饶之间：%s 第%d轮] 查找按钮→等待后复查→点击后结束；未确认则继续等待，超时停止；剩余等待 %.2f 秒',
                    target['name'], round_number, max(0, deadline - images.elapsed))
        center, score = images.find(target, deadline=deadline)
        if center is not None:
            if images.elapsed + pre_wait > deadline:
                LOGGER.info('剩余时间不足以复查%s，结束本阶段循环', target['name'])
                break
            images.sleep(pre_wait)
            center, score = images.find(target, deadline=deadline)
            if center is not None:
                LOGGER.info('确认%s，相似度 %.3f，点击坐标：(%d, %d)', target['name'], score, *center)
                client.tap(*center)
                return
        if images.elapsed < deadline:
            delay = min(1, deadline-images.elapsed)
            LOGGER.info('本轮未确认%s，等待 %.2f 秒后开始下一轮', target['name'], delay)
            images.sleep(delay)
    raise VisualError(f"等待并确认{target['name']}超时")


def finish_abundance_result(
    client, action: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
) -> None:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    _click_confirmed(client, images, action['result_return'], float(action['timeout_seconds']),
                     float(action.get('pre_click_wait_seconds', 1)))
    images.sleep(float(action.get('post_click_wait_seconds', 1)))
    LOGGER.info('已关闭丰饶之间结算页，交给后续动作返回主界面')


def handle_abundance(
    client, action: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
) -> BattleSummary:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    _click_confirmed(client, images, action['challenge_button'], float(action['timeout_seconds']),
                     float(action.get('pre_click_wait_seconds', 1)))
    LOGGER.info('已点击挑战，等待 %.2f 秒加载战斗', float(action['load_wait_seconds']))
    images.sleep(float(action['load_wait_seconds']))
    summary = run_battle(client, action['battle'], template_matcher=template_matcher,
                         asset_root=asset_root, sleep=sleep)
    finish_abundance_result(client, action, template_matcher=template_matcher, asset_root=asset_root, sleep=sleep)
    return summary
