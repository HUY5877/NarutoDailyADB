import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.recruit import handler


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Device:
    def __init__(self, clock):
        self.clock = clock
        self.frames = []
        self.swipes = []
        self.taps = []

    def screenshot(self):
        self.clock.sleep(.1)
        frame = f'frame-{len(self.frames) + 1}'.encode()
        self.frames.append(frame)
        return frame

    def swipe(self, *gesture):
        self.swipes.append(gesture)
        self.clock.sleep(gesture[-1] / 1000)

    def tap(self, *point):
        self.taps.append(point)


def configuration(timeout=60):
    return {
        'timeout_seconds': timeout, 'poll_seconds': 1,
        'ordinary_tab': {
            'name': '普通招募', 'template': 'offline.png',
            'left': 0, 'top': 200, 'right': 300, 'bottom': 1030, 'threshold': .9,
        },
        'ordinary_red_dot': {'top': 800, 'bottom': 980},
    }


def test_ordinary_search_keeps_observing_after_swipe_cap_without_more_input(monkeypatch):
    clock = Clock()
    device = Device(clock)
    monkeypatch.setattr(handler.time, 'monotonic', clock)

    def matcher(png, *args):
        return ((128, 700), .99) if png == b'frame-20' else (None, .2)

    with execution_scope(60, clock=clock, sleep=clock.sleep):
        images = ImageRecognition(device, matcher, sleep=clock.sleep)
        tab, guard = handler._find_ordinary_targets(images, configuration())

    assert len(device.swipes) == 8
    assert device.frames[-1] == b'frame-20'
    assert tab['top'] < 700 < tab['bottom']
    assert guard['bottom'] < 700
    assert device.taps == []


def test_ordinary_search_waits_to_original_deadline_without_extra_swipes(monkeypatch):
    clock = Clock()
    device = Device(clock)
    monkeypatch.setattr(handler.time, 'monotonic', clock)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(30, clock=clock, sleep=clock.sleep):
        images = ImageRecognition(device, lambda *args: (None, .2), sleep=clock.sleep)
        handler._find_ordinary_targets(images, configuration())

    assert len(device.swipes) == 8
    assert clock.now == pytest.approx(30)
    assert device.taps == []


def test_ordinary_search_uses_shared_budget_instead_of_legacy_stage_cap(monkeypatch):
    clock = Clock()
    device = Device(clock)
    monkeypatch.setattr(handler.time, 'monotonic', clock)

    def matcher(png, *args):
        return ((128, 700), .99) if png == b'frame-4' else (None, .2)

    with execution_scope(10, clock=clock, sleep=clock.sleep):
        images = ImageRecognition(device, matcher, sleep=clock.sleep)
        handler._find_ordinary_targets(images, configuration(timeout=.5))

    assert device.frames[-1] == b'frame-4'
    assert len(device.swipes) == 1
    assert device.taps == []


def test_recruit_button_wait_uses_shared_deadline_instead_of_short_phase_cap():
    clock = Clock()
    device = Device(clock)
    target = configuration()['ordinary_tab']

    def matcher(frame, *args):
        return ((128, 700), .99) if len(device.frames) >= 4 else (None, .2)

    with execution_scope(10, clock=clock, sleep=clock.sleep):
        clicked = handler._click_confirmed_target(
            device, target, images=ImageRecognition(device, matcher),
            timeout_seconds=.1, poll_seconds=1, pre_click_wait=1, post_click_wait=1,
        )

    assert clicked is True and device.taps == [(128, 700)]
    assert device.frames[-1] == b'frame-5'


def test_recruit_button_unconfirmed_stops_at_original_deadline_without_input():
    clock = Clock()
    device = Device(clock)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(5, clock=clock, sleep=clock.sleep):
        handler._click_confirmed_target(
            device, configuration()['ordinary_tab'], images=ImageRecognition(device, lambda *args: (None, 0)),
            timeout_seconds=.1, poll_seconds=1, pre_click_wait=1, post_click_wait=1,
        )

    assert clock() == 5 and not device.taps
