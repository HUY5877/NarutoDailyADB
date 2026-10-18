"""公共战斗节奏：按调用方周期连点，仅在结算检查期间停止点击。"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.execution import current_execution, retry_observation, stage_timeout
from naruto_daily.common.keyboard import DEFAULT_BATTLE_KEYS, LDPlayerKeyboard
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.multimodal.prompt_jev import BATTLE_COMPLETION


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
    end_detector: Callable[[bytes], str | None] | None = None,
    before_attack: Callable[[bytes], BattleSummary | None] | None = None,
) -> BattleSummary:
    if (isinstance(check_interval_seconds, bool) or not isinstance(check_interval_seconds, (int, float))
            or not math.isfinite(check_interval_seconds) or check_interval_seconds <= 0):
        raise ValueError('战斗检查间隔必须是正数')
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    # 调用方已经完成加载等待；一批全部输入完成后再开始下一批。
    tap_seconds = float(check_interval_seconds)
    buttons = battle['buttons']
    keyboard_mode = battle.get('input_mode', 'adb') == 'keyboard'
    keyboard = None
    LOGGER.info('进入战斗模块，先检查当前页面；确认后使用 %d 个战斗按钮（%s），以%s运行 %g 秒，再停止输入检查结算',
                len(buttons), '、'.join(button['name'] for button in buttons),
                '雷电键盘逐键短按' if keyboard_mode else 'ADB 并发连点', tap_seconds)
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
        if keyboard_mode:
            key = button.get('key', DEFAULT_BATTLE_KEYS[button['name']])
            LOGGER.debug('战斗按键：%s，按键：%s', button['name'], key)
            keyboard.press(key)
        else:
            LOGGER.debug('战斗点击：%s，坐标：(%d, %d)', button['name'], button['x'], button['y'])
            client.tap(button['x'], button['y'])
        return 1

    targets = battle['end_targets']
    model_calls = 0
    round_number = 0
    next_check = 0.0
    entry_checked = False

    def observe_completion():
        nonlocal model_calls, entry_checked
        screenshot = client.screenshot()
        if remaining() <= 0:
            return None, None
        if end_detector is not None:
            name = end_detector(screenshot)
            if remaining() <= 0:
                return None, None
            if name is not None:
                LOGGER.info('发现战斗结束页面：%s，停止战斗点击，共点击 %d 次', name, taps)
                return BattleSummary(taps, name), None
        for target in targets:
            center, score = images.match(screenshot, target)
            if remaining() <= 0:
                return None, None
            LOGGER.info('[战斗检查 第%d轮] %s：%s，相似度 %.3f，阈值 %.3f',
                        round_number, target['name'], '已匹配' if center is not None else '未匹配',
                        score, float(target['threshold']))
            if center is not None:
                name = target['name']
                LOGGER.info('发现战斗结束标记：%s，停止战斗点击，共点击 %d 次', name, taps)
                return BattleSummary(taps, name), None
        if before_attack is not None and not entry_checked:
            summary = before_attack(screenshot)
            if remaining() <= 0:
                return None, None
            if summary is not None:
                return summary, None
            entry_checked = True
            # Entry checks may take time or navigate; recheck the new page
            # before using a completion model or starting any attack inputs.
            return observe_completion()
        if model is None:
            return None, None
        # Only observations run here. A retry must check local result markers on
        # the new frame before deciding whether another model request is needed.
        model_calls += 1
        with model_request_context('战斗完成检查'):
            result = model.complete_json(screenshot, BATTLE_COMPLETION,
                                         timeout_seconds=remaining(),
                                         refresh_image=client.screenshot)
        if remaining() <= 0:
            return None, None
        if not isinstance(result, dict):
            raise VisualError('战斗完成识别返回格式无效，已停止点击')
        finished, page, reason = result.get('finished'), result.get('page'), result.get('reason')
        if type(finished) is not bool or page not in {'battle', 'finished', 'unknown'} or not isinstance(reason, str):
            raise VisualError('战斗完成识别返回格式无效，已停止点击')
        return None, result

    # 键盘逐键短按并松开；ADB 保持原来的并发点击。
    pool_context = nullcontext() if keyboard_mode else ThreadPoolExecutor(
        max_workers=len(buttons), thread_name_prefix='battle-tap')
    with pool_context as pool:
        while remaining() > 0:
            if elapsed() >= next_check:
                # 前一批全部输入均已返回，先停止全部点击，再截图/调用模型。
                initial_check = round_number == 0
                round_number += 1
                LOGGER.info('[战斗检查 第%d轮] %s；检查结算，%s %g 秒',
                            round_number, '首轮检查，尚未开始连点' if initial_check else '已停止全部连点',
                            '任务总时限剩余' if execution is not None else '本阶段上限', remaining())
                summary, result = retry_observation(observe_completion, name='战斗完成检查')
                if remaining() <= 0:
                    break
                if summary is not None:
                    return summary
                if result is not None:
                    finished = result.get('finished')
                    page = result.get('page')
                    reason = result.get('reason')
                    LOGGER.info('[战斗完成模型 第%d次] page=%s finished=%s reason=%s',
                                model_calls, page, finished, reason)
                    if finished and page == 'finished':
                        return BattleSummary(taps, 'multimodal')
                    if finished or page != 'battle':
                        # 未知或字段矛盾时保持停止点击，重新截图检查本地标记和模型。
                        LOGGER.info('战斗状态尚未确定，保持停止点击，1 秒后重新截图识别')
                        images.sleep(min(1.0, remaining()))
                        continue
                next_check = elapsed() + tap_seconds
                if remaining() > 0:
                    LOGGER.info('开始下一段 %g 秒战斗连点', tap_seconds)
            if remaining() <= 0:
                break
            if keyboard_mode and keyboard is None:
                # 只有首轮识别允许继续战斗，才定位窗口；结果页不激活窗口。
                keyboard = LDPlayerKeyboard.from_client(client, battle.get('keyboard', {}))
            # 等待本批全部输入返回，避免后台点击越过结算处理，也不积压后续批次。
            if keyboard_mode:
                # 失败后立即退出，不能把尚未发送的键积压到后台继续执行。
                taps += sum(tap_button(button) for button in buttons)
            else:
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
