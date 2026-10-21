import asyncio
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import TemplateConfigurationError, VisualError, find_template_in_png
from naruto_daily.squad_assault import SquadAssaultSummary, handle_squad_assault
from naruto_daily.squad_assault import handler as squad_handler
from naruto_daily.squad_assault.handler import _ready_state


POINTS = {
    "ready_marker": (26, 18), "match_button": (100, 70),
    "quadruple_checked": (50, 60), "quadruple_unchecked": (50, 60),
    "dialog_marker": (70, 20), "continue_button": (80, 45), "limit_marker": (75, 60),
    "auto_battle_marker": (75, 15),
}
READY = {"ready_marker", "match_button", "quadruple_unchecked"}
CHECKED = {"ready_marker", "match_button", "quadruple_checked"}
CAP = READY | {"limit_marker"}
BATTLE_ON = {"auto_battle_marker"}


@dataclass
class Scene:
    visible: set
    brightness: float = 1


class SceneClient:
    def __init__(self, scenes, template):
        self.scenes = [item if isinstance(item, Scene) else Scene(item) for item in scenes]
        self.template = template
        self.screenshot_count = 0
        self.taps = []
        self.probes = []
        self.current = None
        self.before_screenshot = lambda: None
        self.after_probe = lambda name: None

    def screenshot(self):
        self.before_screenshot()
        self.current = self.scenes[min(self.screenshot_count, len(self.scenes) - 1)]
        self.screenshot_count += 1
        image = np.zeros((96, 128, 3), dtype=np.uint8)
        if "ready_marker" in self.current.visible:
            image[10:26, 10:42] = (self.template * self.current.brightness).astype(np.uint8)
        return cv2.imencode(".png", image)[1].tobytes()

    def tap(self, x, y):
        self.taps.append((x, y))

    def matcher(self, png, path, roi, threshold):
        assert png.startswith(b"\x89PNG")
        assert roi == (0, 0, 128, 96)
        assert threshold == .9
        key = path.stem
        self.probes.append((self.screenshot_count, key))
        self.after_probe(key)
        return (POINTS[key], .99) if key in self.current.visible else (None, .2)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    template = np.random.default_rng(4).integers(90, 245, (16, 32, 3), dtype=np.uint8)
    cv2.imwrite(str(tmp_path / "ready_marker.png"), template)
    action = {"type": "handle_squad_assault", "timeout_seconds": 60, "poll_seconds": 5,
              "post_click_wait_seconds": 1}
    for key in POINTS:
        action[key] = dict(name=key, template=f"{key}.png", left=0, top=0, right=128, bottom=96, threshold=.9)
    monkeypatch.setattr(squad_handler, 'AUTO_BATTLE_MARKER', action['auto_battle_marker'], raising=False)

    def run(scenes, *, changes=None):
        client = SceneClient(scenes, template)
        waits = []
        selected = {**action, **(changes or {})}
        summary = handle_squad_assault(client, selected, template_matcher=client.matcher,
                                      asset_root=tmp_path, sleep=waits.append)
        return client, waits, summary

    return tmp_path, template, action, run


def test_first_round_is_do_while_even_if_initial_limit_is_visible(setup):
    _, _, _, run = setup
    client, waits, summary = run([CAP, BATTLE_ON, CAP])
    assert summary == SquadAssaultSummary(matches=1, continues=0, quadruple_disabled=False)
    assert client.taps == [POINTS["match_button"]]
    assert [number for number, key in client.probes if key == "limit_marker"] == [3]
    assert waits == [1, 5]


def test_checked_is_clicked_once_and_only_explicit_unchecked_allows_matching(setup):
    _, _, _, run = setup
    unknown = {"ready_marker", "match_button"}
    client, waits, summary = run([CHECKED, CHECKED, unknown, READY, BATTLE_ON, CAP])
    assert client.taps == [POINTS["quadruple_checked"], POINTS["match_button"]]
    assert summary.quadruple_disabled is True
    assert waits == [1, 5, 5, 1, 5]


def test_optional_dialog_only_continues_once_and_does_not_count_as_departure(setup):
    _, _, _, run = setup
    dialog = Scene(CAP | {"dialog_marker", "continue_button"}, .45)
    dimmed_without_dialog_marker = Scene(CAP, .45)
    client, _, summary = run([READY, dialog, dialog, dimmed_without_dialog_marker, CAP, BATTLE_ON, CAP])
    assert summary.matches == 1 and summary.continues == 1
    assert client.taps == [POINTS["match_button"], POINTS["continue_button"]]
    assert client.screenshot_count == 7
    assert [number for number, key in client.probes if key == "limit_marker"] == [7]


def test_no_dialog_polls_battle_every_five_seconds_and_repeats_until_limit(setup):
    _, _, _, run = setup
    client, waits, summary = run([READY, BATTLE_ON, BATTLE_ON, READY, BATTLE_ON, CAP])
    assert summary == SquadAssaultSummary(matches=2, continues=0, quadruple_disabled=False)
    assert client.taps == [POINTS["match_button"]] * 2
    assert waits == [1, 5, 5, 1, 5]


def test_still_ready_after_match_does_not_match_or_check_limit_again(setup):
    _, _, _, run = setup
    client, waits, summary = run([READY, CAP, CAP, BATTLE_ON, CAP])
    assert summary.matches == 1
    assert client.taps == [POINTS["match_button"]]
    assert waits == [1, 5, 5, 5]
    assert [number for number, key in client.probes if key == "limit_marker"] == [5]


@pytest.mark.parametrize("remaining_control", ["match_button", "quadruple_checked", "quadruple_unchecked"])
def test_missing_title_with_remaining_preparation_control_does_not_count_as_departure(setup, remaining_control):
    _, _, _, run = setup
    client, _, summary = run([READY, {remaining_control}, READY, BATTLE_ON, CAP])
    assert summary.matches == 1
    assert client.taps == [POINTS["match_button"]]
    assert [number for number, key in client.probes if key == "limit_marker"] == [5]


def test_return_page_with_unknown_checkbox_keeps_observing_limit(setup):
    _, _, _, run = setup
    unknown = {"ready_marker", "match_button"}
    client, _, summary = run([READY, BATTLE_ON, unknown, CAP])
    assert summary.matches == 1 and client.screenshot_count == 4
    assert client.taps == [POINTS["match_button"]]


@pytest.mark.parametrize("visible", [set(), READY | {"quadruple_checked"}])
def test_unknown_or_conflicting_checkbox_never_counts_as_off(setup, visible):
    root, template, action, _ = setup
    client = SceneClient([visible | {"ready_marker", "match_button", "limit_marker"}], template)
    with pytest.raises(VisualError, match="超时"):
        handle_squad_assault(client, {**action, "timeout_seconds": 10},
                             template_matcher=client.matcher, asset_root=root, sleep=lambda _: None)
    assert client.taps == []


@pytest.mark.parametrize("last", [CHECKED, {"ready_marker", "match_button"}])
def test_unconfirmed_checkbox_toggle_is_not_retried_and_cannot_match(setup, last):
    root, template, action, _ = setup
    client = SceneClient([CHECKED, last], template)
    with pytest.raises(VisualError, match="超时"):
        handle_squad_assault(client, {**action, "timeout_seconds": 10},
                             template_matcher=client.matcher, asset_root=root, sleep=lambda _: None)
    assert client.taps == [POINTS["quadruple_checked"]]


def test_dialog_without_continue_never_clicks_dimmed_match_button(setup):
    root, template, action, _ = setup
    client = SceneClient([READY, Scene(CAP | {"dialog_marker"}, .45)], template)
    with pytest.raises(VisualError, match="超时"):
        handle_squad_assault(client, {**action, "timeout_seconds": 10},
                             template_matcher=client.matcher, asset_root=root, sleep=lambda _: None)
    assert client.taps == [POINTS["match_button"]]
    assert not any(key == "limit_marker" for _, key in client.probes)


def test_shared_deadline_clips_wait_and_prevents_further_input(setup):
    root, template, action, _ = setup
    now = [0.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    client = SceneClient([READY, BATTLE_ON], template)
    with pytest.raises(DailyDeadlineExceeded), execution_scope(4, clock=lambda: now[0], sleep=sleep):
        handle_squad_assault(client, action, template_matcher=client.matcher, asset_root=root)
    assert client.taps == [POINTS["match_button"]]
    assert now == [4] and waits == [1, 3]


def test_shared_budget_is_not_restarted_or_cut_short_by_legacy_stage_timeout(setup):
    root, template, action, _ = setup
    now = [0.0]
    client = SceneClient([READY, BATTLE_ON, CAP], template)
    with execution_scope(8, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)):
        summary = handle_squad_assault(client, {**action, "timeout_seconds": 1},
                                      template_matcher=client.matcher, asset_root=root)
    assert summary.matches == 1 and now == [6]


def test_deadline_after_template_work_rejects_late_match_before_click(setup):
    root, template, action, _ = setup
    now = [0.0]
    client = SceneClient([READY], template)

    def after_probe(key):
        if key == "match_button":
            now[0] = 4

    client.after_probe = after_probe
    with pytest.raises(DailyDeadlineExceeded), execution_scope(4, clock=lambda: now[0]):
        handle_squad_assault(client, action, template_matcher=client.matcher, asset_root=root)
    assert client.taps == []


def test_standalone_timeout_counts_screenshot_wall_time(setup, monkeypatch):
    import naruto_daily.squad_assault.handler as handler
    root, template, action, _ = setup
    now = [0.0]
    monkeypatch.setattr(handler.time, "monotonic", lambda: now[0])
    client = SceneClient([READY], template)
    client.before_screenshot = lambda: now.__setitem__(0, 61)
    with pytest.raises(VisualError, match="超时"):
        handle_squad_assault(client, action, template_matcher=client.matcher, asset_root=root, sleep=lambda _: None)
    assert client.taps == []


def test_cancellation_propagates_without_further_input(setup):
    root, template, action, _ = setup
    client = SceneClient([READY], template)

    def before_screenshot():
        if client.screenshot_count:
            raise asyncio.CancelledError()

    client.before_screenshot = before_screenshot
    with pytest.raises(asyncio.CancelledError):
        handle_squad_assault(client, action, template_matcher=client.matcher, asset_root=root, sleep=lambda _: None)
    assert client.taps == [POINTS["match_button"]]


def test_invalid_png_or_incomplete_ready_patch_is_rejected(setup):
    root, template, action, _ = setup
    client = SceneClient([READY], template)
    client.current = Scene(READY)
    images = ImageRecognition(client, lambda *args: (POINTS["ready_marker"], .99), root)
    with pytest.raises(VisualError, match="截图"):
        _ready_state(images, b"not-png", action["ready_marker"], {})
    png = client.screenshot()
    images.matcher = lambda *args: ((0, 0), .99)
    with pytest.raises(VisualError, match="裁剪不完整"):
        _ready_state(images, png, action["ready_marker"], {})


def test_targets_cannot_invoke_models(setup):
    root, template, action, _ = setup
    action["ready_marker"]["main_menu"] = {}
    client = SceneClient([READY], template)
    with pytest.raises(TemplateConfigurationError, match="CV 模板"):
        handle_squad_assault(client, action, template_matcher=client.matcher, asset_root=root)
    assert client.screenshot_count == 0


def real_action():
    action = {"timeout_seconds": 30, "poll_seconds": 5, "post_click_wait_seconds": 1}
    for key, name, roi in [
        ("ready_marker", "ready", (80, 0, 820, 230)),
        ("match_button", "match", (1550, 740, 1920, 1080)),
        ("quadruple_checked", "quadruple_checked", (700, 960, 835, 1080)),
        ("quadruple_unchecked", "quadruple_unchecked", (700, 960, 835, 1080)),
        ("dialog_marker", "dialog", (490, 235, 1430, 880)),
        ("continue_button", "continue", (570, 520, 1030, 740)),
        ("limit_marker", "limit", (730, 955, 1220, 1080)),
    ]:
        action[key] = dict(name=key, template=f"assets/squad_assault_{name}.png", threshold=.9,
                           **dict(zip(("left", "top", "right", "bottom"), roi)))
    return action


def test_real_dialog_background_correlates_but_brightness_rejects_ready():
    root = Path(__file__).resolve().parents[1]
    target = real_action()["ready_marker"]
    png = (root / "tests/fixtures/squad_assault_quadruple_dialog.png").read_bytes()
    roi = tuple(target[key] for key in ("left", "top", "right", "bottom"))
    center, score = find_template_in_png(png, root / target["template"], roi, .9)
    assert center is not None and score > .95
    images = ImageRecognition(None, asset_root=root)
    assert _ready_state(images, png, target, {}) == "dimmed"
    ready = (root / "tests/fixtures/squad_assault_ready_unchecked.png").read_bytes()
    assert _ready_state(images, ready, target, {}) == "ready"


def test_real_frames_match_continue_and_finish_at_limit_without_other_clicks():
    root = Path(__file__).resolve().parents[1]
    black = cv2.imencode(".png", np.zeros((1080, 1920, 3), np.uint8))[1].tobytes()
    frames = iter([
        (root / "tests/fixtures/squad_assault_ready_unchecked.png").read_bytes(),
        (root / "tests/fixtures/squad_assault_quadruple_dialog.png").read_bytes(),
        black,
        (root / "tests/fixtures/squad_assault_limit.png").read_bytes(),
    ])

    class Client:
        taps = []

        def screenshot(self):
            return next(frames)

        def tap(self, x, y):
            self.taps.append((x, y))

    client = Client()
    summary = handle_squad_assault(client, real_action(), asset_root=root, sleep=lambda _: None)
    assert summary == SquadAssaultSummary(1, 1, False)
    assert len(client.taps) == 3
    assert client.taps[-1] == (45, 330)
    assert 1550 <= client.taps[0][0] < 1920 and 740 <= client.taps[0][1] < 1080
    assert 570 <= client.taps[1][0] < 1030 and 520 <= client.taps[1][1] < 740


def test_missing_auto_text_clicks_fixed_button_once_per_poll_without_extra_screenshots(setup):
    _, _, _, run = setup
    client, waits, summary = run([READY, set(), set(), BATTLE_ON, CAP])
    assert summary == SquadAssaultSummary(matches=1, continues=0, quadruple_disabled=False)
    assert client.taps == [POINTS['match_button'], (45, 330), (45, 330)]
    assert client.screenshot_count == 5
    assert waits == [1, 5, 5, 5]
    assert [number for number, key in client.probes if key == 'auto_battle_marker'] == [2, 3, 4]
