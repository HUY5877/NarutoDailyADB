"""Entry checks protect real battle inputs without using an emulator or network."""

import pytest

from naruto_daily.battle import BattleSummary, run_battle
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.visual import VisualError
from test_battle import Client, Clock, battle_config


class NeverModel:
    def complete_json(self, *args, **kwargs):
        pytest.fail("The completion model must not run before the entry check allows it")


def run(client, clock, **options):
    defaults = dict(
        template_matcher=lambda *args: (None, .2),
        sleep=clock.advance, clock=clock, check_interval_seconds=1,
    )
    defaults.update(options)
    return run_battle(client, battle_config(), **defaults)


def test_entry_check_uses_current_frame_before_model_and_attacks_and_runs_once():
    clock = Clock()
    client = Client(clock)
    events = []

    def guard(png):
        assert not client.taps
        assert png == b"1" and len(client.frames) == 1
        events.append(("guard", png))

    class Model:
        def complete_json(self, png, *args, **kwargs):
            assert events[0] == ("guard", b"1")
            events.append(("model", png))
            if png == b"2":
                assert not client.taps
                return dict(finished=False, page="battle", reason="战斗中")
            return dict(finished=True, page="finished", reason="已结束")

    summary = run(client, clock, before_attack=guard, model=Model())

    assert events == [("guard", b"1"), ("model", b"2"), ("model", b"3")]
    assert summary.end_target == "multimodal"
    assert summary.taps == len(client.taps) == 4


@pytest.mark.parametrize("source", ["detector", "template"])
def test_local_end_detection_precedes_entry_check_and_model(source):
    clock = Clock()
    client = Client(clock)

    def guard(png):
        pytest.fail("An ended battle must not run its entry check")

    options = {"end_detector": lambda png: "房间"} if source == "detector" else {
        "template_matcher": lambda *args: ((50, 50), .99),
    }
    summary = run(client, clock, before_attack=guard, model=NeverModel(), **options)

    assert summary == BattleSummary(0, "房间" if source == "detector" else "result")
    assert not client.taps


def test_entry_check_can_end_battle_without_model_or_attacks():
    clock = Clock()
    client = Client(clock)
    expected = BattleSummary(0, "主动退出落岩秘境")

    def guard(png):
        assert png == b"1" and not client.taps
        return expected

    summary = run(client, clock, before_attack=guard, model=NeverModel())

    assert summary is expected
    assert not client.taps and client.frames == [0]


@pytest.mark.parametrize("new_page", ["result", "room"])
def test_entry_check_page_change_is_recaptured_and_local_end_precedes_model(new_page):
    clock = Clock()
    client = Client(clock)
    page = ["battle"]
    original_screenshot = client.screenshot

    def screenshot():
        return page[0].encode() + b":" + original_screenshot()

    client.screenshot = screenshot

    def guard(png):
        assert png == b"battle:1" and not client.taps
        clock.advance(1)
        page[0] = new_page

    def matcher(png, *args):
        return ((50, 50), .99) if png.startswith(b"result:") else (None, .2)

    def detector(png):
        return "房间" if png.startswith(b"room:") else None

    summary = run(client, clock, before_attack=guard, model=NeverModel(),
                  template_matcher=matcher, end_detector=detector)

    assert summary == BattleSummary(0, "result" if new_page == "result" else "房间")
    assert client.frames == [0, 1] and not client.taps


def test_each_new_battle_requires_its_own_entry_check():
    checked = []

    def guard(png):
        checked.append(png)

    class FinishedModel:
        def complete_json(self, *args, **kwargs):
            return dict(finished=True, page="finished", reason="结算")

    for _ in range(2):
        clock = Clock()
        client = Client(clock)
        summary = run(client, clock, before_attack=guard, model=FinishedModel())
        assert summary.taps == 0 and not client.taps
    assert checked == [b"1", b"1"]


def test_failed_entry_check_retries_new_frame_and_rechecks_local_end_first():
    clock = Clock()
    client = Client(clock)
    checked = []

    def guard(png):
        checked.append(png)
        assert not client.taps
        raise VisualError("秘境名称尚未确认")

    def matcher(png, *args):
        return ((50, 50), .99) if png == b"2" else (None, .2)

    with execution_scope(100, clock=clock, sleep=clock.advance):
        summary = run(client, clock, before_attack=guard, model=NeverModel(), template_matcher=matcher)

    assert checked == [b"1"]
    assert client.frames == [0, 1]
    assert summary == BattleSummary(0, "result")
    assert not client.taps


def test_failed_entry_check_is_not_cached_as_passed_on_retry():
    clock = Clock()
    client = Client(clock)
    checked = []

    def guard(png):
        checked.append(png)
        assert not client.taps
        if len(checked) == 1:
            raise VisualError("秘境名称尚未确认")

    class Model:
        def complete_json(self, png, *args, **kwargs):
            assert png == b"3" and checked == [b"1", b"2"]
            return dict(finished=True, page="finished", reason="结算")

    with execution_scope(100, clock=clock, sleep=clock.advance):
        summary = run(client, clock, before_attack=guard, model=Model())

    assert summary.taps == 0 and not client.taps
    assert client.frames == [0, 1, 1]


def test_entry_check_failure_without_shared_budget_stops_before_attacks():
    clock = Clock()
    client = Client(clock)

    def guard(png):
        raise VisualError("秘境名称尚未确认")

    with pytest.raises(VisualError, match="秘境名称尚未确认"):
        run(client, clock, before_attack=guard, model=NeverModel())
    assert not client.taps and client.frames == [0]


@pytest.mark.parametrize("shared_budget", [False, True])
def test_late_entry_approval_cannot_call_model_or_attack(shared_budget):
    clock = Clock()
    client = Client(clock)

    def guard(png):
        clock.advance(100)

    if shared_budget:
        with pytest.raises(DailyDeadlineExceeded), execution_scope(100, clock=clock, sleep=clock.advance):
            run(client, clock, before_attack=guard, model=NeverModel())
    else:
        with pytest.raises(VisualError, match="战斗等待结束超时"):
            run(client, clock, before_attack=guard, model=NeverModel())
    assert not client.taps and client.frames == [0]


def test_default_battle_without_entry_hook_keeps_existing_attack_flow():
    clock = Clock()
    client = Client(clock)

    def matcher(png, *args):
        return ((50, 50), .99) if png == b"2" else (None, .2)

    summary = run(client, clock, template_matcher=matcher)

    assert summary == BattleSummary(4, "result")
    assert summary.taps == len(client.taps) and client.frames == [0, 1]
