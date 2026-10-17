"""Replay the supplied game screenshots through the real CV handler."""
import json
from pathlib import Path

import pytest

from naruto_daily.squad_assault import handle_squad_assault


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.mark.parametrize("initially_checked", [False, True])
@pytest.mark.parametrize("with_dialog", [False, True])
def test_real_frames_repeat_matches_until_limit_without_clicking_paid_option(
    initially_checked, with_dialog,
):
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    action = next(a for a in config["tasks"]["squad_assault"]
                  if a["type"] == "handle_squad_assault")
    names = (["squad_assault_ready_checked"] * 2 if initially_checked else [])
    # The first match can leave its old ready frame visible briefly.
    names += ["squad_assault_ready_unchecked"] * 2
    if with_dialog:
        names += ["squad_assault_quadruple_dialog"] * 2
    names += ["abundance_battle", "squad_assault_ready_unchecked",
              "abundance_battle", "squad_assault_limit"]
    frames = iter((name, (FIXTURES / (name + ".png")).read_bytes()) for name in names)
    taps, waits = [], []

    class Replay:
        current = None

        def screenshot(self):
            self.current, image = next(frames)
            return image

        def tap(self, x, y):
            taps.append((self.current, x, y))

    result = handle_squad_assault(Replay(), action, asset_root=ROOT, sleep=waits.append)
    assert result.matches == 2
    assert result.continues == int(with_dialog)
    assert result.quadruple_disabled is initially_checked
    assert sum(seconds == 5 for seconds in waits) >= 3

    for frame, x, y in taps:
        if frame == "squad_assault_ready_checked":
            assert 715 <= x <= 805 and 975 <= y <= 1050
        elif frame == "squad_assault_quadruple_dialog":
            # Only the left continue button; never the right paid-confirm button.
            assert 650 <= x <= 930 and 560 <= y <= 700
        else:
            assert frame == "squad_assault_ready_unchecked"
            assert 1640 <= x <= 1870 and 780 <= y <= 1040
    assert len(taps) == 2 + int(initially_checked) + int(with_dialog)


def test_obscured_ready_title_does_not_allow_a_second_match_before_departure():
    import cv2
    import numpy as np
    from naruto_daily.common.visual import find_template_in_png

    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    action = next(a for a in config["tasks"]["squad_assault"]
                  if a["type"] == "handle_squad_assault")
    ready = (FIXTURES / "squad_assault_ready_unchecked.png").read_bytes()
    image = cv2.imdecode(np.frombuffer(ready, np.uint8), cv2.IMREAD_COLOR)
    target = action["ready_marker"]
    image[target["top"]:target["bottom"], target["left"]:target["right"]] = 0
    obscured = cv2.imencode(".png", image)[1].tobytes()
    for key in ("ready_marker", "match_button", "quadruple_unchecked"):
        target = action[key]
        roi = tuple(target[edge] for edge in ("left", "top", "right", "bottom"))
        center, score = find_template_in_png(obscured, ROOT / target["template"], roi, target["threshold"])
        if key == "ready_marker":
            assert center is None
        else:
            assert center is not None and score > .99

    frames = iter([ready, obscured, ready,
                   (FIXTURES / "abundance_battle.png").read_bytes(),
                   (FIXTURES / "squad_assault_limit.png").read_bytes()])
    taps = []

    class Replay:
        number = 0

        def screenshot(self):
            self.number += 1
            return next(frames)

        def tap(self, x, y):
            taps.append((self.number, x, y))

    result = handle_squad_assault(Replay(), action, asset_root=ROOT, sleep=lambda _: None)
    assert result.matches == 1 and result.continues == 0
    assert len(taps) == 1 and taps[0][0] == 1
