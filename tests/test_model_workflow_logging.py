import logging

import pytest

from naruto_daily.common import model_workflow
from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.common.visual import VisualError
from naruto_daily.multimodal.client import MultimodalError


class Client:
    def __init__(self, now, error=None):
        self.now = now
        self.error = error
        self.screenshots = 0

    def screenshot(self):
        self.screenshots += 1
        self.now[0] += 2
        if self.error is not None:
            raise self.error
        return b'private-screenshot-bytes'


class Model:
    def __init__(self, now, *answers):
        self.now = now
        self.answers = iter(answers)
        self.calls = []
        self._key = 'private-api-key'

    def complete_json(self, png, prompt, *, timeout_seconds):
        self.calls.append((png, prompt, timeout_seconds))
        self.now[0] += 3.5
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer


def workflow(monkeypatch, caplog, *answers, timeout=240, max_calls=2, screenshot_error=None):
    now = [100.0]
    monkeypatch.setattr(model_workflow.time, 'monotonic', lambda: now[0])
    caplog.set_level(logging.INFO, logger='naruto_daily')
    client = Client(now, screenshot_error)
    model = Model(now, *answers)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    action = dict(timeout_seconds=timeout, multimodal=dict(timeout_seconds=30, max_calls=max_calls))
    flow = ModelWorkflow(client, action, model=model, sleep=sleep)
    return flow, client, model, sleeps


@pytest.mark.parametrize(('timeout', 'remaining', 'call_timeout'), [(240, 238, 30), (20, 18, 18)])
def test_logs_request_stages_and_budget_without_request_contents(
        monkeypatch, caplog, timeout, remaining, call_timeout):
    answer = {'page': 'takeout', 'can_claim': True}
    flow, client, model, sleeps = workflow(monkeypatch, caplog, answer, timeout=timeout)

    assert flow.read('一乐外卖领取判断', 'private-prompt', lambda result: result['can_claim']) is True

    assert caplog.messages == [
        '[一乐外卖领取判断 第1/2次] 开始截图',
        f'[一乐外卖领取判断 第1/2次] 模型请求开始：阶段剩余 {remaining:.2f} 秒，调用超时 {call_timeout:.2f} 秒',
        '[一乐外卖领取判断 第1/2次] 模型请求返回：耗时 3.50 秒',
        '[一乐外卖领取判断 第1次] True',
    ]
    assert client.screenshots == 1
    assert model.calls == [(b'private-screenshot-bytes', 'private-prompt', remaining)]
    assert sleeps == []
    for private_value in ('private-prompt', 'private-screenshot-bytes', 'private-api-key'):
        assert private_value not in caplog.text


def test_retry_logs_exception_type_and_reason_without_extra_calls(monkeypatch, caplog):
    error = MultimodalError('多模态 API 网络异常或请求超时')
    flow, client, model, sleeps = workflow(monkeypatch, caplog, error, {'page': 'takeout'})

    assert flow.read('外卖复查', 'private-prompt', lambda value: value['page']) == 'takeout'

    assert '[外卖复查 第1/2次] 识别未确认：MultimodalError：多模态 API 网络异常或请求超时；本次耗时 5.50 秒；将重新截图重试' in caplog.messages
    assert '[外卖复查 第2/2次] 开始截图' in caplog.messages
    assert client.screenshots == len(model.calls) == 2
    assert [call[2] for call in model.calls] == [238, 231.5]
    assert sleeps == [1]


def test_final_failure_keeps_original_cause_and_retry_limit(monkeypatch, caplog):
    error = VisualError('未确认一乐外卖页面')
    flow, client, model, sleeps = workflow(monkeypatch, caplog, {}, {})

    def validate(_):
        raise error

    with pytest.raises(VisualError, match='外卖判断多次识别未确认，已停止') as caught:
        flow.read('外卖判断', 'private-prompt', validate)

    assert caught.value.__cause__ is error
    assert client.screenshots == len(model.calls) == 2
    assert sleeps == [1]
    assert 'VisualError：未确认一乐外卖页面' in caplog.messages[-1]
    assert caplog.messages[-1].endswith('已达重试上限，停止识别')


def test_screenshot_exception_keeps_propagation_and_does_not_start_request(monkeypatch, caplog):
    error = RuntimeError('ADB 截图失败')
    flow, client, model, sleeps = workflow(monkeypatch, caplog, screenshot_error=error)

    with pytest.raises(RuntimeError) as caught:
        flow.read('外卖判断', 'private-prompt', lambda value: value)

    assert caught.value is error
    assert caplog.messages == ['[外卖判断 第1/2次] 开始截图']
    assert client.screenshots == 1
    assert model.calls == sleeps == []


def test_late_response_is_logged_but_never_validated(monkeypatch, caplog):
    flow, client, model, sleeps = workflow(monkeypatch, caplog, {}, timeout=4, max_calls=1)
    validated = []

    with pytest.raises(VisualError, match='日常页面处理超时') as caught:
        flow.read('外卖判断', 'private-prompt', validated.append)

    assert isinstance(caught.value.__cause__, VisualError)
    assert '[外卖判断 第1/1次] 模型请求返回：耗时 3.50 秒' in caplog.messages
    assert 'VisualError：日常页面处理超时' in caplog.messages[-1]
    assert validated == sleeps == []
    assert client.screenshots == len(model.calls) == 1
    assert model.calls[0][2] == 2


def test_local_read_deadline_limits_request_even_with_long_action_budget(monkeypatch, caplog):
    flow, client, model, sleeps = workflow(monkeypatch, caplog, {}, max_calls=1)
    validated = []
    with pytest.raises(VisualError, match='结果核验识别超时'):
        flow.read('结果核验', 'private-prompt', validated.append, timeout=5)
    assert model.calls[0][2] == 3
    assert validated == sleeps == []
    assert client.screenshots == len(model.calls) == 1


def test_expired_local_read_deadline_does_not_screenshot_or_call_model(monkeypatch, caplog):
    flow, client, model, sleeps = workflow(monkeypatch, caplog)
    with pytest.raises(VisualError, match='结果核验识别超时'):
        flow.read('结果核验', 'private-prompt', lambda value: value, timeout=0)
    assert client.screenshots == 0
    assert model.calls == sleeps == []
