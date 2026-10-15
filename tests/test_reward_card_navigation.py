"""Replay real reward screenshots through navigation, including an ignored drag."""
import json
import logging
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.common.navigation import navigate_to_card
from naruto_daily.common.visual import VisualError


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def action():
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    return dict(next(a for a in config["tasks"]["recruit"]
                     if a["type"] == "find_card_and_click"), max_swipes=6)


class RewardReplay:
    def __init__(self):
        self.frames = [(FIXTURES / f"reward_cards_{name}.png").read_bytes()
                       for name in ("stuck", "moved", "near", "completed")]
        self.index = 0
        self.swipes = []
        self.taps = []
        self.ignored_start = None

    def screenshot(self):
        return self.frames[self.index]

    def swipe(self, x1, y1, x2, y2, duration):
        self.swipes.append((x1, y1, x2, y2, duration))
        assert x1 - x2 == 450 and y1 == y2 == 550 and duration == 600
        if self.index == 0:
            # These bounds were measured in the stuck screenshot. x=1450 is a gap.
            if not any(left + 20 < x1 < right - 20
                       for left, right in ((708, 1053), (1088, 1434), (1468, 1813))):
                return
            if self.ignored_start is None:
                self.ignored_start = (x1, y1)
                return
            if (x1, y1) == self.ignored_start:
                return
        self.index = min(self.index + 1, 3)

    def tap(self, *point):
        self.taps.append(point)


def test_ignored_drag_retries_from_another_card_then_finds_completed_recruit(caplog):
    client = RewardReplay()
    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        result = navigate_to_card(client, action(), asset_root=ROOT, sleep=lambda _: None)

    assert result.completed and result.swipes == 3
    assert len(client.swipes) == 4
    assert client.swipes[0][:2] != client.swipes[1][:2]
    assert client.index == 3 and not client.taps
    assert "未产生位移" in caplog.text
    assert "实际向左移动" in caplog.text


def test_ignored_alignment_drag_can_retry_before_using_alignment_allowance():
    client = RewardReplay()

    def matcher(png, path, roi, threshold):
        if client.index == 0:
            return ((1820, 492), .99) if path.name == "ninja_recruit_label.png" else (None, .1)
        if path.name == "ninja_recruit_label.png":
            return (1260, 492), .99
        return ((1260, 650), .99) if path.name == "completed_stamp.png" else (None, .1)

    result = navigate_to_card(client, action(), template_matcher=matcher,
                              asset_root=ROOT, sleep=lambda _: None)
    assert result.completed and result.swipes == 1
    assert len(client.swipes) == 2
    assert client.index == 1 and not client.taps


def test_unknown_page_does_not_receive_a_blind_swipe():
    client = RewardReplay()
    client.frames = [cv2.imencode(".png", np.zeros((1080, 1920, 3), dtype=np.uint8))[1].tobytes()]
    with pytest.raises(VisualError, match="卡片内部"):
        navigate_to_card(client, action(), asset_root=ROOT, sleep=lambda _: None)
    assert not client.swipes and not client.taps


def test_uncertain_displacement_reobserves_without_another_drag():
    client = RewardReplay()
    client.frames[1] = cv2.imencode(".png", np.zeros((1080, 1920, 3), dtype=np.uint8))[1].tobytes()

    def swipe(*movement):
        client.swipes.append(movement)
        client.index = 1

    client.swipe = swipe
    with pytest.raises(VisualError, match="位移"):
        navigate_to_card(client, action(), asset_root=ROOT, sleep=lambda _: None)
    assert len(client.swipes) == 1 and not client.taps


def test_completed_recruit_does_not_swipe_or_click():
    client = RewardReplay()
    client.index = 3
    result = navigate_to_card(client, action(), asset_root=ROOT, sleep=lambda _: None)
    assert result.completed and result.swipes == 0
    assert not client.swipes and not client.taps


def test_completed_abundance_at_right_screen_edge_is_confirmed_without_input():
    from naruto_daily.common.execution import execution_scope
    from test_execution import Clock

    config = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
    abundance = next(a for a in config['tasks']['abundance'] if a['type'] == 'find_card_and_click')
    frame = (FIXTURES / 'abundance_card_completed_right_edge.png').read_bytes()
    clock = Clock()

    class CompletedCard:
        def screenshot(self):
            return frame

        def tap(self, *point):
            raise AssertionError('Completed abundance must not receive a click')

        def swipe(self, *movement):
            raise AssertionError('Visible completed abundance must not receive another swipe')

    with execution_scope(10, clock=clock, sleep=clock.sleep):
        result = navigate_to_card(CompletedCard(), abundance, asset_root=ROOT, sleep=clock.sleep)
    assert result.completed and result.swipes == 0


def test_failed_attempt_does_not_consume_the_successful_swipe_limit():
    client = RewardReplay()
    result = navigate_to_card(client, dict(action(), max_swipes=1),
                              template_matcher=lambda *args: (None, .1),
                              asset_root=ROOT, sleep=lambda _: None)
    assert result.swipes == 1
    assert len(client.swipes) == 2 and client.index == 1
