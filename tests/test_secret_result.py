from pathlib import Path

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.config import load_settings


ROOT = Path(__file__).parents[1]


def action():
    return next(a for a in load_settings(ROOT / 'config.example.json').tasks['secret_realm']
                if a['type'] == 'handle_secret_realm')


def test_result_screenshot_matches_end_marker_and_return_button():
    config = action()
    png = (ROOT / 'tests/fixtures/secret_result.png').read_bytes()
    for key in ('result_marker', 'result_return'):
        target = config[key]
        center, score = find_template_in_png(png, ROOT / target['template'],
            tuple(target[k] for k in ('left', 'top', 'right', 'bottom')), target['threshold'])
        assert center is not None and score > .99
    assert config['result_marker'] in config['battle']['end_targets']


class Client:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.last = None
        self.taps = []

    def screenshot(self):
        self.last = next(self.frames, self.last)
        return self.last

    def tap(self, x, y):
        self.taps.append((x, y))


def matcher(frame, path, roi, threshold):
    points = {'secret_deploy': (1787, 920), 'secret_create_room': (1443, 791),
              'secret_result_marker': (537, 837), 'secret_result_return': (209, 1010)}
    return (points[path.stem], 1) if path.stem in frame else (None, .1)


RESULT = {'secret_result_marker', 'secret_result_return'}
ROOM = {'secret_deploy'}


def test_result_return_is_rechecked_and_clicked_once_then_waits_for_room():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([RESULT, RESULT, set(), set(), ROOM])
    sleeps = []
    images = ImageRecognition(client, matcher, sleep=sleeps.append)
    finish_secret_result(client, images, action())
    assert client.taps == [(209, 1010)]
    assert 5 in sleeps


def test_countdown_auto_return_during_click_wait_does_not_click_room_back():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([RESULT, ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), action())
    assert client.taps == []


def test_return_button_without_result_page_does_not_click():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([{'secret_result_return'}])
    config = action()
    config['timeout_seconds'] = 4
    with pytest.raises(VisualError, match='结算'):
        finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), config)
    assert client.taps == []


def test_marker_and_return_button_from_different_frames_never_combine():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([{'secret_result_marker'}, {'secret_result_return'}])
    config = action()
    config['timeout_seconds'] = 4
    with pytest.raises(VisualError):
        finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), config)
    assert client.taps == []


def test_battle_stops_on_real_settlement_screenshot_without_attacking():
    from naruto_daily.battle import run_battle
    config = action()
    png = (ROOT / 'tests/fixtures/secret_result.png').read_bytes()
    client = Client([b'battle', png])
    def detect(frame, path, roi, threshold):
        if frame == b'battle':
            return ((1800, 60), 1) if path.stem == 'battle_pause' else (None, .1)
        return find_template_in_png(frame, path, roi, threshold)
    summary = run_battle(client, config['battle'], template_matcher=detect,
                         asset_root=ROOT, sleep=lambda _: None)
    assert summary.end_target == config['result_marker']['name']
    assert not client.taps


def test_return_click_is_not_repeated_when_network_keeps_result_visible():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([RESULT] * 5 + [ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), action())
    assert client.taps == [(209, 1010)]
