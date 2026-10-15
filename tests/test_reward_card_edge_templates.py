"""Replay each real task card in the last visible slot of the reward list."""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.common.execution import execution_scope
from naruto_daily.common.navigation import navigate_to_card
from naruto_daily.common.visual import find_template_in_png
from test_execution import Clock


ROOT = Path(__file__).parents[1]
CASES = [
    ('organization_pray_label.png', 'abundance_card_completed_right_edge.png', 683, True),
    ('coin_fortune_label.png', 'coin_fortune_card.png', 187, False),
    ('ninja_recruit_label.png', 'abundance_card_completed_right_edge.png', 1062, True),
    ('abundance_label.png', 'abundance_card.png', 862, False),
    ('secret_realm_label.png', 'abundance_card.png', 487, False),
    ('survival_challenge_label.png', 'abundance_card_completed_right_edge.png', 1446, True),
]


def card_actions():
    payload = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
    actions = {}

    def visit(value):
        if isinstance(value, dict):
            if value.get('type') == 'find_card_and_click':
                actions[Path(value['template']).name] = value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload['tasks'])
    return actions


def last_slot_frame(case):
    _, fixture, title_x, _ = case
    original = cv2.imdecode(np.frombuffer((ROOT / 'tests/fixtures' / fixture).read_bytes(),
                                         dtype=np.uint8), cv2.IMREAD_COLOR)
    canvas = np.zeros((1080, 1920, 3), dtype=np.uint8)
    shift_x = 1822 - title_x
    # The fortune fixture is a crop of the actual card, so restore its list height.
    shift_y = 140 if fixture == 'coin_fortune_card.png' else 0
    height = min(original.shape[0], 1080 - shift_y)
    width = min(original.shape[1], 1920 - shift_x)
    canvas[shift_y:shift_y + height, shift_x:shift_x + width] = original[:height, :width]
    return cv2.imencode('.png', canvas)[1].tobytes()


@pytest.mark.parametrize('case', CASES, ids=lambda c: c[0])
def test_every_reward_task_in_last_slot_identifies_its_own_completion_or_button(case):
    action = card_actions()[case[0]]
    clock = Clock()

    class LastSlotCard:
        taps = []

        def screenshot(self):
            return frame

        def swipe(self, *movement):
            raise AssertionError('An actionable last-slot card must not be swiped again')

        def tap(self, x, y):
            assert not case[3], 'Completed card must not be clicked'
            assert 1700 <= x < 1920 and 600 <= y <= 740
            self.taps.append((x, y))

    frame = last_slot_frame(case)
    client = LastSlotCard()
    with execution_scope(10, clock=clock, sleep=clock.sleep):
        summary = navigate_to_card(client, action, asset_root=ROOT, sleep=clock.sleep)
    assert summary.completed is case[3]
    assert summary.swipes == 0
    assert len(client.taps) == (0 if case[3] else 1)


@pytest.mark.parametrize(
    ('wanted', 'case'),
    [(wanted[0], case) for wanted in CASES for case in CASES if wanted[0] != case[0]],
    ids=[f'{wanted[0]}-vs-{case[0]}'
         for wanted in CASES for case in CASES if wanted[0] != case[0]],
)
def test_last_slot_title_does_not_match_another_reward_task(wanted, case):
    action = card_actions()[wanted]
    center, _ = find_template_in_png(last_slot_frame(case), ROOT / action['template'],
                                     (1580, 300, 1920, 560), action['threshold'])
    assert center is None
