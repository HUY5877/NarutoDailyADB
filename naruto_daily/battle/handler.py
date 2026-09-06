"""按配置循环释放技能，识别到结算界面后停止的通用战斗模块。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png


LOGGER = logging.getLogger('naruto_daily')


@dataclass(frozen=True)
class BattleSummary:
    taps: int
    end_target: str


def run_battle(
    client, battle: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BattleSummary:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    marker = battle['battle_marker']
    try:
        images.wait_for(marker, timeout_seconds=float(battle['start_timeout_seconds']), poll_seconds=1)
    except VisualError as exc:
        raise VisualError(f'等待进入战斗超时或识别失败：{exc}') from exc
    LOGGER.info('已进入战斗，循环一技能、二技能、大招、普攻')
    started, waited = clock(), images.elapsed
    timeout = float(battle['timeout_seconds'])
    interval = float(battle['tap_interval_seconds'])
    buttons = battle['buttons']
    taps = 0

    def remaining():
        # ADB 截图和点击耗时也计入战斗上限；测试中注入的 sleep 同样有效。
        return timeout - max(clock() - started, images.elapsed - waited)

    # 每个按钮点击前判断整帧，结算标记优先于战斗标记。
    targets = battle['end_targets'] + [marker]
    round_number = 0
    while remaining() > 0:
        round_number += 1
        LOGGER.info('[战斗检查 第%d轮] 先找结算标记，找到即停止；否则确认战斗画面后点击%s；画面未确认则暂停，本阶段上限 %g 秒',
                    round_number, buttons[taps % len(buttons)]['name'], timeout)
        index, _ = images.either(targets, deadline=images.elapsed + remaining())
        if index is not None and index < len(battle['end_targets']):
            name = targets[index]['name']
            LOGGER.info('发现战斗结束标记：%s，停止战斗点击，共点击 %d 次', name, taps)
            return BattleSummary(taps, name)
        if remaining() <= 0:
            break
        if index is None:
            LOGGER.info('当前未确认战斗或结算界面，暂停点击，1 秒后再检查')
            delay = max(0.0, min(1, remaining()))
            if delay > 0:
                images.sleep(delay)
            continue
        button = buttons[taps % len(buttons)]
        LOGGER.info('战斗点击：%s，坐标：(%d, %d)', button['name'], button['x'], button['y'])
        client.tap(button['x'], button['y'])
        taps += 1
        delay = max(0.0, min(interval, remaining()))
        if delay > 0:
            images.sleep(delay)
    raise VisualError(f'战斗等待结束超时（{timeout:g} 秒），已停止点击')
