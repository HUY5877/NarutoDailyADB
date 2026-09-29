"""公共战斗节奏：按调用方周期连点，仅在结算检查期间停止点击。"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.execution import current_execution, stage_timeout
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.multimodal.prompts import BATTLE_COMPLETION


LOGGER = logging.getLogger('naruto_daily')
# 默认连续点击30秒后检查；秘境调用时缩短为18秒。
BATTLE_CHECK_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class BattleSummary:
    taps: int
    end_target: str


def run_battle(
    client, battle: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    model: MultimodalClient | None = None,
    check_interval_seconds: float = BATTLE_CHECK_INTERVAL_SECONDS,
) -> BattleSummary:
    if (isinstance(check_interval_seconds, bool) or not isinstance(check_interval_seconds, (int, float))
            or not math.isfinite(check_interval_seconds) or check_interval_seconds <= 0):
        raise ValueError('战斗检查间隔必须是正数')
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    # 调用方已经完成加载等待；一批全部输入完成后再开始下一批。
    tap_seconds = float(check_interval_seconds)
    buttons = battle['buttons']
    LOGGER.info('开始战斗模块，%d 个战斗按钮（%s）并发连点 %g 秒后停止点击并检查结算，确认仍在战斗后立即恢复',
                len(buttons), '、'.join(button['name'] for button in buttons), tap_seconds)
    started, waited = clock(), images.elapsed
    execution = current_execution()
    timeout = stage_timeout(float(battle['timeout_seconds']))
    interval = float(battle['tap_interval_seconds'])
    taps = 0

    def elapsed():
        # ADB、截图、模型请求和点击间隔均计入总时限；注入的 sleep 同样有效。
        return max(clock() - started, images.elapsed - waited)

    def remaining():
        if execution is not None:
            return execution.remaining()
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
                # 前一批全部输入均已返回，先停止全部点击，再截图/调用模型。
                initial_check = round_number == 0
                round_number += 1
                LOGGER.info('[战斗检查 第%d轮] %s；检查结算，%s %g 秒',
                            round_number, '进入战斗前检查' if initial_check else '已停止全部连点',
                            '任务总时限剩余' if execution is not None else '本阶段上限', remaining())
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
                    with model_request_context('战斗完成检查'):
                        result = model.complete_json(screenshot, BATTLE_COMPLETION,
                                                     timeout_seconds=remaining(),
                                                     refresh_image=client.screenshot)
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
                next_check = elapsed() + tap_seconds
                if remaining() > 0:
                    LOGGER.info('开始下一段 %g 秒战斗连点', tap_seconds)
            if remaining() <= 0:
                break
            # 等待本批全部输入返回，避免后台点击越过结算处理，也不积压后续批次。
            pending = [pool.submit(copy_context().run, tap_button, button) for button in buttons]
            taps += sum(future.result() for future in pending)
            delay = max(0.0, min(interval, remaining(), next_check - elapsed()))
            if delay > 0:
                images.sleep(delay)
    raise VisualError(f'战斗等待结束超时（{timeout:g} 秒），已停止点击')


def run_battle_without_first_skill(
    client, battle: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    model: MultimodalClient | None = None,
    check_interval_seconds: float = BATTLE_CHECK_INTERVAL_SECONDS,
) -> BattleSummary:
    """丰饶之间使用：跳过一技能，避免退出战斗后误点超影免费扫荡。"""
    filtered_battle = dict(battle, buttons=[
        button for button in battle['buttons'] if button['name'] != '一技能'
    ])
    return run_battle(client, filtered_battle, template_matcher=template_matcher,
                      asset_root=asset_root, sleep=sleep, clock=clock, model=model,
                      check_interval_seconds=check_interval_seconds)
