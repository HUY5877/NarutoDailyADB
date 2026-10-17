"""CV replays use cropped game pixels, never generated game art.

Clipboard sources, in order: 95701b6b (rewards), 5a93ab88 (unchecked),
7184f65e (fourfold dialog), 87026254 (checked), c1802a38 (limit).
The dialog's window chrome is removed with (1, 34, 737, 448); the other
sources already contain the 736x414 game. All five are normalized to
1920x1080 with INTER_CUBIC. Template crops omit the red annotation edges.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.common.visual import find_template_in_png


ROOT = Path(__file__).parents[1]
ASSETS = ROOT / 'assets'
FIXTURES = ROOT / 'tests' / 'fixtures'
THRESHOLD = .9
REGIONS = {
    'label': (0, 400, 1920, 580),
    'ready': (80, 0, 820, 230),
    'quadruple_checked': (700, 960, 835, 1080),
    'quadruple_unchecked': (700, 960, 835, 1080),
    'match': (1550, 740, 1920, 1080),
    'continue': (570, 520, 1030, 740),
    'limit': (730, 955, 1220, 1080),
    'dialog': (490, 235, 1430, 880),
}


def fixture(name):
    return (FIXTURES / f'squad_assault_{name}.png').read_bytes()


def match(name, frame):
    return find_template_in_png(
        fixture(frame), ASSETS / f'squad_assault_{name}.png', REGIONS[name], THRESHOLD,
    )


@pytest.mark.parametrize('frame', [
    'rewards', 'ready_unchecked', 'quadruple_dialog', 'ready_checked', 'limit',
])
def test_game_fixtures_use_the_runtime_resolution_without_window_chrome(frame):
    image = cv2.imdecode(np.frombuffer(fixture(frame), np.uint8), cv2.IMREAD_COLOR)
    assert image.shape == (1080, 1920, 3)


@pytest.mark.parametrize('template,frame,center', [
    ('label', 'rewards', (1051, 498)),
    ('ready', 'ready_unchecked', (426, 109)),
    ('ready', 'ready_checked', (426, 109)),
    ('ready', 'limit', (426, 109)),
    ('quadruple_checked', 'ready_checked', (763, 1017)),
    ('quadruple_unchecked', 'ready_unchecked', (763, 1017)),
    ('match', 'ready_unchecked', (1753, 926)),
    ('match', 'ready_checked', (1753, 926)),
    ('match', 'limit', (1753, 926)),
    ('continue', 'quadruple_dialog', (792, 617)),
    ('dialog', 'quadruple_dialog', (920, 328)),
    ('limit', 'limit', (981, 1022)),
])
def test_templates_locate_the_actual_game_controls(template, frame, center):
    actual, score = match(template, frame)
    assert actual == center
    assert score >= .98


@pytest.mark.parametrize('template,frame', [
    ('quadruple_checked', 'ready_unchecked'),
    ('quadruple_checked', 'limit'),
    ('quadruple_unchecked', 'ready_checked'),
    ('quadruple_unchecked', 'limit'),
    ('quadruple_checked', 'quadruple_dialog'),
    ('quadruple_unchecked', 'quadruple_dialog'),
    ('limit', 'ready_unchecked'),
    ('limit', 'ready_checked'),
    ('limit', 'quadruple_dialog'),
    ('continue', 'ready_unchecked'),
    ('continue', 'ready_checked'),
    ('continue', 'limit'),
    ('dialog', 'ready_unchecked'),
    ('dialog', 'ready_checked'),
    ('dialog', 'limit'),
    ('ready', 'rewards'),
    ('label', 'ready_unchecked'),
])
def test_opposite_states_and_unrelated_pages_cannot_supply_controls(template, frame):
    center, score = match(template, frame)
    assert center is None
    assert score < .85


def test_modal_foreground_requires_dialog_guard_even_when_background_correlates():
    # Normalized correlation tolerates dimming, so a title alone is not a page guard.
    ready_center, ready_score = match('ready', 'quadruple_dialog')
    assert ready_center is not None and ready_score > .98
    assert match('dialog', 'quadruple_dialog')[0] is not None
    assert match('continue', 'quadruple_dialog')[0] is not None

    image = cv2.imdecode(np.frombuffer(fixture('quadruple_dialog'), np.uint8), 1)
    template = cv2.imread(str(ASSETS / 'squad_assault_ready.png'))
    height, width = template.shape[:2]
    left, top = ready_center[0] - width // 2, ready_center[1] - height // 2
    background = image[top:top + height, left:left + width]
    ratio = (cv2.cvtColor(background, cv2.COLOR_BGR2GRAY).mean()
             / cv2.cvtColor(template, cv2.COLOR_BGR2GRAY).mean())
    assert ratio < .3


def test_card_button_is_scoped_to_squad_assault_instead_of_its_neighbors():
    title, _ = match('label', 'rewards')
    left, top, right, bottom = title[0] - 140, title[1] + 80, title[0] + 185, title[1] + 240
    assert left >= 871 and right < 1255  # Bounds of this card and the next card.
    center, score = find_template_in_png(
        fixture('rewards'), ASSETS / 'go_now_button.png', (left, top, right, bottom), THRESHOLD,
    )
    assert center == (1075, 657)
    assert score > .98
    completed, _ = find_template_in_png(
        fixture('rewards'), ASSETS / 'completed_stamp.png', (left, top, right, bottom), THRESHOLD,
    )
    assert completed is None


def test_shared_completed_stamp_keeps_its_scale_and_does_not_borrow_an_adjacent_stamp():
    png = (FIXTURES / 'reward_cards_completed.png').read_bytes()
    center, score = find_template_in_png(png, ASSETS / 'completed_stamp.png',
                                         (700, 578, 955, 745), THRESHOLD)
    assert center == (840, 652) and score > .98
    center, score = find_template_in_png(png, ASSETS / 'completed_stamp.png',
                                         (310, 578, 635, 739), THRESHOLD)
    assert center is None and score < .85
