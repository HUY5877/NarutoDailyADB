"""一乐外卖：每轮依次点击三个盒子，识别仍有可领取项则继续。"""
import logging
import time
from pathlib import Path

from .common.model_workflow import ModelWorkflow
from .common.visual import VisualError, find_template_in_png
from .multimodal.prompt_jev import TAKEOUT_PROMPT

LOGGER = logging.getLogger('naruto_daily')
TAKEOUT_POINTS = ((450, 520), (880, 520), (1310, 520))


def _takeout(data):
    if not isinstance(data, dict) or data.get('page') not in ('takeout', 'reward'):
        raise VisualError('未确认一乐外卖页面')
    if data['page'] == 'reward':
        return None
    slots = data.get('claimable_slots')
    if (not isinstance(slots, list) or any(type(i) is not int or i not in (1, 2, 3) for i in slots)
            or len(set(slots)) != len(slots)):
        raise VisualError('外卖领取位置不明确')
    return sorted(slots)


def handle_ichiraku_takeout(client, action, *, model=None, template_matcher=find_template_in_png,
                            asset_root=Path('.'), sleep=time.sleep):
    flow = ModelWorkflow(client, action, model=model, template_matcher=template_matcher,
                         asset_root=asset_root, sleep=sleep)
    round_number = 0
    while True:
        round_number += 1
        LOGGER.info('[一乐外卖 第%d轮] 从左到右各点击一次，再截图检查领取结果', round_number)
        for point in TAKEOUT_POINTS:
            flow.tap(point)
        slots = flow.read('一乐外卖领取结果', TAKEOUT_PROMPT, _takeout)
        while slots is None:
            LOGGER.info('已确认一乐外卖奖励弹层，关闭后重新截图检查')
            flow.tap((1550, 740))
            slots = flow.read('一乐外卖领取结果', TAKEOUT_PROMPT, _takeout)
        if not slots:
            LOGGER.info('一乐外卖已无可领取盒子，结束领取')
            return
        LOGGER.info('一乐外卖仍有可领取位置 %s，继续下一轮三次点击', slots)
