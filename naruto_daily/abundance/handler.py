"""丰饶之间的挑战入口和结算处理，战斗操作复用通用模块。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from naruto_daily.battle import BattleSummary, run_battle_without_first_skill
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.execution import retry_observation, stage_timeout
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.prompts import ABUNDANCE_AFTER_BATTLE


LOGGER = logging.getLogger('naruto_daily')
# 固定 1920×1080 画面上，已实机确认可收起超影服务弹窗的左侧空白处。
SERVICE_POPUP_DISMISS = (400, 540)


def _click_confirmed(client, images: ImageRecognition, target: dict, timeout: float, pre_wait: float,
                     click_point: tuple[int, int] | None = None):
    deadline = images.elapsed + stage_timeout(timeout)
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
                point = click_point or center
                LOGGER.info('确认%s，相似度 %.3f，点击坐标：(%d, %d)', target['name'], score, *point)
                client.tap(*point)
                return
        if images.elapsed < deadline:
            delay = min(1, deadline-images.elapsed)
            LOGGER.info('本轮未确认%s，等待 %.2f 秒后开始下一轮', target['name'], delay)
            images.sleep(delay)
    raise VisualError(f"等待并确认{target['name']}超时")


def finish_abundance_result(
    client, action: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
    model: MultimodalClient | None = None, clock: Callable[[], float] = time.monotonic,
) -> None:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    timeout = stage_timeout(float(action['timeout_seconds']))
    if model is not None:
        started = clock()
        def observe():
            png = client.screenshot()
            center, _ = images.match(png, action['result_return'])
            if center is not None:
                return center, None
            remaining = stage_timeout(timeout - (clock() - started))
            if remaining <= 0:
                raise VisualError('丰饶战后页面识别超时，未点击')
            result = model.complete_json(png, ABUNDANCE_AFTER_BATTLE, timeout_seconds=remaining)
            remaining = stage_timeout(timeout - (clock() - started))
            if remaining <= 0:
                raise VisualError('丰饶战后页面识别超时，忽略迟到结果，未点击')
            return None, result

        center, result = retry_observation(observe, name='丰饶战后页面')
        if center is None:
            # 只在结算文字缺失时识别当前页面，不能把通用 finished 当成结算页。
            page = result.get('page') if isinstance(result, dict) else None
            if not isinstance(page, str) or page not in {'result', 'service_popup', 'abundance', 'home'}:
                raise VisualError('无法确认丰饶战后页面，未点击或继续关闭')
            LOGGER.info('[丰饶战后页面] 模型识别为 %s', page)
            if page in {'result', 'service_popup'}:
                client.tap(*SERVICE_POPUP_DISMISS)
                LOGGER.info('已确认战后页面（%s），点击任意位置 (%d, %d) 一次，等待后进入关闭按钮检查',
                            page, *SERVICE_POPUP_DISMISS)
                images.sleep(float(action.get('post_click_wait_seconds', 1)))
                return
            if page in {'abundance', 'home'}:
                LOGGER.info('已离开丰饶结算页，直接交给后续回主界面检查')
                return
        timeout = stage_timeout(timeout - (clock() - started))
    _click_confirmed(client, images, action['result_return'], timeout,
                     float(action.get('pre_click_wait_seconds', 1)),
                     click_point=SERVICE_POPUP_DISMISS)
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
    model = (MultimodalClient(action["multimodal"], asset_root=asset_root)
             if action.get("multimodal") else None)
    summary = run_battle_without_first_skill(client, action['battle'], template_matcher=template_matcher,
                                             asset_root=asset_root, sleep=sleep, model=model)
    finish_abundance_result(client, action, template_matcher=template_matcher,
                            asset_root=asset_root, sleep=sleep, model=model)
    return summary
