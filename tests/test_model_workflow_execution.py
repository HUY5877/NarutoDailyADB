"""Visual rechecks use fresh frames within one shared execution deadline."""

import pytest

from naruto_daily.common import model_workflow
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.model_workflow import ModelWorkflow, require_page
from naruto_daily.common.visual import TemplateConfigurationError, VisualError
from naruto_daily.multimodal.client import MultimodalConfigurationError, MultimodalRequestError


POINT = (10, 20)
UNKNOWN = {"page": "unknown"}
READY = {"page": "ready", "point": [10, 20]}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Device:
    def __init__(self):
        self.frames = []
        self.taps = []

    def screenshot(self):
        frame = f"frame-{len(self.frames) + 1}".encode()
        self.frames.append(frame)
        return frame

    def tap(self, x, y):
        self.taps.append((x, y))


class Model:
    def __init__(self, clock, observe):
        self.clock = clock
        self.observe = observe
        self.calls = []

    def complete_json(self, png, prompt, *, timeout_seconds, **kwargs):
        self.calls.append((png, timeout_seconds))
        self.clock.sleep(1)
        result = self.observe(png)
        if isinstance(result, Exception):
            raise result
        return result


def target(data):
    require_page(data, "ready")
    return tuple(data["point"])


def workflow(monkeypatch, clock, device, model, *, max_calls=2, timeout=3):
    monkeypatch.setattr(model_workflow.time, "monotonic", clock)
    action = {
        "timeout_seconds": timeout,
        "post_click_wait_seconds": 0,
        "multimodal": {"max_calls": max_calls, "timeout_seconds": 30},
    }
    return ModelWorkflow(device, action, model=model, sleep=clock.sleep)


def test_visual_rechecks_exceed_max_calls_and_click_only_after_fresh_confirmation(monkeypatch):
    clock, device = Clock(), Device()

    def observe(frame):
        assert device.taps == []
        return READY if frame == b"frame-6" else UNKNOWN

    model = Model(clock, observe)
    with execution_scope(20, clock=clock, sleep=clock.sleep):
        flow = workflow(monkeypatch, clock, device, model)
        flow.tap(flow.read("点击前确认", "offline prompt", target, timeout=.1))

    assert device.taps == [POINT]
    assert device.frames == [b"frame-1", b"frame-2", b"frame-3", b"frame-4", b"frame-5", b"frame-6"]
    assert [frame for frame, _ in model.calls] == device.frames
    assert [remaining for _, remaining in model.calls] == [20, 18, 16, 14, 12, 10]


def test_unconfirmed_visuals_continue_to_shared_deadline_without_clicking(monkeypatch):
    clock, device = Clock(), Device()
    model = Model(clock, lambda _: UNKNOWN)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(7, clock=clock, sleep=clock.sleep):
        flow = workflow(monkeypatch, clock, device, model)
        flow.tap(flow.read("点击前确认", "offline prompt", target))

    assert device.taps == []
    assert clock.now == 7
    assert device.frames == [b"frame-1", b"frame-2", b"frame-3", b"frame-4"]
    assert [remaining for _, remaining in model.calls] == [7, 5, 3, 1]


def test_visual_rechecks_and_next_read_share_deadline_without_resetting_budget(monkeypatch):
    clock, device = Clock(), Device()
    model = Model(clock, lambda frame: READY if frame in {b"frame-4", b"frame-6"} else UNKNOWN)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(10, clock=clock, sleep=clock.sleep):
        flow = workflow(monkeypatch, clock, device, model)
        flow.tap(flow.read("第一个目标", "offline prompt", target))
        flow.tap(flow.read("第二个目标", "offline prompt", target))

    assert device.taps == [POINT]
    assert clock.now == 10
    assert [remaining for _, remaining in model.calls] == [10, 8, 6, 4, 3, 1]


def test_late_confirmation_after_visual_recheck_cannot_click(monkeypatch):
    clock, device = Clock(), Device()
    model = Model(clock, lambda frame: READY if frame == b"frame-2" else UNKNOWN)

    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=clock, sleep=clock.sleep):
        flow = workflow(monkeypatch, clock, device, model)
        flow.tap(flow.read("点击前确认", "offline prompt", target))

    assert clock.now == 3
    assert device.taps == []
    assert [frame for frame, _ in model.calls] == [b"frame-1", b"frame-2"]


@pytest.mark.parametrize("failure", [
    RuntimeError("offline device/model failure"),
    TemplateConfigurationError("offline missing template"),
    MultimodalConfigurationError("offline missing channel configuration"),
    MultimodalRequestError("offline unauthorized", status_code=401),
    MultimodalRequestError("offline network failure", retryable=True),
])
def test_nonvisual_and_request_failures_are_not_retried_by_workflow(monkeypatch, failure):
    clock, device = Clock(), Device()
    model = Model(clock, lambda _: failure)

    with pytest.raises(type(failure)) as caught, execution_scope(20, clock=clock, sleep=clock.sleep):
        flow = workflow(monkeypatch, clock, device, model)
        flow.tap(flow.read("点击前确认", "offline prompt", target))

    assert caught.value is failure
    assert device.taps == []
    assert device.frames == [b"frame-1"]
    assert clock.now == 1


def test_visual_rechecks_outside_execution_keep_configured_call_limit(monkeypatch):
    clock, device = Clock(), Device()
    model = Model(clock, lambda _: UNKNOWN)
    flow = workflow(monkeypatch, clock, device, model, max_calls=3, timeout=40)

    with pytest.raises(VisualError, match="多次识别未确认"):
        flow.tap(flow.read("点击前确认", "offline prompt", target))

    assert device.taps == []
    assert device.frames == [b"frame-1", b"frame-2", b"frame-3"]
    assert [remaining for _, remaining in model.calls] == [40, 38, 36]


def button_workflow(device, matcher):
    action = dict(timeout_seconds=.1, poll_seconds=1, post_click_wait_seconds=0,
                  button=dict(name='确认按钮', template='offline.png', left=0, top=0,
                              right=1920, bottom=1080, threshold=.9))
    return ModelWorkflow(device, action, model=object(), template_matcher=matcher)


def test_template_button_retries_unreadable_frames_then_clicks_latest_confirmation():
    clock, device, seen = Clock(), Device(), []

    def match(frame, *args):
        seen.append(frame)
        if len(seen) <= 4:
            raise VisualError('无法解析当前截图')
        return (50, 60), 1

    with execution_scope(20, clock=clock, sleep=clock.sleep):
        button_workflow(device, match).button('button')

    assert device.taps == [(50, 60)]
    assert seen == device.frames == [b'frame-1', b'frame-2', b'frame-3', b'frame-4', b'frame-5']


def test_template_button_unreadable_frames_stop_at_original_deadline_without_clicking():
    clock, device = Clock(), Device()

    def match(*args):
        raise VisualError('无法解析当前截图')

    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=clock, sleep=clock.sleep):
        button_workflow(device, match).button('button')

    assert clock() == 3 and not device.taps


@pytest.mark.parametrize('failure', [TemplateConfigurationError('缺少模板'), RuntimeError('设备离线')])
def test_template_button_hard_failure_is_not_retried(failure):
    clock, device = Clock(), Device()

    def match(*args):
        raise failure

    with pytest.raises(type(failure)) as caught, execution_scope(20, clock=clock, sleep=clock.sleep):
        button_workflow(device, match).button('button')

    assert caught.value is failure
    assert device.frames == [b'frame-1'] and not device.taps and clock() == 0


def test_template_button_late_confirmation_after_retries_cannot_click():
    clock, device = Clock(), Device()

    def match(frame, *args):
        if frame == b'frame-1':
            raise VisualError('无法解析当前截图')
        clock.sleep(3)
        return (50, 60), 1

    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=clock, sleep=clock.sleep):
        button_workflow(device, match).button('button')

    assert device.frames == [b'frame-1', b'frame-2'] and not device.taps
