import base64
import time

import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.multimodal.agent import Joystick, run_agent
from naruto_daily.multimodal.transport import MultimodalRequestError


class Device:
    def __init__(self):
        self.frames = 0
        self.moves = []

    def screenshot(self):
        self.frames += 1
        return f'frame-{self.frames}'.encode()

    def swipe(self, *args):
        self.moves.append(args)


def image_of(payload):
    urls = [part['image_url']['url'] for message in payload['messages']
            if isinstance(message.get('content'), list) for part in message['content']
            if part.get('type') == 'image_url']
    assert len(urls) == 1
    return base64.b64decode(urls[0].split(',')[1])


def test_sdk_retries_five_times_with_new_frames_and_without_replaying_movement(agent_server, monkeypatch):
    from naruto_daily.multimodal import transport
    monkeypatch.setattr(transport, 'REQUEST_RETRY_SECONDS', 0)
    answers = iter([dict(reached=False, action='d', observation='outside')]
                   + [{'http_status': 503}] * 5
                   + [dict(reached=True, action=None, observation='arrived')])

    class Model:
        def agent_connection(self):
            return agent_server.connection(lambda *_: next(answers))

    device = Device()
    with execution_scope(time.monotonic() + 1500):
        result = run_agent(device, Model(), 'goal', joystick=Joystick(200, 800, 80), sleep=lambda _: None)
    assert result.reached and result.steps == 1
    assert len(device.moves) == 1
    assert [image_of(p) for p in agent_server.payloads] == [f'frame-{i}'.encode() for i in range(1, 8)]
    assert all(p['tools'] == agent_server.payloads[1]['tools'] for p in agent_server.payloads[1:])
    assert all(sum(m['role'] == 'tool' for m in p['messages']) == 1 for p in agent_server.payloads[1:])


def test_sdk_permanent_auth_error_is_not_retried(agent_server):
    class Model:
        def agent_connection(self):
            return agent_server.connection(lambda *_: {'http_status': 401})

    device = Device()
    with pytest.raises(MultimodalRequestError) as error, execution_scope(time.monotonic() + 1500):
        run_agent(device, Model(), 'goal', joystick=Joystick(200, 800, 80))
    assert error.value.status_code == 401
    assert len(agent_server.payloads) == 1
    assert device.moves == []


def test_sdk_keeps_daily_deadline_exception_and_does_not_apply_late_movement(agent_server):
    now = [0]

    def late_move(*_):
        now[0] = 1500
        return dict(reached=False, action='d', observation='outside')

    class Model:
        def agent_connection(self):
            return agent_server.connection(late_move)

    device = Device()
    with pytest.raises(DailyDeadlineExceeded), execution_scope(1500, clock=lambda: now[0]):
        run_agent(device, Model(), 'goal', joystick=Joystick(200, 800, 80))
    assert len(agent_server.payloads) == 1
    assert device.moves == []
