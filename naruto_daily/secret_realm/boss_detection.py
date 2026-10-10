"""毒风 BOSS 的本地 HUD 快检；不确定时交回原导航模型。"""
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


_ASSETS = Path(__file__).resolve().parents[2] / 'assets'
_MATCH_THRESHOLD = 0.82


@lru_cache(maxsize=16)
def _template(name: str) -> np.ndarray | None:
    try:
        data = (_ASSETS / name).read_bytes()
    except OSError:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)


@lru_cache(maxsize=1)
def _ending_templates():
    # 已有 1920×1080 模板缩至检测坐标系；失败字样来自现有真实失败截图。
    specs = (
        ('secret_exit_battle.png', .625),
        ('secret_exit_question.png', .625),
        ('secret_result_marker.png', .625),
        ('secret_reward_overlay.png', .625),
        ('abundance_result_dismiss.png', .625),
        ('secret_poison_failure.png', 1.0),
    )
    targets = []
    for name, factor in specs:
        source = _template(name)
        if source is None:
            return None
        for scale in (.9, 1.0, 1.1):
            targets.append(cv2.resize(source, None, fx=factor * scale,
                                      fy=factor * scale, interpolation=cv2.INTER_AREA))
    return tuple(targets)


def _ended_or_covered(gray: np.ndarray) -> bool:
    targets = _ending_templates()
    if targets is None:
        return True  # 无法排除结束画面时，不抢先报告导航完成。
    region = gray[90:670, 70:1130]
    return any(cv2.minMaxLoc(cv2.matchTemplate(region, target, cv2.TM_CCOEFF_NORMED))[1] >= .80
               for target in targets)


def poison_boss_visible(png: bytes) -> bool:
    """同时确认底部风徽章、BOSS 名称和红血条，才允许提前结束导航。

    只用于毒风未解封导航期间，不是通用战斗/结算判断。模板按 16:9
    游戏截图缩放，局部遮挡、其他分辨率比例或损坏数据均保守返回 False。
    """
    if not png:
        return False
    try:
        screen = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if screen is None:
            return False
        height, width = screen.shape[:2]
        if height < 270 or abs(width / height - 16 / 9) > 0.04:
            return False
        screen = cv2.resize(screen, (1200, 675), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        wind = _template('secret_poison_boss_wind.png')
        name = _template('secret_poison_boss_name.png')
        if wind is None or name is None:
            return False
        # 玩家血条在顶部；仅允许命中底部 BOSS HUD 的固定范围。
        region = gray[500:650, 620:790]
        _, score, _, location = cv2.minMaxLoc(cv2.matchTemplate(region, wind, cv2.TM_CCOEFF_NORMED))
        if score < _MATCH_THRESHOLD:
            return False
        wind_x, wind_y = 620 + location[0], 500 + location[1]
        # 名称必须在徽章左侧同一条 HUD 上，不能拼接画面中无关目标。
        names = gray[wind_y + 35:wind_y + 85, wind_x - 165:wind_x - 15]
        _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(names, name, cv2.TM_CCOEFF_NORMED))
        if score < _MATCH_THRESHOLD:
            return False
        # 长红血条位于名称下面；红线、顶部玩家血条或蓝色封印圈不足以命中。
        bar = screen[wind_y + 72:wind_y + 101, 150:wind_x]
        hsv = cv2.cvtColor(bar, cv2.COLOR_BGR2HSV)
        red = (((hsv[:, :, 0] <= 10) | (hsv[:, :, 0] >= 170))
               & (hsv[:, :, 1] >= 170) & (hsv[:, :, 2] >= 120))
        if np.count_nonzero(np.sum(red, axis=1) >= 170) < 5:
            return False
        # 失败/暂停时底部 HUD 可能还在；结束提示优先于 BOSS 血条。
        return not _ended_or_covered(gray)
    except (cv2.error, ValueError, TypeError):
        return False
