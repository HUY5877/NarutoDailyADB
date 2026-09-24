import pytest

from naruto_daily.multimodal.agent import AgentError, Joystick, run_agent


class Device:
    def __init__(self):
        self.events = []
        self.frames = 0

    def screenshot(self):
        self.frames += 1
        self.events.append('screenshot')
        return f'frame-{self.frames}'.encode()

    def swipe(self, *args):
        self.events.append(args)


class Model:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.requests = []

    def complete_json(self, png, prompt):
        self.requests.append((png, prompt))
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def model_connection(monkeypatch, agent_server):
    monkeypatch.setattr(Model, 'agent_connection', lambda self: agent_server.connection(self.complete_json), raising=False)


def decision(action=None, reached=False):
    return dict(observation='人物在封印外', reached=reached, action=action)


def run(device, model, **kwargs):
    return run_agent(device, model, '走到封印处', joystick=Joystick(200, 800, 80),
                     sleep=lambda _: None, **kwargs)


def test_initial_goal_reached_never_moves():
    device = Device()
    result = run(device, Model([decision(reached=True)]))
    assert result.reached and result.steps == 0
    assert device.events == ['screenshot']


def test_bare_success_flag_without_scene_evidence_cannot_finish():
    from agent_fakes import tool_call
    answer = {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
        tool_call('finish', {'reached': True, 'observation': '人物已在蓝圈内'})]}}
    device = Device()
    with pytest.raises(AgentError):
        run(device, Model([answer]))
    assert device.events == ['screenshot']


def test_move_accepts_flat_native_evidence_matching_the_finish_schema():
    from agent_fakes import tool_call
    answer = {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
        tool_call('move', {'direction': 'w', 'status': 'pending', 'observation': '目标尚未完成'})]}}
    device = Device()
    result = run(device, Model([answer, decision(reached=True)]))
    assert result.reached and result.steps == 1 and device.frames == 2


def test_one_move_per_fresh_observation_and_all_wasd_directions():
    device = Device()
    model = Model([decision(k) for k in 'wasd'] + [decision(reached=True)])
    result = run(device, model)
    assert result.reached and result.steps == 4
    assert device.events == ['screenshot', (200, 800, 200, 720, 300),
                             'screenshot', (200, 800, 120, 800, 300),
                             'screenshot', (200, 800, 200, 880, 300),
                             'screenshot', (200, 800, 280, 800, 300), 'screenshot']
    assert [p for p, _ in model.requests] == [b'frame-1', b'frame-2', b'frame-3', b'frame-4', b'frame-5']
    assert all('走到封印处' in prompt for _, prompt in model.requests)
    assert 'move' in model.requests[1][1] and 'direction' in model.requests[1][1]


@pytest.mark.parametrize('reached', [False, True])
def test_fiftieth_move_is_verified_without_a_fifty_first_move(reached):
    device = Device()
    model = Model([decision('d')] * 50 + [decision(None if reached else 'd', reached)])
    result = run(device, model)
    assert result.reached is reached and result.steps == 50
    assert result.status == ('reached' if reached else 'max_steps')
    assert device.frames == 51
    assert len([e for e in device.events if isinstance(e, tuple)]) == 50


@pytest.mark.parametrize('answer', [
    {}, [], decision('wa'), decision(['w', 'a']), decision('tap'),
    decision('w', 'false'), decision('w', True),
    dict(observation=42, reached=False, action='w'),
])
def test_bad_model_output_stops_before_movement(answer):
    device = Device()
    with pytest.raises(AgentError):
        run(device, Model([answer]))
    assert device.events == ['screenshot']


@pytest.mark.parametrize('limit', [0, 51, True, 1.5])
def test_invalid_step_budget_fails_before_screenshot(limit):
    device = Device()
    with pytest.raises(ValueError):
        run(device, Model([]), max_steps=limit)
    assert not device.events


def test_model_error_after_move_does_not_continue():
    device = Device()
    with pytest.raises(AgentError):
        run(device, Model([decision('w'), RuntimeError('offline')]))
    assert device.events == ['screenshot', (200, 800, 200, 720, 300), 'screenshot']


def test_wait_happens_before_post_move_screenshot():
    device = Device()
    run_agent(device, Model([decision('d'), decision(reached=True)]), '封印',
              joystick=Joystick(200, 800, 80),
              sleep=lambda seconds: device.events.append(('sleep', seconds)))
    assert device.events == ['screenshot', (200, 800, 280, 800, 300),
                             ('sleep', 0.5), 'screenshot']


@pytest.mark.parametrize('goal', ['', '  ', None])
def test_empty_goal_is_rejected_before_screenshot(goal):
    device = Device()
    with pytest.raises(ValueError):
        run_agent(device, Model([]), goal, joystick=Joystick(200, 800, 80))
    assert not device.events


@pytest.mark.parametrize('delay', [-1, float('nan'), float('inf'), True])
def test_invalid_settle_delay_is_rejected_before_moving(delay):
    device = Device()
    with pytest.raises(ValueError):
        run(device, Model([]), settle_seconds=delay)
    assert not device.events


@pytest.mark.parametrize('values', [(20, 20, 80), (200, 800, 0),
                                  (200, 800, 80, 0), (True, 800, 80)])
def test_invalid_joystick_is_rejected(values):
    with pytest.raises(ValueError):
        Joystick(*values)


def test_adb_failure_does_not_request_another_decision():
    device = Device()
    def fail(*args):
        raise RuntimeError('device disconnected')
    device.swipe = fail
    model = Model([decision('a')])
    with pytest.raises(RuntimeError, match='disconnected'):
        run(device, model)
    assert device.frames == 1
    assert len(model.requests) == 1


def test_final_not_reached_without_another_action_returns_step_limit():
    device = Device()
    result = run(device, Model([decision('d'), decision()]), max_steps=1)
    assert not result.reached and result.status == 'max_steps' and result.steps == 1
    assert device.events == ['screenshot', (200, 800, 280, 800, 300), 'screenshot']


def test_full_history_at_allowed_observation_length_can_be_returned():
    device = Device()
    answer = dict(observation='外' * 2000, reached=False, action='d')
    result = run(device, Model([answer] * 50 + [decision(reached=True)]))
    assert result.reached and len(result.history) == 50 and result.steps == 50


def test_navigation_runs_in_python_without_starting_a_subprocess(monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail('导航不应启动 Node 或其他子进程')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    result = run(Device(), Model([decision('w'), decision(reached=True)]))
    assert result.reached and result.steps == 1


@pytest.mark.parametrize('calls, finish_reason', [
    ([('move', {'direction': 'w', 'status': 'pending', 'observation': '外'}),
      ('move', {'direction': 'd', 'status': 'pending', 'observation': '外'})], 'tool_calls'),
    ([('move', {'direction': 'w', 'status': 'pending', 'observation': '外'}),
      ('finish', {'reached': True, 'observation': '到达'})], 'tool_calls'),
    ([('finish', {'reached': 'true', 'observation': '到达'})], 'tool_calls'),
    ([('move', {'direction': 'w', 'status': 'pending', 'observation': ' '})], 'tool_calls'),
    ([('move', {'direction': 'w', 'status': 'pending', 'observation': '外', 'count': 2})], 'tool_calls'),
    ([('move', {'direction': 'w', 'status': 'pending', 'observation': '外'})], 'length'),
    ([], 'stop'),
])
def test_invalid_tool_turn_never_moves_or_retries(calls, finish_reason, agent_server):
    from agent_fakes import tool_call
    answer = {'message': {'role': 'assistant', 'content': '完成',
                         'tool_calls': [tool_call(name, args, str(i)) for i, (name, args) in enumerate(calls)]},
              'finish_reason': finish_reason}
    device = Device()
    with pytest.raises(AgentError):
        run(device, Model([answer]))
    assert device.events == ['screenshot']
    assert len(agent_server.payloads) == 1


def native_decision(name, status, **extra):
    from agent_fakes import tool_call
    args = dict(status=status, observation='当前画面事实', **extra)
    return {'message': {'role': 'assistant', 'content': None, 'tool_calls': [tool_call(name, args)]}}


def test_pending_finish_corrects_tool_choice_on_same_frame_without_rescreenshot():
    device = Device()
    model = Model([native_decision('finish', 'pending'), decision('w'), decision(reached=True)])
    result = run(device, model)
    assert result.reached and result.steps == 1
    assert [png for png, _ in model.requests] == [b'frame-1', b'frame-1', b'frame-2']
    assert '尚不满足目标完成条件' in model.requests[1][1]
    assert device.events == ['screenshot', (200, 800, 200, 720, 300), 'screenshot']


@pytest.mark.parametrize('name', ['move', 'finish'])
def test_blocked_scene_stops_before_action_or_success(name):
    device = Device()
    answer = native_decision(name, 'blocked', **({'direction': 'w'} if name == 'move' else {}))
    model = Model([answer])
    with pytest.raises(AgentError, match='不可导航'):
        run(device, model)
    assert device.events == ['screenshot'] and len(model.requests) == 1


def test_completed_evidence_on_move_finishes_without_unnecessary_movement():
    device = Device()
    result = run(device, Model([native_decision('move', 'reached', direction='w')]))
    assert result.reached and result.steps == 0
    assert device.events == ['screenshot']


def test_contradictory_finish_has_bounded_sdk_retries_without_new_screenshots():
    device = Device()
    model = Model([native_decision('finish', 'pending')] * 4)
    with pytest.raises(AgentError):
        run(device, model)
    assert len(model.requests) == 4 and device.events == ['screenshot']


@pytest.mark.parametrize('reached', [False, True])
def test_retry_does_not_reset_fifty_move_budget(reached):
    device = Device()
    model = Model([native_decision('finish', 'pending')] + [decision('d')] * 50 +
                  [decision(None if reached else 'd', reached)])
    result = run(device, model)
    assert result.reached is reached and result.steps == 50
    assert len(model.requests) == 52 and device.frames == 51
    assert len([e for e in device.events if isinstance(e, tuple)]) == 50


@pytest.mark.parametrize('name', ['move', 'finish'])
def test_terminal_screen_at_step_limit_does_not_return_max_steps(name):
    device = Device()
    answer = native_decision(name, 'blocked', **({'direction': 'w'} if name == 'move' else {}))
    model = Model([decision('d'), answer])
    with pytest.raises(AgentError, match='不可导航'):
        run(device, model, max_steps=1)
    assert len(model.requests) == 2 and device.frames == 2


@pytest.mark.parametrize('evidence_type', [None, str, object])
def test_invalid_evidence_type_fails_before_device_read(evidence_type):
    device = Device()
    with pytest.raises(ValueError):
        run(device, Model([]), evidence_type=evidence_type)
    assert device.events == []


def test_http_retries_reuse_same_frame_without_replaying_moves(monkeypatch):
    from naruto_daily.multimodal import transport
    waits = []
    monkeypatch.setattr(transport.time, 'sleep', waits.append)
    device = Device()
    model = Model([{'http_status': 429}, decision('w'),
                   {'http_status': 503}, decision(reached=True)])
    result = run(device, model)
    assert result.reached and result.steps == 1
    assert waits == [2, 2]
    assert [png for png, _ in model.requests] == [b'frame-1', b'frame-1', b'frame-2', b'frame-2']
    assert device.events == ['screenshot', (200, 800, 200, 720, 300), 'screenshot']


def test_agent_sdk_does_not_add_retries_after_shared_limit(monkeypatch):
    from naruto_daily.multimodal import transport
    waits = []
    monkeypatch.setattr(transport.time, 'sleep', waits.append)
    device = Device()
    model = Model([{'http_status': 429}] * 4)
    with pytest.raises(AgentError):
        run(device, model)
    assert len(model.requests) == 4
    assert waits == [2, 2, 2]
    assert device.events == ['screenshot']
