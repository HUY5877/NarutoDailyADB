from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from naruto_daily.common import main_screen
from naruto_daily.common.main_screen import parse_main_screen
from naruto_daily.common.navigation import required_template_tap
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.config import ConfigError, load_settings
from naruto_daily.return_to_main import return_to_main
from naruto_daily.runner import ActionRunner

ROOT = Path(__file__).parents[1]
MODEL = dict(base_url='https://example.test/v1', model='vision', timeout_seconds=30, max_calls=4)


def target(menu='equipment'):
    return dict(name=menu, main_menu=menu, multimodal=deepcopy(MODEL))


def action(menu='equipment'):
    return dict(target(menu), type='main_menu_tap', timeout_seconds=60,
                poll_seconds=1, pre_click_wait_seconds=1, post_click_wait_seconds=1)


def answer(menu='equipment', *, found=True, page='main', candidate_id=3):
    return dict(page=page, target=menu, found=found, candidate_id=candidate_id)


def png(width=1920, height=1080, color=100):
    return cv2.imencode('.png', np.full((height, width, 3), color, dtype=np.uint8))[1].tobytes()


def fake_model(monkeypatch, complete):
    monkeypatch.setattr(main_screen, 'MultimodalClient',
                        lambda *a, **kw: SimpleNamespace(complete_json=complete))


def test_model_failure_logs_scene_budget_and_type_without_payload_and_preserves_error(monkeypatch, caplog):
    now, contexts, active = [0.0], [], []
    failure = RuntimeError('secret-api-key private-prompt screenshot-payload')
    monkeypatch.setattr(main_screen.time, 'monotonic', lambda: now[0])

    @contextmanager
    def context(scene):
        contexts.append(scene)
        active.append(scene)
        try:
            yield
        finally:
            active.pop()

    def complete(image, prompt, *, timeout_seconds):
        assert active == ['主界面识别/home']
        assert timeout_seconds == 6
        now[0] = 4
        raise failure

    monkeypatch.setattr(main_screen, 'model_request_context', context)
    fake_model(monkeypatch, complete)
    recognizer = main_screen.MainScreenRecognition(MODEL, ROOT)
    with caplog.at_level('ERROR', logger='naruto_daily'):
        with pytest.raises(RuntimeError) as caught:
            recognizer.match(png(96, 54), target('home'), timeout_seconds=6)
    assert caught.value is failure
    assert contexts == ['主界面识别/home'] and active == []
    assert '目标=home' in caplog.text and '异常类型=RuntimeError' in caplog.text
    assert '已耗时 4.00 秒' in caplog.text
    assert '本次识别总预算 6.00 秒' in caplog.text and '剩余预算 2.00 秒' in caplog.text
    assert not any(text in caplog.text for text in ('secret-api-key', 'private-prompt', 'screenshot-payload'))


@pytest.mark.parametrize('size', [(1920, 1080), (1169, 660), (960, 540)])
def test_model_coordinates_scale_to_actual_screenshot_without_template_matching(monkeypatch, size):
    raw = png(*size)
    seen = []
    def complete(image, prompt, *, timeout_seconds):
        seen.append(image)
        assert '背景' in prompt and 'equipment' in prompt
        return answer()
    fake_model(monkeypatch, complete)
    def no_template(*args):
        raise AssertionError('主界面禁止回退到模板')
    center, _ = ImageRecognition(None, no_template).match(raw, target())
    assert center == ((round(340*size[0]/1920) + round(500*size[0]/1920))//2,
                      (round(910*size[1]/1080) + size[1])//2)
    assert len(seen) == 1 and cv2.imdecode(np.frombuffer(seen[0], np.uint8), 1).shape[1] == 960


@pytest.mark.parametrize('result', [
    answer(found=False), answer(page='other'), answer(page='unknown'), answer(page='equipment_menu'),
])
def test_hidden_or_wrong_page_does_not_produce_button(result):
    assert parse_main_screen(result, 'equipment', 1920, 1080, {3: (420, 995)})[0] is None


@pytest.mark.parametrize('result', [
    {}, answer('rewards'), answer(candidate_id=-1),
    answer(candidate_id=99), answer(candidate_id='3'),
    answer(candidate_id=True), answer(candidate_id=None),
    dict(answer(), found='true'),
])
def test_malformed_model_output_never_becomes_coordinates(result):
    with pytest.raises(VisualError):
        parse_main_screen(result, 'equipment', 1920, 1080, {3: (420, 995)})


def test_equipment_submenu_is_distinguished_from_main_icon():
    assert parse_main_screen(answer('equipment_submenu'), 'equipment_submenu', 1920, 1080, {})[0] is None
    result = answer('equipment_submenu', page='equipment_menu', candidate_id=1)
    assert parse_main_screen(result, 'equipment_submenu', 1920, 1080, {1: (132, 835)})[0] == (132, 835)


def test_runner_rechecks_latest_model_position_before_clicking(monkeypatch):
    frames = iter([png(color=20), png(color=210)])
    responses = iter([answer(), answer(candidate_id=4)])
    fake_model(monkeypatch, lambda *a, **kw: next(responses))
    taps = []
    client = SimpleNamespace(screenshot=lambda: next(frames), tap=lambda *xy: taps.append(xy))
    ActionRunner(client, sleep=lambda _: None).run([action()])
    assert taps == [(580, 995)]


def test_disappeared_button_does_not_click_cached_position_and_calls_are_bounded(monkeypatch):
    calls, taps = [], []
    def complete(*a, **kw):
        calls.append(1)
        return answer(found=len(calls) == 1)
    fake_model(monkeypatch, complete)
    client = SimpleNamespace(screenshot=lambda: png(), tap=lambda *xy: taps.append(xy))
    with pytest.raises(VisualError, match='调用上限'):
        required_template_tap(client, action(), sleep=lambda _: None)
    # 查找最多4次，第一次找到后复查2次未确认；重新查找不能重置查找额度。
    assert not taps and len(calls) == MODEL['max_calls'] + 2


@pytest.mark.parametrize('menu,page', [('equipment_submenu', 'equipment_menu'), ('rewards', 'main')])
@pytest.mark.parametrize('max_calls,confirm_misses', [(3, 0), (2, 1)])
def test_last_search_call_can_still_confirm_and_click(monkeypatch, menu, page, max_calls, confirm_misses):
    first_id, last_id = (7, 7) if menu == 'rewards' else (1, 2)
    responses = iter([answer(menu, found=False)] * (max_calls - 1)
                     + [answer(menu, page=page, candidate_id=first_id)]
                     + [answer(menu, found=False)] * confirm_misses
                     + [answer(menu, page=page, candidate_id=last_id)])
    calls, taps = [], []
    def complete(*args, **kwargs):
        calls.append(1)
        return next(responses)
    fake_model(monkeypatch, complete)
    config = action(menu)
    config['multimodal']['max_calls'] = max_calls
    def screenshot():
        if menu != 'rewards':
            return png()
        fixture = 'reward_cards_stuck.png' if taps else 'main_rewards_right_hud.png'
        return (ROOT / 'tests/fixtures' / fixture).read_bytes()
    client = SimpleNamespace(screenshot=screenshot, tap=lambda *xy: taps.append(xy))
    required_template_tap(client, config, sleep=lambda _: None)
    expected = (282, 835) if menu == 'equipment_submenu' else (1845, 537)
    # 奖励首次确认后复用坐标，装备菜单仍须模型复查最新位置。
    expected_calls = max_calls if menu == 'rewards' else max_calls + confirm_misses + 1
    assert taps == [expected] and len(calls) == expected_calls


def test_confirmation_budget_survives_repeated_search_rounds(monkeypatch):
    # 每轮重新找到后都复查失败，累计三次复查就停止，不能每轮刷新额度。
    responses = iter([answer(), answer(found=False), answer(found=False),
                      answer(), answer(found=False)])
    calls, taps = [], []
    def complete(*args, **kwargs):
        calls.append(1)
        return next(responses)
    fake_model(monkeypatch, complete)
    config = action()
    config['multimodal']['max_calls'] = 3
    client = SimpleNamespace(screenshot=lambda: png(), tap=lambda *xy: taps.append(xy))
    with pytest.raises(VisualError, match='点击前复查.*调用上限'):
        required_template_tap(client, config, sleep=lambda _: None)
    assert not taps and len(calls) == 5


def test_confirmation_keeps_original_total_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main_screen.time, 'monotonic', lambda: now[0])
    delays = iter([20, 20, 17])
    calls, taps = [], []
    def complete(*args, **kwargs):
        calls.append(1)
        now[0] += next(delays)
        return answer(found=len(calls) == 3)
    def sleep(seconds): now[0] += seconds
    fake_model(monkeypatch, complete)
    config = action()
    config['multimodal']['max_calls'] = 3
    client = SimpleNamespace(screenshot=lambda: png(), tap=lambda *xy: taps.append(xy))
    with pytest.raises(VisualError, match='超时'):
        required_template_tap(client, config, sleep=sleep)
    assert not taps and len(calls) == 3 and now[0] == 60


def test_late_response_is_rejected_and_actual_model_time_counts(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main_screen.time, 'monotonic', lambda: now[0])
    def complete(*a, **kw):
        now[0] += 31
        return answer()
    fake_model(monkeypatch, complete)
    taps = []
    client = SimpleNamespace(screenshot=lambda: png(), tap=lambda *xy: taps.append(xy))
    with pytest.raises(VisualError, match='超时'):
        required_template_tap(client, action(), sleep=lambda s: None)
    assert taps == []


def test_screenshot_time_is_included_in_main_button_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main_screen.time, 'monotonic', lambda: now[0])
    calls = []
    fake_model(monkeypatch, lambda *a, **kw: calls.append(1))
    def screenshot():
        now[0] += 61
        return png()
    client = SimpleNamespace(screenshot=screenshot, tap=lambda *xy: pytest.fail('过期截图不能点击'))
    with pytest.raises(VisualError, match='超时'):
        required_template_tap(client, action(), sleep=lambda _: None)
    assert not calls


def test_fixed_page_targets_still_only_use_templates(monkeypatch):
    fake_model(monkeypatch, lambda *a, **kw: pytest.fail('固定页面不应调用模型'))
    calls = []
    def matcher(*args):
        calls.append(args)
        return (100, 200), .99
    fixed = dict(name='开始扫荡', template='sweep.png', left=0, top=0, right=200, bottom=300, threshold=.9)
    assert ImageRecognition(None, matcher).match(b'fixed screenshot', fixed) == ((100, 200), .99)
    assert len(calls) == 1


def test_home_detection_cancels_close_on_the_recheck(monkeypatch):
    responses = iter([answer('home', found=False, page='other'), answer('home')])
    fake_model(monkeypatch, lambda *a, **kw: next(responses))
    close = dict(name='关闭', template='close.png', left=1600, top=0, right=1920, bottom=200, threshold=.95)
    config = dict(main_page_marker=target('home'), close_target=close, max_rounds=3, idle_rounds=2,
                  poll_seconds=1, pre_click_wait_seconds=1, post_click_wait_seconds=1)
    taps = []
    client = SimpleNamespace(screenshot=lambda: png(), tap=lambda *xy: taps.append(xy))
    summary = return_to_main(client, config, template_matcher=lambda *a: ((1800, 80), 1), sleep=lambda _: None)
    assert summary.clicks == 0 and taps == []


def test_default_configuration_migrates_every_main_entry_and_keeps_fixed_targets():
    settings = load_settings(ROOT / 'config.example.json')
    targets, markers = [], []
    def walk(value):
        if isinstance(value, dict):
            if value.get('type') == 'main_menu_tap': targets.append(value['main_menu'])
            if 'main_page_marker' in value: markers.append(value['main_page_marker'])
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(settings.tasks)
    assert sorted(targets) == ['activities', 'equipment', 'equipment_submenu'] + ['rewards'] * 7
    assert len(markers) == 17 and all(m['main_menu'] == 'home' and 'template' not in m for m in markers)
    assert settings.tasks['abundance'][2]['template'] == 'assets/abundance_label.png'
    assert any(action.get('type') == 'required_template_tap'
               and action.get('template') == 'assets/ichiraku_tab.png'
               for action in settings.tasks['ichiraku_takeout'])


@pytest.mark.parametrize('change', [
    lambda a: a.update(main_menu='home'), lambda a: a.update(main_menu='unknown'),
    lambda a: a['multimodal'].update(timeout_seconds=90),
    lambda a: a.update(template='unused.png'),
])
def test_invalid_main_entry_configuration_is_rejected(tmp_path, change):
    data = json.loads((ROOT / 'config.example.json').read_text(encoding='utf8'))
    change(data['tasks']['equipment_upgrade'][3])
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(data), encoding='utf8')
    with pytest.raises(ConfigError): load_settings(path)


@pytest.mark.parametrize('found', [True, False])
def test_scan_handoff_does_not_use_home_model_as_authorization(monkeypatch, tmp_path, found):
    from test_login_service_daily import session_for
    observations = []
    class MainModel:
        def __init__(self, *a): pass
        def match(self, image, target, timeout):
            assert target['main_menu'] == 'home' and 0 < timeout <= 1
            observations.append(image)
            return ((960, 540), 1) if found else (None, 0)
    monkeypatch.setattr(main_screen, 'MainScreenRecognition', MainModel)
    session, clock, calls = session_for(tmp_path, matcher=lambda *a: (None, 0),
        change=lambda data: data['tasks']['login'][-1].update(main_page_marker=target('home')))
    with pytest.raises(TimeoutError):
        session.wait_for_authorization(clock() + 1)
    assert not session._authorized
    assert observations == []
    assert not any('tap' in command for command, _ in calls)
