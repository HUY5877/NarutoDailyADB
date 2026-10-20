"""决斗场入口逐帧观察、复查和操作边界，不连接设备或模型服务。"""
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.arena.entry import enter_arena
from naruto_daily.common import model_workflow
from naruto_daily.common.execution import execution_scope
from naruto_daily.common.visual import NonRetryableVisualError, VisualError
from naruto_daily.multimodal.client import MultimodalRequestError
from naruto_daily.multimodal.prompt_jev import ARENA_ENTRY_PROMPT


ABSENT = {'page': 'main', 'arena_visible': False}
VISIBLE = {'page': 'main', 'arena_visible': True}


def entry_position(x=950, y=550):
    return {'page': 'main', 'name': '决斗场', 'center': [x, y]}


def action_config(**overrides):
    target = dict(left=0, top=0, right=1920, bottom=1080, threshold=.85)
    return {
        'timeout_seconds': 5, 'poll_seconds': .01,
        'pre_click_wait_seconds': .01, 'post_click_wait_seconds': .01,
        'max_swipes': 15, 'multimodal': {'max_calls': 1},
        'battle_entry': target | {'name': '忍术对战图标', 'template': 'assets/arena_ninjutsu_battle.png'},
        'ready_marker': target | {'name': '好友切磋', 'template': 'assets/arena_friendly_match.png'},
    } | overrides


class Replay:
    """每次截图取得新帧；两阶段识别必须使用同一个 PNG 对象。"""

    def __init__(self, monkeypatch, observations, *, frames=(), matches=None, action=None):
        self.action = action or action_config()
        self.clock = 0.0
        self.events, self.calls, self.taps, self.swipes = [], [], [], []
        self.observations = deque(observations)
        self.frames = deque([name for phase, name, result in observations if phase == 'gate'] + list(frames))
        self.matches = matches or {}
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
        assert self.last_frame is not None, '流程截图超出脚本范围'
        png = self.png(self.last_frame)
        self.events.append(('screen', self.last_frame))
        return png

    def complete_json(self, png, prompt, *, timeout_seconds):
        assert timeout_seconds > 0
        assert self.observations, '发生未授权的额外模型调用'
        phase = 'gate' if prompt == ARENA_ENTRY_PROMPT else 'locate'
        expected_phase, expected_frame, result = self.observations.popleft()
        assert (phase, self.names[png]) == (expected_phase, expected_frame)
        self.events.append((phase, expected_frame))
        self.calls.append((phase, expected_frame, png))
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
        center = self.matches.get((name, template))
        return center, .99 if center is not None else .1

    def run(self):
        return enter_arena(self, self.action, model=self,
                           template_matcher=self.matcher, sleep=self.sleep)


def direct_observations():
    return [('gate', 'initial', ABSENT),
            ('gate', 'found', VISIBLE), ('locate', 'found', entry_position()),
            ('gate', 'recheck', VISIBLE), ('locate', 'recheck', entry_position(1000, 600))]


def ready_matches():
    return {('battle-old', 'arena_ninjutsu_battle.png'): (280, 960),
            ('battle-new', 'arena_ninjutsu_battle.png'): (310, 950),
            ('friendly', 'arena_friendly_match.png'): (1750, 480)}


def test_resets_then_gates_each_scene_and_rechecks_both_clicks(monkeypatch):
    observations = [('gate', 'initial', VISIBLE), ('gate', 'empty', ABSENT), *direct_observations()[1:]]
    replay = Replay(monkeypatch, observations, frames=['battle-old', 'battle-new', 'friendly'],
                    matches=ready_matches())
    replay.run()

    significant = [event for event in replay.events if event[0] in {'gate', 'locate', 'swipe', 'tap'}]
    assert significant == [
        ('gate', 'initial'), *[('swipe', 'right')] * 5,
        ('gate', 'empty'), ('swipe', 'left'),
        ('gate', 'found'), ('locate', 'found'),
        ('gate', 'recheck'), ('locate', 'recheck'),
        ('tap', (1000, 600)), ('tap', (310, 950)),
    ]
    for gate, locate in (replay.calls[2:4], replay.calls[4:6]):
        assert gate[2] is locate[2]
    assert replay.calls[2][2] != replay.calls[4][2]
    assert replay.taps == [(1000, 600), (310, 950)]
    assert ('match', 'friendly', 'arena_friendly_match.png') in replay.events
    for x1, y1, x2, y2, duration in replay.swipes:
        assert abs(x2 - x1) >= 1920 * .3 and duration >= 600
        assert y1 == y2 and 1080 * .7 < y1 < 1080 * .85


@pytest.mark.parametrize('last_scene_visible', [False, True])
def test_search_caps_at_fifteen_swipes_and_observes_the_last_frame(monkeypatch, last_scene_visible):
    observations = [('gate', 'initial', ABSENT)]
    observations.extend(('gate', f'scene-{index}', ABSENT) for index in range(15))
    observations.append(('gate', 'scene-15', VISIBLE if last_scene_visible else ABSENT))
    if last_scene_visible:
        observations.extend([('locate', 'scene-15', entry_position()),
                             ('gate', 'recheck', VISIBLE), ('locate', 'recheck', entry_position(1000, 600))])
    replay = Replay(monkeypatch, observations, frames=['battle-old', 'battle-new', 'friendly'],
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
    assert ('gate', 'scene-15') in replay.events
    for index, event in enumerate(replay.events):
        if event == ('swipe', 'left'):
            assert replay.events[index - 1][0] == 'gate'
    assert not replay.observations


@pytest.mark.parametrize('after_reset', [False, True])
@pytest.mark.parametrize('uncertain', [
    {'page': 'unknown', 'arena_visible': False},
    {'page': 'main', 'arena_visible': None},
    {'page': 'main', 'arena_visible': 0},
])
def test_unknown_never_authorizes_scene_swipes_or_generation(monkeypatch, after_reset, uncertain):
    observations = [('gate', 'initial', ABSENT)] if after_reset else []
    observations.extend([('gate', 'unknown-1', uncertain), ('gate', 'unknown-2', uncertain)])
    replay = Replay(monkeypatch, observations, action=action_config(multimodal={'max_calls': 2}))
    with pytest.raises(VisualError, match='识别未确认'):
        replay.run()
    assert len(replay.swipes) == (5 if after_reset else 0)
    assert all(x1 < x2 for x1, _, x2, _, _ in replay.swipes)
    assert replay.taps == []
    assert all(phase == 'gate' for phase, _, _ in replay.calls)
    assert not replay.observations


@pytest.mark.parametrize('center', [
    [True, 550], [950, False], [950.0, 550], [950, 550.5],
    [20, 550], [1900, 550], [950, 50], [950, 1000],
])
def test_invalid_or_toolbar_coordinates_never_reach_adb(monkeypatch, center):
    observations = direct_observations()[:3]
    observations[-1] = ('locate', 'found', entry_position() | {'center': center})
    replay = Replay(monkeypatch, observations)
    with pytest.raises(VisualError, match='识别未确认'):
        replay.run()
    assert len(replay.swipes) == 5 and replay.taps == []


def test_disappearing_arena_does_not_click_the_original_center(monkeypatch):
    observations = direct_observations()[:3] + [('gate', 'disappeared', ABSENT)]
    replay = Replay(monkeypatch, observations, action=action_config(max_swipes=0))
    with pytest.raises(VisualError, match='未确认决斗场入口'):
        replay.run()
    assert replay.taps == []
    assert [phase for phase, _, _ in replay.calls] == ['gate', 'gate', 'locate', 'gate']


def test_disappearing_ninjutsu_icon_is_observed_again_before_click(monkeypatch):
    matches = ready_matches() | {('battle-again', 'arena_ninjutsu_battle.png'): (350, 940),
                                 ('battle-again-check', 'arena_ninjutsu_battle.png'): (370, 930)}
    replay = Replay(monkeypatch, direct_observations(),
                    frames=['battle-old', 'gone', 'battle-again', 'battle-again-check', 'friendly'],
                    matches=matches)
    replay.run()
    assert replay.taps == [(1000, 600), (370, 930)]
    assert ('match', 'gone', 'arena_ninjutsu_battle.png') in replay.events


@pytest.mark.parametrize('call_index', [0, 1, 2, 3, 4])
@pytest.mark.parametrize('failure', ['late', 'timeout'])
def test_late_or_timed_out_model_cannot_trigger_followup_actions(monkeypatch, call_index, failure):
    observations = direct_observations()
    phase, frame, answer = observations[call_index]
    def late(replay):
        replay.clock += replay.action['timeout_seconds'] + 1
        return answer
    observations[call_index] = (phase, frame, late if failure == 'late' else TimeoutError('model timeout'))
    replay = Replay(monkeypatch, observations)
    with pytest.raises((VisualError, TimeoutError)):
        replay.run()
    assert replay.taps == []
    assert len(replay.swipes) == (0 if call_index == 0 else 5)
    assert len(replay.calls) == call_index + 1
    assert not any(event[0] == 'match' for event in replay.events)


def test_missing_friendly_match_does_not_report_entry_success(monkeypatch):
    matches = ready_matches()
    matches.pop(('friendly', 'arena_friendly_match.png'))
    replay = Replay(monkeypatch, direct_observations(), frames=['battle-old', 'battle-new', 'friendly'],
                    matches=matches, action=action_config(timeout_seconds=.25))
    with pytest.raises(VisualError, match='超时'):
        replay.run()
    assert replay.taps == [(1000, 600), (310, 950)]
    assert replay.clock <= .26
    assert all(event[2] == 'arena_friendly_match.png'
               for event in replay.events if event[0] == 'match' and event[1] == 'friendly')


@pytest.mark.parametrize('width,height', [(1600, 900), (1080, 1920)])
def test_wrong_screen_size_stops_before_any_model_or_input(monkeypatch, width, height):
    replay = Replay(monkeypatch, direct_observations())
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    replay.images['initial'] = cv2.imencode('.png', frame)[1].tobytes()
    with pytest.raises(NonRetryableVisualError, match='1920×1080'):
        replay.run()
    assert replay.calls == [] and replay.swipes == [] and replay.taps == []
    assert replay.events == [('screen', 'initial')]


def test_generation_network_retry_waits_and_requires_a_new_jev_authorization(monkeypatch):
    observations = [
        ('gate', 'initial', ABSENT),
        ('gate', 'failed', VISIBLE),
        ('locate', 'failed', MultimodalRequestError('connection lost', retryable=True)),
        ('gate', 'recovered-negative', ABSENT),
        ('gate', 'found', VISIBLE), ('locate', 'found', entry_position()),
        ('gate', 'recheck', VISIBLE), ('locate', 'recheck', entry_position(1000, 600)),
    ]
    replay = Replay(monkeypatch, observations, frames=['battle-old', 'battle-new', 'friendly'],
                    matches=ready_matches(), action=action_config(multimodal={'max_calls': 1}))
    with execution_scope(50, clock=lambda: replay.clock, sleep=replay.sleep):
        replay.run()

    failed_index = replay.events.index(('locate', 'failed'))
    assert replay.events[failed_index:failed_index + 6] == [
        ('locate', 'failed'), ('sleep', 10),
        ('screen', 'recovered-negative'), ('gate', 'recovered-negative'),
        ('swipe', 'left'), ('sleep', .01),
    ]
    assert not any(phase == 'locate' and frame == 'recovered-negative'
                   for phase, frame, _ in replay.calls)
    assert replay.calls[1][2] is replay.calls[2][2]
    assert replay.calls[2][2] != replay.calls[3][2]
    assert len(replay.swipes) == 6 and replay.taps == [(1000, 600), (310, 950)]
    assert sum(event == ('sleep', 10) for event in replay.events) == 1
    assert not replay.observations
