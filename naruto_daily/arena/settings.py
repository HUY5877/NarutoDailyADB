"""进入决斗场前，确认高级设置中的「只匹配玩家」已关闭。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import cv2
import numpy as np

from naruto_daily.common.execution import current_execution
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import NonRetryableVisualError, VisualError, find_template_in_png
from naruto_daily.return_to_main import return_to_main


LOGGER = logging.getLogger('naruto_daily')
# 从两列设置项之间的空隙拖动，避开文字、复选框和主场景预览按钮。
SETTINGS_SWIPE = (1090, 900, 1090, 300, 650)
SETTINGS_SWIPE_RETRIES = ((1090, 700, 1090, 300, 650), (1090, 500, 1090, 300, 650))
# 仅比较滚动列表内部，排除主界面动画、左侧标签和设置窗口边框。
SETTINGS_CONTENT_ROI = (505, 250, 1660, 935)
CHECKBOX_RADIUS = 40


def _settings_change_ratio(before, after):
    left, top, right, bottom = SETTINGS_CONTENT_ROI
    def content(png):
        frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        return cv2.GaussianBlur(frame[top:bottom, left:right], (5, 5), 0)

    # 忽略轻微渲染噪声；至少 1% 的列表像素明显变化才视为滚动生效。
    return float(np.mean(cv2.absdiff(content(before), content(after)) > 20))


class _SettingsFlow:
    """给识别和已有回主界面模块共用的限时输入适配器。"""

    def __init__(self, client, action, matcher, asset_root, sleep, clock):
        self.client, self.action, self.clock = client, action, clock
        self.started = clock()
        self.images = ImageRecognition(client, matcher, asset_root, sleep)

    def remaining(self):
        execution = current_execution()
        if execution is not None:
            return execution.remaining()
        elapsed = max(self.images.elapsed, self.clock() - self.started)
        remaining = self.action['timeout_seconds'] - elapsed
        if remaining <= 0:
            raise VisualError('决斗场匹配设置处理超时，已停止操作')
        return remaining

    def pause(self, seconds):
        self.images.sleep(min(float(seconds), self.remaining()))
        self.remaining()

    def screenshot(self):
        self.remaining()
        png = self.client.screenshot()
        self.remaining()  # 迟到截图不能授权任何输入。
        frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise VisualError('决斗场匹配设置截图无法解析')
        if frame.shape[:2] != (1080, 1920):
            raise NonRetryableVisualError('决斗场匹配设置要求 1920×1080 安卓画面，已停止操作')
        return png

    def tap(self, x, y):
        self.remaining()
        LOGGER.info('点击坐标：(%d, %d)', x, y)
        self.client.tap(x, y)
        self.remaining()

    def match(self, png, target):
        result = self.images.match(png, target)
        self.remaining()
        return result

    def button(self, key):
        target = self.action[key]
        while True:
            center, score = self.match(self.screenshot(), target)
            if center is not None:
                self.pause(self.action['pre_click_wait_seconds'])
                center, score = self.match(self.screenshot(), target)
                if center is not None:
                    LOGGER.info('OpenCV 复查确认%s，相似度 %.3f', target['name'], score)
                    self.tap(*center)
                    self.pause(self.action['post_click_wait_seconds'])
                    return
            LOGGER.info('尚未确认%s，相似度 %.3f，重新截图后再判断', target['name'], score)
            self.pause(self.action['poll_seconds'])

    def _scroll_frame(self):
        # 只有设置页标签仍可见时才拖动，页面意外切换时停止输入并等待。
        while True:
            png = self.screenshot()
            center, score = self.match(png, self.action['advanced_tab'])
            if center is not None:
                return png
            LOGGER.info('滚动前未确认高级设置，相似度 %.3f，暂停滑动', score)
            self.pause(self.action['poll_seconds'])

    def scroll(self):
        for attempt, swipe in enumerate((SETTINGS_SWIPE, *SETTINGS_SWIPE_RETRIES)):
            before = self._scroll_frame()
            if attempt:
                LOGGER.info('更换高级设置滑动起点为 (%d, %d) 重试', *swipe[:2])
            self.remaining()
            self.client.swipe(*swipe)
            self.pause(self.action['post_click_wait_seconds'])
            after = self._scroll_frame()
            change_ratio = _settings_change_ratio(before, after)
            self.remaining()
            if change_ratio >= .01:
                LOGGER.info('高级设置滑动已生效，列表内容变化 %.1f%%', change_ratio * 100)
                return
            label, _ = self.match(after, self.action['players_only_label'])
            if label is not None:
                LOGGER.info('高级设置列表未移动，但已找到决斗场只匹配玩家，继续检查状态')
                return
            LOGGER.info('高级设置列表未移动，滑动起点 (%d, %d)', *swipe[:2])
        raise VisualError('高级设置滑动未生效，更换起点重试后仍未找到决斗场只匹配玩家')

    def checkbox(self):
        png = self.screenshot()
        label, score = self.match(png, self.action['players_only_label'])
        if label is None:
            LOGGER.info('未确认决斗场只匹配玩家文字，相似度 %.3f，暂不点击', score)
            return None, None
        x = label[0] + self.action['checkbox_x_offset']
        y = label[1] + self.action['checkbox_y_offset']
        roi = dict(left=x - CHECKBOX_RADIUS, top=y - CHECKBOX_RADIUS,
                   right=x + CHECKBOX_RADIUS, bottom=y + CHECKBOX_RADIUS,
                   threshold=self.action['checkbox_threshold'])
        if not (0 <= roi['left'] < roi['right'] <= 1920
                and 0 <= roi['top'] < roi['bottom'] <= 1080):
            raise NonRetryableVisualError('决斗场只匹配玩家复选框区域超出截图')
        checked, checked_score = self.match(png, dict(roi, template=self.action['checked_template']))
        unchecked, unchecked_score = self.match(png, dict(roi, template=self.action['unchecked_template']))
        LOGGER.info('决斗场只匹配玩家状态：勾选模板 %.3f，未勾选模板 %.3f',
                    checked_score, unchecked_score)
        if (checked is None) == (unchecked is None):
            return None, None
        return (True, checked) if checked is not None else (False, unchecked)


def prepare_arena_matchmaking(client, action, *, template_matcher=find_template_in_png,
                              asset_root=Path('.'), sleep=time.sleep, clock=time.monotonic):
    """只在明确勾选时取消一次；确认关闭后调用已有回主界面流程。"""
    flow = _SettingsFlow(client, action, template_matcher, asset_root, sleep, clock)
    flow.button('settings_button')
    flow.button('advanced_tab')
    for index in range(action['scroll_swipes']):
        LOGGER.info('高级设置向上滑动 %d/%d', index + 1, action['scroll_swipes'])
        flow.scroll()

    while True:
        checked, _ = flow.checkbox()
        if checked is not None:
            flow.pause(action['pre_click_wait_seconds'])
            checked, center = flow.checkbox()
            if checked is False:
                LOGGER.info('已确认决斗场只匹配玩家未勾选，保持关闭')
                break
            if checked is True:
                LOGGER.info('已确认决斗场只匹配玩家已勾选，点击一次取消')
                flow.tap(*center)
                flow.pause(action['post_click_wait_seconds'])
                # 发出一次取消后仅观察，不能因画面未更新而再次切换开关。
                while True:
                    checked, _ = flow.checkbox()
                    if checked is False:
                        LOGGER.info('已截图确认决斗场只匹配玩家取消勾选成功')
                        break
                    flow.pause(action['poll_seconds'])
                break
        flow.pause(action['poll_seconds'])

    flow.remaining()
    return_to_main(flow, action['return_to_main'], template_matcher=template_matcher,
                   asset_root=asset_root, sleep=flow.pause)
    flow.remaining()
    LOGGER.info('决斗场匹配设置准备完成，已回主界面，继续原有场景查找')
