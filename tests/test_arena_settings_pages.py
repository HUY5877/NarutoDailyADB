"""用真实标签裁图验证高级设置选中态，所有输入只记录在假设备中。"""

from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.arena.settings import _SettingsFlow
from naruto_daily.common.visual import VisualError, find_template_in_png


ROOT = Path(__file__).resolve().parents[1]
TABS = ('function', 'expression', 'downloads', 'advanced')
OTHER_TABS = TABS[:-1]
SELECTED_TEMPLATE = ROOT / 'assets/settings_advanced_selected.png'
SELECTED_ROI = (215, 390, 285, 610)
ORIGINAL_SWIPE = (1090, 900, 1090, 300, 650)


def read_image(path):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


class TabDevice:
    """四页均保留高级设置文字，选中边框随真实标签裁图变化。"""

    def __init__(self, tab, *, switch_after_swipe=None, checkbox_visible=False,
                 ignore_advanced_taps=False):
        self.action = deepcopy(json.loads((ROOT / 'config.example.json').read_text(
            encoding='utf-8'))['tasks']['arena_weekly'][0])
        self.action.update(timeout_seconds=.8, poll_seconds=.2, pre_click_wait_seconds=.05,
                           post_click_wait_seconds=.05)
        self.tab = tab
        self.switch_after_swipe = switch_after_swipe
        self.checkbox_visible = checkbox_visible
        self.ignore_advanced_taps = ignore_advanced_taps
        self.clock = 0.
        self.scroll_position = 0
        self.swipes = []
        self.taps = []
        self.frames = {
            name: read_image(ROOT / 'tests/fixtures/settings_tabs' / f'{name}.png')
            for name in TABS
        }

    @staticmethod
    def paste_template(frame, relative, center):
        template = read_image(ROOT / relative)
        height, width = template.shape[:2]
        left, top = center[0] - width // 2, center[1] - height // 2
        frame[top:top + height, left:left + width] = template

    def screenshot(self):
        frame = np.full((1080, 1920, 3), 35, dtype=np.uint8)
        # 错页右侧内容不同；换页不能被当成高级设置滚动成功。
        shade = 205 if self.tab == 'advanced' else 65 + 35 * TABS.index(self.tab)
        frame[280:770, 500:1660] = shade
        for row in (390, 500, 610):
            top = row - self.scroll_position * 55
            cv2.rectangle(frame, (600, top), (1550, top + 35), (30, 30, 30), -1)
        tab_crop = self.frames[self.tab]
        assert tab_crop.shape[:2] == (220, 270)
        frame[390:610, 200:470] = tab_crop
        if self.checkbox_visible:
            # 即使右侧恰有可匹配的目标，也必须先确认左侧选中高级设置。
            self.paste_template(frame, self.action['players_only_label']['template'], (1238, 462))
            point = (1238 + self.action['checkbox_x_offset'],
                     462 + self.action['checkbox_y_offset'])
            self.paste_template(frame, self.action['checked_template'], point)
        return cv2.imencode('.png', frame)[1].tobytes()

    def advanced_text_center(self):
        target = self.action['advanced_tab']
        roi = tuple(target[key] for key in ('left', 'top', 'right', 'bottom'))
        center, _ = find_template_in_png(self.screenshot(), ROOT / target['template'],
                                         roi, target['threshold'])
        return center

    def tap(self, *point):
        self.taps.append(point)
        if point == self.advanced_text_center() and not self.ignore_advanced_taps:
            self.tab = 'advanced'

    def swipe(self, *arguments):
        self.swipes.append(arguments)
        if self.switch_after_swipe is not None:
            self.tab = self.switch_after_swipe
        elif self.tab == 'advanced':
            self.scroll_position += 1

    def sleep(self, seconds):
        self.clock += seconds

    def flow(self):
        return _SettingsFlow(self, self.action, find_template_in_png, ROOT,
                             self.sleep, lambda: self.clock)


@pytest.mark.parametrize('tab', TABS)
def test_real_tab_fixtures_distinguish_selection_from_visible_advanced_text(tab):
    device = TabDevice(tab)
    png = device.screenshot()
    target = device.action['advanced_tab']
    roi = tuple(target[key] for key in ('left', 'top', 'right', 'bottom'))

    text, _ = find_template_in_png(png, ROOT / target['template'], roi, target['threshold'])
    selected, _ = find_template_in_png(png, SELECTED_TEMPLATE, SELECTED_ROI, .95)

    assert text is not None, '高级设置文字在其他标签页也可见，不能单独作为页面判断'
    assert (selected is not None) is (tab == 'advanced')


@pytest.mark.parametrize('tab', OTHER_TABS)
def test_other_settings_pages_cannot_authorize_any_swipe(tab):
    device = TabDevice(tab)

    with pytest.raises(VisualError):
        device.flow().scroll()

    assert device.swipes == []


def test_selected_advanced_page_can_scroll():
    device = TabDevice('advanced')

    device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE]
    assert device.scroll_position == 1


@pytest.mark.parametrize('tab', TABS)
def test_clicking_advanced_tab_from_each_initial_page_allows_scrolling(tab):
    device = TabDevice(tab)
    expected_click = device.advanced_text_center()
    flow = device.flow()

    flow.button('advanced_tab')
    flow.scroll()

    assert expected_click is not None
    assert device.taps == [expected_click]
    assert device.tab == 'advanced'
    assert device.swipes == [ORIGINAL_SWIPE]
    assert device.scroll_position == 1


@pytest.mark.parametrize('tab', OTHER_TABS)
def test_ignored_advanced_tab_click_does_not_authorize_scrolling(tab):
    device = TabDevice(tab, ignore_advanced_taps=True)
    expected_click = device.advanced_text_center()
    flow = device.flow()

    flow.button('advanced_tab')
    with pytest.raises(VisualError, match='未确认高级设置已选中'):
        flow.scroll()

    assert expected_click is not None
    assert device.taps == [expected_click]
    assert device.tab == tab
    assert device.swipes == []
    assert device.scroll_position == 0


@pytest.mark.parametrize('tab', OTHER_TABS)
def test_switching_tabs_after_a_swipe_stops_without_fallback(tab):
    device = TabDevice('advanced', switch_after_swipe=tab)

    with pytest.raises(VisualError):
        device.flow().scroll()

    assert device.swipes == [ORIGINAL_SWIPE]
    assert device.scroll_position == 0


@pytest.mark.parametrize('tab', OTHER_TABS)
def test_other_settings_pages_cannot_confirm_a_checkbox(tab):
    device = TabDevice(tab, checkbox_visible=True)

    with pytest.raises(VisualError):
        device.flow().checkbox()

    assert device.swipes == []


def test_selected_advanced_page_can_confirm_the_same_checkbox():
    device = TabDevice('advanced', checkbox_visible=True)

    checked, center = device.flow().checkbox()

    assert checked is True
    assert center == (1575, 465)
    assert device.swipes == []
