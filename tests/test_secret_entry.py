from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto_daily.config import load_settings
from naruto_daily.tasks import build_all, build_task

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('filename,point', [
    ('secret_entry_main.png', (1055, 574)),
    ('secret_entry_challenge.png', (22, 324)),
])
def test_real_screenshot_candidates_cover_clickable_entry(filename, point):
    from naruto_daily.secret_realm.enter import entry_candidates
    png = (ROOT / 'tests/fixtures' / filename).read_bytes()
    preview, centers = entry_candidates(png)
    assert preview and point in centers.values()


def test_farm_only_logs_in_then_uses_ninja_entry_and_shared_post_processing():
    settings = load_settings(ROOT / 'config.example.json')
    farm = build_task(settings, 'secret_realm_farm')
    login = settings.tasks['login']
    assert farm[:len(login)] == login
    assert farm[len(login)]['method'] == 'ninja_challenge'
    assert farm[len(login)+1:] == settings.tasks['secret_realm'][1:]
    assert not any(a.get('method') == 'ninja_challenge' for a in build_all(settings))
    assert not any(a.get('method') == 'ninja_challenge' for a in build_task(settings, 'login'))


def test_reward_entry_completed_skips_battle_but_returns_home(monkeypatch):
    from naruto_daily import runner
    seen = []
    monkeypatch.setattr(runner, 'enter_secret_realm', lambda *a, **kw: False)
    monkeypatch.setattr(runner, 'handle_secret_realm', lambda *a, **kw: seen.append('battle'))
    monkeypatch.setattr(runner, 'return_to_main', lambda *a, **kw: seen.append('home'))
    runner.ActionRunner(object()).run([
        {'type': 'enter_secret_realm'}, {'type': 'handle_secret_realm'}, {'type': 'return_to_main'}])
    assert seen == ['home']


def test_ninja_entry_swipes_once_and_rechecks_model_coordinates(monkeypatch):
    from naruto_daily.secret_realm import enter
    png = (ROOT / 'tests/fixtures/secret_entry_main.png').read_bytes()
    calls, taps, sleeps, swipes = [], [], [], []
    answers = iter([
        {'page': 'main', 'name': '忍者挑战', 'candidate_id': 4},
        {'page': 'main', 'name': '忍者挑战', 'candidate_id': 4},
        {'page': 'unknown', 'name': None, 'candidate_id': None},
        {'page': 'ninja_challenge', 'name': '秘境探险', 'candidate_id': 1},
        {'page': 'ninja_challenge', 'name': '秘境探险', 'candidate_id': 1},
    ])
    def complete(*args, **kwargs):
        calls.append(args)
        return next(answers)
    monkeypatch.setattr(enter, 'MultimodalClient', lambda *a, **kw: SimpleNamespace(complete_json=complete))
    monkeypatch.setattr(enter, 'return_to_main', lambda *a, **kw: None)
    client = SimpleNamespace(screenshot=lambda: png if not taps else (ROOT / 'tests/fixtures/secret_entry_challenge.png').read_bytes(),
                             tap=lambda *xy: taps.append(xy), swipe=lambda *xy: swipes.append(xy))
    action = load_settings(ROOT / 'config.example.json').tasks['secret_realm_farm'][0]
    matcher = lambda *a: ((1443, 791), 1)
    assert enter.enter_secret_realm(client, action, template_matcher=matcher, sleep=sleeps.append)
    assert taps == [(1055, 574), (22, 324)]
    assert len(swipes) == 1 and swipes[0][0] > swipes[0][2]
    assert 1 in sleeps and sleeps.count(5) >= 3
    assert len(calls) == 5


@pytest.mark.parametrize('result', [
    {'page': 'main', 'name': '忍者挑战', 'candidate_id': -1},
    {'page': 'main', 'name': '忍者挑战', 'candidate_id': 99},
    {'page': 'main', 'name': '忍者挑战', 'candidate_id': True},
    {'page': 'ninja_challenge', 'name': '团队副本', 'candidate_id': 1},
    {'page': 'unknown', 'name': None, 'candidate_id': None},
])
def test_invalid_or_wrong_target_never_produces_click(result):
    from naruto_daily.secret_realm.enter import parse_entry_center
    assert parse_entry_center(result, 'main', '忍者挑战', {1: (1055, 574)}) is None


@pytest.mark.parametrize('completed', [True, False])
def test_reward_entry_calls_existing_navigation_and_preserves_completion(monkeypatch, completed):
    from naruto_daily.secret_realm import enter
    seen = []
    monkeypatch.setattr(enter, 'return_to_main', lambda *a, **kw: seen.append('home'))
    monkeypatch.setattr(enter, 'required_template_tap', lambda *a, **kw: seen.append('reward'))
    def navigate(*a, **kw):
        seen.append('card')
        return SimpleNamespace(completed=completed)
    monkeypatch.setattr(enter, 'navigate_to_card', navigate)
    action = load_settings(ROOT / 'config.example.json').tasks['secret_realm'][0]
    assert enter.enter_secret_realm(object(), action) is not completed
    assert seen == ['home', 'reward', 'card']


def test_farm_cli_dry_run_does_not_click_or_call_model():
    from naruto_daily.cli import main
    client = SimpleNamespace(devices=lambda: ['a'], screen_size=lambda: (1920, 1080))
    assert main(['run', 'secret_realm_farm', '--config', str(ROOT / 'config.example.json'), '--dry-run'],
                client_factory=lambda *a, **kw: client) == 0


@pytest.mark.parametrize('late', [False, True])
def test_missing_or_late_entry_never_clicks(late):
    from naruto_daily.secret_realm.enter import _locate_and_tap
    from naruto_daily.common.recognition import ImageRecognition
    from naruto_daily.multimodal.client import MultimodalError
    png = (ROOT / 'tests/fixtures/secret_entry_main.png').read_bytes()
    taps, snapshots, sleeps = [], [], []
    def screenshot():
        snapshots.append(True)
        return png
    client = SimpleNamespace(screenshot=screenshot, tap=lambda *xy: taps.append(xy))
    model = SimpleNamespace(complete_json=lambda *a, **kw: {
        'page': 'main' if late else 'unknown', 'name': '忍者挑战', 'candidate_id': 4})
    action = load_settings(ROOT / 'config.example.json').tasks['secret_realm_farm'][0]
    images = ImageRecognition(client, sleep=sleeps.append)
    ticks = iter([0, action['timeout_seconds'] + 1] if late else [0] * (2 * action['multimodal']['max_calls']))
    with pytest.raises(MultimodalError, match='迟到|调用上限'):
        _locate_and_tap(images, model, action, 'main', '忍者挑战', clock=lambda: next(ticks))
    assert taps == []
    if not late:
        assert 1 in sleeps and len(snapshots) > 1


def test_disappeared_target_is_not_clicked_after_wait():
    from naruto_daily.secret_realm.enter import _locate_and_tap
    from naruto_daily.common.recognition import ImageRecognition
    from naruto_daily.multimodal.client import MultimodalError
    png = (ROOT / 'tests/fixtures/secret_entry_main.png').read_bytes()
    taps = []
    answers = iter([{'page': 'main', 'name': '忍者挑战', 'candidate_id': 4}] +
                   [{'page': 'unknown', 'name': None, 'candidate_id': None}] * 5)
    client = SimpleNamespace(screenshot=lambda: png, tap=lambda *xy: taps.append(xy))
    model = SimpleNamespace(complete_json=lambda *a, **kw: next(answers, {'page': 'unknown', 'name': None, 'candidate_id': None}))
    action = load_settings(ROOT / 'config.example.json').tasks['secret_realm_farm'][0]
    with pytest.raises(MultimodalError, match='调用上限'):
        _locate_and_tap(ImageRecognition(client, sleep=lambda _: None), model, action,
                        'main', '忍者挑战', clock=lambda: 0)
    assert taps == []


def test_invalid_entry_configuration_rejected_before_login(tmp_path):
    import json
    from naruto_daily.config import ConfigError
    payload = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
    payload['tasks']['secret_realm_farm'][0]['swipe'].update(x1=500, x2=1450)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ConfigError, match='向左'):
        load_settings(path)
