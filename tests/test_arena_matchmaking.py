"""决斗场匹配偏好的操作边界；假设备与真实模板均不连接 ADB。"""
from collections import deque
from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.arena import settings
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.visual import VisualError, find_template_in_png


ROOT = Path(__file__).resolve().parents[1]


def action_config(**overrides):
    action = deepcopy(json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))[
        'tasks']['arena_weekly'][0])
    assert action['type'] == 'prepare_arena_matchmaking'
    return action | {
        'timeout_seconds': 5, 'poll_seconds': .2,
        'pre_click_wait_seconds': .01, 'post_click_wait_seconds': .01,
        'scroll_swipes': 3,
    } | overrides


class Replay:
    """画面随实际输入变化；同一帧的标签与两种勾选状态一起识别。"""

    def __init__(self, monkeypatch, states=('checked', 'checked', 'checked', 'unchecked'), *,
                 labels=(), action=None, real_templates=False, size=(1920, 1080),
                 late_screenshot=None, advanced_disappears_after=None, button_centers=None):
        self.action = action or action_config()
        self.clock = 0.
        self.events, self.taps, self.swipes, self.returns = [], [], [], []
        self.scroll_position = 0
        self.frames = {}
        self.phase = 'main'
        self.states = deque(states)
        self.labels = deque(labels)
        self.state = self.states[0] if self.states else 'unknown'
        self.label = (1200, 650)
        self.real_templates = real_templates
        self.size = size
        self.late_screenshot = late_screenshot
        self.advanced_disappears_after = advanced_disappears_after
        self.screen_count = 0
        self.button_centers = {phase: deque(points) for phase, points in (button_centers or {}).items()}
        self.button_center = {'main': (86, 225), 'settings': (350, 520)}
        monkeypatch.setattr(settings, 'return_to_main', self.return_to_main)

    @property
    def checkbox(self):
        x, y = self.label
        return (x + self.action['checkbox_x_offset'], y + self.action['checkbox_y_offset'])

    def screenshot(self):
        self.screen_count += 1
        if self.phase in self.button_centers and self.button_centers[self.phase]:
            self.button_center[self.phase] = self.button_centers[self.phase].popleft()
        if self.phase == 'advanced' and self.scroll_position >= self.action['scroll_swipes']:
            if self.states:
                self.state = self.states.popleft()
            if self.labels:
                self.label = self.labels.popleft()
        advanced_visible = self.phase in {'settings', 'advanced'}
        if self.advanced_disappears_after is not None:
            advanced_visible &= len(self.swipes) < self.advanced_disappears_after
        data = dict(phase=self.phase, state=self.state, label=self.label,
                    advanced_visible=advanced_visible,
                    button=self.button_center.get(self.phase),
                    bottom=self.scroll_position >= self.action['scroll_swipes'])
        width, height = self.size
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[0, 0] = (self.screen_count, 50, 150)
        if self.phase == 'advanced':
            # 列表画面随位置移动，不能用截图计数或背景变化冒充滚动成功。
            offset = (self.scroll_position * 25) % 200
            for y in (400, 600, 800):
                cv2.rectangle(frame, (600, y - offset), (950, y - offset + 40),
                              (180, 180, 180), -1)
        if self.real_templates:
            self.render(frame, data)
        png = cv2.imencode('.png', frame)[1].tobytes()
        self.frames[png] = data
        self.events.append(('screen', self.screen_count, data))
        if self.late_screenshot is not None:
            self.late_screenshot(self)
        return png

    def render(self, frame, data):
        def paste(relative, center):
            template = cv2.imread(str(ROOT / relative), cv2.IMREAD_COLOR)
            assert template is not None, relative
            h, w = template.shape[:2]
            x, y = center
            frame[y - h // 2:y - h // 2 + h, x - w // 2:x - w // 2 + w] = template

        if data['phase'] == 'main':
            paste(self.action['settings_button']['template'], data['button'])
        if data['advanced_visible']:
            paste(self.action['advanced_tab']['template'],
                  data['button'] if data['phase'] == 'settings' else (350, 520))
        if data['phase'] == 'advanced' and data['bottom']:
            paste(self.action['players_only_label']['template'], data['label'])
            paste(self.action['unchecked_template' if data['state'] == 'unchecked' else 'checked_template'],
                  self.checkbox)
            # 同列别行的勾选不能影响目标复选框状态。
            paste(self.action['checked_template'], (self.checkbox[0], self.checkbox[1] + 100))

    def matcher(self, png, path, roi, threshold):
        data, template = self.frames[png], Path(path).name
        center = None
        if template == Path(self.action['settings_button']['template']).name and data['phase'] == 'main':
            center = data['button']
        elif template == Path(self.action['advanced_tab']['template']).name and data['advanced_visible']:
            center = data['button'] if data['phase'] == 'settings' else (350, 520)
        elif data['phase'] == 'advanced' and data['bottom']:
            if template == Path(self.action['players_only_label']['template']).name and data['state'] != 'missing-label':
                center = data['label']
            elif template == Path(self.action['checked_template']).name and data['state'] in {'checked', 'ambiguous'}:
                center = (data['label'][0] + self.action['checkbox_x_offset'],
                          data['label'][1] + self.action['checkbox_y_offset'])
            elif template == Path(self.action['unchecked_template']).name and data['state'] in {'unchecked', 'ambiguous'}:
                center = (data['label'][0] + self.action['checkbox_x_offset'],
                          data['label'][1] + self.action['checkbox_y_offset'])
        if center is not None:
            left, top, right, bottom = roi
            if not (left <= center[0] < right and top <= center[1] < bottom):
                center = None
        self.events.append(('match', self.screen_count, template, roi, center))
        return center, .999 if center is not None else .1

    def tap(self, *point):
        self.taps.append(point)
        self.events.append(('tap', self.screen_count, point))
        if self.phase == 'main':
            assert point == self.button_center['main']
            self.phase = 'settings'
        elif self.phase == 'settings':
            assert point == self.button_center['settings']
            self.phase = 'advanced'

    def swipe(self, *points):
        self.swipes.append(points)
        self.events.append(('swipe', self.screen_count, points))
        self.scroll_position = min(self.scroll_position + 1, self.action['scroll_swipes'])

    def sleep(self, seconds):
        self.events.append(('sleep', seconds))
        self.clock += seconds

    def return_to_main(self, client, action, **kwargs):
        assert self.phase == 'advanced'
        assert self.state == 'unchecked', '只有确认已取消勾选才能回主界面'
        assert action == self.action['return_to_main']
        self.returns.append((client, action, kwargs))
        self.events.append(('return', self.screen_count))

    def run(self):
        return settings.prepare_arena_matchmaking(
            self, self.action, template_matcher=find_template_in_png if self.real_templates else self.matcher,
            asset_root=ROOT, sleep=self.sleep, clock=lambda: self.clock)


def test_checked_is_cancelled_once_then_verified_before_return(monkeypatch):
    replay = Replay(monkeypatch, action=action_config(scroll_swipes=10))
    replay.run()
    assert replay.taps == [(86, 225), (350, 520), (1537, 653)]
    assert len(replay.swipes) == 10
    assert len(replay.returns) == 1
    assert replay.events[-1][0] == 'return'
    for event in replay.events:
        if event[0] == 'swipe':
            assert event[2] == (1090, 900, 1090, 300, 650)
            assert any(previous[0] == 'match' and previous[1] == event[1]
                       and previous[2] == 'settings_advanced_tab.png' and previous[4] is not None
                       for previous in replay.events)


@pytest.mark.parametrize('states', [('unchecked',), ('checked', 'unchecked')])
def test_unchecked_or_manually_cancelled_during_recheck_is_not_toggled(monkeypatch, states):
    replay = Replay(monkeypatch, states)
    replay.run()
    assert replay.taps == [(86, 225), (350, 520)]
    assert len(replay.returns) == 1


def test_recheck_refreshes_label_and_checkbox_position(monkeypatch):
    # 滑动后的验证帧、状态初查、点击前复查、点击后确认。
    replay = Replay(monkeypatch, labels=[(1200, 650), (1200, 650), (1250, 710), (1250, 710)])
    replay.run()
    assert replay.taps[-1] == (1587, 713)
    assert (1537, 653) not in replay.taps


@pytest.mark.parametrize('phase, original, fresh, index', [
    ('main', (86, 225), (100, 235), 0),
    ('settings', (350, 520), (370, 530), 1),
])
def test_button_disappearing_on_recheck_is_observed_again_with_fresh_center(
        monkeypatch, phase, original, fresh, index):
    replay = Replay(monkeypatch, ('unchecked',),
                    button_centers={phase: [original, None, fresh, fresh]})
    replay.run()
    assert replay.taps[index] == fresh
    assert original not in replay.taps
    assert len(replay.returns) == 1


def test_click_that_does_not_clear_checkbox_is_never_repeated(monkeypatch):
    replay = Replay(monkeypatch, ('checked',))
    with pytest.raises((VisualError, TimeoutError)):
        replay.run()
    assert replay.taps == [(86, 225), (350, 520), (1537, 653)]
    assert not replay.returns


@pytest.mark.parametrize('state', ['unknown', 'ambiguous', 'missing-label'])
def test_uncertain_state_never_clicks_checkbox_or_returns(monkeypatch, state):
    replay = Replay(monkeypatch, (state,))
    with pytest.raises((VisualError, TimeoutError)):
        replay.run()
    assert replay.taps == [(86, 225), (350, 520)]
    assert len(replay.swipes) == replay.action['scroll_swipes']
    assert not replay.returns


@pytest.mark.parametrize('state', ['unknown', 'ambiguous', 'missing-label'])
def test_uncertainty_on_preclick_frame_discards_earlier_checked_state(monkeypatch, state):
    replay = Replay(monkeypatch, ('checked', 'checked', state))
    with pytest.raises((VisualError, TimeoutError)):
        replay.run()
    assert replay.taps == [(86, 225), (350, 520)]
    assert not replay.returns


def test_disappearing_advanced_tab_stops_further_swipes(monkeypatch):
    replay = Replay(monkeypatch, advanced_disappears_after=1)
    with pytest.raises((VisualError, TimeoutError)):
        replay.run()
    assert len(replay.swipes) == 1
    assert replay.taps == [(86, 225), (350, 520)]
    assert not replay.returns


@pytest.mark.parametrize('size', [(960, 540), (1080, 1920)])
def test_unsupported_resolution_stops_before_any_input(monkeypatch, size):
    replay = Replay(monkeypatch, size=size)
    with pytest.raises(VisualError):
        replay.run()
    assert not replay.taps and not replay.swipes and not replay.returns


def test_late_first_screenshot_cannot_authorize_input(monkeypatch):
    replay = Replay(monkeypatch, late_screenshot=lambda device: setattr(device, 'clock', 6.))
    with pytest.raises((VisualError, TimeoutError)):
        replay.run()
    assert not replay.taps and not replay.swipes and not replay.returns


def test_shared_deadline_stops_after_late_checkbox_recheck(monkeypatch):
    def expire_on_recheck(device):
        if device.phase == 'advanced' and len(device.swipes) == device.action['scroll_swipes']:
            if not device.states:  # 第二次目标框截图已获得坐标，但总期限刚好到达。
                device.clock = 20.

    replay = Replay(monkeypatch, ('checked', 'checked', 'checked'), late_screenshot=expire_on_recheck)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(10., clock=lambda: replay.clock, sleep=replay.sleep):
            replay.run()
    assert replay.taps == [(86, 225), (350, 520)]
    assert not replay.returns


@pytest.mark.parametrize('states, checkbox_taps', [
    (('unchecked',), []),
    (('checked', 'checked', 'checked', 'unchecked'), [(1537, 653)]),
])
def test_real_templates_ignore_checked_box_on_another_row(monkeypatch, states, checkbox_taps):
    replay = Replay(monkeypatch, states, real_templates=True)
    replay.run()
    assert replay.taps == [(86, 225), (350, 520), *checkbox_taps]
    assert len(replay.returns) == 1
