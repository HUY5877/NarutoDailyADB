"""自动按钮只能使用仍有效的战斗观察，不能沿用切页前的判断。"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.multimodal import prompt_jev
from naruto_daily.squad_assault import handler


ROOT = Path(__file__).resolve().parents[1]


def encode(frame):
    return cv2.imencode('.png', frame)[1].tobytes()


def test_jev_has_only_one_explicit_auto_click_permission():
    prompt = prompt_jev.SQUAD_AUTO_BATTLE
    assert isinstance(prompt, prompt_jev.ChoicePrompt)
    allowed = [item for item in prompt.choices.values() if item['can_enable_auto']]
    assert allowed == [dict(page='battle', can_enable_auto=True)]
    assert {'other', 'unknown'} <= {item['page'] for item in prompt.choices.values()}


def test_fresh_frame_can_change_battle_animation_without_changing_auto_button():
    png = (ROOT / 'tests/fixtures/squad_assault_battle_auto_off.png').read_bytes()
    frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    frame[430:950, 200:1500] = 0
    assert handler._auto_button_unchanged(png, encode(frame))


@pytest.mark.parametrize('fixture', [
    'main_rewards_right_hud.png', 'squad_assault_ready_unchecked.png',
    'squad_assault_quadruple_dialog.png', 'squad_assault_limit.png',
])
def test_battle_confirmation_expires_when_fresh_frame_has_changed_page(fixture):
    battle = (ROOT / 'tests/fixtures/squad_assault_battle_auto_off.png').read_bytes()
    other = (ROOT / 'tests/fixtures' / fixture).read_bytes()
    assert not handler._auto_button_unchanged(battle, other)


def test_dimmed_or_blank_button_and_changed_resolution_cannot_reuse_observation():
    png = (ROOT / 'tests/fixtures/squad_assault_battle_auto_off.png').read_bytes()
    frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert not handler._auto_button_unchanged(png, encode((frame * .5).astype(np.uint8)))
    assert not handler._auto_button_unchanged(png, encode(np.zeros_like(frame)))
    assert not handler._auto_button_unchanged(png, encode(frame[::2, ::2]))
    blank = encode(np.zeros_like(frame))
    assert not handler._auto_button_unchanged(blank, blank)
