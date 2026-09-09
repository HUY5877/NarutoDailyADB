"""出战加载期间处理饰品掉落次数用尽的继续挑战提示。"""
from __future__ import annotations

import logging
import time

from naruto_daily.common.visual import VisualError


LOGGER = logging.getLogger('naruto_daily')
WARNING = dict(name='继续挑战无法获得饰品提示', template='assets/secret_deploy_warning_text.png',
               left=540, top=390, right=1380, bottom=570, threshold=.85)
CONFIRM = dict(name='继续挑战确定按钮', template='assets/secret_deploy_warning_confirm.png',
               left=780, top=620, right=1140, bottom=800, threshold=.85)


def wait_after_deploy(images, action, *, confirmed_tickets, clock=time.monotonic):
    """只沿用本次出战前确认的正数券数；确认弹窗一次后重新等待加载。"""
    if type(confirmed_tickets) is not int or confirmed_tickets <= 0:
        raise VisualError('本次出战未确认挑战券大于0，不能确认继续挑战')
    started, waited = clock(), images.elapsed
    load_wait = float(action['load_wait_seconds'])
    clicked = False

    def remaining():
        return load_wait - max(clock()-started, images.elapsed-waited)

    def inspect():
        png = images.client.screenshot()
        if images.match(png, WARNING)[0] is None:
            return False, None
        return True, images.match(png, CONFIRM)[0]

    while True:
        visible, center = inspect()
        if center is not None and not clicked:
            images.sleep(action['pre_click_wait_seconds'])
            visible, center = inspect()
            if center is not None:
                images.client.tap(*center)
                clicked = True
                LOGGER.info('本次出战前已确认挑战券 %d 张；已点击继续挑战的确定 (%d, %d)，重新等待 %g 秒加载',
                            confirmed_tickets, *center, load_wait)
                started, waited = clock(), images.elapsed
                continue
        left = remaining()
        if left <= 0:
            if visible:
                raise VisualError('继续挑战提示确认超时，未重复点击或开始攻击')
            return clicked
        images.sleep(min(float(action['poll_seconds']), left))
