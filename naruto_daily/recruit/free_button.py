"""多模态读取免费招募次数及按钮坐标，点击前重新确认同一帧红点。"""
from __future__ import annotations

import logging
import re
import time

from naruto_daily.multimodal.client import MultimodalError
from naruto_daily.multimodal.prompts import FREE_RECRUIT
from naruto_daily.common.execution import current_execution, stage_timeout


LOGGER = logging.getLogger('naruto_daily')


def parse_free_recruit(result: dict, target: dict, category: str):
    required = {'category', 'free_count', 'button_text'}
    if not isinstance(result, dict) or not required.issubset(result):
        raise MultimodalError('模型返回的免费招募结果缺少字段')
    count, text = result['free_count'], result['button_text']
    if type(count) is not int or count < 0:
        raise MultimodalError('模型返回的免费抽数必须是非负整数')
    if count == 0 or result['category'] != category:
        return None
    if not isinstance(text, str):
        raise MultimodalError('模型返回的免费按钮文字格式不正确')
    text = ''.join(text.split())
    label = re.fullmatch(r'免费([0-9]+)抽', text)
    if not ((text == '免费' and count == 1) or (label and int(label[1]) == count)):
        return None
    center = result.get('center')
    if not isinstance(center, list) or len(center) != 2:
        raise MultimodalError('模型返回的免费招募坐标必须是 [x, y]')
    x, y = center
    if type(x) is not int or type(y) is not int:
        raise MultimodalError("模型返回的免费招募坐标必须是整数")
    if not (target['left'] <= x < target['right'] and target['top'] <= y < target['bottom']):
        raise MultimodalError("模型返回的免费招募坐标超出按钮区域")
    return count, (x, y)


def click_free_recruit(client, target: dict, *, category: str, guard: dict, images, model,
                       timeout_seconds: float, poll_seconds: float, pre_click_wait: float,
                       post_click_wait: float, max_calls: int, clock=time.monotonic) -> bool:
    deadline = images.elapsed + stage_timeout(timeout_seconds)
    calls = 0
    no_red_dot = object()

    def probe(png):
        nonlocal calls
        center, _ = images.match(png, guard)
        if center is None:
            return no_red_dot
        if images.elapsed >= deadline:
            raise MultimodalError(f'{category}免费按钮识别超时')
        if calls >= max_calls:
            raise MultimodalError(f'{category}免费按钮识别达到模型调用上限')
        started = clock()
        try:
            result = model.complete_json(png, FREE_RECRUIT, timeout_seconds=stage_timeout(deadline - images.elapsed))
            calls += 1
        finally:
            if current_execution() is None:
                images.elapsed += max(0, clock() - started)
        if images.elapsed >= deadline:
            raise MultimodalError(f'{category}免费按钮识别超时，忽略迟到的模型结果')
        button = parse_free_recruit(result, target, category)
        if button:
            LOGGER.info('模型确认%s：免费 %d 抽，模型按钮坐标 (%d, %d)', category, button[0], *button[1])
        else:
            LOGGER.info('模型未确认%s当前可用的免费按钮（检查选中标签、按钮文字和抽数）', category)
        return button

    def check():
        return images.check(probe, lambda value: value is not None,
                            f'{category}免费招募按钮（多模态）', deadline=deadline)

    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[%s免费按钮 第%d轮] 模型读取免费抽数和坐标→等待后重新截图复查→点击最新模型坐标；'
                    '未确认则补查或轮询，红点消失则取消；剩余 %.2f 秒，调用 %d/%d 次',
                    category, round_number, max(0, deadline - images.elapsed), calls, max_calls)
        first = check()
        if first is no_red_dot:
            LOGGER.info('%s红点未确认，取消免费招募', category)
            return False
        if first is not None:
            if images.elapsed + pre_click_wait >= deadline:
                break
            images.sleep(pre_click_wait)
            confirmed = check()
            if confirmed is no_red_dot:
                LOGGER.info('点击免费招募按钮前%s红点未确认，取消点击', category)
                return False
            if confirmed is not None and confirmed[0] == first[0]:
                LOGGER.info('复查确认%s免费 %d 抽，点击最新模型坐标 (%d, %d)',
                            category, confirmed[0], *confirmed[1])
                client.tap(*confirmed[1])
                images.sleep(post_click_wait)
                return True
            LOGGER.info('本轮复查未确认相同免费抽数，不点击；下一轮重新识别')
        if images.elapsed < deadline:
            delay = min(poll_seconds, deadline - images.elapsed)
            LOGGER.info('本轮未完成%s免费招募，等待 %.2f 秒后继续模型识别', category, delay)
            images.sleep(delay)
    raise MultimodalError(f'{category}免费按钮识别超时')
