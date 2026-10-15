from contextlib import nullcontext

import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.survival_challenge.handler import _dismiss_transfer_if_present, TRANSFER_DISMISS
from test_daily_observation_retry import Device, target
from test_execution import Clock


@pytest.mark.parametrize('scoped', [False, True])
@pytest.mark.parametrize('misses', [1, 2, 3])
def test_transfer_missed_frames_are_retried_before_dismissal(scoped, misses):
    clock, device = Clock(), Device()
    observations = []

    def matcher(frame, *args):
        observations.append((frame, tuple(device.taps)))
        return ((100, 100), 1) if int(frame) == misses + 1 else (None, .2)

    scope = execution_scope(30, clock=clock, sleep=clock.sleep) if scoped else nullcontext()
    with scope:
        _dismiss_transfer_if_present(
            device, ImageRecognition(device, matcher, sleep=clock.sleep), target('transfer'),
        )

    assert device.taps == [TRANSFER_DISMISS]
    assert all(taps == () for _, taps in observations[:misses + 1])
    assert observations[misses + 1][1] == (TRANSFER_DISMISS,)
    assert len(device.frames) == misses + 1 + 4
    assert clock() == misses + 4


@pytest.mark.parametrize('scoped', [False, True])
def test_absent_transfer_stops_after_initial_check_and_three_retries(scoped):
    clock, device = Clock(), Device()
    scope = execution_scope(30, clock=clock, sleep=clock.sleep) if scoped else nullcontext()
    with scope:
        _dismiss_transfer_if_present(
            device, ImageRecognition(device, lambda *args: (None, .2), sleep=clock.sleep),
            target('transfer'),
        )

    assert device.frames == [b'1', b'2', b'3', b'4']
    assert device.taps == []
    assert clock() == 3


@pytest.mark.parametrize('scoped', [False, True])
def test_dismissed_transfer_is_not_declared_closed_on_one_missed_frame(scoped):
    clock, device = Clock(), Device()

    def matcher(frame, *args):
        return ((100, 100), 1) if frame in (b'1', b'3') else (None, .2)

    scope = execution_scope(30, clock=clock, sleep=clock.sleep) if scoped else nullcontext()
    with scope:
        _dismiss_transfer_if_present(
            device, ImageRecognition(device, matcher, sleep=clock.sleep), target('transfer'),
        )

    assert device.taps == [TRANSFER_DISMISS, TRANSFER_DISMISS]
    assert device.frames == [b'1', b'2', b'3', b'4', b'5', b'6', b'7']
    assert clock() == 6


@pytest.mark.parametrize('scoped', [False, True])
def test_late_transfer_retry_result_cannot_trigger_dismissal(scoped):
    clock, device = Clock(), Device()

    def matcher(frame, *args):
        if frame == b'4':
            clock.now = 6
            return (100, 100), 1
        return None, .2

    scope = execution_scope(5, clock=clock, sleep=clock.sleep) if scoped else nullcontext()
    error = DailyDeadlineExceeded if scoped else VisualError
    with pytest.raises(error):
        with scope:
            _dismiss_transfer_if_present(
                device, ImageRecognition(device, matcher, sleep=clock.sleep), target('transfer'),
                deadline=5, elapsed=clock,
            )

    assert device.frames == [b'1', b'2', b'3', b'4']
    assert device.taps == []


@pytest.mark.parametrize('scoped', [False, True])
def test_transfer_retry_cannot_extend_short_deadline(scoped):
    clock, device = Clock(), Device()
    scope = execution_scope(2.5, clock=clock, sleep=clock.sleep) if scoped else nullcontext()
    error = DailyDeadlineExceeded if scoped else VisualError
    with pytest.raises(error):
        with scope:
            _dismiss_transfer_if_present(
                device, ImageRecognition(device, lambda *args: (None, .2), sleep=clock.sleep),
                target('transfer'), deadline=2.5, elapsed=clock,
            )

    assert device.frames == [b'1', b'2', b'3']
    assert device.taps == []
    assert clock() == 2.5
