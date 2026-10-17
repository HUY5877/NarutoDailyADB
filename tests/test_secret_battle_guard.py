"""Real secret handler, preparation and battle loop share one guarded entry frame."""

import pytest

from naruto_daily.common.execution import execution_scope
from naruto_daily.common.visual import VisualError
from naruto_daily.multimodal.prompt_jev import BATTLE_COMPLETION
from naruto_daily.multimodal.prompts import SECRET_BATTLE_REALM, SECRET_REALM_TICKETS
from naruto_daily.secret_realm import handler
from test_battle import Clock
from test_secret_realm import secret_action


DEPLOY, PAUSE, EXIT, CONFIRM, RESULT_RETURN = (
    (1787, 920), (1860, 50), (780, 680), (790, 690), (209, 1010),
)


class Device:
    def __init__(self, *, after_deploy='battle', pause_fails=False):
        self.state = 'room'
        self.after_deploy = after_deploy
        self.pause_fails = pause_fails
        self.frames = {}
        self.events = []
        self.taps = []
        self.attacks = []
        self.inside_battle_module = False
        self.entry_frame = None

    def screenshot(self):
        png = f'{self.state}:{len(self.frames)}'.encode()
        self.frames[png] = self.state
        self.events.append(('frame', png))
        return png

    def tap(self, x, y):
        point = (x, y)
        self.taps.append(point)
        transitions = {
            DEPLOY: ('room', self.after_deploy),
            PAUSE: ('battle', 'pause'),
            EXIT: ('pause', 'confirm'),
            CONFIRM: ('confirm', 'room'),
            RESULT_RETURN: ('result', 'room'),
        }
        if point not in transitions:
            self.attacks.append(point)
            pytest.fail(f'Unconfirmed secret battle must not receive attack input {point}')
        expected, next_state = transitions[point]
        assert self.state == expected
        assert self.frames[next(reversed(self.frames))] == expected
        self.state = next_state
        self.events.append(('tap', point))
        if point == PAUSE and self.pause_fails:
            raise VisualError('暂停已发生后的设备异常')

    def matcher(self, png, path, roi, threshold):
        state = self.frames[png]
        targets = {
            'room': {'secret_deploy': DEPLOY},
            'battle': {'secret_battle_pause': PAUSE},
            'pause': {'secret_exit_battle': EXIT},
            'confirm': {'secret_exit_question': (960, 480), 'secret_exit_confirm': CONFIRM},
            'result': {'secret_result_marker': (537, 837), 'secret_result_return': RESULT_RETURN},
        }
        center = targets.get(state, {}).get(path.stem)
        return center, 1.0 if center is not None else .1


class Model:
    def __init__(self, device, replies, *, settle_after_name=False):
        self.device = device
        self.replies = iter(replies)
        self.tickets = iter([1, 1, 0])
        self.name_frames = []
        self.ticket_frames = []
        self.completion_frames = []
        self.settle_after_name = settle_after_name

    def complete_json(self, png, prompt, **kwargs):
        state = self.device.frames[png]
        if prompt == SECRET_REALM_TICKETS:
            assert state == 'room'
            self.ticket_frames.append(png)
            self.device.events.append(('tickets', png))
            return {'page': 'room', 'tickets': next(self.tickets)}
        if prompt == SECRET_BATTLE_REALM:
            assert self.device.inside_battle_module, 'Realm recognition must execute inside run_battle'
            assert png == self.device.entry_frame, 'Preparation must use the provided battle frame'
            assert self.device.events[-1] == ('frame', png), 'No extra screenshot may replace the entry frame'
            assert state == 'battle' and not self.device.attacks
            self.name_frames.append(png)
            self.device.events.append(('name', png))
            reply = next(self.replies)
            if self.settle_after_name:
                self.device.state = 'result'
            return reply
        if prompt == BATTLE_COMPLETION:
            self.completion_frames.append(png)
            pytest.fail('A skipped or locally completed secret battle must not call the completion model')
        pytest.fail('Unexpected model prompt')


def realm(name):
    return {'page': 'battle', 'realm_name': name}


def execute(monkeypatch, device, replies=(), *, settle_after_name=False):
    action = secret_action()
    action.update(load_wait_seconds=0, pre_click_wait_seconds=0,
                  post_click_wait_seconds=0, poll_seconds=.1)
    model = Model(device, replies, settle_after_name=settle_after_name)
    monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw: model)
    real_battle = handler.run_battle

    def enter_real_battle(*args, **kwargs):
        before_attack = kwargs['before_attack']

        def observe_entry_frame(png):
            device.entry_frame = png
            return before_attack(png)

        kwargs = dict(kwargs, before_attack=observe_entry_frame)
        device.events.append(('battle_module',))
        device.inside_battle_module = True
        try:
            return real_battle(*args, **kwargs)
        finally:
            device.inside_battle_module = False

    monkeypatch.setattr(handler, 'run_battle', enter_real_battle)
    completed = handler.handle_secret_realm(device, action, template_matcher=device.matcher,
                                            sleep=lambda _: None)
    return completed, model


@pytest.mark.parametrize('name', ['落岩秘境', '阴阳秘境', '毒风秘境'])
def test_skipped_realm_is_named_inside_real_battle_and_exits_without_attacks(monkeypatch, name):
    device = Device()
    completed, model = execute(monkeypatch, device, [realm(name)])
    assert completed == 0 and device.state == 'room'
    assert device.taps == [DEPLOY, PAUSE, EXIT, CONFIRM]
    assert len(model.name_frames) == 1 and len(model.ticket_frames) == 3
    assert model.completion_frames == [] and device.attacks == []
    assert [event[0] for event in device.events].index('battle_module') < \
        [event[0] for event in device.events].index('name')


@pytest.mark.parametrize('page', ['result', 'room'])
def test_local_end_on_first_battle_frame_bypasses_realm_model_and_attacks(monkeypatch, page):
    device = Device(after_deploy=page)
    completed, model = execute(monkeypatch, device)
    assert completed == 1 and device.state == 'room'
    assert model.name_frames == [] and model.completion_frames == [] and device.attacks == []
    assert device.taps == [DEPLOY] + ([RESULT_RETURN] if page == 'result' else [])
    assert len(model.ticket_frames) == 3


def test_unknown_name_retries_fresh_battle_frame_before_rock_exit(monkeypatch):
    clock, device = Clock(), Device()
    with execution_scope(30, clock=clock, sleep=clock.advance):
        completed, model = execute(monkeypatch, device, [
            {'page': 'unknown', 'realm_name': None}, realm('溶岩秘境'),
        ])
    assert completed == 0
    assert len(model.name_frames) == 2 and model.name_frames[0] != model.name_frames[1]
    assert clock() >= 1, 'Unknown realm must wait for a fresh observation without any battle input'
    assert device.taps == [DEPLOY, PAUSE, EXIT, CONFIRM]
    assert model.completion_frames == [] and device.attacks == []
    first_name = device.events.index(('name', model.name_frames[0]))
    second_name = device.events.index(('name', model.name_frames[1]))
    assert all(event[0] != 'tap' for event in device.events[first_name + 1:second_name])


def test_page_that_settles_during_realm_model_is_recaptured_before_any_attack(monkeypatch):
    device = Device()
    completed, model = execute(monkeypatch, device, [realm('烈炎秘境')], settle_after_name=True)
    assert completed == 1 and device.state == 'room'
    assert len(model.name_frames) == 1
    assert model.completion_frames == [] and device.attacks == []
    assert device.taps == [DEPLOY, RESULT_RETURN]
    name_event = device.events.index(('name', model.name_frames[0]))
    next_frame = next(event[1] for event in device.events[name_event + 1:] if event[0] == 'frame')
    assert device.frames[next_frame] == 'result'


def test_partial_exit_failure_under_shared_budget_is_not_replayed(monkeypatch):
    clock, device = Clock(), Device(pause_fails=True)
    with execution_scope(30, clock=clock, sleep=clock.advance):
        with pytest.raises(VisualError):
            execute(monkeypatch, device, [realm('岩秘境')])
    assert device.state == 'pause'
    assert device.taps == [DEPLOY, PAUSE] and device.attacks == []
    assert sum(event[0] == 'name' for event in device.events) == 1
    assert clock() < 30, 'Input-side failure must stop immediately rather than replaying until the deadline'
