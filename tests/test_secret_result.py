from pathlib import Path

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from battle_replay import load_legacy_adb_settings


ROOT = Path(__file__).parents[1]


def action():
    return next(a for a in load_legacy_adb_settings(ROOT / 'config.example.json').tasks['secret_realm']
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
              'secret_result_marker': (537, 837), 'secret_result_return': (209, 1010),
              'secret_reward_overlay': (960, 260)}
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


@pytest.mark.parametrize('frame', [
    {'secret_result_return'},
    {'secret_result_return', 'secret_reward_overlay'},
    RESULT | {'secret_reward_overlay'},
], ids=['return-only', 'return-with-reward-title', 'all-result-markers'])
def test_confirmed_result_prefers_rechecked_return_over_blank_tap(frame):
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([frame, frame, ROOM])
    sleeps = []
    config = action()
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=sleeps.append),
                         config, result_confirmed=True)
    assert client.taps == [(209, 1010)]
    assert sleeps[0] == config['pre_click_wait_seconds']


def test_confirmed_return_disappears_into_room_during_recheck_without_click():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([{'secret_result_return'}, ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                         action(), result_confirmed=True)
    assert client.taps == []


@pytest.mark.parametrize('page, expected_taps', [
    ('secret_deploy', []),
    ('secret_create_room', [(1443, 791)]),
])
def test_confirmed_result_checks_room_or_lobby_before_visible_return(page, expected_taps):
    from naruto_daily.secret_realm.handler import finish_secret_result
    frame = {page, 'secret_result_return', 'secret_reward_overlay'}
    client = Client([frame, frame, frame, ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                         action(), result_confirmed=True)
    assert client.taps == expected_taps


def test_confirmed_result_checks_ninja_selection_before_visible_return(monkeypatch):
    from naruto_daily.secret_realm import handler
    ninja = {'ninja', 'secret_result_return', 'secret_reward_overlay'}
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda png: (150, 1000) if png == ninja else None)
    client = Client([ninja, ninja, ROOM])
    handler.finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                                 action(), result_confirmed=True)
    assert client.taps == [(150, 1000)]


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


@pytest.mark.parametrize('fixture', ['secret_poison_unreleased_failure.png', 'secret_yinyang_battle.png'])
def test_local_failure_recognizes_real_settlements(fixture):
    from naruto_daily.secret_realm.result_detection import secret_failure_visible
    # 历史阴阳 fixture 虽以 battle 命名，画面实际显示失败和点击任意位置关闭。
    assert secret_failure_visible((ROOT / 'tests/fixtures' / fixture).read_bytes())


@pytest.mark.parametrize('fixture', [
    'secret_poison_boss_active.png', 'secret_flame_battle.png', 'secret_result.png',
    'secret_reward_overlay.png', 'secret_reward_room.png', 'secret_pause_menu.png',
    'secret_exit_dialog.png', 'secret_deploy_warning.png',
])
def test_local_failure_does_not_authorize_dismissal_on_other_pages(fixture):
    from naruto_daily.secret_realm.result_detection import secret_failure_visible
    assert not secret_failure_visible((ROOT / 'tests/fixtures' / fixture).read_bytes())


@pytest.mark.parametrize('png', [b'', b'invalid screenshot', None, set()])
def test_local_failure_rejects_unreadable_images(png):
    from naruto_daily.secret_realm.result_detection import secret_failure_visible
    assert not secret_failure_visible(png)


def test_confirmed_navigation_result_taps_blank_until_room():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([set(), set(), ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                         action(), result_confirmed=True)
    assert client.taps == [(960, 900)]


def test_confirmed_result_already_returned_to_room_does_not_tap():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([ROOM])
    finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                         action(), result_confirmed=True)
    assert client.taps == []


@pytest.mark.parametrize('confirmed', [False, True])
def test_ninja_selection_rechecks_and_clicks_latest_back_position(monkeypatch, confirmed):
    from naruto_daily.secret_realm import handler
    first, latest = {'ninja_old'}, {'ninja_new'}
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda png: (150, 1000) if png == first else (160, 1010) if png == latest else None)
    client = Client([first, latest, ROOM])
    handler.finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                                 action(), result_confirmed=confirmed)
    assert client.taps == [(160, 1010)]


def test_ninja_selection_disappears_before_click_without_tapping_room_back(monkeypatch):
    from naruto_daily.secret_realm import handler
    ninja = {'ninja'}
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda png: (150, 1000) if png == ninja else None)
    client = Client([ninja, ROOM])
    handler.finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), action())
    assert client.taps == []


def test_ninja_selection_does_not_inherit_blank_taps_after_return(monkeypatch):
    from naruto_daily.secret_realm import handler
    ninja = {'ninja'}
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda png: (150, 1000) if png == ninja else None)
    client = Client([ninja, ninja, set(), set(), ROOM])
    handler.finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                                 action(), result_confirmed=True)
    assert client.taps == [(150, 1000)]


def test_result_recheck_switching_to_ninja_selection_never_taps_blank(monkeypatch):
    from naruto_daily.secret_realm import handler
    ninja = {'ninja'}
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda png: (150, 1000) if png == ninja else None)
    client = Client([RESULT, ninja, ninja, ninja, ROOM])
    handler.finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                                 action(), result_confirmed=True)
    assert client.taps == [(150, 1000)]


def test_unconfirmed_unknown_page_never_taps_blank():
    from naruto_daily.secret_realm.handler import finish_secret_result
    client = Client([set()])
    config = action()
    config['timeout_seconds'] = 4
    with pytest.raises(VisualError, match='结算返回房间超时'):
        finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None), config)
    assert client.taps == []


def test_regular_battle_failure_stops_then_dismisses_to_room_without_another_model_call():
    from types import SimpleNamespace
    from naruto_daily.battle import run_battle
    from naruto_daily.secret_realm.handler import finish_secret_result

    failure = (ROOT / 'tests/fixtures/secret_yinyang_battle.png').read_bytes()
    room = (ROOT / 'tests/fixtures/secret_reward_room.png').read_bytes()

    class FailureClient(Client):
        def screenshot(self):
            return room if self.taps else failure

    calls = []

    def complete(*args, **kwargs):
        calls.append(True)
        return {'finished': True, 'page': 'finished', 'reason': '失败，点击任意位置关闭界面'}

    config = action()
    client = FailureClient([])
    summary = run_battle(client, config['battle'], asset_root=ROOT, sleep=lambda _: None,
                         model=SimpleNamespace(complete_json=complete))
    assert summary.taps == 0 and client.taps == []
    finish_secret_result(client, ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None), config)
    assert client.taps == [(960, 900)] and calls == [True]


@pytest.mark.parametrize('fixture', ['secret_ninja_selected.png', 'secret_ninja_loading.png'])
def test_real_ninja_selection_stops_battle_and_returns_to_room_without_model(fixture):
    from types import SimpleNamespace
    from naruto_daily.battle import run_battle
    from naruto_daily.secret_realm import handler

    selection = (ROOT / 'tests/fixtures' / fixture).read_bytes()
    room = (ROOT / 'tests/fixtures/secret_reward_room.png').read_bytes()
    back = handler.find_ninja_selection_return(selection)
    assert back is not None

    class NinjaClient(Client):
        def screenshot(self):
            return room if self.taps else selection

    def unexpected_model(*args, **kwargs):
        raise AssertionError('更换忍者页应通过本地图片识别，不调用模型')

    config = action()
    client = NinjaClient([])
    summary = run_battle(client, config['battle'], asset_root=ROOT, sleep=lambda _: None,
                         model=SimpleNamespace(complete_json=unexpected_model),
                         end_detector=handler._ninja_selection_end)
    assert summary.taps == 0 and summary.end_target == '秘境更换忍者页' and client.taps == []
    handler.finish_secret_result(client, ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None), config)
    assert client.taps == [back]
