import json
import logging
from copy import deepcopy
from pathlib import Path
from threading import Lock

import pytest

from naruto_daily.config import ConfigError, load_settings
from naruto_daily.runner import ActionRunner
from naruto_daily.tasks import build_task
from naruto_daily.common.visual import find_template_in_png


ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def main_model(monkeypatch):
    """主界面用确定的模型响应回放；固定页面继续使用真实模板。"""
    from naruto_daily.common import main_screen
    home = (ROOT / 'tests/fixtures/main_page_reward_dot.png').read_bytes()
    class Model:
        def __init__(self, *a): pass
        def match(self, png, target, timeout):
            return ((1800, 500), 1) if png in (home, b'screen') else (None, 0)
    monkeypatch.setattr(main_screen, 'MainScreenRecognition', Model)


@pytest.mark.parametrize('completed_delay_frames', [0, 2])
def test_real_screens_follow_abundance_from_main_page_through_result_and_back(monkeypatch, completed_delay_frames):
    from naruto_daily.abundance import handler
    from naruto_daily.battle import run_battle_without_first_skill
    now = [0.0]
    monkeypatch.setattr(handler, 'run_battle_without_first_skill',
                        lambda *a, **kw: run_battle_without_first_skill(*a, **kw, clock=lambda: now[0]))
    settings = load_settings(ROOT / 'config.example.json')
    frames = {name: (ROOT / 'tests/fixtures' / path).read_bytes() for name, path in {
        'home': 'main_page_reward_dot.png', 'cards': 'abundance_card.png',
        'challenge': 'abundance_challenge.png', 'battle': 'abundance_battle.png',
        'result': 'abundance_result.png', 'completed': 'abundance_completed.png',
    }.items()}

    class ReplayClient:
        lock = Lock()
        state = 'home'
        battle_taps = []
        challenge_clicks = 0
        result_extra_taps = 0
        dismiss_clicks = 0
        completed_checks = 0
        completed_confirmed = False
        def screenshot(self):
            if self.state == 'completed':
                self.completed_checks += 1
                if self.completed_checks <= completed_delay_frames:
                    return frames['result']
                self.completed_confirmed = True
            return frames[self.state]
        def tap(self, x, y):
            with self.lock:
                self._tap(x, y)
        def _tap(self, x, y):
            if self.state == 'home':
                self.state = 'cards'
            elif self.state == 'cards':
                assert 700 < x < 1000 and 600 < y < 750
                self.state = 'challenge'
            elif self.state == 'challenge':
                assert 600 < x < 970 and y > 850
                self.challenge_clicks += 1
                self.state = 'battle'
            elif self.state == 'battle':
                assert (x, y) != (1500, 960), '丰饶战斗不能点击一技能'
                now[0] += .25
                self.battle_taps.append((x, y))
                if len(self.battle_taps) == 3:
                    self.state = 'result'
            elif self.state == 'result':
                if x > 1300:
                    assert (x, y) != (1500, 960), '结算检查前不能误点超影免费扫荡'
                    # 示例配置没有模型，结算模板仍在下一个战斗检查周期才发现。
                    now[0] += .25
                    self.result_extra_taps += 1
                    return
                assert (x, y) == (400, 540)
                self.dismiss_clicks += 1
                self.state = 'completed'
            else:
                assert self.state == 'completed' and self.completed_confirmed
                assert x > 1700 and y < 150, '确认今日已完成挑战后才允许关闭页面返回主界面'
                self.state = 'home'
        def swipe(self, *args): raise AssertionError('卡片已在当前页面，不应滑动')

    client, waits = ReplayClient(), []
    result = ActionRunner(client, asset_root=ROOT, sleep=waits.append).run(build_task(settings, 'abundance'))
    assert result.action_count == 5 and client.state == 'home'
    assert client.challenge_clicks == 1
    assert client.result_extra_taps == 117
    assert client.dismiss_clicks == 1 and client.completed_confirmed
    assert client.completed_checks > completed_delay_frames
    assert sorted(client.battle_taps) == sorted([(1535, 750), (1725, 650), (1715, 885)])
    assert now[0] == 30
    assert waits.count(10) == 1


@pytest.mark.parametrize('fixture, expected', [
    ('abundance_completed.png', True),
    ('abundance_battle.png', False), ('abundance_challenge.png', False),
    ('abundance_result.png', False), ('abundance_service_popup.png', False),
    ('abundance_card.png', False), ('main_page_reward_dot.png', False),
])
def test_completed_challenge_template_distinguishes_real_pages(fixture, expected):
    from naruto_daily.abundance.handler import COMPLETED_TARGET

    target = COMPLETED_TARGET
    center, _ = find_template_in_png((ROOT / 'tests/fixtures' / fixture).read_bytes(),
        ROOT / target['template'], tuple(target[k] for k in ('left', 'top', 'right', 'bottom')),
        target['threshold'])
    assert (center is not None) is expected


@pytest.mark.parametrize('already_completed', [False, True])
@pytest.mark.parametrize('with_model', [False, True])
def test_completed_page_stops_battle_locally_and_returns_home(
    monkeypatch, caplog, already_completed, with_model,
):
    from naruto_daily.abundance import handler
    from naruto_daily.battle import run_battle_without_first_skill
    from naruto_daily.return_to_main import return_to_main

    caplog.set_level(logging.INFO, logger='naruto_daily')
    settings = load_settings(ROOT / 'config.example.json')
    action, back = deepcopy(settings.tasks['abundance'][3:])
    original_battle = deepcopy(action['battle'])
    frames = {name: (ROOT / 'tests/fixtures' / filename).read_bytes() for name, filename in {
        'challenge': 'abundance_challenge.png', 'battle': 'abundance_battle.png',
        'completed': 'abundance_completed.png', 'home': 'main_page_reward_dot.png',
    }.items()}
    now, model_calls = [0.0], []

    class Model:
        def __init__(self, *args, **kwargs):
            assert with_model

        def complete_json(self, png, *args, **kwargs):
            assert png == frames['battle'], '已完成页面必须通过本地模板停止，不能请求模型'
            model_calls.append(png)
            return {'page': 'battle', 'finished': False, 'reason': '仍在战斗'}

    if with_model:
        action['multimodal'] = {'timeout_seconds': 30, 'max_calls': 6}
    monkeypatch.setattr(handler, 'MultimodalClient', Model)
    monkeypatch.setattr(handler, 'run_battle_without_first_skill',
                        lambda *a, **kw: run_battle_without_first_skill(*a, **kw, clock=lambda: now[0]))

    def unexpected_finish(*args, **kwargs):
        raise AssertionError('已完成页面不能继续等待结算关闭提示')

    monkeypatch.setattr(handler, 'finish_abundance_result', unexpected_finish)

    class ReplayClient:
        def __init__(self):
            self.state = 'challenge'
            self.lock = Lock()
            self.taps = []
            self.checked_completed = False

        def screenshot(self):
            if self.state == 'completed':
                self.checked_completed = True
            return frames[self.state]

        def tap(self, x, y):
            with self.lock:
                if self.state == 'challenge':
                    assert 600 < x < 970 and y > 850
                    self.state = 'completed' if already_completed else 'battle'
                elif self.state == 'battle':
                    assert (x, y) in {(1535, 750), (1725, 650), (1715, 885)}
                    now[0] += 10
                    if now[0] >= 30:
                        self.state = 'completed'
                else:
                    assert self.state == 'completed' and self.checked_completed
                    assert x > 1700 and y < 150, '检查到已完成后只允许关闭页面，不再连点或点空白'
                    self.state = 'home'
                self.taps.append((x, y))

    client = ReplayClient()
    summary = handler.handle_abundance(client, action, asset_root=ROOT, sleep=lambda _: None)
    assert summary.end_target == handler.COMPLETED_TARGET['name']
    assert summary.taps == (0 if already_completed else 3)
    assert client.state == 'completed' and client.checked_completed
    assert len(client.taps) == 1 + summary.taps
    assert len(model_calls) == int(with_model and not already_completed)
    assert action['battle'] == original_battle
    assert '丰饶之间今日已完成挑战：已匹配，相似度 1.000，阈值 0.900' in caplog.text
    assert '跳过结算关闭' in caplog.text
    return_to_main(client, back, asset_root=ROOT, sleep=lambda _: None)
    assert client.state == 'home' and len(client.taps) == 2 + summary.taps


@pytest.mark.parametrize('fixture, expected', [
    ('main_page_reward_dot.png', True), ('main_page_activity_icon.png', True),
    ('activity_page_close.png', False), ('abundance_challenge.png', False),
    ('abundance_battle.png', False), ('abundance_result.png', False),
])
def test_reward_text_marker_distinguishes_main_page_even_with_new_red_dot(fixture, expected):
    center, _ = find_template_in_png((ROOT/'tests/fixtures'/fixture).read_bytes(),
        ROOT/'assets/main_reward_label.png', (1700, 400, 1920, 700), .9)
    assert (center is not None) is expected


@pytest.mark.parametrize('change', [
    lambda a: a.update(load_wait_seconds=1),
    lambda a: a['challenge_button'].update(right=1400),
    lambda a: a['battle'].update(end_targets=[]),
    lambda a: a['battle'].update(tap_interval_seconds=False),
    lambda a: a['battle'].update(tap_interval_seconds=-.01),
    lambda a: a['battle'].update(timeout_seconds=999),
    lambda a: a['battle']['buttons'][0].update(x=1920),
    lambda a: a['battle']['buttons'].reverse(),
])
def test_invalid_battle_configuration_is_rejected(tmp_path, change):
    payload = json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
    change(payload['tasks']['abundance'][3])
    path = tmp_path/'config.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ConfigError):
        load_settings(path)


@pytest.mark.parametrize('task', ['abundance', 'secret_realm'])
def test_zero_battle_delay_is_valid_for_continuous_clicking(tmp_path, task):
    payload = json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
    action = next(a for a in payload['tasks'][task] if 'battle' in a)
    action['battle']['tap_interval_seconds'] = 0
    path = tmp_path/'config.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    settings = load_settings(path)
    assert next(a for a in settings.tasks[task] if 'battle' in a)['battle']['tap_interval_seconds'] == 0


def test_completed_abundance_card_skips_challenge_and_battle_but_returns_home():
    settings = load_settings(ROOT/'config.example.json')
    card, handler, back = settings.tasks['abundance'][2:]
    calls = []
    class Client:
        def screenshot(self): return b'screen'
        def tap(self, *xy): calls.append(xy)
    def matcher(png, path, roi, threshold):
        if path.name in ('abundance_label.png', 'completed_stamp.png', 'main_reward_label.png'):
            return (860, 490), .99
        raise AssertionError('已完成的任务不应查询挑战或战斗目标')
    ActionRunner(Client(), template_matcher=matcher, sleep=lambda _: None).run([card, handler, back])
    assert calls == []


def test_service_popup_real_frames_dismiss_then_return_home():
    from naruto_daily.abundance import finish_abundance_result
    from naruto_daily.return_to_main import return_to_main
    config = load_settings(ROOT / 'config.example.json')
    action, back = config.tasks['abundance'][3:]
    frames = {name: (ROOT / 'tests/fixtures' / filename).read_bytes() for name, filename in {
        'popup': 'abundance_service_popup.png', 'completed': 'abundance_completed.png',
        'home': 'main_page_reward_dot.png',
    }.items()}

    class ReplayClient:
        def __init__(self):
            self.state = 'popup'
            self.taps = []
        def screenshot(self):
            return frames[self.state]
        def tap(self, x, y):
            self.taps.append((x, y))
            if self.state == 'popup':
                assert (x, y) == (400, 540)
                self.state = 'completed'
            else:
                assert self.state == 'completed' and x > 1700 and y < 150
                self.state = 'home'

    class Model:
        def complete_json(self, png, prompt, *, timeout_seconds):
            assert png == frames['popup']
            return {'page': 'service_popup'}

    client = ReplayClient()
    finish_abundance_result(client, action, asset_root=ROOT, model=Model(), sleep=lambda _: None)
    assert client.state == 'completed' and client.taps == [(400, 540)]
    summary = return_to_main(client, back, asset_root=ROOT, sleep=lambda _: None)
    assert client.state == 'home' and len(client.taps) == 2
