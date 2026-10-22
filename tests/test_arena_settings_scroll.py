"""高级设置滑动用真实像素和模板验证，不连接模拟器。"""

from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.arena.settings import ADVANCED_SELECTED, _SettingsFlow
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.visual import VisualError, find_template_in_png


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_SWIPE = (1090, 900, 1090, 300, 650)
FIRST_FALLBACK = (1090, 700, 1090, 300, 650)
SECOND_FALLBACK = (1090, 500, 1090, 300, 650)


class SettingsDevice:
    """无效手势不改变列表位置；背景动画与单像素噪声独立变化。"""

    def __init__(self, *, effective_starts=(), target_visible=False,
                 animation=False, pixel_noise=False, tab_disappears=False,
                 late_after_swipe=False):
        self.action = deepcopy(json.loads((ROOT / 'config.example.json').read_text(
            encoding='utf-8'))['tasks']['arena_weekly'][0])
        self.action.update(timeout_seconds=.8, poll_seconds=.2,
                           post_click_wait_seconds=.05)
        self.effective_starts = set(effective_starts)
        self.target_visible = target_visible
        self.animation, self.pixel_noise = animation, pixel_noise
        self.tab_disappears, self.late_after_swipe = tab_disappears, late_after_swipe
        self.clock = 0.
        self.scroll_position = 0
        self.swipes = []
        self.screen_count = 0

    @staticmethod
    def paste_template(frame, relative, center):
        data = np.fromfile(ROOT / relative, dtype=np.uint8)
        template = cv2.imdecode(data, cv2.IMREAD_COLOR)
        assert template is not None, relative
        height, width = template.shape[:2]
        left, top = center[0] - width // 2, center[1] - height // 2
        frame[top:top + height, left:left + width] = template

    def screenshot(self):
        self.screen_count += 1
        frame = np.full((1080, 1920, 3), 35, dtype=np.uint8)
        if self.animation:
            shade = 220 if self.screen_count % 2 else 15
            frame[:, :180] = shade
            frame[:180, :] = shade

        # 只有滚动位置改变，这些列表内容才移动；没有按调用次数生成底部。
        frame[280:770, 500:1660] = (205, 205, 205)
        for row in (390, 500, 610):
            top = row - self.scroll_position * 55
            cv2.rectangle(frame, (600, top), (1550, top + 35), (75, 75, 75), -1)
        if self.pixel_noise:
            frame[600, 900] = 255 if self.screen_count % 2 else 0

        if not (self.tab_disappears and self.swipes):
            self.paste_template(frame, self.action['advanced_tab']['template'], (350, 520))
            self.paste_template(frame, ADVANCED_SELECTED['template'], (249, 498))
        if self.target_visible:
            self.paste_template(frame, self.action['players_only_label']['template'], (1238, 462))

        if self.late_after_swipe and self.swipes:
            self.clock = 2.
        return cv2.imencode('.png', frame)[1].tobytes()

    def swipe(self, *arguments):
        self.swipes.append(arguments)
        if arguments[1] in self.effective_starts:
            self.scroll_position += 1

    def sleep(self, seconds):
        self.clock += seconds

    def flow(self):
        return _SettingsFlow(self, self.action, find_template_in_png, ROOT,
                             self.sleep, lambda: self.clock)


def test_ineffective_original_start_retries_inside_the_list():
    device = SettingsDevice(effective_starts={700})

    device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE, FIRST_FALLBACK]
    assert device.scroll_position == 1


def test_second_fallback_is_used_only_after_both_higher_starts_fail():
    device = SettingsDevice(effective_starts={500})

    device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE, FIRST_FALLBACK, SECOND_FALLBACK]
    assert device.scroll_position == 1


@pytest.mark.parametrize('visual_changes', [
    {}, {'animation': True}, {'pixel_noise': True},
], ids=['static', 'background-animation', 'single-pixel-noise'])
def test_all_ineffective_starts_fail_without_mistaking_animation_or_noise_for_scroll(visual_changes):
    device = SettingsDevice(**visual_changes)

    with pytest.raises(VisualError, match='高级设置滑动未生效'):
        device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE, FIRST_FALLBACK, SECOND_FALLBACK]
    assert device.scroll_position == 0


def test_visible_target_allows_a_stationary_bottom_without_fallback():
    device = SettingsDevice(target_visible=True)

    device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE]
    assert device.scroll_position == 0


def test_disappearing_advanced_tab_stops_fallback_input():
    device = SettingsDevice(tab_disappears=True)

    with pytest.raises(VisualError):
        device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE]
    assert device.scroll_position == 0


def test_late_post_swipe_screenshot_cannot_authorize_fallback():
    device = SettingsDevice(late_after_swipe=True)

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(1., clock=lambda: device.clock, sleep=device.sleep):
            device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE]
    assert device.scroll_position == 0
