"""Duel evdev packets and acknowledged persistent transport, without a device."""
import base64
from queue import Queue
import struct
from types import SimpleNamespace

import pytest

from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common import fast_duel_tap as fast


POINTS = ((1730, 445), (1680, 260), (1530, 750), (1715, 885), (1300, 965), (1730, 650))
CAPS = '''add device 1: /dev/input/event4
  name: "mouse"
add device 2: /dev/input/event2
  name: "input"
    KEY (0001): BTN_TOOL_FINGER BTN_TOUCH
    ABS_MT_SLOT : value 0, min 0, max 15, fuzz 0
    ABS_MT_POSITION_X : value 0, min 0, max 1919, fuzz 0
    ABS_MT_POSITION_Y : value 0, min 0, max 1079, fuzz 0
    ABS_MT_TRACKING_ID : value 0, min 0, max 65535, fuzz 0
    ABS_MT_PRESSURE : value 0, min 0, max 2, fuzz 0
'''


def events(packet, bits=64):
    return [(kind, code, value) for _, _, kind, code, value in
            struct.iter_unpack('<qqHHi' if bits == 64 else '<iiHHi', packet)]


@pytest.mark.parametrize('bits', [32, 64])
def test_six_points_use_separate_slots_and_one_complete_press_release(bits):
    device = fast.TouchDevice('/dev/input/event2', bits, 15)
    down, up = fast.touch_frames(device, POINTS)
    pressed, released = events(down, bits), events(up, bits)
    assert pressed[0] == (1, 330, 1)
    assert pressed[-1] == released[-1] == (0, 0, 0)
    assert [v for t, c, v in pressed if (t, c) == (3, 47)] == [10, 11, 12, 13, 14, 15]
    assert [v for t, c, v in pressed if (t, c) == (3, 53)] == [1730, 1680, 1530, 1715, 1300, 1730]
    assert [v for t, c, v in pressed if (t, c) == (3, 54)] == [445, 260, 750, 885, 965, 650]
    assert len({v for t, c, v in pressed if (t, c) == (3, 57)}) == 6
    assert [v for t, c, v in released if (t, c) == (3, 47)] == [10, 11, 12, 13, 14, 15]
    assert [v for t, c, v in released if (t, c) == (3, 57)] == [-1] * 6
    assert (1, 330, 0) in released


@pytest.mark.parametrize('change', [
    lambda text: text.replace('max 15', 'max 4'),
    lambda text: text.replace('max 1919', 'max 1279'),
    lambda text: text.replace('max 1079', 'max 719'),
    lambda text: text.replace('BTN_TOUCH', ''),
    lambda text: text.replace('name: "input"', 'name: "other"'),
])
def test_incompatible_touch_devices_do_not_receive_injection(change):
    assert fast.find_touch_device(change(CAPS), 64, 6) is None


def test_discovery_uses_the_advertised_device_and_native_event_size():
    assert fast.find_touch_device(CAPS, 64, 6) == fast.TouchDevice('/dev/input/event2', 64, 15)


class FakeProcess:
    def __init__(self, *, reply=b'OK\n'):
        self.output = Queue()
        self.output.put(b'READY\n')
        self.stdout = SimpleNamespace(readline=self.output.get, close=lambda: None)
        self.stdin = self
        self.lines = []
        self.returncode = None
        self.killed = False
        self.reply = reply

    def write(self, line):
        self.lines.append(line)
        if line == b'STOP\n':
            self.returncode = 0
            self.output.put(b'')
        elif self.reply is not None:
            self.output.put(self.reply)
        return len(line)

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        assert self.returncode is not None
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1
        self.output.put(b'')


def test_persistent_connection_reuses_one_process():
    process, now, starts = FakeProcess(), [0.0], []
    def launch(command, **kwargs):
        starts.append(command)
        return process
    device = fast.TouchDevice('/dev/input/event2', 64, 15)
    channel = fast.DuelTapChannel('adb.exe', 'emulator-5554', device, POINTS,
                                  popen=launch, clock=lambda: now[0])
    with channel:
        for _ in range(3):
            channel.tap()
    assert len(starts) == 1
    assert len(process.lines) == 4 and process.lines[-1] == b'STOP\n'
    for line in process.lines[:-1]:
        down, up = [base64.b64decode(part) for part in line.split()]
        assert events(down)[-1] == events(up)[-1] == (0, 0, 0)
    assert not process.killed


def test_channel_is_lazy_and_does_not_send_when_readiness_exhausts_stage_budget():
    process, now, starts = FakeProcess(), [0.0], []
    def launch(*args, **kwargs):
        starts.append(True)
        now[0] = .04
        return process
    channel = fast.DuelTapChannel('adb.exe', 'device',
        fast.TouchDevice('/dev/input/event2', 64, 15), POINTS, popen=launch)
    with channel:
        assert starts == []
        assert not channel.tap(lambda: now[0] < .03)
    assert starts == [True] and process.lines == [b'STOP\n']


def test_ready_failure_closes_transport_without_sending_a_touch():
    process = FakeProcess()
    process.output.get_nowait()
    process.output.put(b'ERROR\n')
    channel = fast.DuelTapChannel('adb.exe', 'device',
        fast.TouchDevice('/dev/input/event2', 64, 15), POINTS,
        popen=lambda *args, **kwargs: process)
    with pytest.raises(AdbError):
        with channel:
            channel.tap()
    assert process.lines == [b'STOP\n']


def test_execution_deadline_during_ack_still_closes_and_releases():
    process, now = FakeProcess(), [0.0]
    original_write = process.write
    def write(line):
        result = original_write(line)
        if line != b'STOP\n':
            now[0] = 1.0
        return result
    process.write = write
    channel = fast.DuelTapChannel('adb.exe', 'device',
        fast.TouchDevice('/dev/input/event2', 64, 15), POINTS,
        popen=lambda *args, **kwargs: process)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(1, clock=lambda: now[0]):
            with channel:
                channel.tap()
    assert process.lines[-1] == b'STOP\n'
    assert len(process.lines) == 2


def test_unused_channel_never_opens_or_sends_cleanup_touches():
    channel = fast.DuelTapChannel('adb.exe', 'device',
        fast.TouchDevice('/dev/input/event2', 64, 15), POINTS,
        popen=lambda *args, **kwargs: pytest.fail('entry result must not open touch device'))
    with channel:
        pass


@pytest.mark.parametrize('reply', [b'ERROR\n', b''])
def test_failed_batch_is_not_retried_and_the_channel_is_closed(reply):
    process = FakeProcess(reply=reply)
    channel = fast.DuelTapChannel('adb.exe', 'emulator-5554',
        fast.TouchDevice('/dev/input/event2', 64, 15), POINTS,
        popen=lambda *a, **kw: process)
    with pytest.raises(AdbError):
        with channel:
            channel.tap()
    assert len([line for line in process.lines if line != b'STOP\n']) == 1
    assert process.lines[-1] == b'STOP\n'


def test_missing_batch_ack_stops_without_queuing_more_batches():
    process = FakeProcess(reply=None)
    channel = fast.DuelTapChannel('adb.exe', 'emulator-5554',
        fast.TouchDevice('/dev/input/event2', 64, 15), POINTS,
        popen=lambda *a, **kw: process, timeout=.02)
    with pytest.raises(AdbError, match='超时'):
        with channel:
            channel.tap()
    assert len([line for line in process.lines if line != b'STOP\n']) == 1


def test_unsupported_device_falls_back_before_opening_any_input_channel(monkeypatch):
    client = SimpleNamespace(serial='device', adb_path='adb.exe',
        _command=lambda *a: SimpleNamespace(stdout='unsupported'))
    monkeypatch.setattr(fast, 'DuelTapChannel', lambda *a, **kw: pytest.fail('unsupported device must not open input'))
    with fast.fast_duel_taps(client, POINTS) as tap:
        assert tap is None
