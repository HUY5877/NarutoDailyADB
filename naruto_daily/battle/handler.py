"""公共战斗节奏：按调用方周期连点，仅在结算检查期间停止点击。"""
from __future__ import annotations

import logging
import math
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.adb_client import AdbServerConnectionError
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, PhaseDeadlineExceeded, current_execution, current_phase_execution,
    retry_observation, stage_timeout,
)
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.multimodal.prompt_jev import BATTLE_COMPLETION


LOGGER = logging.getLogger('naruto_daily')
# 默认连续点击30秒后检查；秘境调用时缩短为18秒。
BATTLE_CHECK_INTERVAL_SECONDS = 30.0
# 1920×1080 战斗画面的按钮内点击位置；字母仅用于对应用户指定的六个区域。
_DUEL_BUTTON_CENTERS = (
    ('秘卷', 1730, 445),   # E
    ('通灵', 1680, 260),   # R：靠左避开决斗场大厅的决斗季大奖入口
    ('二技能', 1530, 750),  # I
    ('普攻', 1715, 885),   # K
    ('替身', 1300, 965),   # SPACE
    ('大招', 1730, 650),   # L
)


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
    prefer_fast_taps: bool = False,
    _tap_batch: Callable[[Callable[[], bool]], bool] | None = None,
    _recover_adb_server: bool = False,
) -> BattleSummary:
    if (isinstance(check_interval_seconds, bool) or not isinstance(check_interval_seconds, (int, float))
            or not math.isfinite(check_interval_seconds) or check_interval_seconds <= 0):
        raise ValueError('战斗检查间隔必须是正数')
    if prefer_fast_taps and _tap_batch is None:
        return _run_fast_battle(
            client, battle, template_matcher=template_matcher, asset_root=asset_root,
            sleep=sleep, clock=clock, model=model, check_interval_seconds=check_interval_seconds,
            end_detector=end_detector, before_attack=before_attack,
        )
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    # 调用方已经完成加载等待；一批全部输入完成后再开始下一批。
    tap_seconds = float(check_interval_seconds)
    buttons = battle['buttons']
    LOGGER.info('进入战斗模块，先检查当前页面；确认后使用 %d 个战斗按钮（%s）并发连点 %g 秒，再停止点击检查结算',
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
        tap = getattr(client, 'battle_tap', client.tap) if _recover_adb_server else client.tap
        tap(button['x'], button['y'])
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

    with ThreadPoolExecutor(max_workers=len(buttons), thread_name_prefix='battle-tap') as pool:
        while remaining() > 0:
            if elapsed() >= next_check:
                # 前一批全部输入均已返回，先停止全部点击，再截图/调用模型。
                initial_check = round_number == 0
                round_number += 1
                LOGGER.info('[战斗检查 第%d轮] %s；检查结算，%s %g 秒',
                            round_number, '首轮检查，尚未开始连点' if initial_check else '已停止全部连点',
                            '任务总时限剩余' if execution is not None else '本阶段上限', remaining(),
                            extra={'execution_checkpoint': True})
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
            # 等待本批全部输入返回，避免后台点击越过结算处理，也不积压后续批次。
            batch_started = elapsed()
            if _tap_batch is None:
                pending = [pool.submit(copy_context().run, tap_button, button) for button in buttons]
                if current_phase_execution() is None and not _recover_adb_server:
                    taps += sum(future.result() for future in pending)
                else:
                    errors = []
                    for future in pending:
                        try:
                            taps += future.result()
                        except Exception as error:
                            errors.append(error)
                    if errors:
                        if current_phase_execution() is None and not any(
                                isinstance(error, AdbServerConnectionError) for error in errors):
                            raise errors[0]
                        # Drain every input first. A recoverable phase timeout must
                        # not conceal a daily timeout or a concurrent device error.
                        daily_error = next((error for error in errors
                                            if isinstance(error, DailyDeadlineExceeded)
                                            and not isinstance(error, PhaseDeadlineExceeded)), None)
                        if daily_error is not None:
                            raise daily_error
                        ordinary_error = next((error for error in errors
                                               if not isinstance(error, (PhaseDeadlineExceeded,
                                                                         AdbServerConnectionError))), None)
                        if ordinary_error is not None:
                            raise ordinary_error
                        phase_error = next((error for error in errors
                                            if isinstance(error, PhaseDeadlineExceeded)), None)
                        if phase_error is not None:
                            raise phase_error
                        ensure_server = getattr(client, 'ensure_battle_adb_server', None)
                        if not _recover_adb_server or not callable(ensure_server):
                            raise errors[0]
                        if remaining() <= 0:
                            break
                        ensure_server()
                        if remaining() <= 0:
                            break
            else:
                # 高速通道整轮按下、抬起并确认后返回，不留后台触摸越过截图检查。
                if _tap_batch(lambda: remaining() > 0 and elapsed() < next_check):
                    taps += len(buttons)
            batch_delay = interval if _tap_batch is None else interval - (elapsed() - batch_started)
            delay = max(0.0, min(batch_delay, remaining(), next_check - elapsed()))
            if delay > 0:
                images.sleep(delay)
    raise VisualError(f'战斗等待结束超时（{timeout:g} 秒），已停止点击')


def _run_fast_battle(client, battle: dict, **options) -> BattleSummary:
    """整场战斗共用通道；暂停观察时保持连接，返回或异常退出时关闭。"""
    points = tuple((button['x'], button['y']) for button in battle['buttons'])
    factory = getattr(client, 'battle_taps', None)
    if not callable(factory):
        # 兼容只实现了旧决斗场通道接口的客户端。
        factory = getattr(client, 'duel_taps', None)
    channel = factory(points) if callable(factory) else nullcontext(None)
    with channel as tap_batch:
        prepared = dict(battle)
        if tap_batch is not None:
            from naruto_daily.common.fast_duel_tap import TAP_PERIOD_SECONDS
            prepared['tap_interval_seconds'] = max(float(battle['tap_interval_seconds']), TAP_PERIOD_SECONDS)
            LOGGER.info('高速战斗目标点击频率：每个按钮每秒 %g 次',
                        1 / prepared['tap_interval_seconds'], extra={'backend_only': True})
            options['_tap_batch'] = tap_batch
        return run_battle(client, prepared, **options)


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
                      check_interval_seconds=check_interval_seconds, _recover_adb_server=True)


def run_duel_battle(
    client, battle: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    model: MultimodalClient | None = None,
    check_interval_seconds: float = BATTLE_CHECK_INTERVAL_SECONDS,
    end_detector: Callable[[bytes], str | None] | None = None,
    before_attack: Callable[[bytes], BattleSummary | None] | None = None,
) -> BattleSummary:
    """决斗场专用：循环并发点击六个按钮内的位置，不包含一技能。"""
    if client.screen_size() != (1920, 1080):
        raise VisualError('决斗场战斗只支持 1920×1080 安卓画面，已停止点击')
    duel_battle = dict(battle, buttons=[
        dict(name=name, x=x, y=y) for name, x, y in _DUEL_BUTTON_CENTERS
    ])
    return _run_fast_battle(
        client, duel_battle, template_matcher=template_matcher, asset_root=asset_root,
        sleep=sleep, clock=clock, model=model, check_interval_seconds=check_interval_seconds,
        end_detector=end_detector, before_attack=before_attack,
    )
