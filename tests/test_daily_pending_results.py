"""Pending game actions observe fresh results within one shared daily deadline."""
import pytest

from naruto_daily.common import model_workflow
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.common.visual import VisualError
from naruto_daily.multimodal import prompt_jev, prompts
from naruto_daily.equipment.handler import (
    ADVANCE, BLANK, EQUIPMENT_POINTS, MATERIAL_BUTTON, MATERIAL_POINTS, START_SWEEP,
    _finish_material, handle_equipment_upgrade,
)
from naruto_daily.ichiraku import handle_ichiraku_takeout


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Device:
    def __init__(self, clock):
        self.clock = clock
        self.taps = []
        self.tap_times = []
        self.frames = []

    def screenshot(self):
        frame = f'frame-{len(self.frames) + 1}'.encode()
        self.frames.append(frame)
        return frame

    def tap(self, x, y):
        self.taps.append((x, y))
        self.tap_times.append(self.clock())


class Model:
    def __init__(self, clock, device, *answers, repeat=None):
        self.clock, self.device = clock, device
        self.answers = iter(answers)
        self.repeat = repeat
        self.observations = []

    def complete_json(self, png, prompt, **kwargs):
        self.observations.append((png, prompt, tuple(self.device.taps)))
        self.clock.sleep(1)
        try:
            answer = next(self.answers)
        except StopIteration:
            if self.repeat is None:
                raise AssertionError('unexpected extra observation')
            answer = self.repeat
        return answer() if callable(answer) else answer


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(model_workflow.time, 'monotonic', clock)
    return clock


def action(**overrides):
    return dict(timeout_seconds=1, max_rounds=1, poll_seconds=1,
                post_click_wait_seconds=0, sweep_timeout_seconds=5,
                multimodal={'max_calls': 2}, **overrides)


def takeout(can_claim=False):
    return dict(page='takeout', can_claim=can_claim)


def material(button):
    return dict(page='material', action=button)


def overview(levels=None):
    return dict(page='equipment', progressions=levels or [85, 45, 23, 85, 2, 78])


def setup(stamina):
    return dict(page='sweep_setup', stamina=stamina, auto_stop=True)


def result(stamina=15, animation_finished=True):
    return dict(page='sweep_result', stamina=stamina, completed_runs=1,
                animation_finished=animation_finished)


def advance(ready=False, selected=5):
    return dict(page='equipment', selected_slot=selected, can_advance=ready)


def filled_slots(count=6):
    return [state for _ in range(count) for state in
            (dict(page='material_info'), dict(page='equipment'))]


def run(handler, clock, *answers, repeat=None, deadline=100, config=None):
    device = Device(clock)
    model = Model(clock, device, *answers, repeat=repeat)
    with execution_scope(deadline, clock=clock, sleep=clock.sleep):
        handler(device, config or action(), model=model, sleep=clock.sleep)
    return device, model


def test_takeout_checks_first_screenshot_before_clicking_left_middle_right(clock):
    points = [(450, 520), (880, 520), (1310, 520)]

    class CheckedDevice(Device):
        def tap(self, x, y):
            assert self.frames
            super().tap(x, y)

    device = CheckedDevice(clock)
    model = Model(clock, device, takeout(True), takeout())
    with execution_scope(100, clock=clock, sleep=clock.sleep):
        handle_ichiraku_takeout(device, action(), model=model, sleep=clock.sleep)
    assert device.taps == points
    assert [taps for _, _, taps in model.observations] == [(), tuple(points)]
    assert len(device.frames) == len(model.observations) == 2
    assert all(prompt is prompt_jev.TAKEOUT_PROMPT
               and isinstance(prompt, prompt_jev.ChoicePrompt)
               for _, prompt, _ in model.observations)


def test_takeout_initially_claimed_boxes_finish_without_clicking(clock):
    device, model = run(handle_ichiraku_takeout, clock, takeout())
    assert not device.taps
    assert len(device.frames) == len(model.observations) == 1


def test_takeout_reward_can_take_more_than_one_close_to_return(clock):
    device, model = run(handle_ichiraku_takeout, clock,
                        *[dict(page='reward', can_claim=None)] * 6, takeout())
    assert device.taps == [(1550, 740)] * 6
    assert len(device.frames) == len(model.observations) == 7


def test_takeout_repeats_all_three_boxes_while_still_claimable(clock):
    device, model = run(handle_ichiraku_takeout, clock,
                        takeout(True), takeout(True), takeout(True), takeout())
    points = [(450, 520), (880, 520), (1310, 520)]
    assert device.taps == points * 3
    assert [taps for _, _, taps in model.observations] == [
        tuple(points * rounds) for rounds in range(4)
    ]
    assert len(set(device.frames)) == len(model.observations)


def test_takeout_waits_for_confirmed_takeout_after_reward_and_unknown_frames(clock):
    device, model = run(handle_ichiraku_takeout, clock,
                        dict(page='reward', can_claim=None), *[dict(page='unknown', can_claim=None)] * 5,
                        takeout(True), takeout())
    points = [(450, 520), (880, 520), (1310, 520)]
    after_close = [(1550, 740)]
    assert device.taps == after_close + points
    assert all(taps == tuple(after_close) for _, _, taps in model.observations[1:-1])
    assert model.observations[-1][2] == tuple(after_close + points)
    assert len(set(device.frames)) == len(model.observations)


def test_takeout_repeated_rounds_stop_at_shared_deadline(clock):
    device = Device(clock)
    model = Model(clock, device, repeat=takeout(True))
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(20, clock=clock, sleep=clock.sleep):
            handle_ichiraku_takeout(device, action(), model=model, sleep=clock.sleep)
    assert device.taps == [(450, 520), (880, 520), (1310, 520)] * (len(model.observations) - 1)
    assert len(model.observations) > 4
    assert all(when < 20 for when in device.tap_times)
    assert clock() == 20


@pytest.mark.parametrize('answer', [takeout(True), dict(page='reward', can_claim=None), takeout()])
def test_takeout_late_confirmation_cannot_start_round_close_reward_or_succeed(clock, answer):
    device = Device(clock)

    def late_confirmation():
        clock.now = 10
        return answer

    model = Model(clock, device, late_confirmation)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(10, clock=clock, sleep=clock.sleep):
            handle_ichiraku_takeout(device, action(), model=model, sleep=clock.sleep)
    assert not device.taps


def test_takeout_shared_deadline_stops_in_middle_of_three_clicks(clock):
    device = Device(clock)
    model = Model(clock, device, takeout(True))
    config = action()
    config['post_click_wait_seconds'] = 1
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(2.5, clock=clock, sleep=clock.sleep):
            handle_ichiraku_takeout(device, config, model=model, sleep=clock.sleep)
    assert device.taps == [(450, 520), (880, 520)]
    assert device.tap_times == [1, 2]
    assert len(device.frames) == len(model.observations) == 1
    assert clock() == 2.5


def test_equipment_can_close_more_than_four_confirmed_layers(clock):
    device, _ = run(handle_equipment_upgrade, clock, overview(),
                    dict(page='material_info'), *[dict(page='material_info')] * 5,
                    dict(page='equipment'), *filled_slots(5), advance())
    assert [point for point in device.taps if point in MATERIAL_POINTS] == list(MATERIAL_POINTS)
    assert device.taps.count(BLANK) == 11
    assert START_SWEEP not in device.taps
    assert ADVANCE not in device.taps


def test_equipment_layer_that_never_closes_uses_shared_deadline(clock):
    device = Device(clock)
    model = Model(clock, device, overview(), dict(page='material_info'),
                  repeat=dict(page='material_info'))
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(12, clock=clock, sleep=clock.sleep):
            handle_equipment_upgrade(device, action(), model=model, sleep=clock.sleep)
    assert device.taps[:2] == [EQUIPMENT_POINTS[4], MATERIAL_POINTS[0]]
    assert device.taps[2:] == [BLANK] * 10
    assert all(when < 12 for when in device.tap_times)


def test_synthesis_clicks_once_then_observes_past_old_retry_cap_until_equip(clock):
    device = Device(clock)
    model = Model(clock, device, *[material('synthesize')] * 7,
                  dict(page='sweep_setup'), material('sweep'), material('equip'),
                  dict(page='equipment'))
    with execution_scope(50, clock=clock, sleep=clock.sleep):
        flow = ModelWorkflow(device, action(), model=model, sleep=clock.sleep)
        _finish_material(flow, 'synthesize')
    assert device.taps == [MATERIAL_BUTTON, MATERIAL_BUTTON]
    assert all(taps == (MATERIAL_BUTTON,) for _, _, taps in model.observations[:-1])
    assert model.observations[-1][2] == (MATERIAL_BUTTON, MATERIAL_BUTTON)
    assert len(device.frames) == 11


def test_synthesis_pending_until_deadline_has_no_extra_synthesize_or_blank(clock):
    device = Device(clock)
    model = Model(clock, device, repeat=material('synthesize'))
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(20, clock=clock, sleep=clock.sleep):
            _finish_material(ModelWorkflow(device, action(), model=model, sleep=clock.sleep),
                             'synthesize')
    assert device.taps == [MATERIAL_BUTTON]
    assert len(model.observations) > 6
    assert clock() == 20


def test_synthesis_late_equip_confirmation_cannot_equip(clock):
    device = Device(clock)

    def late_equip():
        clock.now = 10
        return material('equip')

    model = Model(clock, device, late_equip)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(10, clock=clock, sleep=clock.sleep):
            _finish_material(ModelWorkflow(device, action(), model=model, sleep=clock.sleep),
                             'synthesize')
    assert device.taps == [MATERIAL_BUTTON]


def test_equipment_after_equip_observes_other_foregrounds_without_closing_them(clock):
    device = Device(clock)
    model = Model(clock, device, dict(page='sweep_setup'), dict(page='sweep_result'),
                  dict(page='material_info'), dict(page='material'), dict(page='equipment'))
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        _finish_material(ModelWorkflow(device, action(), model=model, sleep=clock.sleep), 'equip')
    assert device.taps == [MATERIAL_BUTTON, BLANK]
    assert all(taps == (MATERIAL_BUTTON,) for _, _, taps in model.observations[:4])
    assert model.observations[-1][2] == (MATERIAL_BUTTON, BLANK)


def test_equipment_after_equip_unknown_layer_until_deadline_has_no_blank(clock):
    device = Device(clock)
    model = Model(clock, device, repeat=dict(page='sweep_setup'))
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(15, clock=clock, sleep=clock.sleep):
            _finish_material(ModelWorkflow(device, action(), model=model, sleep=clock.sleep), 'equip')
    assert device.taps == [MATERIAL_BUTTON]
    assert clock() == 15


def test_equipment_continues_new_confirmed_round_beyond_local_max_rounds(clock):
    device, model = run(handle_equipment_upgrade, clock, overview(), *filled_slots(),
                        advance(True), overview([85, 45, 23, 85, 50, 78]),
                        *filled_slots(), advance(False, selected=3))
    assert [point for point in device.taps if point in MATERIAL_POINTS] == list(MATERIAL_POINTS) * 2
    assert [point for point in device.taps if point in EQUIPMENT_POINTS] == [EQUIPMENT_POINTS[4], EQUIPMENT_POINTS[2]]
    assert device.taps.count(ADVANCE) == 1
    assert model.observations[-1][1] == prompt_jev.EQUIPMENT_ADVANCE


def test_equipment_rounds_share_original_deadline_instead_of_restarting_budget(clock):
    device = Device(clock)
    states = iter((overview(), *filled_slots(), advance(True)) * 3)
    model = Model(clock, device, repeat=lambda: next(states))
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(31, clock=clock, sleep=clock.sleep):
            handle_equipment_upgrade(device, action(), model=model, sleep=clock.sleep)
    assert device.taps.count(ADVANCE) == 2
    assert all(when < 31 for when in device.tap_times)
    assert clock() == 31


def sweep_tail():
    return (dict(page='material'), material('sweep'), setup(5),
            dict(page='material'), dict(page='equipment'))


def test_sweep_waits_beyond_local_timeout_then_closes_confirmed_result(clock):
    def slow_result():
        clock.now = 131
        return result()

    device, _ = run(handle_equipment_upgrade, clock, overview(), material('sweep'),
                    setup(20), slow_result, *sweep_tail(), deadline=160)
    assert device.taps.count(START_SWEEP) == 1
    assert device.tap_times[device.taps.index(BLANK)] == 131


def test_sweep_rechecks_animation_and_unknown_stamina_without_relaxing_validation(clock):
    device, model = run(handle_equipment_upgrade, clock, overview(), material('sweep'),
                        setup(20), result(animation_finished=False),
                        result(animation_finished=False), result(stamina=None), result(),
                        *sweep_tail())
    assert device.taps.count(START_SWEEP) == 1
    reads = [taps for _, prompt, taps in model.observations if prompt == prompts.EQUIPMENT_SWEEP_RESULT]
    assert len(reads) == 4
    assert all(START_SWEEP in taps and BLANK not in taps for taps in reads)
    assert device.tap_times[device.taps.index(BLANK)] == 10


def test_sweep_close_waits_for_material_through_other_foregrounds_without_extra_blank(clock):
    device, model = run(handle_equipment_upgrade, clock, overview(), material('sweep'),
                        setup(20), result(), dict(page='equipment'), dict(page='sweep_setup'),
                        dict(page='material_info'), *sweep_tail())
    assert device.taps.count(START_SWEEP) == 1
    assert all(taps.count(BLANK) == 1 for _, _, taps in model.observations[4:8])
    assert all(len(taps) == 5 for _, _, taps in model.observations[4:8])


def test_sweep_close_unexpected_foreground_until_deadline_does_not_repeat_inputs(clock):
    device = Device(clock)
    model = Model(clock, device, overview(), material('sweep'), setup(20), result(),
                  repeat=dict(page='equipment'))
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(25, clock=clock, sleep=clock.sleep):
            handle_equipment_upgrade(device, action(), model=model, sleep=clock.sleep)
    assert device.taps == [EQUIPMENT_POINTS[4], MATERIAL_POINTS[0], MATERIAL_BUTTON,
                           START_SWEEP, BLANK]
    assert clock() == 25


def test_takeout_without_execution_can_close_reward_until_takeout_returns(clock):
    device = Device(clock)
    model = Model(clock, device, *[dict(page='reward', can_claim=None)] * 6, takeout())
    config = action()
    config['timeout_seconds'] = 100
    handle_ichiraku_takeout(device, config, model=model, sleep=clock.sleep)
    assert device.taps == [(1550, 740)] * 6


def test_takeout_without_execution_can_repeat_more_than_three_rounds(clock):
    device = Device(clock)
    model = Model(clock, device, *[takeout(True)] * 5, takeout())
    config = action()
    config['timeout_seconds'] = 100
    handle_ichiraku_takeout(device, config, model=model, sleep=clock.sleep)
    assert device.taps == [(450, 520), (880, 520), (1310, 520)] * 5
    assert len(model.observations) == 6


def test_takeout_without_execution_repeated_rounds_stop_at_local_timeout(clock):
    device = Device(clock)
    model = Model(clock, device, repeat=takeout(True))
    config = action()
    config['timeout_seconds'] = 5
    with pytest.raises(VisualError, match='超时'):
        handle_ichiraku_takeout(device, config, model=model, sleep=clock.sleep)
    assert device.taps == [(450, 520), (880, 520), (1310, 520)] * 4
    assert all(when < 5 for when in device.tap_times)
    assert clock() == 5
