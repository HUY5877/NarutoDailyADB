"""Real reward-card images exercise swipe selection and horizontal motion."""

import importlib
from pathlib import Path

import cv2
import numpy as np
import pytest


FIXTURES = Path(__file__).parent / "fixtures"


def helpers():
    try:
        return importlib.import_module("naruto_daily.common.reward_cards")
    except ModuleNotFoundError as error:
        if error.name == "naruto_daily.common.reward_cards":
            pytest.fail("Reward-card image helpers have not been implemented")
        raise


def image(name):
    return (FIXTURES / f"reward_cards_{name}.png").read_bytes()


def png(frame):
    return cv2.imencode(".png", frame)[1].tobytes()


def decode(data):
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def test_stuck_page_selects_card_interiors_instead_of_fixed_gap():
    # Catches selecting the old x1450 gap or a partially visible boundary card.
    starts = helpers().reward_card_swipe_starts(image("stuck"))
    assert len(starts) >= 2
    assert 1170 <= starts[0][0] <= 1320
    assert any(1550 <= x <= 1730 for x, _ in starts)
    assert all(y == 550 and x - 450 >= 300 for x, y in starts)
    complete_cards = [(708, 1055), (1088, 1435), (1467, 1814)]
    assert all(any(left + 40 < x < right - 40 for left, right in complete_cards)
               for x, _ in starts)
    assert [abs(x - 1450) for x, _ in starts] == sorted(abs(x - 1450) for x, _ in starts)


@pytest.mark.parametrize("name", ["moved", "near", "completed"])
def test_changed_page_recomputes_multiple_interior_starts(name):
    # Catches carrying a previous screenshot's card coordinates into a moved list.
    starts = helpers().reward_card_swipe_starts(image(name))
    assert len(starts) >= 2
    assert all(750 <= x < 1920 and y == 550 for x, y in starts)
    assert starts != helpers().reward_card_swipe_starts(image("stuck"))


def test_real_offset_swipe_measures_content_motion_not_gesture_distance():
    # Catches assuming the 450px gesture moved the cards exactly 450px.
    displacement = helpers().reward_card_displacement(image("stuck"), image("moved"))
    assert displacement is not None
    assert -545 < displacement < -525


@pytest.mark.parametrize("before,after", [("moved", "near"), ("near", "completed")])
def test_real_following_swipes_have_consistent_leftward_content_motion(before, after):
    # Catches matching the repeating card frame or a reused scroll icon to the wrong card.
    displacement = helpers().reward_card_displacement(image(before), image(after))
    assert displacement is not None
    assert -650 < displacement < -350


def test_reverse_pair_reports_rightward_motion():
    # Catches reporting an unsigned distance or reversing the before/after convention.
    displacement = helpers().reward_card_displacement(image("moved"), image("stuck"))
    assert displacement is not None
    assert 525 < displacement < 545


def test_unchanged_card_content_is_confirmed_stationary():
    # Catches treating an ineffective gap swipe as a successful movement.
    assert helpers().reward_card_displacement(image("stuck"), image("stuck")) == 0


def test_bottom_animation_cannot_fake_a_card_swipe():
    # Catches whole-screen comparison being driven by the animated reward boxes.
    after = decode(image("stuck"))
    after[760:] = np.random.default_rng(7).integers(0, 256, after[760:].shape, dtype=np.uint8)
    assert helpers().reward_card_displacement(image("stuck"), png(after)) == 0


def test_small_card_overlay_does_not_override_other_cards_motion():
    # Catches a single local animation or transient mark overruling independent card matches.
    after = decode(image("moved"))
    after[280:310, 600:630] = (0, 0, 0)
    displacement = helpers().reward_card_displacement(image("stuck"), png(after))
    assert displacement is not None
    assert -545 < displacement < -525


def test_repeated_gold_frames_without_distinct_content_do_not_confirm_motion():
    # Catches identifying a non-game grid or identical repeated cards by their frames alone.
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    for left in (327, 707, 1087, 1467):
        frame[140:740, left:left + 348] = (110, 190, 230)
    assert helpers().reward_card_swipe_starts(png(frame)) == []
    assert helpers().reward_card_displacement(png(frame), png(frame)) is None


@pytest.mark.parametrize("data", [b"not-a-png", png(np.zeros((1080, 1920, 3), dtype=np.uint8)),
                                  png(np.zeros((540, 960, 3), dtype=np.uint8))],
                         ids=["corrupt", "blank", "wrong-size"])
def test_unknown_or_invalid_page_never_produces_input_coordinates(data):
    # Catches interpreting invalid dimensions, blank screens or corrupt bytes as reward cards.
    assert helpers().reward_card_swipe_starts(data) == []
    assert helpers().reward_card_displacement(data, data) is None


def test_page_change_does_not_claim_card_displacement():
    # Catches continuing to drag after navigation or an overlay removes the reward cards.
    unknown = png(np.zeros((1080, 1920, 3), dtype=np.uint8))
    assert helpers().reward_card_displacement(image("stuck"), unknown) is None
