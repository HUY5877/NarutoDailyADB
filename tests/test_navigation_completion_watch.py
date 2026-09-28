import asyncio
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from naruto_daily.multimodal.agent import Joystick
from naruto_daily.multimodal.agent.runner import GoalEvidence
from naruto_daily.multimodal.agent import claude_runner, compat_runner, completion_watch


class Device:
    def __init__(self):
        self.boss = False
        self.moves = []
        self.screenshots = 0

    def screenshot(self):
        self.screenshots += 1
        return b'boss' if self.boss else b'seal'

    def swipe(self, *args):
        self.moves.append(args)
        self.boss = True


class Model:
    def agent_connection(self):
        return dict(base_url='http://127.0.0.1:12345/v1', api_key='offline-key',
                    model='offline', timeout_seconds=30)


def probe(png):
    return '已出现毒风 BOSS' if png == b'boss' else None


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.setattr(completion_watch, 'POLL_SECONDS', 0.01)
    monkeypatch.setattr(completion_watch, 'CLEANUP_GRACE_SECONDS', 0.05)
    for backend in (compat_runner, claude_runner):
        monkeypatch.setattr(backend, 'model_api_connection', lambda connection: nullcontext(connection))


def install_sdk(monkeypatch, backend, behavior, *, on_exit=None, process=None):
    """Keep real movement callbacks, replace only the model's asynchronous wait."""
    if backend is compat_runner:
        class Agent:
            def __init__(self, *args, **kwargs):
                pass

            def tool_plain(self, **kwargs):
                def register(callback):
                    self.move = callback
                    return callback
                return register

            async def run(self, *args, **kwargs):
                async def move():
                    evidence = SimpleNamespace(
                        state=lambda: 'pending', observation='旧截图仍有封印', direction='d',
                        model_dump_json=lambda: '{}')
                    return await self.move(evidence)
                return SimpleNamespace(output=await behavior(move))
        monkeypatch.setattr(backend, 'Agent', Agent)
    else:
        tools = {}

        def server(**kwargs):
            tools.update({tool.name: tool for tool in kwargs['tools']})
            return {}

        class SDK:
            def __init__(self, **kwargs):
                self._transport = SimpleNamespace(_process=process)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                if on_exit is not None:
                    await on_exit()

            async def query(self, messages):
                async for _ in messages:
                    pass

            async def receive_response(self):
                async def move():
                    return await tools['move'].handler(
                        dict(status='pending', observation='旧截图仍有封印', direction='d'))
                await behavior(move)
                yield object.__new__(backend.ResultMessage)
        monkeypatch.setattr(backend, 'create_sdk_mcp_server', server)
        monkeypatch.setattr(backend, 'ClaudeSDKClient', SDK)


def run(backend, device):
    return backend.run_agent(device, Model(), '到达封印', joystick=Joystick(327, 825, 80),
                             sleep=lambda _: None, completion_probe=probe,
                             evidence_type=GoalEvidence)


@pytest.mark.parametrize('backend', [compat_runner, claude_runner])
def test_initial_boss_returns_without_starting_sdk(monkeypatch, backend):
    device = Device()
    device.boss = True

    def forbidden(*args, **kwargs):
        raise AssertionError('已开战不应启动模型连接')

    monkeypatch.setattr(backend, 'model_api_connection', forbidden)
    result = run(backend, device)
    assert result.reached and result.steps == 0
    assert not device.moves and device.screenshots == 1


@pytest.mark.parametrize('backend', [compat_runner, claude_runner])
def test_slow_model_is_cancelled_and_late_move_is_blocked(monkeypatch, backend):
    device = Device()
    events = []

    async def behavior(move):
        # The world changes while an old screenshot request remains in flight.
        device.boss = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            events.append('cancelled')
            try:
                await move()
            finally:
                events.append('late_callback_finished')

    install_sdk(monkeypatch, backend, behavior)
    result = run(backend, device)
    assert result.reached and result.steps == 0 and result.observation == '已出现毒风 BOSS'
    assert events == ['cancelled', 'late_callback_finished']
    assert not device.moves


@pytest.mark.parametrize('backend', [compat_runner, claude_runner])
def test_move_triggers_boss_and_returns_history_without_next_request(monkeypatch, backend):
    device = Device()

    async def behavior(move):
        await move()
        pytest.fail('移动后的截图已完成，不应再请求模型')

    install_sdk(monkeypatch, backend, behavior)
    result = run(backend, device)
    assert result.reached and result.steps == 1
    assert len(result.history) == 1 and result.history[0].action == 'd'
    assert device.moves == [(327, 825, 407, 825, 300)]


@pytest.mark.parametrize('backend', [compat_runner, claude_runner])
def test_new_boss_before_stale_move_prevents_input(monkeypatch, backend):
    device = Device()

    async def behavior(move):
        # Return before the monitoring interval; the movement guard must catch it.
        device.boss = True
        await move()

    install_sdk(monkeypatch, backend, behavior)
    result = run(backend, device)
    assert result.reached and result.steps == 0
    assert not device.moves


def test_completion_monitor_error_cancels_request_and_does_not_claim_success():
    events = []
    device = Device()

    def bad_probe(png):
        raise RuntimeError('设备断开')

    async def pending():
        try:
            await asyncio.sleep(30)
        finally:
            events.append('stopped')

    with pytest.raises(RuntimeError, match='设备断开'):
        asyncio.run(completion_watch.CompletionWatch(device, bad_probe).run(pending()))
    assert events == ['stopped'] and not device.moves


def test_claude_completion_kills_only_owned_cli_and_bounds_slow_exit(monkeypatch):
    device = Device()
    events = []
    process = SimpleNamespace(kill=lambda: events.append('owned_cli_killed'))

    async def behavior(move):
        device.boss = True
        await asyncio.sleep(30)

    async def slow_exit():
        events.append('cleanup_started')
        try:
            await asyncio.sleep(20)
        finally:
            events.append('cleanup_stopped')

    install_sdk(monkeypatch, claude_runner, behavior, on_exit=slow_exit, process=process)
    started = time.monotonic()
    result = run(claude_runner, device)
    assert time.monotonic() - started < 1
    assert result.reached and not device.moves
    assert events == ['owned_cli_killed', 'cleanup_started', 'cleanup_stopped']
