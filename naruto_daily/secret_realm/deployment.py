"""出战加载期间处理饰品掉落次数用尽的继续挑战提示。"""
from __future__ import annotations

import logging
import time

from naruto_daily.common.visual import VisualError
from naruto_daily.common.execution import check_execution, current_execution


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
    execution = current_execution()
    clicked = False

    def remaining():
        return load_wait - max(clock()-started, images.elapsed-waited)

    def inspect():
        check_execution()
        png = images.client.screenshot()
        visible = images.match(png, WARNING)[0] is not None
        center = images.match(png, CONFIRM)[0] if visible else None
        check_execution()
        return visible, center

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
                if execution is None:
                    raise VisualError('继续挑战提示确认超时，未重复点击或开始攻击')
                # 固定加载等待结束后，提示仍在前景就继续确认，直到共享总时限。
                left = execution.remaining()
            else:
                return clicked
        images.sleep(min(float(action['poll_seconds']), left))
