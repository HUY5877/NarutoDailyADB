"""公共战斗节奏：并发连点 30 秒，停止点击后暂停至少 5 秒并检查结算。"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.prompts import BATTLE_COMPLETION


LOGGER = logging.getLogger('naruto_daily')
# 丰饶和秘境共用一组节奏参数：（连续点击秒数，含结算检查的暂停秒数）。
BATTLE_CYCLE_SECONDS = (30.0, 5.0)


@dataclass(frozen=True)
class BattleSummary:
    taps: int
    end_target: str


def run_battle(
    client, battle: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    model: MultimodalClient | None = None,
) -> BattleSummary:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    # 调用方已经完成加载等待；一批四个输入完成后再开始下一批。
    tap_seconds, pause_seconds = BATTLE_CYCLE_SECONDS
    LOGGER.info('开始战斗模块，四个战斗按钮并发连点 %g 秒后停止点击，暂停至少 %g 秒并检查一次结算',
                tap_seconds, pause_seconds)
    started, waited = clock(), images.elapsed
    timeout = float(battle['timeout_seconds'])
    interval = float(battle['tap_interval_seconds'])
    buttons = battle['buttons']
    taps = 0

    def elapsed():
        # ADB、截图、模型请求和暂停均计入总时限；注入的 sleep 同样有效。
        return max(clock() - started, images.elapsed - waited)

    def remaining():
        return timeout - elapsed()

    def tap_button(button):
        if remaining() <= 0:
            return 0
        LOGGER.debug('战斗点击：%s，坐标：(%d, %d)', button['name'], button['x'], button['y'])
        client.tap(button['x'], button['y'])
        return 1

    targets = battle['end_targets']
    model_calls = 0
    round_number = 0
    next_check = 0.0
    with ThreadPoolExecutor(max_workers=len(buttons), thread_name_prefix='battle-tap') as pool:
        while remaining() > 0:
            if elapsed() >= next_check:
                # 前一批四个输入均已返回，先停止全部点击，再截图/调用模型。
                paused_at = elapsed()
                initial_check = round_number == 0
                round_number += 1
                LOGGER.info('[战斗检查 第%d轮] %s；检查结算，本阶段上限 %g 秒',
                            round_number, '进入战斗前检查' if initial_check else '已停止全部连点，开始暂停', timeout)
                screenshot = client.screenshot()
                if remaining() <= 0:
                    break
                for target in targets:
                    center, _ = images.match(screenshot, target)
                    if remaining() <= 0:
                        break
                    if center is not None:
                        name = target['name']
                        LOGGER.info('发现战斗结束标记：%s，停止战斗点击，共点击 %d 次', name, taps)
                        return BattleSummary(taps, name)
                if remaining() <= 0:
                    break
                if model is not None:
                    # 正常的“仍在战斗”由总时限约束，不累计消耗其他阶段的识别次数上限。
                    model_calls += 1
                    result = model.complete_json(screenshot, BATTLE_COMPLETION,
                                                 timeout_seconds=remaining())
                    if remaining() <= 0:
                        break
                    finished = result.get('finished')
                    page = result.get('page')
                    reason = result.get('reason')
                    if type(finished) is not bool or page not in {'battle', 'finished', 'unknown'} or not isinstance(reason, str):
                        raise VisualError('战斗完成识别返回格式无效，已停止点击')
                    LOGGER.info('[战斗完成模型 第%d次] page=%s finished=%s reason=%s',
                                model_calls, page, finished, reason)
                    if finished or page != 'battle':
                        return BattleSummary(taps, 'multimodal')
                if not initial_check:
                    # 检查与暂停重叠；检查不足 5 秒才补足，超过则不额外等待。
                    delay = min(pause_seconds - (elapsed() - paused_at), remaining())
                    if delay > 0:
                        LOGGER.info('战斗尚未结束，保持停止点击，再暂停 %.2f 秒', delay)
                        images.sleep(delay)
                next_check = elapsed() + tap_seconds
                if remaining() > 0:
                    LOGGER.info('开始下一段 %g 秒战斗连点', tap_seconds)
            if remaining() <= 0:
                break
            # 等待本批全部输入返回，避免后台点击越过结算处理，也不积压后续批次。
            pending = [pool.submit(tap_button, button) for button in buttons]
            taps += sum(future.result() for future in pending)
            delay = max(0.0, min(interval, remaining(), next_check - elapsed()))
            if delay > 0:
                images.sleep(delay)
    raise VisualError(f'战斗等待结束超时（{timeout:g} 秒），已停止点击')
