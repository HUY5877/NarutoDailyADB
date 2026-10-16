"""一乐外卖：先识别，有可领取项才点击三个盒子，再循环检查。"""
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
    can_claim = data.get('can_claim')
    if type(can_claim) is not bool:
        raise VisualError('外卖领取状态不明确')
    return can_claim


def handle_ichiraku_takeout(client, action, *, model=None, template_matcher=find_template_in_png,
                            asset_root=Path('.'), sleep=time.sleep):
    flow = ModelWorkflow(client, action, model=model, template_matcher=template_matcher,
                         asset_root=asset_root, sleep=sleep)
    round_number = 0
    while True:
        can_claim = flow.read('一乐外卖领取结果', TAKEOUT_PROMPT, _takeout)
        while can_claim is None:
            LOGGER.info('已确认一乐外卖奖励弹层，关闭后重新截图检查')
            flow.tap((1550, 740))
            can_claim = flow.read('一乐外卖领取结果', TAKEOUT_PROMPT, _takeout)
        if not can_claim:
            LOGGER.info('一乐外卖已无可领取盒子，结束领取')
            return
        round_number += 1
        LOGGER.info('[一乐外卖 第%d轮] 确认仍可领取，从左到右各点击一次后重新检查', round_number)
        for point in TAKEOUT_POINTS:
            flow.tap(point)
