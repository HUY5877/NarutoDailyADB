"""Secret room/result observations share the existing execution deadline."""
import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.secret_realm import handler
from test_execution import Clock


def target(name):
    return dict(name=name, template=name + '.png', left=0, top=0,
                right=1920, bottom=1080, threshold=.9)


def action(**overrides):
    config = dict(timeout_seconds=.1, pre_click_wait_seconds=0,
                  post_click_wait_seconds=0, poll_seconds=1)
    for name in ('deploy_button', 'create_room', 'result_marker', 'result_return', 'reward_overlay'):
        config[name] = target(name)
    return dict(config, **overrides)


class Device:
    def __init__(self):
        self.frames = []
        self.taps = []

    def screenshot(self):
        frame = str(len(self.frames) + 1).encode()
        self.frames.append(frame)
        return frame

    def tap(self, *point):
        self.taps.append(point)


WAITS = [handler._enter_room, handler.finish_secret_result]


@pytest.mark.parametrize('wait', WAITS, ids=['room', 'result'])
def test_direct_secret_wait_observes_past_local_timeout_until_room(wait):
    clock, device = Clock(), Device()

    def matcher(frame, path, *args):
        return ((1700, 900), 1) if path.stem == 'deploy_button' and clock() >= 2 else (None, 0)

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        wait(device, ImageRecognition(device, matcher), action())

    assert clock() == 2
    assert device.frames == [b'1', b'2', b'3'] and device.taps == []


@pytest.mark.parametrize('wait', WAITS, ids=['room', 'result'])
def test_unknown_secret_wait_stops_at_shared_deadline_without_inputs(wait):
    clock, device = Clock(), Device()
    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=clock, sleep=clock.sleep):
        wait(device, ImageRecognition(device, lambda *args: (None, 0)), action())

    assert clock() == 3 and device.frames == [b'1', b'2', b'3']
    assert device.taps == []


@pytest.mark.parametrize('wait', WAITS, ids=['room', 'result'])
def test_secret_wait_without_scope_preserves_local_timeout(wait):
    clock, device = Clock(), Device()
    images = ImageRecognition(device, lambda *args: (None, 0), sleep=clock.sleep)
    with pytest.raises(VisualError):
        wait(device, images, action())

    assert clock() == .1 and device.frames == [b'1']
    assert device.taps == []


@pytest.mark.parametrize('page', ['result', 'ninja'])
def test_secret_result_insufficient_click_wait_reaches_shared_deadline(monkeypatch, page):
    clock, device = Clock(), Device()
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda frame: (150, 1000) if page == 'ninja' else None)

    def matcher(frame, path, *args):
        return ((200, 1000), 1) if page == 'result' and path.stem in {'result_marker', 'result_return'} else (None, 0)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=clock, sleep=clock.sleep):
        handler.finish_secret_result(device, ImageRecognition(device, matcher),
                                     action(pre_click_wait_seconds=5))

    assert clock() == 3 and device.frames == [b'1'] and device.taps == []


def result_matcher(pages):
    def matcher(frame, path, *args):
        visible = pages.get(int(frame), set())
        points = dict(deploy_button=(1700, 900), result_return=(200, 1000),
                      result_marker=(500, 800), reward_overlay=(960, 260))
        return (points[path.stem], 1) if path.stem in visible else (None, 0)
    return matcher


def test_confirmed_secret_result_unknown_new_foreground_is_only_observed():
    clock, device = Clock(), Device()
    matcher = result_matcher({3: {'deploy_button'}})
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        handler.finish_secret_result(device, ImageRecognition(device, matcher),
                                     action(timeout_seconds=30), result_confirmed=True)

    assert device.frames == [b'1', b'2', b'3'] and device.taps == []


def test_secret_return_click_is_not_followed_by_blank_on_unknown_foreground():
    clock, device = Clock(), Device()
    matcher = result_matcher({1: {'result_return'}, 2: {'result_return'}, 5: {'deploy_button'}})
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        handler.finish_secret_result(device, ImageRecognition(device, matcher),
                                     action(timeout_seconds=30, pre_click_wait_seconds=1),
                                     result_confirmed=True)

    assert device.frames == [b'1', b'2', b'3', b'4', b'5']
    assert device.taps == [(200, 1000)]


@pytest.mark.parametrize('visible', [{'result_return'}, {'reward_overlay'}], ids=['result', 'reward'])
def test_secret_confirmed_current_result_layer_can_still_be_dismissed(visible):
    clock, device = Clock(), Device()
    pages = ({1: visible, 2: {'deploy_button'}} if 'reward_overlay' in visible else
             {1: visible, 2: visible, 3: visible, 4: {'deploy_button'}})
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        handler.finish_secret_result(device, ImageRecognition(device, result_matcher(pages)),
                                     action(timeout_seconds=30), result_confirmed=True)

    assert device.taps == ([(960, 900)] if 'reward_overlay' in visible else [(200, 1000), (960, 900)])


def test_secret_unknown_foreground_preserves_legacy_blank_rule_without_scope():
    clock, device = Clock(), Device()
    matcher = result_matcher({3: {'deploy_button'}})
    handler.finish_secret_result(device, ImageRecognition(device, matcher, sleep=clock.sleep),
                                 action(timeout_seconds=30), result_confirmed=True)

    assert device.taps == [(960, 900)]
