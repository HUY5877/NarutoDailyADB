"""免费招募点击未生效后的重新定位与单次补点回归。"""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.multimodal.client import MultimodalError, MultimodalRequestError
from naruto_daily.recruit import free_button, handler


FREE_TARGET = dict(name="免费招募按钮", left=900, top=700, right=1500, bottom=910)


def target(name):
    return dict(name=name, template=f"{name}.png", left=0, top=0,
                right=1920, bottom=1080, threshold=.9)


TAB, GUARD = target("tab"), target("guard")
RESULT_CENTER, NINJA_CENTER = (600, 840), (960, 985)


def action(*, legacy=False):
    result = {"result_confirm": target("result")}
    if not legacy:
        result["ninja_result_confirm"] = target("ninja")
    return result


def answer(*, count=1, center=(1100, 800), category="普通招募", text=None):
    return dict(category=category, free_count=count,
                button_text=f"免费{count}抽" if text is None else text,
                center=list(center) if center is not None else None)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Device:
    def __init__(self, *frames):
        self.sequence = [frame.encode() for frame in frames]
        self.frames, self.taps = [], []

    def screenshot(self):
        frame = self.sequence[min(len(self.frames), len(self.sequence) - 1)]
        self.frames.append(frame)
        return frame

    def tap(self, *point):
        self.taps.append(point)


class Model:
    def __init__(self, *responses):
        self.responses = list(responses) or [answer()]
        self.calls = []

    def complete_json(self, frame, prompt, **kwargs):
        response = self.responses[min(len(self.calls), len(self.responses) - 1)]
        self.calls.append((frame, kwargs))
        if isinstance(response, Exception):
            raise response
        return response


def match(frame, path, roi, threshold):
    page = frame.decode().split(":")[0]
    visible = {
        "free": {"tab", "guard"},
        "no-dot": {"tab"},
        "no-tab": {"guard"},
        "result": {"result"},
        "ninja": {"ninja"},
        "result-with-free": {"result", "tab", "guard"},
        "ninja-with-free": {"ninja", "tab", "guard"},
        "animation": set(),
    }[page]
    centers = {"result": RESULT_CENTER, "ninja": NINJA_CENTER}
    return (centers.get(path.stem, (200, 400)), .99) if path.stem in visible else (None, .1)


def probe_for(images, model, *, max_calls=10, clock=lambda: 0):
    return free_button.make_free_recruit_retry_probe(
        FREE_TARGET, category="普通招募", tab=TAB, guard=GUARD,
        images=images, model=model, max_calls=max_calls, clock=clock,
    )


def confirm(device, images, probe, *, timeout=8, pre_wait=.1, legacy=False):
    return handler._confirm_recruit_result(
        device, action(legacy=legacy), images=images, timeout_seconds=timeout,
        poll_seconds=.2, pre_click_wait=pre_wait, post_click_wait=.1,
        retry_button=probe,
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_retry_relocates_free_button_twice_and_uses_latest_coordinates(legacy):
    device = Device("free:first", "free:confirm", "result:first", "result:confirm")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer(center=(1100, 800)), answer(center=(1240, 850)))

    assert confirm(device, images, probe_for(images, model), legacy=legacy)

    assert device.taps == [(1240, 850), RESULT_CENTER]
    assert [frame for frame, _ in model.calls] == [b"free:first", b"free:confirm"]


def test_ignored_retry_is_not_repeated_until_timeout():
    device = Device("free:first", "free:confirm", "free:still")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())

    with pytest.raises(VisualError, match="超时"):
        confirm(device, images, probe_for(images, model), timeout=3)

    assert device.taps == [(1100, 800)]
    assert len(model.calls) == 2


@pytest.mark.parametrize("page, expected", [
    ("result-with-free", [RESULT_CENTER]),
    ("ninja-with-free", [NINJA_CENTER, RESULT_CENTER]),
])
def test_result_templates_take_priority_over_free_button_model(page, expected):
    device = Device(f"{page}:first", f"{page}:confirm", "result:first", "result:confirm")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())

    assert confirm(device, images, probe_for(images, model))

    assert device.taps == expected
    assert model.calls == []


def test_ninja_animation_never_restarts_free_recruit():
    device = Device("ninja:first", "ninja:confirm", "free:animation-a",
                    "free:animation-b", "result:first", "result:confirm")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())

    assert confirm(device, images, probe_for(images, model))

    assert device.taps == [NINJA_CENTER, RESULT_CENTER]
    assert model.calls == []


@pytest.mark.parametrize("page", ["ninja", "result"])
def test_result_seen_once_disables_retry_even_if_confirmation_disappears(page):
    device = Device(f"{page}:first", "free:after-result")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())

    with pytest.raises(VisualError, match="超时"):
        confirm(device, images, probe_for(images, model), timeout=3)

    assert device.taps == []
    assert model.calls == []


@pytest.mark.parametrize("page", ["ninja", "result"])
def test_result_appearing_during_free_confirmation_permanently_cancels_retry(page):
    device = Device("free:first", f"{page}:appeared", "free:after-result")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())

    if page == "result":
        assert confirm(device, images, probe_for(images, model), timeout=3)
        assert device.taps == [RESULT_CENTER]
    else:
        with pytest.raises(VisualError, match="超时"):
            confirm(device, images, probe_for(images, model), timeout=3)

    assert (1100, 800) not in device.taps
    assert [frame for frame, _ in model.calls] == [b"free:first"]


def test_animation_without_result_or_free_conditions_does_not_call_model():
    device = Device("animation:first", "animation:second", "result:first", "result:confirm")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())

    assert confirm(device, images, probe_for(images, model))

    assert device.taps == [RESULT_CENTER]
    assert model.calls == []


@pytest.mark.parametrize("lost", ["no-dot", "no-tab"])
def test_retry_cancels_when_required_page_condition_disappears(lost):
    device = Device("free:first", f"{lost}:confirm")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())

    with pytest.raises(VisualError, match="超时"):
        confirm(device, images, probe_for(images, model), timeout=3)

    assert device.taps == []
    assert [frame for frame, _ in model.calls] == [b"free:first"]


def test_retry_probe_never_combines_tab_and_red_dot_from_different_frames():
    device = Device("no-dot:first", "no-tab:second")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())
    probe = probe_for(images, model)

    assert probe(b"no-dot:first", 10) is None
    assert probe(b"no-tab:second", 10) is None
    assert device.frames == []
    assert device.taps == []
    assert model.calls == []


@pytest.mark.parametrize("response", [
    answer(count=0), answer(category="高级招募"), answer(text="10抽"),
    answer(text="01:00后免费"), answer(count=1, text="免费5抽"),
])
def test_retry_probe_rejects_unavailable_paid_or_wrong_category(response):
    device = Device("free:first")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(response)

    assert probe_for(images, model)(b"free:first", 10) is None

    assert device.frames == []
    assert device.taps == []
    assert [frame for frame, _ in model.calls] == [b"free:first"]


@pytest.mark.parametrize("center", [None, (899, 800), (1500, 800), (1100, 910), (True, 800)])
def test_retry_probe_rejects_invalid_model_coordinates(center):
    device = Device("free:first")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer(center=center))

    with pytest.raises(MultimodalError):
        probe_for(images, model)(b"free:first", 10)

    assert device.taps == []


def test_changed_free_count_needs_a_new_matching_pair_before_retry():
    device = Device("free:first", "free:changed", "free:latest-first",
                    "free:latest-confirm", "result:first", "result:confirm")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer(count=2, center=(1100, 800)), answer(count=1, center=(1150, 810)),
                  answer(count=1, center=(1200, 820)), answer(count=1, center=(1250, 830)))

    assert confirm(device, images, probe_for(images, model))

    assert device.taps == [(1250, 830), RESULT_CENTER]


def test_free_button_disappearing_on_recheck_is_not_clicked():
    device = Device("free:first", "free:unavailable")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer(), answer(count=0))

    with pytest.raises(VisualError, match="超时"):
        confirm(device, images, probe_for(images, model, max_calls=20), timeout=3)

    assert device.taps == []


def test_retry_probe_obeys_model_call_limit_without_shared_execution():
    device = Device("free:first")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())
    probe = probe_for(images, model, max_calls=1)

    assert probe(b"free:first", 10) == (1, (1100, 800))
    with pytest.raises(MultimodalError, match="调用上限"):
        probe(b"free:second", 10)

    assert len(model.calls) == 1
    assert device.taps == []


@pytest.mark.parametrize("recovery", ["result", "no-dot"])
def test_model_failure_retries_fresh_frame_and_rechecks_page_before_model(recovery):
    clock = Clock()
    device = Device("free:failed", f"{recovery}:fresh", f"{recovery}:confirm")
    model = Model(MultimodalRequestError("临时网络失败", retryable=True), answer())
    expected = pytest.raises(DailyDeadlineExceeded) if recovery == "no-dot" else nullcontext()
    with expected, execution_scope(15, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device, match)
        probe = probe_for(images, model, clock=clock)
        if recovery == "result":
            assert confirm(device, images, probe, timeout=.1)
        else:
            confirm(device, images, probe, timeout=.1)

    assert [frame for frame, _ in model.calls] == [b"free:failed"]
    assert device.frames[1] == f"{recovery}:fresh".encode()
    assert device.taps == ([RESULT_CENTER] if recovery == "result" else [])


def test_model_failure_before_free_confirmation_relocates_using_fresh_frame():
    clock = Clock()
    device = Device("free:first", "free:failed", "free:latest", "result:first", "result:confirm")
    model = Model(answer(center=(1100, 800)),
                  MultimodalRequestError("临时网络失败", retryable=True),
                  answer(center=(1250, 850)))
    with execution_scope(20, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device, match)
        assert confirm(device, images, probe_for(images, model, clock=clock), timeout=.1)

    assert [frame for frame, _ in model.calls] == [b"free:first", b"free:failed", b"free:latest"]
    assert device.taps == [(1250, 850), RESULT_CENTER]


def test_retry_probe_ignores_model_response_after_absolute_deadline():
    clock = Clock()
    device = Device("free:first")
    images = ImageRecognition(device, match, sleep=lambda _: None)

    def complete(*args, **kwargs):
        clock.advance(3)
        return answer()

    probe = probe_for(images, SimpleNamespace(complete_json=complete), clock=clock)
    with pytest.raises(MultimodalError, match="超时"):
        probe(b"free:first", 2)

    assert device.taps == []


def test_retry_confirmation_wait_cannot_extend_original_deadline():
    device = Device("free:first")
    images = ImageRecognition(device, match, sleep=lambda _: None)
    model = Model(answer())
    images.elapsed = 7

    with pytest.raises(VisualError, match="超时"):
        confirm(device, images, probe_for(images, model), timeout=1, pre_wait=1)

    assert images.elapsed <= 8
    assert device.taps == []
    assert len(model.calls) == 1


def test_retry_uses_shared_budget_and_may_pass_legacy_model_call_cap():
    clock = Clock()
    device = Device("free:missing-a", "free:missing-b", "free:first", "free:confirm",
                    "result:first", "result:confirm")
    model = Model(answer(count=0), answer(count=0), answer(), answer(center=(1240, 850)))
    with execution_scope(5, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device, match)
        assert confirm(device, images, probe_for(images, model, max_calls=1, clock=clock), timeout=.01)

    assert device.taps == [(1240, 850), RESULT_CENTER]
    assert len(model.calls) == 4
    assert clock() < 5


def test_retry_and_final_result_wait_share_original_daily_deadline():
    clock = Clock()
    device = Device("free:first", "free:confirm", "animation:waiting")
    model = Model(answer())
    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=clock, sleep=clock.advance):
        images = ImageRecognition(device, match)
        confirm(device, images, probe_for(images, model, clock=clock), timeout=.01)

    assert clock() == pytest.approx(3)
    assert device.taps == [(1100, 800)]
    assert len(model.calls) == 2


def test_handle_free_recruit_connects_retry_to_current_category(monkeypatch):
    device = Device(
        "free:entry", "free:dot", "free:dot-check", "free:tab", "free:tab-check",
        "free:after-tab", "free:initial-first", "free:initial-confirm",
        "free:retry-first", "free:retry-confirm", "result:first", "result:confirm", "no-dot:done",
    )
    model = Model(*[
        answer(category="高级招募", center=center)
        for center in [(1100, 800), (1150, 810), (1200, 820), (1250, 830)]
    ])
    configuration = dict(
        action(), timeout_seconds=10, poll_seconds=.2,
        pre_click_wait_seconds=.1, post_click_wait_seconds=.1,
        advanced_tab=TAB, advanced_red_dot=GUARD,
        ordinary_tab=target("ordinary"), ordinary_red_dot=target("ordinary-guard"),
        free_button=FREE_TARGET, multimodal={"max_calls": 6},
    )
    monkeypatch.setattr(handler, "MultimodalClient", lambda *args, **kwargs: model)
    monkeypatch.setattr(handler, "_find_ordinary_targets", lambda images, config:
                        (config["ordinary_tab"], config["ordinary_red_dot"]))

    summary = handler.handle_free_recruit(device, configuration, template_matcher=match, sleep=lambda _: None)

    assert summary == handler.RecruitSummary(1, 0)
    assert device.taps == [(200, 400), (1150, 810), (1250, 830), RESULT_CENTER]
    assert [frame for frame, _ in model.calls] == [
        b"free:initial-first", b"free:initial-confirm", b"free:retry-first", b"free:retry-confirm",
    ]
