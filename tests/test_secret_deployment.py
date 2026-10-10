from pathlib import Path

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.config import load_settings


ROOT = Path(__file__).parents[1]


def action():
    return next(a for a in load_settings(ROOT / 'config.example.json').tasks['secret_realm']
                if a['type'] == 'handle_secret_realm')


class Client:
    def __init__(self, fixture='secret_deploy_warning', stuck=False):
        self.fixture = fixture
        self.stuck = stuck
        self.taps = []
        self.frames = 0

    def screenshot(self):
        self.frames += 1
        fixture = 'secret_flame_battle' if self.taps and not self.stuck else self.fixture
        return (ROOT / 'tests/fixtures' / (fixture + '.png')).read_bytes()

    def tap(self, x, y):
        assert 800 < x < 1100 and 640 < y < 760  # 确定，不能点上方复选框。
        self.taps.append((x, y))


def test_positive_tickets_confirm_warning_once_and_wait_for_loading():
    from naruto_daily.secret_realm.deployment import wait_after_deploy
    client, waits = Client(), []
    assert wait_after_deploy(ImageRecognition(client, asset_root=ROOT, sleep=waits.append),
                             action(), confirmed_tickets=32, clock=lambda: 0)
    assert len(client.taps) == 1
    assert sum(waits) == 12  # 点击前3秒 + 确认后9秒加载。


@pytest.mark.parametrize('fixture', ['secret_flame_battle', 'secret_leave_team_dialog'])
def test_no_warning_or_other_confirm_dialog_is_not_clicked(fixture):
    from naruto_daily.secret_realm.deployment import wait_after_deploy
    client, waits = Client(fixture), []
    assert not wait_after_deploy(ImageRecognition(client, asset_root=ROOT, sleep=waits.append),
                                 action(), confirmed_tickets=1, clock=lambda: 0)
    assert client.taps == [] and sum(waits) == 9


@pytest.mark.parametrize('tickets', [0, None, -1, True, '32'])
def test_unverified_or_zero_tickets_never_allow_confirm(tickets):
    from naruto_daily.secret_realm.deployment import wait_after_deploy
    client = Client()
    with pytest.raises(VisualError, match='挑战券'):
        wait_after_deploy(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None),
                          action(), confirmed_tickets=tickets, clock=lambda: 0)
    assert client.taps == [] and client.frames == 0


def test_delayed_warning_is_found_and_loading_wait_restarts_after_confirm():
    from naruto_daily.secret_realm.deployment import wait_after_deploy
    client, waits = Client(), []
    original_screenshot = client.screenshot
    def screenshot():
        if sum(waits) < 6:
            return (ROOT / 'tests/fixtures/secret_reward_room.png').read_bytes()
        return original_screenshot()
    client.screenshot = screenshot
    assert wait_after_deploy(ImageRecognition(client, asset_root=ROOT, sleep=waits.append),
                             action(), confirmed_tickets=32, clock=lambda: 0)
    assert len(client.taps) == 1 and sum(waits) == 18


def test_warning_still_visible_after_confirm_times_out_without_repeat_click():
    from naruto_daily.secret_realm.deployment import wait_after_deploy
    client = Client(stuck=True)
    with pytest.raises(VisualError, match='确认.*超时'):
        wait_after_deploy(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None),
                          action(), confirmed_tickets=32, clock=lambda: 0)
    assert len(client.taps) == 1


def test_warning_disappears_during_preclick_wait_cancels_click():
    from naruto_daily.secret_realm.deployment import wait_after_deploy
    client, waits = Client(), []
    original_screenshot = client.screenshot
    def screenshot():
        if waits:
            return (ROOT / 'tests/fixtures/secret_flame_battle.png').read_bytes()
        return original_screenshot()
    client.screenshot = screenshot
    assert not wait_after_deploy(ImageRecognition(client, asset_root=ROOT, sleep=waits.append),
                                 action(), confirmed_tickets=1, clock=lambda: 0)
    assert client.taps == []


def test_warning_and_confirm_must_be_on_same_frame():
    from naruto_daily.secret_realm.deployment import wait_after_deploy
    client = Client()
    def matcher(png, path, roi, threshold):
        missing = 'secret_deploy_warning_text' if client.frames % 2 == 0 else 'secret_deploy_warning_confirm'
        if path.stem == missing:
            return None, .1
        return find_template_in_png(png, path, roi, threshold)
    wait_after_deploy(ImageRecognition(client, matcher, asset_root=ROOT, sleep=lambda _: None),
                      action(), confirmed_tickets=1, clock=lambda: 0)
    assert client.taps == []
