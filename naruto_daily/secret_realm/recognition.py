"""秘境房间挑战券读取：未知不等于零，不混用过期券数。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from naruto_daily.multimodal.client import MultimodalError
from naruto_daily.multimodal.prompts import SECRET_REALM_TICKETS


LOGGER = logging.getLogger('naruto_daily')


@dataclass(frozen=True)
class RoomState:
    tickets: int
    deploy_center: tuple[int, int]


def parse_tickets(result: dict) -> int | None:
    if not isinstance(result, dict) or not {'page', 'tickets'}.issubset(result):
        raise MultimodalError('秘境挑战券识别结果缺少字段')
    if result['page'] != 'room' or result['tickets'] is None:
        return None
    count = result['tickets']
    if type(count) is not int or count < 0:
        raise MultimodalError('秘境剩余挑战券必须是非负整数或 null')
    return count


def wait_for_tickets(images, model, action, *, clock=time.monotonic):
    deadline = images.elapsed + action['timeout_seconds']
    calls = 0

    def probe(png):
        nonlocal calls
        center, _ = images.match(png, action['deploy_button'])
        if center is None:
            return None
        if images.elapsed >= deadline:
            raise MultimodalError('秘境挑战券读取超时')
        if calls >= action['multimodal']['max_calls']:
            raise MultimodalError('秘境挑战券读取达到模型调用上限')
        calls += 1
        start = clock()
        try:
            result = model.complete_json(png, SECRET_REALM_TICKETS, timeout_seconds=deadline-images.elapsed)
        finally:
            images.elapsed += max(0, clock()-start)
        if images.elapsed >= deadline:
            raise MultimodalError('秘境挑战券读取超时，忽略迟到的结果')
        count = parse_tickets(result)
        LOGGER.info('模型读取秘境剩余挑战券：%s；调用 %d/%d 次',
                    count if count is not None else '未确认', calls, action['multimodal']['max_calls'])
        return RoomState(count, center) if count is not None else None

    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[秘境券数 第%d轮] 等待出战房间→模型读取剩余总券数；未确认则补查，剩余 %.2f 秒',
                    round_number, deadline-images.elapsed)
        count = images.check(probe, lambda result: result is not None, '秘境剩余挑战券', deadline=deadline)
        if count is not None:
            return count
        delay = min(action['poll_seconds'], max(0, deadline-images.elapsed))
        if delay:
            images.sleep(delay)
    raise MultimodalError('秘境房间或挑战券读取超时，未继续出战')
