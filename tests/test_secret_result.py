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


def test_result_return_is_rechecked_then_taps_screen_until_room():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([RESULT, RESULT, set(), set(), ROOM])
    sleeps = []
    images = ImageRecognition(client, matcher, sleep=sleeps.append)
    finish_secret_result(client, images, action())
    assert client.taps == [(209, 1010), (960, 900)]
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
    client = Client([png])
    def detect(frame, path, roi, threshold):
        return find_template_in_png(frame, path, roi, threshold)
    summary = run_battle(client, config['battle'], template_matcher=detect,
                         asset_root=ROOT, sleep=lambda _: None)
    assert summary.end_target == config['result_marker']['name']
    assert not client.taps


def test_result_still_visible_after_return_receives_screen_taps():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([RESULT] * 5 + [ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), action())
    assert client.taps == [(209, 1010)] + [(960, 900)] * 3


def test_overlay_requires_multiple_taps_and_room_stops_further_taps():
    from naruto_daily.secret_realm.handler import finish_secret_result
    class OverlayClient(Client):
        def screenshot(self):
            if not self.taps:
                return RESULT
            return ROOM if len(self.taps) >= 3 else set()
    client = OverlayClient([])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), action())
    assert client.taps == [(209, 1010), (960, 900), (960, 900)]


def test_screen_taps_stop_at_timeout_if_room_never_appears():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([RESULT, RESULT, set()])
    config = action()
    config['timeout_seconds'] = 13
    waits = []
    with pytest.raises(VisualError, match='结算返回房间超时'):
        finish_secret_result(client, ImageRecognition(client, matcher, sleep=waits.append), config)
    assert client.taps[0] == (209, 1010)
    assert 1 < len(client.taps) <= 4
    assert all(point == (960, 900) for point in client.taps[1:])


def test_exit_confirmation_allows_dismissing_overlay_until_room():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([set(), set(), ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                         action(), exited_battle=True)
    assert client.taps == [(960, 900)]


def test_reward_overlay_stops_battle_then_blank_area_tap_returns_to_room():
    from naruto_daily.battle import run_battle
    from naruto_daily.secret_realm.handler import finish_secret_result
    config = action()
    config['battle']['timeout_seconds'] = .1
    overlay = (ROOT / 'tests/fixtures/secret_reward_overlay.png').read_bytes()
    room = (ROOT / 'tests/fixtures/secret_reward_room.png').read_bytes()
    class RewardClient(Client):
        def screenshot(self):
            return room if self.taps else overlay
    client = RewardClient([])
    summary = run_battle(client, config['battle'], asset_root=ROOT, sleep=lambda _: None)
    assert summary.taps == 0 and client.taps == []
    finish_secret_result(client, ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None), config)
    assert client.taps == [(960, 900)]


def test_reward_overlay_before_card_result_still_clicks_return_button():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([{'secret_reward_overlay'}, RESULT, RESULT, ROOM])
    def detect(frame, path, roi, threshold):
        if path.stem == 'secret_reward_overlay' and path.stem in frame:
            return (960, 260), 1
        return matcher(frame, path, roi, threshold)
    finish_secret_result(client, ImageRecognition(client, detect, sleep=lambda _: None), action())
    assert client.taps == [(960, 900), (209, 1010)]


def test_reward_overlay_after_card_result_closes_without_second_return():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([RESULT, RESULT, {'secret_reward_overlay'}, ROOM])
    def detect(frame, path, roi, threshold):
        if path.stem == 'secret_reward_overlay' and path.stem in frame:
            return (960, 260), 1
        return matcher(frame, path, roi, threshold)
    finish_secret_result(client, ImageRecognition(client, detect, sleep=lambda _: None), action())
    assert client.taps == [(209, 1010), (960, 900)]
