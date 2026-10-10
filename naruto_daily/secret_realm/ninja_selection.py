"""本地确认秘境更换忍者页，再定位返回按钮，避免误点房间的同款返回。"""
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


_ASSET_ROOT = Path(__file__).resolve().parents[2] / 'assets'
# 模板来自 622x350 的用户截图；统一尺寸后可匹配实际 1920x1080 截图。
_REFERENCE_SIZE = (622, 350)
_TARGETS = {
    'outfit': ((70, 180, 165, 260), .86),
    'info': ((375, 40, 482, 70), .88),
    'confirm': ((430, 267, 550, 310), .88),
    'return': ((0, 309, 115, 350), .88),
}


@lru_cache(maxsize=4)
def _template(name):
    try:
        png = (_ASSET_ROOT / f'secret_ninja_{name}.png').read_bytes()
        return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    except (OSError, cv2.error, ValueError):
        return None


def _find(screen, name):
    template = _template(name)
    if template is None:
        return None
    (left, top, right, bottom), threshold = _TARGETS[name]
    region = screen[top:bottom, left:right]
    height, width = template.shape
    if height > region.shape[0] or width > region.shape[1]:
        return None
    _, score, _, point = cv2.minMaxLoc(
        cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
    if score < threshold:
        return None
    return left + point[0] + width / 2, top + point[1] + height / 2


def find_ninja_selection_return(png: bytes) -> tuple[int, int] | None:
    """识别更换忍者页并返回原截图内的返回按钮中心；不确定时不授权点击。"""
    if not isinstance(png, (bytes, bytearray)) or not png:
        return None
    try:
        screen = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        if screen is None:
            return None
        height, width = screen.shape
        if height < 270 or abs(width / height - 16 / 9) > .04:
            return None
        screen = cv2.resize(screen, _REFERENCE_SIZE, interpolation=cv2.INTER_AREA)
        # 角色加载中没有衣服图标，改用固定的“查看信息”标记。
        # 两条路径都要求右下确认，不能只凭通用返回按钮认定页面。
        if _find(screen, 'confirm') is None:
            return None
        if _find(screen, 'outfit') is None and _find(screen, 'info') is None:
            return None
        point = _find(screen, 'return')
        if point is None:
            return None
        return (round(point[0] * width / _REFERENCE_SIZE[0]),
                round(point[1] * height / _REFERENCE_SIZE[1]))
    except (cv2.error, ValueError):
        return None
