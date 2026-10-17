"""一乐外卖：每轮点击两种布局的六个位置，识别仍可领取则继续。"""
import logging
import time
from pathlib import Path

from .common.model_workflow import ModelWorkflow
from .common.visual import VisualError, find_template_in_png
from .multimodal.prompt_jev import TAKEOUT_PROMPT

LOGGER = logging.getLogger('naruto_daily')
# 带“体力追回”一栏与不带该栏时，三个免费外卖位置不同；按横坐标合并点击。
TAKEOUT_POINTS = ((450, 520), (550, 520), (880, 520),
                  (1100, 520), (1310, 520), (1650, 520))

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
        round_number += 1
        LOGGER.info('[一乐外卖 第%d轮] 从左到右点击两种布局的6个位置，再截图检查领取结果', round_number)
        for point in TAKEOUT_POINTS:
            flow.tap(point)
        can_claim = flow.read('一乐外卖领取结果', TAKEOUT_PROMPT, _takeout)
        while can_claim is None:
            LOGGER.info('已确认一乐外卖奖励弹层，关闭后重新截图检查')
            flow.tap((1550, 740))
            can_claim = flow.read('一乐外卖领取结果', TAKEOUT_PROMPT, _takeout)
        if not can_claim:
            LOGGER.info('一乐外卖已无可领取盒子，结束领取')
            return
        LOGGER.info('一乐外卖仍可领取，继续下一轮六次点击')
