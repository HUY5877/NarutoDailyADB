"""奖励入口坐标只在本次任务内复用，其他入口和页面确认仍走原流程。"""
from collections import deque
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from naruto_daily.common import main_screen, navigation
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.runner import ActionRunner


POINT = (1845, 537)


def png(width=1920, height=1080):
    return cv2.imencode('.png', np.zeros((height, width, 3), np.uint8))[1].tobytes()


def target(menu='rewards'):
    return dict(name=menu, main_menu=menu, multimodal=dict(timeout_seconds=30, max_calls=3))


def action():
    return dict(target(), type='main_menu_tap', timeout_seconds=30,
                poll_seconds=1, pre_click_wait_seconds=1, post_click_wait_seconds=1)


def fake_recognizer(monkeypatch, replies=()):
    calls, answers = [], deque(replies)

    class Recognizer:
        def __init__(self, *args):
            pass

        def match(self, image, current, timeout_seconds=None):
            calls.append((image, current['main_menu']))
            result = answers.popleft() if answers else (POINT, .8)
            if isinstance(result, Exception):
                raise result
            return result() if callable(result) else result

    monkeypatch.setattr(main_screen, 'MainScreenRecognition', Recognizer)
    return calls


def device(image):
    taps = []
    return SimpleNamespace(serial='same-serial', screenshot=lambda: image,
                           tap=lambda *point: taps.append(point), taps=taps)


def test_independent_recognizer_reuses_reward_without_more_model_or_template_calls(monkeypatch, caplog):
    calls = fake_recognizer(monkeypatch)
    frame = png()
    images = ImageRecognition(device(frame), matcher=lambda *args: pytest.fail('缓存不应调用模板'))
    assert images.match(frame, target()) == (POINT, .8)
    images.main_call_limit = 1
    with caplog.at_level('INFO', logger='naruto_daily'):
        assert images.match(frame, target()) == (POINT, .8)
    assert len(calls) == 1 and images.main_calls == {'查找': 1, '点击前复查': 0}
    assert '复用本次任务奖励入口坐标' in caplog.text


def test_cross_action_cache_still_confirms_reward_page_after_every_click(monkeypatch):
    calls = fake_recognizer(monkeypatch)
    frame, destinations = png(), []
    client = device(frame)
    monkeypatch.setattr(navigation, 'reward_card_swipe_starts',
                        lambda image: destinations.append(image) or [(100, 100)])
    result = ActionRunner(client, sleep=lambda _: None).run([action(), action()])
    assert result.action_count == 2
    assert client.taps == [POINT, POINT]
    assert len(calls) == 1
    assert destinations == [frame, frame]


@pytest.mark.parametrize('first_run_fails', [False, True])
def test_same_client_starts_with_empty_cache_on_next_task(monkeypatch, first_run_fails):
    calls = fake_recognizer(monkeypatch)
    client = device(png())
    monkeypatch.setattr(navigation, 'reward_card_swipe_starts', lambda _: [(100, 100)])
    runner = ActionRunner(client, sleep=lambda _: None)
    if first_run_fails:
        with pytest.raises(ValueError, match='不支持'):
            runner.run([action(), dict(type='invalid-action')])
    else:
        runner.run([action()])
    runner.run([action()])
    assert len(calls) == 2 and client.taps == [POINT, POINT]


def test_nested_same_client_reuses_cache_and_other_client_cannot_overwrite_it(monkeypatch):
    from naruto_daily.common.reward_button_cache import reward_button_cache_scope

    points = [POINT, (1800, 520), (1810, 530)]
    calls = fake_recognizer(monkeypatch, [(point, .8) for point in points])
    frame = png()
    first, other = device(frame), device(frame)  # 即使serial相同，客户端对象也必须隔离。
    with reward_button_cache_scope(first):
        assert ImageRecognition(first).match(frame, target())[0] == points[0]
        with reward_button_cache_scope(first):
            assert ImageRecognition(first).match(frame, target())[0] == points[0]
        with reward_button_cache_scope(other):
            assert ImageRecognition(other).match(frame, target())[0] == points[1]
        assert ImageRecognition(first).match(frame, target())[0] == points[0]
    assert ImageRecognition(first).match(frame, target())[0] == points[2]
    assert len(calls) == 3


def test_local_fallback_caches_are_not_shared_between_independent_recognizers(monkeypatch):
    calls = fake_recognizer(monkeypatch)
    frame = png()
    client = device(frame)
    first, second = ImageRecognition(client), ImageRecognition(client)
    first.match(frame, target())
    second.match(frame, target())
    first.match(frame, target())
    second.match(frame, target())
    assert len(calls) == 2


@pytest.mark.parametrize('menu', ['home', 'activities', 'equipment', 'equipment_submenu'])
def test_only_reward_entry_is_cached(monkeypatch, menu):
    calls = fake_recognizer(monkeypatch, [((100, 100), .8), ((200, 200), .9)])
    frame = png()
    images = ImageRecognition(device(frame))
    assert images.match(frame, target(menu))[0] == (100, 100)
    assert images.match(frame, target(menu))[0] == (200, 200)
    assert len(calls) == 2


def test_changed_image_size_clears_old_position_even_if_new_recognition_fails(monkeypatch):
    calls = fake_recognizer(monkeypatch, [(POINT, .8), (None, .3), ((1810, 520), .9)])
    frame = png()
    images = ImageRecognition(device(frame))
    assert images.match(frame, target())[0] == POINT
    assert images.match(png(1280, 720), target())[0] is None
    assert images.match(frame, target())[0] == (1810, 520)
    assert len(calls) == 3


@pytest.mark.parametrize('failure', [(None, .3), VisualError('候选未通过复核')])
def test_unconfirmed_reward_result_is_never_cached(monkeypatch, failure):
    calls = fake_recognizer(monkeypatch, [failure, (POINT, .8)])
    frame = png()
    images = ImageRecognition(device(frame))
    if isinstance(failure, Exception):
        with pytest.raises(VisualError):
            images.match(frame, target())
    else:
        assert images.match(frame, target())[0] is None
    assert images.match(frame, target()) == (POINT, .8)
    assert images.match(frame, target()) == (POINT, .8)
    assert len(calls) == 2


@pytest.mark.parametrize('timeout', [0, -1])
def test_cached_reward_cannot_bypass_standalone_timeout(monkeypatch, timeout):
    calls = fake_recognizer(monkeypatch)
    frame = png()
    images = ImageRecognition(device(frame))
    images.match(frame, target())
    with pytest.raises(VisualError, match='超时'):
        images.match(frame, target(), timeout_seconds=timeout)
    assert len(calls) == 1


def test_cached_reward_cannot_bypass_task_deadline(monkeypatch):
    calls = fake_recognizer(monkeypatch)
    frame, now = png(), [0.]
    images = ImageRecognition(device(frame))
    images.match(frame, target())
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(1, clock=lambda: now[0]):
            now[0] = 1
            images.match(frame, target())
    assert len(calls) == 1


def test_expired_model_result_does_not_seed_a_later_cache_hit(monkeypatch):
    now = [0.]
    def late():
        now[0] = 1
        return POINT, .8
    calls = fake_recognizer(monkeypatch, [late, ((1810, 520), .9)])
    frame = png()
    images = ImageRecognition(device(frame))
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(1, clock=lambda: now[0]):
            images.match(frame, target())
    assert images.match(frame, target())[0] == (1810, 520)
    assert len(calls) == 2


def test_invalid_image_never_uses_cached_coordinates(monkeypatch):
    calls = fake_recognizer(monkeypatch)
    frame = png()
    images = ImageRecognition(device(frame))
    images.match(frame, target())
    with pytest.raises(VisualError, match='截图'):
        images.match(b'not-a-png', target())
    assert len(calls) == 1
