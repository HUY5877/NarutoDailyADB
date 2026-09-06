from pathlib import Path

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError


TARGET = {"name": "按钮", "template": "button.png", "left": 0, "top": 0,
          "right": 100, "bottom": 100, "threshold": 0.85}


class Frames:
    def __init__(self):
        self.count = 0

    def screenshot(self):
        self.count += 1
        return str(self.count).encode()


def test_miss_refreshes_once_and_returns_new_coordinates():
    client, waits = Frames(), []
    def matcher(png, path, roi, threshold):
        assert path == Path("assets/button.png")
        assert roi == (0, 0, 100, 100) and threshold == 0.85
        return (None, 0.816) if png == b"1" else ((60, 70), 0.99)
    images = ImageRecognition(client, matcher, Path("assets"), waits.append)
    assert images.find(TARGET) == ((60, 70), 0.99)
    assert waits == [1] and client.count == 2


def test_success_does_not_add_wait_and_missing_target_retries_only_once():
    client, waits = Frames(), []
    images = ImageRecognition(client, lambda *args: ((50, 60), 0.99), sleep=waits.append)
    assert images.find(TARGET)[0] == (50, 60)
    assert waits == [] and client.count == 1
    images.matcher = lambda *args: (None, 0.4)
    assert images.find(TARGET) == (None, 0.4)
    assert waits == [1] and client.count == 3


def test_group_retry_rechecks_every_guard_on_fresh_frame():
    client, waits = Frames(), []
    images = ImageRecognition(client, sleep=waits.append)
    def probe(png):
        return ((None, True) if png == b"1" else ((60, 70), False))
    result = images.check(probe, lambda value: value[0] is not None and value[1], "按钮和红点")
    assert result == ((60, 70), False)
    assert waits == [1] and client.count == 2


def test_retry_budget_counts_against_timeout():
    client, waits = Frames(), []
    images = ImageRecognition(client, lambda *args: (None, 0.4), sleep=waits.append)
    with pytest.raises(VisualError, match="超时"):
        images.wait_for(TARGET, timeout_seconds=2, poll_seconds=1)
    assert sum(waits) == 2
    assert client.count == 3


def test_no_retry_sleep_past_deadline_and_decode_errors_propagate():
    client, waits = Frames(), []
    images = ImageRecognition(client, lambda *args: (None, 0.4), sleep=waits.append)
    assert images.find(TARGET, deadline=0.5)[0] is None
    assert waits == []
    def broken(*args):
        raise VisualError("无法解析截图")
    images.matcher = broken
    with pytest.raises(VisualError, match="无法解析"):
        images.find(TARGET)
    assert waits == []
