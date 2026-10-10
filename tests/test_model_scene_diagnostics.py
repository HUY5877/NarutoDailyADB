from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from naruto_daily.multimodal.agent import claude_runner, compat_runner
from naruto_daily.common import model_workflow
from naruto_daily.multimodal.client import MultimodalRequestError
from naruto_daily.multimodal.diagnostics import current_model_scene
from naruto_daily.battle import handler as battle_handler


PRIVATE_DETAILS = 'private-key https://private.example/request private-prompt private-screenshot'


class Device:
    def __init__(self):
        self.moves = []

    def screenshot(self):
        return b'private-screenshot'

    def swipe(self, *args):
        self.moves.append(args)

    def tap(self, *args):
        pytest.fail('诊断测试不得产生点击')


def record_scene(monkeypatch, module):
    active = []
    seen = []
    original_context = module.model_request_context

    @contextmanager
    def context(scene):
        active.append(scene)
        seen.append(scene)
        try:
            with original_context(scene):
                yield
        finally:
            active.pop()

    monkeypatch.setattr(module, 'model_request_context', context)
    return active, seen


@pytest.mark.parametrize(('backend', 'scene'), [
    (compat_runner, '导航/兼容SDK'),
    (claude_runner, '导航/ClaudeSDK'),
])
def test_navigation_logs_safe_exception_chain_and_scene_without_input_or_retry(
        monkeypatch, caplog, backend, scene):
    active, seen = record_scene(monkeypatch, backend)
    entered = []
    cause = TimeoutError(PRIVATE_DETAILS)
    error = (compat_runner.OpenAIError(PRIVATE_DETAILS) if backend is compat_runner
             else RuntimeError(PRIVATE_DETAILS))
    error.__cause__ = cause

    class FailingSDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            assert current_model_scene() == scene
            entered.append(active.copy())
            raise error

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(backend, 'ModelAsyncClient' if backend is compat_runner else 'ClaudeSDKClient', FailingSDK)
    model = SimpleNamespace(agent_connection=lambda: dict(
        base_url='https://private.example/v1', api_key='private-key', model='offline', timeout_seconds=30))
    device = Device()

    with pytest.raises(backend.AgentError, match='导航模型请求或工具参数异常') as caught:
        backend.run_agent(device, model, 'private-prompt', joystick=SimpleNamespace(move=lambda *_: None)
                          if backend is claude_runner else compat_runner.Joystick(200, 800, 80),
                          evidence_type=backend.GoalEvidence)

    assert entered == [[scene]] and seen == [scene] and active == []
    assert device.moves == []
    assert scene in caplog.text and type(error).__name__ in caplog.text and 'TimeoutError' in caplog.text
    for value in PRIVATE_DETAILS.split():
        assert value not in caplog.text and value not in str(caught.value)


def test_workflow_context_and_request_failure_preserve_retry_boundary(monkeypatch, caplog):
    active, seen = record_scene(monkeypatch, model_workflow)
    error = MultimodalRequestError('接口请求失败：error_type=TimeoutError error_kind=timeout')
    error.__cause__ = TimeoutError(PRIVATE_DETAILS)
    calls = []

    def fail(*args, **kwargs):
        assert current_model_scene() == '外卖复查'
        calls.append(active.copy())
        raise error

    action = dict(timeout_seconds=60, multimodal=dict(timeout_seconds=30, max_calls=3))
    flow = model_workflow.ModelWorkflow(Device(), action, model=SimpleNamespace(complete_json=fail))
    with pytest.raises(MultimodalRequestError) as caught:
        flow.read('外卖复查', 'private-prompt', lambda result: result)

    assert caught.value is error
    assert calls == [['外卖复查']] and seen == ['外卖复查'] and active == []
    assert '外卖复查' in caplog.text and 'TimeoutError' in caplog.text
    for value in PRIVATE_DETAILS.split():
        assert value not in caplog.text


def test_battle_completion_model_runs_with_scene_without_attacking(monkeypatch):
    active, seen = record_scene(monkeypatch, battle_handler)
    calls = []

    def complete(*args, **kwargs):
        assert current_model_scene() == '战斗完成检查'
        calls.append(active.copy())
        return dict(finished=True, page='finished', reason='已结束')

    battle = dict(timeout_seconds=60, tap_interval_seconds=0,
                  buttons=[dict(name='普攻', x=1, y=1)], end_targets=[])
    result = battle_handler.run_battle(Device(), battle, model=SimpleNamespace(complete_json=complete))

    assert result.taps == 0
    assert calls == [['战斗完成检查']] and seen == ['战斗完成检查'] and active == []
