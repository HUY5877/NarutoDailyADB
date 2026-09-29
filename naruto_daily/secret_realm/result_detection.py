"""秘境失败结算字样的本地识别，不把其他弹窗或未知画面当成可关闭结算。"""
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


_FAILURE_TEMPLATE = Path(__file__).resolve().parents[2] / 'assets/secret_poison_failure.png'


@lru_cache(maxsize=1)
def _failure_templates():
    try:
        data = _FAILURE_TEMPLATE.read_bytes()
    except OSError:
        return ()
    source = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if source is None:
        return ()
    return tuple(cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                 for scale in (.9, 1.0, 1.1))


def secret_failure_visible(png: bytes) -> bool:
    """按16:9画面归一化，确认中央失败字样；解码或匹配不确定时不授权点击。"""
    if not isinstance(png, (bytes, bytearray)) or not png:
        return False
    try:
        screen = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        if screen is None:
            return False
        height, width = screen.shape
        if height < 270 or abs(width / height - 16 / 9) > .04:
            return False
        screen = cv2.resize(screen, (1200, 675), interpolation=cv2.INTER_AREA)
        region = screen[90:430, 150:1050]
        return any(cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))[1] >= .84
                   for template in _failure_templates())
    except (cv2.error, ValueError):
        return False
