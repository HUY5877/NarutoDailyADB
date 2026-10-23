"""模板观察小队突袭，并在等待战斗结束时按需开启自动战斗。"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
import time
from typing import Callable

import cv2
import numpy as np

from naruto_daily.common.execution import MODEL_REQUEST_TIMEOUT_SECONDS, check_execution, stage_timeout
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import (
    NonRetryableVisualError, TemplateConfigurationError, VisualError, _decode_png, _load_template,
    find_template_in_png,
)
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.multimodal.prompt_jev import SQUAD_AUTO_BATTLE
from naruto_daily.multimodal.transport import MultimodalError, MultimodalRequestError


LOGGER = logging.getLogger("naruto_daily")
READY_BRIGHTNESS_RATIO = 0.8
# 1920×1080 战斗页自动按钮左上侧；只能在确认战斗后使用。
AUTO_BATTLE_POINT = (45, 330)
AUTO_BATTLE_MARKER = dict(
    name="自动战斗中", template="assets/squad_assault_auto_battle.png",
    left=820, top=95, right=1120, bottom=180, threshold=.9,
)
TARGET_KEYS = (
    "ready_marker", "match_button", "quadruple_checked", "quadruple_unchecked",
    "dialog_marker", "continue_button", "limit_marker",
)


@dataclass(frozen=True)
class SquadAssaultSummary:
    matches: int
    continues: int
    quadruple_disabled: bool


def _auto_button_unchanged(before: bytes, after: bytes) -> bool:
    """模型等待期间可能切页，只核对固定“自动”文字区，避开战斗动画。"""
    observed, fresh = _decode_png(before), _decode_png(after)
    if observed.shape[:2] != (1080, 1920) or fresh.shape[:2] != (1080, 1920):
        return False
    patches = [cv2.cvtColor(frame[390:425, 35:105], cv2.COLOR_BGR2GRAY)
               for frame in (observed, fresh)]
    if any(float(patch.std()) < 20 for patch in patches):
        return False
    score = float(cv2.matchTemplate(patches[1], patches[0], cv2.TM_CCOEFF_NORMED)[0, 0])
    difference = float(cv2.absdiff(*patches).mean())
    # 相关系数无法排除弹窗的变暗遮罩，还要核对绝对亮度差。
    return score >= .9 and difference <= 15


def _ready_state(images, png: bytes, target: dict, templates: dict) -> str:
    """相关系数不区分变暗遮罩，另核对同位置标题高亮像素的实际亮度。"""
    dimmed = False
    screen = None
    for name in (target["template"], *target.get("alternate_templates", [])):
        candidate = dict(target, template=name, alternate_templates=[])
        center, _ = images.match(png, candidate)
        if center is None:
            continue
        path = images.asset_root / name
        if path not in templates:
            templates[path] = _load_template(path)
        template = templates[path]
        if screen is None:
            screen = _decode_png(png)
        height, width = template.shape[:2]
        left, top = center[0] - width // 2, center[1] - height // 2
        right, bottom = left + width, top + height
        if left < 0 or top < 0 or right > screen.shape[1] or bottom > screen.shape[0]:
            raise VisualError("小队突袭准备页标题裁剪不完整，未继续点击")
        gray = (template if template.ndim == 2 else
                cv2.cvtColor(template[:, :, :3], cv2.COLOR_BGR2GRAY))
        valid = template[:, :, 3] > 0 if template.ndim == 3 and template.shape[2] == 4 else np.ones(gray.shape, bool)
        if not np.any(valid):
            raise TemplateConfigurationError("小队突袭准备页模板没有可识别内容")
        bright = valid & (gray >= max(80, float(np.percentile(gray[valid], 70))))
        if not np.any(bright):
            raise TemplateConfigurationError("小队突袭准备页模板缺少可核验的高亮标题")
        patch = cv2.cvtColor(screen[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
        ratio = float(np.mean(patch[bright])) / float(np.mean(gray[bright]))
        if ratio >= READY_BRIGHTNESS_RATIO:
            return "ready"
        dimmed = True
    return "dimmed" if dimmed else "absent"


def handle_squad_assault(
    client, action: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."), sleep: Callable[[float], None] = time.sleep,
    model: MultimodalClient | None = None,
) -> SquadAssaultSummary:
    """首次必匹配一次；明确离开并回到准备页后才检查收益上限。"""
    for key in TARGET_KEYS:
        target = action[key]
        if "template" not in target or "main_menu" in target or "multimodal" in target:
            raise TemplateConfigurationError(f"小队突袭{key}必须使用 CV 模板")
    timeout = float(action.get("timeout_seconds", 1800))
    poll = float(action.get("poll_seconds", 5))
    post_wait = float(action.get("post_click_wait_seconds", 1))
    if not all(math.isfinite(value) for value in (timeout, poll, post_wait)) or timeout <= 0 or poll <= 0 or post_wait < 0:
        raise TemplateConfigurationError("小队突袭超时、轮询和点击等待配置无效")
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    timeout = stage_timeout(timeout)
    deadline = images.elapsed + timeout
    wall_deadline = time.monotonic() + timeout
    templates = {}
    matches = continues = 0
    disabled = False
    awaiting_return = False
    departed = False
    continued = False

    def remaining():
        check_execution()
        value = min(deadline - images.elapsed, wall_deadline - time.monotonic())
        if value <= 0:
            raise VisualError("小队突袭等待超时，未继续匹配或点击")
        return value

    def wait(seconds):
        images.sleep(min(seconds, remaining()))
        remaining()

    def match(png, key):
        remaining()
        result = images.match(png, action[key])
        remaining()
        return result[0]

    def tap(center):
        remaining()
        client.tap(*center)
        remaining()

    def enable_auto_if_battle(png):
        nonlocal model
        try:
            if model is None:
                model = MultimodalClient({'timeout_seconds': MODEL_REQUEST_TIMEOUT_SECONDS},
                                         asset_root=asset_root)
            with model_request_context('小队突袭自动战斗前检查'):
                result = model.complete_json(
                    png, SQUAD_AUTO_BATTLE,
                    timeout_seconds=min(remaining(), MODEL_REQUEST_TIMEOUT_SECONDS),
                )
        except MultimodalError as exc:
            # 配置或不可重试请求错误继续交给上层；网络及解析失败下一轮重截，绝不点击。
            if isinstance(exc, NonRetryableVisualError) or (
                isinstance(exc, MultimodalRequestError) and not exc.retryable
            ):
                raise
            remaining()
            LOGGER.warning('小队突袭 JEV 战斗判断失败（%s），本轮不点击自动，等待重新截图',
                           type(exc).__name__)
            return
        remaining()
        if not (isinstance(result, dict) and result.get('page') == 'battle'
                and result.get('can_enable_auto') is True):
            LOGGER.info('小队突袭 JEV 未确认仍在战斗且可以开启自动，本轮不点击')
            return
        fresh = client.screenshot()
        remaining()
        if not _auto_button_unchanged(png, fresh):
            LOGGER.info('小队突袭 JEV 返回后自动按钮区域已变化，本轮不点击，等待重新识别')
            return
        blocked = any(match(fresh, key) is not None for key in (
            'ready_marker', 'dialog_marker', 'continue_button', 'match_button',
            'quadruple_checked', 'quadruple_unchecked',
        ))
        auto_center, _ = images.match(fresh, AUTO_BATTLE_MARKER)
        remaining()
        if blocked or auto_center is not None:
            LOGGER.info('小队突袭点击前已出现准备页、提示或自动战斗中，本轮不点击自动')
            return
        LOGGER.info('小队突袭 JEV 确认仍在战斗且自动按钮未变化，点击左侧自动按钮一次')
        tap(AUTO_BATTLE_POINT)

    while True:
        remaining()
        png = client.screenshot()
        remaining()
        # 前景提示始终优先。背景标题即使相关系数很高，也不能触发匹配或完成。
        dialog = match(png, "dialog_marker")
        continue_center = match(png, "continue_button")
        if dialog is not None or continue_center is not None:
            if awaiting_return and continue_center is not None and not continued:
                LOGGER.info("小队突袭出现四倍奖励提示，仅点击继续出战，不点击确认勾选")
                tap(continue_center)
                continues += 1
                continued = True
                wait(post_wait)
            else:
                LOGGER.info("等待小队突袭提示消失，未再次点击或操作背景页面")
                wait(poll)
            continue

        ready = _ready_state(images, png, action["ready_marker"], templates)
        remaining()
        if ready != "ready":
            if awaiting_return and ready == "absent":
                # 标题可能被局部遮住；同帧仍有准备页控件时，不能把漏检当作离页。
                controls_remain = any(match(png, key) is not None for key in (
                    "match_button", "quadruple_checked", "quadruple_unchecked",
                ))
                if not controls_remain:
                    departed = True
                    auto_center, _ = images.match(png, AUTO_BATTLE_MARKER)
                    remaining()
                    if auto_center is None:
                        enable_auto_if_battle(png)
                    else:
                        LOGGER.info("小队突袭已检测到“自动战斗中”文案，无需点击自动按钮")
                else:
                    LOGGER.info("小队突袭标题未确认但准备页控件仍在，不认定离页或重复匹配")
            LOGGER.info("小队突袭%s，%.2f 秒后重新截图", "页面被遮挡" if ready == "dimmed" else "尚未回到准备页", poll)
            wait(poll)
            continue
        if awaiting_return:
            if not departed:
                LOGGER.info("已发送匹配，尚未观察到离开准备页，不重复匹配")
                wait(poll)
                continue
            if match(png, "limit_marker") is not None:
                LOGGER.info("小队突袭回到准备页，今日收益次数已达上限；交给后续动作返回主界面")
                return SquadAssaultSummary(matches, continues, disabled)
        checked = match(png, "quadruple_checked")
        unchecked = match(png, "quadruple_unchecked")
        if (checked is None) == (unchecked is None):
            LOGGER.info("小队突袭四倍奖励状态未明确，不当作已关闭，等待重新截图")
            wait(poll)
            continue
        if checked is not None:
            if matches == 0 and not disabled:
                LOGGER.info("小队突袭初始四倍奖励已勾选，点击一次关闭，等待明确未勾选状态")
                tap(checked)
                disabled = True
                wait(post_wait)
            else:
                LOGGER.info("四倍奖励仍未确认关闭，不重复点击勾选，也不匹配")
                wait(poll)
            continue

        match_center = match(png, "match_button")
        if match_center is None:
            LOGGER.info("未确认小队突袭匹配按钮，等待重新截图")
            wait(poll)
            continue
        LOGGER.info("小队突袭第%d次匹配", matches + 1)
        tap(match_center)
        matches += 1
        awaiting_return = True
        departed = continued = False
        wait(post_wait)
