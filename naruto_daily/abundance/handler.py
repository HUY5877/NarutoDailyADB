"""丰饶之间的挑战入口和结算处理，战斗操作复用通用模块。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from naruto_daily.battle import BattleSummary, run_battle_without_first_skill
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.execution import (
    PhaseDeadlineExceeded, check_execution, execution_phase_scope, retry_observation, stage_timeout,
)
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.prompt_jev import ABUNDANCE_AFTER_BATTLE


LOGGER = logging.getLogger('naruto_daily')
# 固定 1920×1080 画面上，已实机确认可收起超影服务弹窗的左侧空白处。
SERVICE_POPUP_DISMISS = (400, 540)
# 内置结束标记，让既有丰饶配置也能在战斗检查中识别已完成页。
COMPLETED_TARGET = {
    'name': '丰饶之间今日已完成挑战',
    'template': 'assets/abundance_completed.png',
    'left': 700, 'top': 730, 'right': 1220, 'bottom': 870,
    'threshold': 0.9,
}
ABUNDANCE_BATTLE_SECONDS = 180


def _click_confirmed(client, images: ImageRecognition, target: dict, timeout: float, pre_wait: float,
                     click_point: tuple[int, int] | None = None, *, stop_target: dict | None = None):
    deadline = images.elapsed + stage_timeout(timeout)

    def find_target():
        if stop_target is None:
            return False, *images.find(target, deadline=deadline)
        # 奖励可能在等待或点击前复查期间自动消失；同一帧优先判断完成页。
        index, matches = images.either([stop_target, target], deadline=deadline)
        if index == 0:
            LOGGER.info('已通过本地模板确认%s，相似度 %.3f，跳过%s',
                        stop_target['name'], matches[0][1], target['name'])
            return True, *matches[0]
        return False, *matches[-1]

    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[丰饶之间：%s 第%d轮] 查找按钮→等待后复查→点击后结束；未确认则继续等待，超时停止；剩余等待 %.2f 秒',
                    target['name'], round_number, max(0, deadline - images.elapsed))
        stopped, center, score = find_target()
        if stopped:
            return False
        if center is not None:
            if images.elapsed + pre_wait > deadline:
                LOGGER.info('剩余时间不足以复查%s，结束本阶段循环', target['name'])
                break
            images.sleep(pre_wait)
            stopped, center, score = find_target()
            if stopped:
                return False
            if center is not None:
                point = click_point or center
                LOGGER.info('确认%s，相似度 %.3f，点击坐标：(%d, %d)', target['name'], score, *point)
                client.tap(*point)
                return True
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
    started = clock()

    def remaining():
        return stage_timeout(timeout - max(clock() - started, images.elapsed))

    def wait_completed():
        images.wait_for(COMPLETED_TARGET, timeout_seconds=remaining(), poll_seconds=1)
        LOGGER.info('已通过本地模板确认丰饶之间今日已完成挑战，交给后续动作返回主界面')

    # 战斗检查命中奖励后，画面也可能已经自动回到完成页。
    center, score = images.match(client.screenshot(), COMPLETED_TARGET)
    if center is not None:
        LOGGER.info('已通过本地模板确认丰饶之间今日已完成挑战，相似度 %.3f，跳过结算关闭', score)
        return
    if model is not None:
        def observe():
            png = client.screenshot()
            if images.match(png, COMPLETED_TARGET)[0] is not None:
                return None, {'page': 'abundance'}
            center, _ = images.match(png, action['result_return'])
            if center is not None:
                return center, None
            budget = remaining()
            if budget <= 0:
                raise VisualError('丰饶战后页面识别超时，未点击')
            result = model.complete_json(png, ABUNDANCE_AFTER_BATTLE, timeout_seconds=budget)
            if remaining() <= 0:
                raise VisualError('丰饶战后页面识别超时，忽略迟到结果，未点击')
            # Validate inside the read-only observation so an unknown page can be
            # recaptured without replaying the battle or a result-dismiss click.
            page = result.get('page') if isinstance(result, dict) else None
            if not isinstance(page, str) or page not in {'result', 'service_popup', 'abundance', 'home'}:
                raise VisualError('无法确认丰饶战后页面，未点击或继续关闭')
            return None, result

        center, result = retry_observation(observe, name='丰饶战后页面')
        if center is None:
            # 只在结算文字缺失时识别当前页面，不能把通用 finished 当成结算页。
            page = result['page']
            LOGGER.info('[丰饶战后页面] 模型识别为 %s', page)
            if page in {'result', 'service_popup'}:
                client.tap(*SERVICE_POPUP_DISMISS)
                LOGGER.info('已确认战后页面（%s），点击任意位置 (%d, %d) 一次，随后本地检查今日已完成挑战',
                            page, *SERVICE_POPUP_DISMISS)
                images.sleep(float(action.get('post_click_wait_seconds', 1)))
                wait_completed()
                return
            if page == 'abundance':
                wait_completed()
                return
            if page == 'home':
                LOGGER.info('已离开丰饶结算页，直接交给后续回主界面检查')
                return
    clicked = _click_confirmed(client, images, action['result_return'], remaining(),
                               float(action.get('pre_click_wait_seconds', 1)),
                               click_point=SERVICE_POPUP_DISMISS, stop_target=COMPLETED_TARGET)
    if not clicked:
        return
    images.sleep(float(action.get('post_click_wait_seconds', 1)))
    LOGGER.info('已关闭丰饶之间奖励页，重新截图检查今日已完成挑战')
    wait_completed()


def handle_abundance(
    client, action: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
) -> BattleSummary | None:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    _click_confirmed(client, images, action['challenge_button'], float(action['timeout_seconds']),
                     float(action.get('pre_click_wait_seconds', 1)))
    LOGGER.info('已点击挑战，等待 %.2f 秒加载战斗', float(action['load_wait_seconds']))
    images.sleep(float(action['load_wait_seconds']))
    model = (MultimodalClient(action["multimodal"], asset_root=asset_root)
             if action.get("multimodal") else None)
    battle = dict(action['battle'], end_targets=[COMPLETED_TARGET, *action['battle']['end_targets']])
    try:
        with execution_phase_scope(ABUNDANCE_BATTLE_SECONDS, sleep=sleep):
            summary = run_battle_without_first_skill(client, battle, template_matcher=template_matcher,
                                                     asset_root=asset_root, sleep=sleep, model=model)
    except PhaseDeadlineExceeded:
        # All battle inputs have stopped before returning to the daily budget.
        check_execution()
        LOGGER.warning('丰饶之间战斗已达到 3 分钟，停止战斗并跳过结算；执行后续回主界面动作，再继续其他任务')
        return None
    if summary.end_target == COMPLETED_TARGET['name']:
        LOGGER.info('已通过本地模板确认丰饶之间今日已完成挑战，跳过结算关闭，交给后续动作返回主界面')
        return summary
    finish_abundance_result(client, action, template_matcher=template_matcher,
                            asset_root=asset_root, sleep=sleep, model=model)
    return summary
