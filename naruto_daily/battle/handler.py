"""并发点击战斗按钮，每隔 20 秒检查结算的通用战斗模块。"""
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
RESULT_CHECK_INTERVAL_SECONDS = 20.0


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
    LOGGER.info('开始战斗模块，四个战斗按钮并发连点，每 %g 秒检查一次结算', RESULT_CHECK_INTERVAL_SECONDS)
    started, waited = clock(), images.elapsed
    timeout = float(battle['timeout_seconds'])
    interval = float(battle['tap_interval_seconds'])
    buttons = battle['buttons']
    taps = 0

    def elapsed():
        # 使用实际经过时间，ADB 耗时也计入20秒周期；注入的 sleep 同样有效。
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
    model_max_calls = int(getattr(model, "config", {}).get("max_calls", 0)) if model is not None else 0
    round_number = 0
    next_check = 0.0
    with ThreadPoolExecutor(max_workers=len(buttons), thread_name_prefix='battle-tap') as pool:
        while remaining() > 0:
            if elapsed() >= next_check:
                round_number += 1
                LOGGER.info('[战斗检查 第%d轮] 检查结算标记；未结束则四键并发连点，%g 秒后再检查；本阶段上限 %g 秒',
                            round_number, RESULT_CHECK_INTERVAL_SECONDS, timeout)
                screenshot = client.screenshot()
                for target in targets:
                    center, _ = images.match(screenshot, target)
                    if center is not None:
                        name = target['name']
                        LOGGER.info('发现战斗结束标记：%s，停止战斗点击，共点击 %d 次', name, taps)
                        return BattleSummary(taps, name)
                if model is not None:
                    if model_calls >= model_max_calls:
                        raise VisualError('战斗完成识别达到模型调用上限，已停止点击')
                    model_calls += 1
                    result = model.complete_json(screenshot, BATTLE_COMPLETION,
                                                 timeout_seconds=max(0.01, remaining()))
                    finished = result.get('finished')
                    page = result.get('page')
                    reason = result.get('reason')
                    if type(finished) is not bool or page not in {'battle', 'finished', 'unknown'} or not isinstance(reason, str):
                        raise VisualError('战斗完成识别返回格式无效，已停止点击')
                    LOGGER.info('[战斗完成模型 第%d/%d] page=%s finished=%s reason=%s',
                                model_calls, model_max_calls, page, finished, reason)
                    if finished or page != 'battle':
                        return BattleSummary(taps, 'multimodal')
                next_check = elapsed() + RESULT_CHECK_INTERVAL_SECONDS
            if remaining() <= 0:
                break
            # 等待本批全部输入返回，避免后台点击越过结算处理，也不积压后续批次。
            pending = [pool.submit(tap_button, button) for button in buttons]
            taps += sum(future.result() for future in pending)
            delay = max(0.0, min(interval, remaining(), next_check - elapsed()))
            if delay > 0:
                images.sleep(delay)
    raise VisualError(f'战斗等待结束超时（{timeout:g} 秒），已停止点击')
