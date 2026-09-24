from __future__ import annotations

from pathlib import Path

import pytest

from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.config import load_settings
from naruto_daily.survival_challenge.handler import handle_survival_challenge


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('key,fixture,bounds', [
    ('start_sweep', 'survival_map.png', (1080, 910, 1250, 1050)),
    ('ready', 'survival_roster.png', (805, 830, 1110, 940)),
    ('confirm_first', 'survival_confirm_first.png', (805, 625, 1110, 735)),
    ('confirm_second', 'survival_confirm_second.png', (625, 625, 935, 735)),
])
def test_survival_templates_find_actual_buttons_at_adb_resolution(key, fixture, bounds):
    settings = load_settings(ROOT / 'config.example.json')
    action_value = next(a for a in settings.tasks['survival_challenge']
                        if a['type'] == 'handle_survival_challenge')
    t = action_value[key]
    center, score = find_template_in_png(
        (ROOT / 'tests/fixtures' / fixture).read_bytes(), ROOT / t['template'],
        tuple(t[k] for k in ('left', 'top', 'right', 'bottom')), t['threshold'],
    )
    assert center is not None, f'{key} did not match: {score:.3f}'
    assert bounds[0] <= center[0] <= bounds[2]
    assert bounds[1] <= center[1] <= bounds[3]


@pytest.mark.parametrize('key,fixture', [
    ('confirm_first', 'survival_confirm_second.png'),
    ('confirm_second', 'survival_confirm_first.png'),
    ('ready', 'survival_map.png'),
])
def test_survival_does_not_click_buttons_from_wrong_stage(key, fixture):
    action_value = next(a for a in load_settings(ROOT / 'config.example.json').tasks['survival_challenge']
                        if a['type'] == 'handle_survival_challenge')
    t = action_value[key]
    center, _ = find_template_in_png(
        (ROOT / 'tests/fixtures' / fixture).read_bytes(), ROOT / t['template'],
        tuple(t[k] for k in ('left', 'top', 'right', 'bottom')), t['threshold'],
    )
    assert center is None


def target(name):
    return {
        'name': name, 'template': name + '.png',
        'left': 0, 'top': 0, 'right': 1920, 'bottom': 1080,
        'threshold': .85,
    }


def action():
    return {
        'type': 'handle_survival_challenge',
        'timeout_seconds': 180,
        'max_rounds': 5,
        'wait_seconds': 60,
        'poll_seconds': 1,
        'transfer_marker': target('transfer'),
        'start_sweep': target('start_sweep'),
        'ready': target('ready'),
        'confirm_first': target('confirm_first'),
        'confirm_second': target('confirm_second'),
        'reset': target('reset'),
        'reset_confirm': target('reset_confirm'),
        'multimodal': {
            'base_url': 'https://example.test/v1', 'model': 'vision',
            'timeout_seconds': 10, 'max_calls': 6,
        },
    }


class Client:
    def __init__(self, transfer=True):
        self.state = 'transfer' if transfer else 'map'
        self.taps = []

    def screenshot(self):
        return self.state.encode()

    def tap(self, x, y):
        self.taps.append((x, y, self.state))
        if self.state == 'transfer':
            self.state = 'map'
        elif self.state == 'map' and x == 1100:
            self.state = 'ready'
        elif self.state == 'ready':
            self.state = 'confirm_first'
        elif self.state == 'confirm_first':
            self.state = 'confirm_second'
        elif self.state == 'confirm_second':
            self.state = 'map'
        elif self.state == 'map' and x == 1400:
            self.state = 'reset_confirm'
        elif self.state == 'reset_confirm':
            self.state = 'map'


class Model:
    def __init__(self, counts):
        self.counts = iter(counts)
        self.calls = 0

    def complete_json(self, png, prompt, *, timeout_seconds):
        self.calls += 1
        return {'reset_count': next(self.counts)}


def run(action_value, client, model, waits, *, background_start=False):
    def matcher(png, path, roi, threshold):
        name = path.stem
        visible = {
            'transfer': {'transfer'},
            'map': {'start_sweep', 'reset'},
            'ready': {'ready'},
            'confirm_first': {'confirm_first'},
            'confirm_second': {'confirm_second'},
            'reset_confirm': {'reset_confirm'},
        }.get(client.state, set())
        if background_start and client.state in {'ready', 'confirm_first', 'confirm_second'}:
            visible.add('start_sweep')
        centers = {
            'start_sweep': (1100, 900), 'ready': (1100, 900),
            'confirm_first': (1100, 900), 'confirm_second': (1100, 900),
            'reset': (1400, 900), 'reset_confirm': (1100, 900),
            'transfer': (1000, 400),
        }
        return (centers[name], .99) if name in visible else (None, .2)

    return handle_survival_challenge(client, action_value, model=model,
                                     template_matcher=matcher, sleep=waits.append)


def test_survival_dismisses_transfer_then_sweeps_until_model_reports_zero():
    client, waits = Client(), []
    run(action(), client, Model([2, 0]), waits)
    assert [(x, y) for x, y, _ in client.taps] == [
        (960, 540), (1100, 900), (1100, 900), (1100, 900),
        (1100, 900), (1400, 900), (1100, 900), (1100, 900),
        (1100, 900), (1100, 900), (1100, 900),
    ]
    assert waits.count(60) == 2


def test_survival_without_transfer_starts_directly_and_zero_breaks():
    client, waits = Client(transfer=False), []
    run(action(), client, Model([0]), waits)
    assert client.taps[0][0] == 1100
    assert (960, 540, 'map') not in client.taps
    assert waits[-1] == 60


def test_survival_rejects_ambiguous_reset_count_without_reset_click():
    client, waits = Client(transfer=False), []
    with pytest.raises(VisualError, match='重置次数'):
        run(action(), client, Model([1]), waits)
    assert not any(x == 1400 for x, _, _ in client.taps)


def test_survival_rejects_invalid_model_result():
    client, waits = Client(transfer=False), []
    with pytest.raises(VisualError, match='重置次数'):
        run(action(), client, Model([None]), waits)
    assert not any(x == 1400 for x, _, _ in client.taps)


def test_unresponsive_start_sweep_retries_twice_then_stops():
    client, waits = Client(transfer=False), []
    action_value = action()
    action_value['timeout_seconds'] = 900
    client.tap = lambda x, y: client.taps.append((x, y, client.state))
    with pytest.raises(VisualError, match='开始扫荡点击后仍停留原页面，已补点 2 次'):
        run(action_value, client, Model([]), waits)
    assert len(client.taps) == 3  # Initial click and at most two fresh-screen retries.
    assert all(state == 'map' for _, _, state in client.taps)
    assert sum(waits) <= 21


def test_start_sweep_recovers_when_first_click_does_not_change_page(caplog):
    client, waits = Client(transfer=False), []
    original_tap = client.tap

    def tap(x, y):
        if not client.taps:
            client.taps.append((x, y, client.state))
        else:
            original_tap(x, y)

    client.tap = tap
    with caplog.at_level('INFO', logger='naruto_daily'):
        run(action(), client, Model([0]), waits)
    assert [state for _, _, state in client.taps] == ['map', 'map', 'ready', 'confirm_first', 'confirm_second']
    assert '第1/2次补点' in caplog.text
    assert '停止补点开始扫荡' in caplog.text
    assert waits[-1] == 60


@pytest.mark.parametrize('next_stage', ['ready', 'confirm_first', 'confirm_second'])
def test_foreground_stage_prevents_retrying_start_sweep_behind_dialog(next_stage):
    client, waits = Client(transfer=False), []
    original_tap = client.tap

    def tap(x, y):
        was_map = client.state == 'map'
        original_tap(x, y)
        if was_map:
            client.state = next_stage

    client.tap = tap
    run(action(), client, Model([0]), waits, background_start=True)
    assert sum(state == 'map' for _, _, state in client.taps) == 1
    expected = ['ready', 'confirm_first', 'confirm_second']
    assert [state for _, _, state in client.taps[1:]] == expected[expected.index(next_stage):]


@pytest.mark.parametrize('eventually_ready', [False, True])
def test_unknown_loading_screen_is_waited_for_without_blind_reclick(eventually_ready):
    client, waits = Client(transfer=False), []
    original_tap = client.tap
    loading_shots = 0

    def tap(x, y):
        was_map = client.state == 'map'
        original_tap(x, y)
        if was_map:
            client.state = 'loading'

    def screenshot():
        nonlocal loading_shots
        if client.state == 'loading':
            loading_shots += 1
            if eventually_ready and loading_shots == 6:
                client.state = 'ready'
        return client.state.encode()

    client.tap, client.screenshot = tap, screenshot
    if eventually_ready:
        run(action(), client, Model([0]), waits)
    else:
        with pytest.raises(VisualError, match='开始扫荡后等待名单或扫荡确认页面超时'):
            run(action(), client, Model([]), waits)
        assert len(client.taps) == 1
        assert sum(waits) <= 21
    assert not any(state == 'loading' for _, _, state in client.taps)
    assert sum(state == 'map' for _, _, state in client.taps) == 1


def test_late_post_click_screenshot_cannot_trigger_start_sweep_retry(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('naruto_daily.survival_challenge.handler.time.monotonic', lambda: now[0])
    client, waits = Client(transfer=False), []
    client.tap = lambda x, y: client.taps.append((x, y, client.state))

    def screenshot():
        if client.taps:
            now[0] += 25
        return client.state.encode()

    client.screenshot = screenshot
    with pytest.raises(VisualError, match='开始扫荡后等待名单或扫荡确认页面超时'):
        run(action(), client, Model([]), waits)
    assert len(client.taps) == 1


def test_slow_screenshot_result_cannot_trigger_click_after_button_timeout(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('naruto_daily.survival_challenge.handler.time.monotonic', lambda: now[0])
    client, waits = Client(transfer=False), []
    screenshots = [0]

    def screenshot():
        screenshots[0] += 1
        if screenshots[0] == 2:
            now[0] += 25
        return client.state.encode()

    client.screenshot = screenshot
    with pytest.raises(VisualError, match='start_sweep'):
        run(action(), client, Model([0]), waits)
    assert client.taps == []


@pytest.mark.parametrize('roster_already_confirmed', [False, True])
def test_survival_runs_actual_screen_sequence_with_or_without_roster(roster_already_confirmed):
    action_value = next(a for a in load_settings(ROOT / 'config.example.json').tasks['survival_challenge']
                        if a['type'] == 'handle_survival_challenge')
    steps = [('survival_map.png', (1080, 910, 1250, 1050))]
    if not roster_already_confirmed:
        steps += [
            ('survival_roster.png', (805, 830, 1110, 940)),
            ('survival_confirm_first.png', (805, 625, 1110, 735)),
        ]
    steps += [('survival_confirm_second.png', (625, 625, 935, 735))]
    pngs = {name: (ROOT / 'tests/fixtures' / name).read_bytes() for name, _ in steps}
    waits = []

    class ScreenClient:
        def __init__(self):
            self.index = 0

        def screenshot(self):
            return pngs[steps[self.index][0]] if self.index < len(steps) else pngs['survival_map.png']

        def tap(self, x, y):
            assert self.index < len(steps), 'Unexpected click after sweep confirmation'
            left, top, right, bottom = steps[self.index][1]
            assert left <= x <= right and top <= y <= bottom
            self.index += 1

    class CountModel:
        def complete_json(self, png, prompt, *, timeout_seconds):
            assert client.index == len(steps)
            assert waits[-1] == 60
            assert png == pngs['survival_map.png']
            return {'reset_count': 0}

    client = ScreenClient()
    handle_survival_challenge(client, action_value, model=CountModel(),
                             asset_root=ROOT, sleep=waits.append)
    assert client.index == (2 if roster_already_confirmed else 4)
