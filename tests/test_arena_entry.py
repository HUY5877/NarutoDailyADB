"""决斗场入口逐帧定位和操作边界，不连接设备或模型服务。"""
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.arena.entry import enter_arena
from naruto_daily.common import model_workflow
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.visual import NonRetryableVisualError, VisualError
from naruto_daily.multimodal.client import MultimodalRequestError
from naruto_daily.multimodal.prompt_jev import ChoicePrompt


def entry_position(x=1000, y=600):
    return {'page': 'main', 'name': '决斗场', 'center': [x, y]}


def action_config(**overrides):
    target = dict(left=0, top=0, right=1920, bottom=1080, threshold=.85)
    return {
        'timeout_seconds': 30, 'poll_seconds': .01,
        'pre_click_wait_seconds': .01, 'post_click_wait_seconds': .01,
        'max_swipes': 15, 'multimodal': {'max_calls': 1},
        'battle_entry': target | {'name': '忍术对战图标', 'template': 'assets/arena_ninjutsu_battle.png'},
        'ready_marker': target | {'name': '好友切磋', 'template': 'assets/arena_friendly_match.png'},
    } | overrides


class Replay:
    """为归位、搜索和到达校验提供独立画面，并记录设备动作。"""

    def __init__(self, monkeypatch, observations, *, frames=(), matches=None, action=None):
        self.action = action or action_config()
        self.clock = 0.0
        self.events, self.taps, self.swipes = [], [], []
        self.observations = deque(observations)
        self.frames = deque(['initial', *(name for name, _ in observations), *frames])
        self.matches = matches or {}
        self.match_regions = {}
        self.images = {}
        self.names = {}
        self.last_frame = None
        monkeypatch.setattr(model_workflow.time, 'monotonic', lambda: self.clock)

    def png(self, name):
        if name not in self.images:
            frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
            frame[0, 0] = (len(self.images) + 1, 30, 90)
            self.images[name] = cv2.imencode('.png', frame)[1].tobytes()
            self.names[self.images[name]] = name
        return self.images[name]

    def screenshot(self):
        if self.frames:
            self.last_frame = self.frames.popleft()
        png = self.png(self.last_frame)
        self.events.append(('screen', self.last_frame))
        return png

    def complete_json(self, png, prompt, *, timeout_seconds):
        assert timeout_seconds > 0
        assert not isinstance(prompt, ChoicePrompt), '查找入口只需调用坐标模型'
        assert self.observations, '发生额外的模型调用'
        expected_frame, result = self.observations.popleft()
        assert self.names[png] == expected_frame
        self.events.append(('locate', expected_frame))
        if isinstance(result, Exception):
            raise result
        return result(self) if callable(result) else result

    def swipe(self, *points):
        self.swipes.append(points)
        self.events.append(('swipe', 'right' if points[0] < points[2] else 'left'))

    def tap(self, *point):
        self.taps.append(point)
        self.events.append(('tap', point))

    def sleep(self, seconds):
        self.events.append(('sleep', seconds))
        self.clock += seconds

    def matcher(self, png, path, roi, threshold):
        name, template = self.names[png], Path(path).name
        self.events.append(('match', name, template))
        self.match_regions.setdefault(template, set()).add(roi)
        center = self.matches.get((name, template))
        return center, .99 if center is not None else .1

    def run(self):
        return enter_arena(self, self.action, model=self,
                           template_matcher=self.matcher, sleep=self.sleep)


def ready_matches():
    return {('battle-old', 'arena_ninjutsu_battle.png'): (280, 960),
            ('battle-new', 'arena_ninjutsu_battle.png'): (310, 950),
            ('friendly', 'arena_friendly_match.png'): (1750, 480)}


NO_TITLE_FRAMES = ('no-title',) * 5
BATTLE_FRAMES = ('battle-old', 'battle-new', 'friendly')
READY_FRAMES = NO_TITLE_FRAMES + BATTLE_FRAMES


def test_first_search_frame_uses_only_coordinate_model(monkeypatch):
    replay = Replay(monkeypatch, [('found', entry_position())],
                    frames=READY_FRAMES, matches=ready_matches(),
                    action=action_config(pre_click_wait_seconds=.5))
    replay.run()
    actions = [event for event in replay.events if event[0] != 'sleep']
    assert actions[:9] == [
        ('screen', 'initial'), *[('swipe', 'right')] * 5,
        ('screen', 'found'), ('locate', 'found'), ('tap', (1000, 600)),
    ]
    located = replay.events.index(('locate', 'found'))
    assert replay.events[located + 1] == ('tap', (1000, 600))
    assert replay.taps == [(1000, 600), (310, 950)]
    assert ('match', 'friendly', 'arena_friendly_match.png') in replay.events
    assert not replay.frames and not replay.observations
    for x1, y1, x2, y2, duration in replay.swipes:
        assert x2 - x1 >= 1920 * .3 and duration >= 600
        assert y1 == y2 and 1080 * .7 < y1 < 1080 * .85


@pytest.mark.parametrize('unconfirmed', [
    pytest.param({'page': 'unknown'}, id='unknown'),
    pytest.param(entry_position() | {'page': 'unknown'}, id='wrong-page-with-coordinate'),
    pytest.param(entry_position() | {'name': '丰饶之间'}, id='wrong-name'),
    pytest.param({'page': 'main', 'name': '决斗场'}, id='missing-center'),
    pytest.param(entry_position() | {'center': None}, id='null-center'),
    pytest.param(entry_position() | {'center': [950]}, id='incomplete-coordinate'),
    pytest.param(entry_position(True, 550), id='boolean-x'),
    pytest.param(entry_position(950, False), id='boolean-y'),
    pytest.param(entry_position(950.0, 550), id='float-x'),
    pytest.param(entry_position(950, 550.5), id='float-y'),
    pytest.param(entry_position(20, 550), id='left-edge'),
    pytest.param(entry_position(1900, 550), id='right-toolbar'),
    pytest.param(entry_position(950, 50), id='top-toolbar'),
    pytest.param(entry_position(950, 1000), id='bottom-toolbar'),
])
def test_unconfirmed_position_swipes_left_then_locates_on_new_frame(monkeypatch, unconfirmed):
    replay = Replay(monkeypatch, [('missed', unconfirmed), ('next', entry_position(1111, 601))],
                    frames=READY_FRAMES, matches=ready_matches())
    replay.run()
    start = replay.events.index(('locate', 'missed'))
    actions = [event for event in replay.events[start:] if event[0] != 'sleep']
    assert actions[:5] == [
        ('locate', 'missed'), ('swipe', 'left'), ('screen', 'next'),
        ('locate', 'next'), ('tap', (1111, 601)),
    ]
    assert replay.taps == [(1111, 601), (310, 950)]
    assert len(replay.swipes) == 6 and not replay.observations
    x1, y1, x2, y2, duration = replay.swipes[-1]
    assert x1 - x2 >= 1920 * .3 and duration >= 600
    assert y1 == y2 and 1080 * .7 < y1 < 1080 * .85


@pytest.mark.parametrize('last_scene_visible', [False, True])
def test_search_caps_at_fifteen_swipes_and_observes_the_last_frame(monkeypatch, last_scene_visible):
    observations = [(f'scene-{index}', {'page': 'unknown'}) for index in range(15)]
    observations.append(('scene-15', entry_position() if last_scene_visible else {'page': 'unknown'}))
    replay = Replay(monkeypatch, observations, frames=READY_FRAMES,
                    matches=ready_matches(), action=action_config(max_swipes=100))
    if last_scene_visible:
        replay.run()
        assert replay.taps == [(1000, 600), (310, 950)]
    else:
        with pytest.raises(VisualError, match='15'):
            replay.run()
        assert replay.taps == []
    assert len(replay.swipes) == 20
    assert sum(x1 > x2 for x1, _, x2, _, _ in replay.swipes) == 15
    assert ('locate', 'scene-15') in replay.events
    assert not replay.observations


def test_respects_smaller_configured_search_distance(monkeypatch):
    replay = Replay(monkeypatch, [('first', {'page': 'unknown'}), ('last', {'page': 'unknown'})],
                    action=action_config(max_swipes=1))
    with pytest.raises(VisualError, match='1'):
        replay.run()
    assert replay.swipes[-1][0] > replay.swipes[-1][2]
    assert len(replay.swipes) == 6 and not replay.observations and replay.taps == []


def test_retryable_coordinate_request_moves_to_next_scene_without_backoff(monkeypatch):
    error = MultimodalRequestError('model timed out', retryable=True)
    replay = Replay(monkeypatch, [('request-failed', error), ('next', entry_position())],
                    frames=READY_FRAMES, matches=ready_matches())
    with execution_scope(50, clock=lambda: replay.clock, sleep=replay.sleep):
        replay.run()
    start = replay.events.index(('locate', 'request-failed'))
    assert replay.events[start:start + 5] == [
        ('locate', 'request-failed'), ('swipe', 'left'), ('sleep', .01),
        ('screen', 'next'), ('locate', 'next'),
    ]
    assert replay.taps == [(1000, 600), (310, 950)]


def test_nonretryable_request_stops_without_advancing(monkeypatch):
    error = MultimodalRequestError('request stopped', retryable=False)
    replay = Replay(monkeypatch, [('first', error)])
    with execution_scope(50, clock=lambda: replay.clock, sleep=replay.sleep):
        with pytest.raises(MultimodalRequestError):
            replay.run()
    assert replay.events[-1] == ('locate', 'first')
    assert len(replay.swipes) == 5 and replay.taps == []


@pytest.mark.parametrize('answer', [entry_position(), {'page': 'unknown'}])
def test_result_after_daily_deadline_never_causes_input(monkeypatch, answer):
    def late(replay):
        replay.clock += 6
        return answer

    replay = Replay(monkeypatch, [('late', late)])
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(5, clock=lambda: replay.clock, sleep=replay.sleep):
            replay.run()
    assert len(replay.swipes) == 5 and replay.taps == []
    assert replay.events[-1] == ('locate', 'late')


def test_title_popups_require_five_consecutive_misses_before_battle(monkeypatch):
    title = 'arena_title_confirm.png'
    confirmations = {'title-a': (1170, 852), 'title-b': (1190, 870), 'title-c': (1210, 890)}
    probes = (('no-title',) * 4 + ('title-a', 'title-b') +
              ('no-title',) * 4 + ('title-c',) + NO_TITLE_FRAMES)
    matches = ready_matches() | {(frame, title): point for frame, point in confirmations.items()}
    replay = Replay(monkeypatch, [('found', entry_position())], frames=probes + BATTLE_FRAMES, matches=matches)
    replay.run()

    start = replay.events.index(('tap', (1000, 600))) + 1
    end = replay.events.index(('screen', 'battle-old'))
    expected = [('sleep', replay.action['post_click_wait_seconds'])]
    for frame in probes:
        expected.extend([('sleep', 1), ('screen', frame), ('match', frame, title)])
        if frame in confirmations:
            expected.append(('tap', confirmations[frame]))
    assert replay.events[start:end] == expected
    assert replay.match_regions[title] == {(0, 540, 1920, 1080)}
    assert replay.taps == [(1000, 600), *confirmations.values(), (310, 950)]


def test_title_match_after_daily_deadline_never_taps_confirm(monkeypatch):
    replay = Replay(monkeypatch, [('found', entry_position())], frames=['title'],
                    matches={('title', 'arena_title_confirm.png'): (1200, 870)})
    matcher = replay.matcher

    def late_match(png, path, roi, threshold):
        result = matcher(png, path, roi, threshold)
        if result[0] is not None:
            replay.clock += 5
        return result

    monkeypatch.setattr(replay, 'matcher', late_match)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(5, clock=lambda: replay.clock, sleep=replay.sleep):
            replay.run()
    assert replay.taps == [(1000, 600)]
    assert replay.events[-1] == ('match', 'title', 'arena_title_confirm.png')


def test_disappearing_ninjutsu_icon_is_observed_again_before_click(monkeypatch):
    matches = ready_matches() | {('battle-again', 'arena_ninjutsu_battle.png'): (350, 940),
                                 ('battle-again-check', 'arena_ninjutsu_battle.png'): (370, 930)}
    replay = Replay(monkeypatch, [('found', entry_position())],
                    frames=NO_TITLE_FRAMES + ('battle-old', 'gone', 'battle-again',
                                              'battle-again-check', 'friendly'),
                    matches=matches)
    replay.run()
    assert replay.taps == [(1000, 600), (370, 930)]
    assert ('match', 'gone', 'arena_ninjutsu_battle.png') in replay.events


def test_missing_friendly_match_does_not_report_entry_success(monkeypatch):
    matches = ready_matches()
    matches.pop(('friendly', 'arena_friendly_match.png'))
    replay = Replay(monkeypatch, [('found', entry_position())], frames=READY_FRAMES,
                    matches=matches, action=action_config(timeout_seconds=5.25))
    with pytest.raises(VisualError, match='超时'):
        replay.run()
    assert replay.taps == [(1000, 600), (310, 950)]
    assert replay.clock <= 5.26
    assert all(event[2] == 'arena_friendly_match.png'
               for event in replay.events if event[0] == 'match' and event[1] == 'friendly')


@pytest.mark.parametrize('width,height', [(1600, 900), (1080, 1920)])
def test_wrong_screen_size_stops_before_any_model_or_input(monkeypatch, width, height):
    replay = Replay(monkeypatch, [])
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    replay.images['initial'] = cv2.imencode('.png', frame)[1].tobytes()
    with pytest.raises(NonRetryableVisualError, match='1920×1080'):
        replay.run()
    assert replay.events == [('screen', 'initial')]
