from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from naruto_daily.common import main_screen
from naruto_daily.common.navigation import required_template_tap
from naruto_daily.common.visual import VisualError


ROOT = Path(__file__).parents[1]
MODEL = dict(base_url='https://example.test/v1', model='vision', timeout_seconds=30, max_calls=2)
TARGET = dict(name='奖励', main_menu='rewards', multimodal=MODEL)


@pytest.fixture(autouse=True)
def isolate_diagnostic_output(monkeypatch, tmp_path):
    save = main_screen.save_rejected_reward
    monkeypatch.setattr(main_screen, 'save_rejected_reward',
                        lambda _asset_root, *args: save(tmp_path, *args))


def fixture_png(name='main_rewards_right_hud.png'):
    # Live 1920x1080 replay with only the right HUD retained; personal information is removed.
    return (ROOT / 'tests/fixtures' / name).read_bytes()


def answer(candidate):
    return dict(page='main', target='rewards', found=True, candidate_id=candidate)


def fake_model(monkeypatch, complete):
    monkeypatch.setattr(main_screen, 'MultimodalClient',
                        lambda *args, **kwargs: SimpleNamespace(complete_json=complete))


@pytest.mark.parametrize('candidate', [1, 2, 3, 4, 5, 6, 8])
def test_other_candidate_is_rejected_even_when_reward_exists_in_same_frame(monkeypatch, candidate, caplog):
    fake_model(monkeypatch, lambda *args, **kwargs: answer(candidate))
    recognizer = main_screen.MainScreenRecognition(MODEL, ROOT)
    with caplog.at_level('INFO', logger='naruto_daily'):
        center, score = recognizer.match(fixture_png(), TARGET)
    assert center is None and score <= .5
    assert f'候选 {candidate}' in caplog.text and '相似度' in caplog.text


@pytest.mark.parametrize('fixture', ['main_rewards_right_hud.png', 'main_page_reward_dot.png'])
def test_reward_variants_keep_model_selected_center(monkeypatch, fixture):
    fake_model(monkeypatch, lambda *args, **kwargs: answer(7))
    center, score = main_screen.MainScreenRecognition(MODEL, str(ROOT)).match(fixture_png(fixture), TARGET)
    assert center == (1845, 537) and score > .5


def test_city_reward_below_old_threshold_keeps_model_selected_center(monkeypatch):
    fake_model(monkeypatch, lambda *args, **kwargs: answer(7))
    center, score = main_screen.MainScreenRecognition(MODEL, ROOT).match(
        fixture_png('main_rewards_city_hud.png'), TARGET)
    assert center == (1845, 537)
    assert .5 < score < .9


@pytest.mark.parametrize('scores,expected_center', [
    ((.49, .5), None),
    ((.5, .49), None),
    ((.5001, .49), (1845, 537)),
    ((.5, .5001), (1845, 537)),
])
def test_reward_confirmation_requires_score_strictly_above_half(monkeypatch, scores, expected_center):
    fake_model(monkeypatch, lambda *args, **kwargs: answer(7))
    values = iter(scores)
    monkeypatch.setattr(main_screen, 'find_template_in_png',
                        lambda *args: (None, next(values)))
    center, _ = main_screen.MainScreenRecognition(MODEL, ROOT).match(fixture_png(), TARGET)
    assert center == expected_center


@pytest.mark.parametrize('size', [(960, 540), (1920, 700)])
def test_reward_guard_rejects_unsupported_frame_size(monkeypatch, size):
    fake_model(monkeypatch, lambda *args, **kwargs: answer(7))
    frame = cv2.imdecode(np.frombuffer(fixture_png(), np.uint8), cv2.IMREAD_COLOR)
    resized = cv2.imencode('.png', cv2.resize(frame, size))[1].tobytes()
    assert main_screen.MainScreenRecognition(MODEL, ROOT).match(resized, TARGET)[0] is None


def test_mall_selected_repeatedly_never_taps(monkeypatch):
    calls, taps = [], []

    def complete(*args, **kwargs):
        calls.append(1)
        return answer(6)

    fake_model(monkeypatch, complete)
    client = SimpleNamespace(screenshot=fixture_png, tap=lambda *xy: taps.append(xy))
    action = dict(TARGET, type='main_menu_tap', timeout_seconds=60, poll_seconds=1,
                  pre_click_wait_seconds=1, post_click_wait_seconds=1)
    with pytest.raises(VisualError, match='调用上限'):
        required_template_tap(client, action, asset_root=ROOT, sleep=lambda _: None)
    assert taps == []
