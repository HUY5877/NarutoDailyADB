import json
from pathlib import Path
from threading import Lock

import pytest

from naruto_daily.config import ConfigError, load_settings
from naruto_daily.runner import ActionRunner
from naruto_daily.tasks import build_task
from naruto_daily.common.visual import find_template_in_png


ROOT = Path(__file__).parents[1]


def test_real_screens_follow_abundance_from_main_page_through_result_and_back(monkeypatch):
    from naruto_daily.abundance import handler
    from naruto_daily.battle import run_battle
    now = [0.0]
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw: run_battle(*a, **kw, clock=lambda: now[0]))
    settings = load_settings(ROOT / 'config.example.json')
    frames = {name: (ROOT / 'tests/fixtures' / path).read_bytes() for name, path in {
        'home': 'main_page_reward_dot.png', 'cards': 'abundance_card.png',
        'challenge': 'abundance_challenge.png', 'battle': 'abundance_battle.png',
        'result': 'abundance_result.png',
    }.items()}

    class ReplayClient:
        lock = Lock()
        state = 'home'
        battle_taps = []
        challenge_clicks = 0
        result_extra_taps = 0
        def screenshot(self): return frames[self.state]
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
                now[0] += .25
                self.battle_taps.append((x, y))
                if len(self.battle_taps) == 4:
                    self.state = 'result'
            else:
                if x > 1300:
                    # 结算在20秒检查间隔内出现时，继续连点直到下次检查。
                    now[0] += .25
                    self.result_extra_taps += 1
                    return
                assert 650 < x < 1300 and y > 950
                self.state = 'home'
        def swipe(self, *args): raise AssertionError('卡片已在当前页面，不应滑动')

    client, waits = ReplayClient(), []
    result = ActionRunner(client, asset_root=ROOT, sleep=waits.append).run(build_task(settings, 'abundance'))
    assert result.action_count == 5 and client.state == 'home'
    assert client.challenge_clicks == 1
    assert client.result_extra_taps == 76
    assert sorted(client.battle_taps) == sorted([(1500, 960), (1535, 750), (1725, 650), (1715, 885)])
    assert now[0] == 20
    assert waits.count(10) == 1


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
