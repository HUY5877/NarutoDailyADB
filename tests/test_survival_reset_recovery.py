"""Recover an unavailable survival sweep only from a fresh, confirmed map."""
import json

import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.survival_challenge import handler
from test_execution import Clock


POINTS = {
    'start_sweep': (1100, 900), 'reset': (1400, 900),
    'ready': (950, 885), 'confirm_first': (940, 700),
    'confirm_second': (800, 700), 'reset_confirm': (1100, 700),
    'sweep_prompt': (960, 450), 'transfer': (1000, 400),
}


def target(name):
    return dict(name=name, template=name + '.png', left=0, top=0,
                right=1920, bottom=1080, threshold=.9)


def action():
    return dict(timeout_seconds=2, max_rounds=1, wait_seconds=1,
                poll_seconds=1, post_click_wait_seconds=1,
                multimodal=dict(max_calls=2),
                transfer_marker=target('transfer'),
                **{name: target(name) for name in POINTS if name != 'transfer'})


class Device:
    def __init__(self, clock, *, blocked_view='map', foreground=None, confirm_delay=0,
                 confirm_release_delay=0):
        self.clock, self.blocked_view = clock, blocked_view
        self.foreground, self.confirm_delay = foreground, confirm_delay
        self.confirm_release_delay = confirm_release_delay
        self.state = 'map_blocked'
        self.reset_at = None
        self.confirm_release_at = None
        self.frames = []
        self.taps = []

    def visible(self):
        starts = sum(point == POINTS['start_sweep'] for point, _, _ in self.taps)
        if self.state == 'map_blocked':
            if starts < 3:
                return {'start_sweep', 'reset'}
            if self.foreground and self.clock() >= 7:
                self.state = self.foreground
                return self.visible()
            if self.blocked_view == 'start_only':
                return {'start_sweep'}
            if self.blocked_view == 'unknown':
                return set()
            if self.blocked_view == 'alternating':
                return {'start_sweep'} if len(self.frames) % 2 else {'reset'}
            return {'start_sweep', 'reset'}
        if self.state == 'reset_confirm':
            foreground = {'reset_confirm'} if self.clock() >= self.reset_at + self.confirm_delay else set()
            return foreground | {'start_sweep', 'reset'}
        if self.state == 'reset_confirm_dismissing':
            if self.clock() >= self.confirm_release_at:
                self.state = 'map_ready'
                return self.visible()
            return {'reset_confirm', 'start_sweep', 'reset'}
        if self.state in ('map_ready', 'completed'):
            return {'start_sweep', 'reset'}
        foreground = {'sweep_prompt', 'confirm_second'} if self.state == 'sweep_prompt' else {self.state}
        return foreground | {'start_sweep', 'reset'}

    def screenshot(self):
        visible = self.visible()
        frame = json.dumps(dict(sequence=len(self.frames) + 1, state=self.state,
                                visible=sorted(visible))).encode()
        self.frames.append((frame, tuple(self.taps)))
        return frame

    def match(self, frame, path, *args):
        return (POINTS[path.stem], 1) if path.stem in json.loads(frame)['visible'] else (None, 0)

    def tap(self, x, y):
        point = (x, y)
        self.taps.append((point, self.state, self.clock()))
        if point == POINTS['start_sweep']:
            if self.state == 'map_ready':
                self.state = 'ready'
            elif self.state != 'map_blocked':
                raise AssertionError(f'START clicked through foreground {self.state}')
        elif point == POINTS['reset']:
            self.state, self.reset_at = 'reset_confirm', self.clock()
        elif point == POINTS['reset_confirm']:
            self.state = 'reset_confirm_dismissing'
            self.confirm_release_at = self.clock() + self.confirm_release_delay
        elif point == POINTS['ready']:
            self.state = 'confirm_first'
        elif point == POINTS['confirm_first']:
            self.state = 'confirm_second'
        elif point == POINTS['confirm_second']:
            self.state = 'completed'
        else:
            raise AssertionError(f'unexpected input {point}')


class Model:
    def __init__(self, clock, device, *counts):
        self.clock, self.device = clock, device
        self.counts = iter(counts)
        self.reads = []

    def complete_json(self, frame, prompt, **kwargs):
        self.reads.append((frame, tuple(self.device.taps)))
        self.clock.sleep(1)
        count = next(self.counts)
        return count() if callable(count) else dict(reset_count=count)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(handler.time, 'monotonic', clock)
    return clock


def execute(clock, device, model, *, deadline=40):
    with execution_scope(deadline, clock=clock, sleep=clock.sleep):
        handler.handle_survival_challenge(device, action(), model=model,
                                          template_matcher=device.match, sleep=clock.sleep)


def points(device):
    return [point for point, _, _ in device.taps]


def test_blocked_sweep_positive_count_resets_once_then_sweeps_normally(clock):
    device = Device(clock)
    model = Model(clock, device, 1, 0)
    execute(clock, device, model)

    assert points(device) == [POINTS['start_sweep']] * 3 + [
        POINTS['reset'], POINTS['reset_confirm'], POINTS['start_sweep'],
        POINTS['ready'], POINTS['confirm_first'], POINTS['confirm_second'],
    ]
    assert device.state == 'completed'
    assert [json.loads(frame)['state'] for frame, _ in model.reads] == ['map_blocked', 'completed']
    assert len(model.reads[0][1]) == 3


class TransferAfterResetDevice(Device):
    def __init__(self, clock, *, dismiss_clicks):
        super().__init__(clock)
        self.dismiss_clicks = dismiss_clicks
        self.transfer_taps = 0

    def visible(self):
        visible = super().visible()
        if self.state == 'map_ready' and self.transfer_taps < self.dismiss_clicks:
            self.state = 'transfer'
        if self.state == 'transfer':
            return {'transfer', 'start_sweep', 'reset'}
        return visible

    def tap(self, x, y):
        if (x, y) == (960, 850):
            assert self.state == 'transfer', 'Dismissal clicked after transfer closed'
            frame, taps = self.frames[-1]
            assert 'transfer' in json.loads(frame)['visible']
            assert len(taps) == len(self.taps), 'Dismissal used a stale frame'
            self.taps.append(((x, y), self.state, self.clock()))
            self.transfer_taps += 1
            if self.transfer_taps == self.dismiss_clicks:
                self.state = 'map_ready'
            return
        super().tap(x, y)


@pytest.mark.parametrize('dismiss_clicks', [2, 3])
def test_reset_transfer_is_rechecked_and_closed_before_background_sweep(clock, dismiss_clicks):
    device = TransferAfterResetDevice(clock, dismiss_clicks=dismiss_clicks)
    model = Model(clock, device, 1, 0)
    execute(clock, device, model)

    assert points(device) == [POINTS['start_sweep']] * 3 + [
        POINTS['reset'], POINTS['reset_confirm'],
    ] + [(960, 850)] * dismiss_clicks + [
        POINTS['start_sweep'], POINTS['ready'], POINTS['confirm_first'], POINTS['confirm_second'],
    ]
    assert device.state == 'completed'
    assert [json.loads(frame)['state'] for frame, _ in model.reads] == ['map_blocked', 'completed']


@pytest.mark.parametrize('misses', [1, 2, 3])
def test_reset_transfer_missed_detection_retries_before_starting_next_sweep(clock, misses):
    class MissedTransferDevice(TransferAfterResetDevice):
        missed_frames = 0

        def match(self, frame, path, *args):
            if (path.stem == 'transfer' and 'transfer' in json.loads(frame)['visible']
                    and self.missed_frames < misses):
                self.missed_frames += 1
                return None, .79
            return super().match(frame, path, *args)

    device = MissedTransferDevice(clock, dismiss_clicks=1)
    model = Model(clock, device, 1, 0)
    execute(clock, device, model)

    assert device.missed_frames == misses
    assert points(device) == [POINTS['start_sweep']] * 3 + [
        POINTS['reset'], POINTS['reset_confirm'], (960, 850),
        POINTS['start_sweep'], POINTS['ready'], POINTS['confirm_first'], POINTS['confirm_second'],
    ]
    assert device.state == 'completed'
    assert len(model.reads) == 2


def test_unclosed_reset_transfer_stops_at_shared_deadline_without_sweep_or_reset_replay(clock):
    device = TransferAfterResetDevice(clock, dismiss_clicks=100)
    model = Model(clock, device, 1)
    with pytest.raises(DailyDeadlineExceeded):
        execute(clock, device, model, deadline=20)

    assert clock() == 20
    assert device.transfer_taps > 1
    assert all(point == (960, 850) for point in points(device)[5:])
    assert points(device).count(POINTS['reset']) == 1
    assert points(device).count(POINTS['reset_confirm']) == 1
    assert points(device).count(POINTS['start_sweep']) == 3
    assert len(model.reads) == 1


def test_blocked_sweep_zero_count_finishes_without_reset_or_more_start(clock):
    device = Device(clock)
    model = Model(clock, device, 0)
    execute(clock, device, model)

    assert points(device) == [POINTS['start_sweep']] * 3
    assert device.state == 'map_blocked'
    assert len(model.reads) == 1


def test_blocked_sweep_uncertain_count_reobserves_fresh_frames_without_more_start(clock):
    device = Device(clock)
    model = Model(clock, device, *[None] * 6, 1, 0)
    execute(clock, device, model, deadline=60)

    assert points(device).count(POINTS['reset']) == 1
    assert points(device).count(POINTS['reset_confirm']) == 1
    assert points(device).count(POINTS['start_sweep']) == 4
    assert all(len(taps) == 3 for _, taps in model.reads[:7])
    assert len({frame for frame, _ in model.reads[:7]}) == 7
    assert device.state == 'completed'


def test_blocked_sweep_count_stays_uncertain_until_original_deadline_without_input(clock):
    device = Device(clock)
    model = Model(clock, device, *[None] * 30)
    with pytest.raises(DailyDeadlineExceeded):
        execute(clock, device, model, deadline=30)

    assert clock() == 30
    assert points(device) == [POINTS['start_sweep']] * 3
    assert len(model.reads) > 6
    assert len({frame for frame, _ in model.reads}) == len(model.reads)


@pytest.mark.parametrize('blocked_view', ['start_only', 'unknown', 'alternating'])
def test_unconfirmed_map_never_combines_old_frames_or_enters_reset(clock, blocked_view):
    device = Device(clock, blocked_view=blocked_view)
    model = Model(clock, device, 0)
    with pytest.raises(DailyDeadlineExceeded):
        execute(clock, device, model, deadline=20)

    assert clock() == 20
    assert points(device) == [POINTS['start_sweep']] * 3
    assert model.reads == []
    assert len(device.frames) > 10


@pytest.mark.parametrize('foreground', ['sweep_prompt', 'confirm_second', 'confirm_first', 'ready'])
def test_foreground_after_blocked_start_prevents_background_map_reset(clock, foreground):
    device = Device(clock, foreground=foreground)
    model = Model(clock, device, 0)
    execute(clock, device, model)

    assert POINTS['reset'] not in points(device)
    assert POINTS['reset_confirm'] not in points(device)
    assert points(device).count(POINTS['start_sweep']) == 3
    assert device.state == 'completed'
    assert [json.loads(frame)['state'] for frame, _ in model.reads] == ['completed']


def test_late_reset_count_cannot_trigger_reset(clock):
    device = Device(clock)

    def late_count():
        clock.now = 30
        return dict(reset_count=1)

    model = Model(clock, device, late_count)
    with pytest.raises(DailyDeadlineExceeded):
        execute(clock, device, model, deadline=30)

    assert len(model.reads) == 1
    assert points(device) == [POINTS['start_sweep']] * 3


def test_delayed_reset_confirmation_is_observed_without_repeating_reset(clock):
    device = Device(clock, confirm_delay=6)
    model = Model(clock, device, 1, 0)
    execute(clock, device, model)

    assert points(device).count(POINTS['reset']) == 1
    assert points(device).count(POINTS['reset_confirm']) == 1
    waiting = [taps for frame, taps in device.frames if json.loads(frame)['state'] == 'reset_confirm']
    assert len(waiting) >= 4
    assert all(len(taps) == 4 for taps in waiting)
    assert device.state == 'completed'


def test_missing_reset_confirmation_waits_to_deadline_without_repeating_reset(clock):
    device = Device(clock, confirm_delay=100)
    model = Model(clock, device, 1)
    with pytest.raises(DailyDeadlineExceeded):
        execute(clock, device, model, deadline=30)

    assert clock() == 30
    assert points(device) == [POINTS['start_sweep']] * 3 + [POINTS['reset']]
    assert len(model.reads) == 1


@pytest.mark.parametrize('stale_count', [0, 1])
@pytest.mark.parametrize('next_foreground', ['ready', 'confirm_second'])
def test_reset_count_received_after_map_changed_is_discarded_for_current_sweep(clock, stale_count,
                                                                            next_foreground):
    device = Device(clock)

    def stale_response():
        device.state = next_foreground
        return dict(reset_count=stale_count)

    model = Model(clock, device, stale_response, 0)
    execute(clock, device, model)

    expected = [POINTS['start_sweep']] * 3
    if next_foreground == 'ready':
        expected += [POINTS['ready'], POINTS['confirm_first']]
    expected += [POINTS['confirm_second']]
    assert points(device) == expected
    assert device.state == 'completed'
    assert [json.loads(frame)['state'] for frame, _ in model.reads] == ['map_blocked', 'completed']


@pytest.mark.parametrize('stale_count', [0, 1])
def test_map_lost_during_count_read_keeps_observing_without_ending_or_resetting(clock, stale_count):
    device = Device(clock)

    def stale_response():
        device.blocked_view = 'unknown'
        return dict(reset_count=stale_count)

    model = Model(clock, device, stale_response)
    with pytest.raises(DailyDeadlineExceeded):
        execute(clock, device, model, deadline=30)

    assert clock() == 30
    assert points(device) == [POINTS['start_sweep']] * 3
    assert len(model.reads) == 1


def test_reset_confirmation_must_disappear_before_background_start_can_be_clicked(clock):
    device = Device(clock, confirm_release_delay=6)
    model = Model(clock, device, 1, 0)
    execute(clock, device, model)

    confirm_time = next(when for point, _, when in device.taps if point == POINTS['reset_confirm'])
    resumed_start = [(state, when) for point, state, when in device.taps
                     if point == POINTS['start_sweep'] and state != 'map_blocked']
    assert len(resumed_start) == 1
    assert resumed_start[0][0] == 'map_ready'
    assert resumed_start[0][1] >= confirm_time + 6
    assert points(device).count(POINTS['reset']) == 1
    assert points(device).count(POINTS['reset_confirm']) == 1
    assert device.state == 'completed'


def test_reset_confirmation_remaining_until_deadline_never_replays_reset_or_start(clock):
    device = Device(clock, confirm_release_delay=100)
    model = Model(clock, device, 1)
    with pytest.raises(DailyDeadlineExceeded):
        execute(clock, device, model, deadline=30)

    assert clock() == 30
    assert points(device) == [POINTS['start_sweep']] * 3 + [POINTS['reset'], POINTS['reset_confirm']]
    assert len(model.reads) == 1


def test_last_start_response_arriving_before_retry_wait_never_reads_reset_early(clock):
    class DelayedSweepDevice(Device):
        def visible(self):
            starts = [when for point, state, when in self.taps
                      if point == POINTS['start_sweep'] and state == 'map_blocked']
            if self.state == 'map_blocked' and len(starts) == 3 and self.clock() >= starts[-1] + 1:
                self.state = 'ready'
            return super().visible()

    device = DelayedSweepDevice(clock)
    # An early map read must be observable even when its count is discarded after
    # the response; the normal flow should first read the count after completing.
    model = Model(clock, device, 0, 0)
    config = dict(action(), poll_seconds=.25, post_click_wait_seconds=.25)
    with execution_scope(20, clock=clock, sleep=clock.sleep):
        handler.handle_survival_challenge(device, config, model=model,
                                          template_matcher=device.match, sleep=clock.sleep)

    assert points(device) == [POINTS['start_sweep']] * 3 + [
        POINTS['ready'], POINTS['confirm_first'], POINTS['confirm_second'],
    ]
    assert device.state == 'completed'
    assert [json.loads(frame)['state'] for frame, _ in model.reads] == ['completed']


@pytest.mark.parametrize('next_foreground', ['ready', 'confirm_first', 'confirm_second'])
def test_foreground_appearing_at_reset_click_check_is_handled_without_reset_or_new_start(clock,
                                                                                     next_foreground):
    class ChangedBeforeResetDevice(Device):
        count_received = False
        post_response_map_confirmed = False

        def visible(self):
            if self.count_received and self.state == 'map_blocked':
                if self.post_response_map_confirmed:
                    self.state = next_foreground
                else:
                    # The first post-response frame still confirms the map. The
                    # next frame, immediately before reset input, has changed.
                    self.post_response_map_confirmed = True
            return super().visible()

    class CountModel(Model):
        def complete_json(self, *args, **kwargs):
            response = super().complete_json(*args, **kwargs)
            if len(self.reads) == 1:
                device.count_received = True
            return response

    device = ChangedBeforeResetDevice(clock)
    model = CountModel(clock, device, 1, 0)
    execute(clock, device, model)

    expected = [POINTS['start_sweep']] * 3
    if next_foreground == 'ready':
        expected += [POINTS['ready']]
    if next_foreground in ('ready', 'confirm_first'):
        expected += [POINTS['confirm_first']]
    expected += [POINTS['confirm_second']]
    assert points(device) == expected
    assert device.state == 'completed'
    assert [json.loads(frame)['state'] for frame, _ in model.reads] == ['map_blocked', 'completed']
