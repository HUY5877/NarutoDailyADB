"""有限选择恢复原场景协议；坐标和任意 OCR 继续由原路径处理。"""
from itertools import chain, combinations
from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto_daily.common import main_screen
from naruto_daily.common.main_screen import parse_main_screen
from naruto_daily.common.visual import VisualError
from naruto_daily.equipment.handler import _can_advance, _material, _material_entry
from naruto_daily.ichiraku import _takeout
from naruto_daily.multimodal.prompt_jev import ChoicePrompt, TAKEOUT_PROMPT, main_screen_prompt
from naruto_daily.multimodal import prompt_jev, prompts
from naruto_daily.secret_realm.enter import parse_entry_center


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('target,page', [
    ('equipment', 'main'), ('activities', 'main'), ('rewards', 'main'),
    ('equipment_submenu', 'equipment_menu'),
])
def test_main_choices_only_restore_current_frame_candidates_and_safe_pages(target, page):
    centers = {2: (111, 222), 7: (333, 444)}
    prompt = main_screen_prompt(target, centers)
    assert isinstance(prompt, ChoicePrompt)
    found = [result for result in prompt.choices.values() if result['found']]
    assert {result['candidate_id'] for result in found} == set(centers)
    assert all(result['page'] == page and result['target'] == target for result in found)
    assert {parse_main_screen(result, target, 1920, 1080, centers)[0] for result in found} == set(centers.values())
    missing = [result for result in prompt.choices.values() if not result['found']]
    assert {result['page'] for result in missing} == {'unknown', 'other', 'equipment_menu', 'main'}
    assert all(result['candidate_id'] is None for result in missing)
    assert all(parse_main_screen(result, target, 1920, 1080, centers)[0] is None for result in missing)
    assert not any(result['found'] for result in main_screen_prompt(target).choices.values())


def test_home_choices_only_confirm_the_full_main_foreground_without_candidates():
    prompt = main_screen_prompt('home', {99: (100, 200)})
    assert all(result['candidate_id'] is None for result in prompt.choices.values())
    found = [result for result in prompt.choices.values() if result['found']]
    assert found == [dict(page='main', target='home', found=True, candidate_id=None)]
    assert parse_main_screen(found[0], 'home', 1920, 1080, {}) == ((960, 540), 1.0)
    for result in prompt.choices.values():
        if result['page'] != 'main':
            assert parse_main_screen(result, 'home', 1920, 1080, {})[0] is None


def test_main_recognition_passes_the_same_frame_candidate_ids_to_choice_prompt(monkeypatch):
    centers = {11: (110, 220), 23: (330, 440)}
    monkeypatch.setattr(main_screen, 'button_candidates', lambda *a: (b'candidate-preview', centers))

    def complete(png, prompt, **kwargs):
        assert png == b'candidate-preview'
        found = [result for result in prompt.choices.values() if result['found']]
        assert {result['candidate_id'] for result in found} == set(centers)
        return next(result for result in found if result['candidate_id'] == 23)

    monkeypatch.setattr(main_screen, 'MultimodalClient',
                        lambda *a, **kw: SimpleNamespace(complete_json=complete))
    recognizer = main_screen.MainScreenRecognition({'timeout_seconds': 30}, ROOT)
    png = (ROOT / 'tests/fixtures/main_rewards_right_hud.png').read_bytes()
    assert recognizer.match(png, {'name': '装备', 'main_menu': 'equipment'}) == ((330, 440), 1.0)


def test_secret_entry_choices_only_restore_current_frame_ids_or_no_click():
    centers = {2: (100, 200), 9: (300, 400)}
    prompt = prompt_jev.secret_entry_prompt('ninja_challenge', '秘境探险', centers)
    assert isinstance(prompt, ChoicePrompt)
    found = [result for result in prompt.choices.values() if result['candidate_id'] is not None]
    assert {result['candidate_id'] for result in found} == set(centers)
    assert {parse_entry_center(result, 'ninja_challenge', '秘境探险', centers)
            for result in found} == set(centers.values())
    unknown = [result for result in prompt.choices.values() if result['page'] == 'unknown']
    assert unknown == [dict(page='unknown', name=None, candidate_id=None)]
    assert parse_entry_center(unknown[0], 'ninja_challenge', '秘境探险', centers) is None
    assert list(prompt_jev.secret_entry_prompt('main', '忍者挑战').choices.values()) == unknown


def test_battle_completion_has_only_confirmed_end_battle_and_safe_unknown():
    results = list(prompt_jev.BATTLE_COMPLETION.choices.values())
    assert {(result['finished'], result['page']) for result in results} == {
        (True, 'finished'), (False, 'battle'), (False, 'unknown'),
    }
    assert all(isinstance(result['reason'], str) and result['reason'] for result in results)
    assert {result['page'] for result in prompt_jev.ABUNDANCE_AFTER_BATTLE.choices.values()} == {
        'service_popup', 'abundance', 'home', 'result', 'unknown',
    }


def test_equipment_choices_preserve_actions_page_guards_and_selected_slot():
    assert {_material(result) for result in prompt_jev.EQUIPMENT_MATERIAL.choices.values()
            if result.get('action') in ('sweep', 'synthesize', 'equip')} == {'sweep', 'synthesize', 'equip'}
    info = next(result for result in prompt_jev.EQUIPMENT_MATERIAL_ENTRY.choices.values()
                if result['page'] == 'material_info')
    assert _material_entry(info) == 'material_info'
    with pytest.raises(VisualError):
        _material(info)
    assert {result['page'] for result in prompt_jev.EQUIPMENT_PAGE.choices.values()} == {
        'equipment', 'material', 'material_info', 'sweep_setup', 'sweep_result', 'unknown',
    }
    advance = [result for result in prompt_jev.EQUIPMENT_ADVANCE.choices.values() if result['page'] == 'equipment']
    assert {(result['selected_slot'], _can_advance(result, result['selected_slot']))
            for result in advance} == {(slot, can) for slot in range(1, 7) for can in (False, True)}
    with pytest.raises(VisualError):
        _can_advance(advance[0], 6)
    with pytest.raises(VisualError):
        _can_advance({'page': 'unknown'}, 1)


def test_takeout_choices_cover_all_eight_combinations_and_safe_foregrounds():
    takeout = [result for result in TAKEOUT_PROMPT.choices.values() if result['page'] == 'takeout']
    expected = set(chain.from_iterable(combinations((1, 2, 3), count) for count in range(4)))
    assert {tuple(_takeout(result)) for result in takeout} == expected
    reward = next(result for result in TAKEOUT_PROMPT.choices.values() if result['page'] == 'reward')
    assert _takeout(reward) is None
    unknown = next(result for result in TAKEOUT_PROMPT.choices.values() if result['page'] == 'unknown')
    with pytest.raises(VisualError):
        _takeout(unknown)


def test_free_text_numbers_and_coordinate_prompts_keep_the_generation_path():
    for prompt in (
        prompts.ACCOUNT_NICKNAME, prompts.FREE_RECRUIT, prompts.SECRET_REALM_TICKETS,
        prompts.SECRET_BATTLE_REALM, prompts.SURVIVAL_RESET_COUNT,
        prompts.EQUIPMENT, prompts.EQUIPMENT_STAMINA, prompts.EQUIPMENT_SWEEP_RESULT,
    ):
        assert type(prompt) is str


@pytest.mark.parametrize('candidate_id', [True, False, 0, -1, '2', 2.0])
def test_candidate_choices_reject_invalid_ids(candidate_id):
    with pytest.raises(ValueError):
        main_screen_prompt('equipment', [candidate_id])
    with pytest.raises(ValueError):
        prompt_jev.secret_entry_prompt('main', '忍者挑战', [candidate_id])
