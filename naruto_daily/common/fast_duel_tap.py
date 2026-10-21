"""Acknowledged battle multitouch over one persistent ADB shell.

Type B slots follow the Linux input multi-touch protocol. Each request presses
all buttons together and releases them before its acknowledgement is returned.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import logging
from queue import Empty, Queue
import re
import shlex
import struct
import subprocess
from threading import Thread
import time

from .adb_client import AdbError
from .execution import check_execution, current_execution


LOGGER = logging.getLogger('naruto_daily.adb')
TAP_PERIOD_SECONDS = .05


@dataclass(frozen=True)
class TouchDevice:
    path: str
    bits: int
    max_slot: int


def find_touch_device(capabilities: str, bits: int, count: int) -> TouchDevice | None:
    if bits not in (32, 64) or not 1 <= count <= 16:
        return None
    for block in re.split(r'(?=add device \d+:)', capabilities):
        path = re.search(r'add device \d+: (/dev/input/event\d+)\s', block)
        if not path or not re.search(r'name:\s*"input"', block):
            continue
        if not all(name in block for name in ('BTN_TOUCH', 'BTN_TOOL_FINGER')):
            continue
        axes = {}
        for name in ('SLOT', 'POSITION_X', 'POSITION_Y', 'TRACKING_ID', 'PRESSURE'):
            axis = re.search(r'ABS_MT_' + name + r'\s*:[^\n]*?min (\d+), max (\d+)', block)
            if axis:
                axes[name] = tuple(map(int, axis.groups()))
        if (axes.get('POSITION_X') != (0, 1919) or axes.get('POSITION_Y') != (0, 1079)
                or axes.get('SLOT', (-1, -1))[0] != 0
                or axes['SLOT'][1] < count - 1
                or axes.get('TRACKING_ID', (-1, -1))[0] != 0
                or axes['TRACKING_ID'][1] < 100 + axes['SLOT'][1]
                or axes.get('PRESSURE', (-1, -1))[0] != 0 or axes['PRESSURE'][1] < 1):
            continue
        return TouchDevice(path[1], bits, axes['SLOT'][1])
    return None


def touch_frames(device: TouchDevice, points) -> tuple[bytes, bytes]:
    if device.bits not in (32, 64) or not 1 <= len(points) <= device.max_slot + 1:
        raise ValueError('高速战斗触摸设备或触点数量无效')
    if any(type(x) is not int or type(y) is not int or not (0 <= x < 1920 and 0 <= y < 1080)
           for x, y in points):
        raise ValueError('高速战斗触摸坐标超出 1920×1080')
    packer = struct.Struct('<qqHHi' if device.bits == 64 else '<iiHHi')
    def pack(events):
        return b''.join(packer.pack(0, 0, *event) for event in events)
    down, up = [(1, 330, 1), (1, 325, 1)], [(1, 330, 0), (1, 325, 0)]
    # Use the high slots so ordinary emulator mouse input can retain slot zero.
    first = device.max_slot - len(points) + 1
    for slot, (x, y) in enumerate(points, first):
        down.extend(((3, 47, slot), (3, 57, 100 + slot),
                     (3, 53, x), (3, 54, y), (3, 58, 1)))
        up.extend(((3, 47, slot), (3, 58, 0), (3, 57, -1)))
    down.append((0, 0, 0))
    up.append((0, 0, 0))
    return pack(down), pack(up)


def channel_script(path: str, release: bytes) -> str:
    release64 = base64.b64encode(release).decode('ascii')
    # base64 itself writes the evdev descriptor, so its ELF class determines
    # input_event size even when the Android shell has a different ABI.
    return f'''exec 3>{shlex.quote(path)} || exit 1
release() {{ printf %s {shlex.quote(release64)} | base64 -d >&3; }}
trap 'release' EXIT
trap 'exit 1' HUP INT TERM
printf 'READY\\n'
while read -r down up; do
    [ "$down" = STOP ] && break
    printf %s "$down" | base64 -d >&3 || exit 1
    sleep 0.01 || exit 1
    printf %s "$up" | base64 -d >&3 || exit 1
    printf 'OK\\n'
done
'''


class DuelTapChannel:
    def __init__(self, adb_path, serial, device: TouchDevice, points, *,
                 popen=subprocess.Popen, clock=time.monotonic, timeout=5):
        self.device, self.points = device, tuple(points)
        self.down, self.up = touch_frames(device, self.points)
        self.command = [str(adb_path)]
        if serial:
            self.command.extend(('-s', serial))
        self.command.extend(('shell', '-T', channel_script(device.path, self.up)))
        self.serial = serial
        self._popen, self._clock, self._timeout = popen, clock, timeout
        self._process = None
        self._reader = None
        self._responses = Queue()
        self._closed = False

    def __enter__(self):
        return self

    def _read_responses(self):
        try:
            while True:
                line = self._process.stdout.readline()
                self._responses.put(line)
                if not line:
                    break
        except (OSError, ValueError):
            self._responses.put(b'')

    def _response(self, expected):
        budget = current_execution()
        timeout = min(self._timeout, budget.remaining()) if budget is not None else self._timeout
        try:
            response = self._responses.get(timeout=timeout)
        except Empty:
            check_execution()
            raise AdbError('高速战斗点击确认超时，已停止发送') from None
        check_execution()
        if response.strip() != expected:
            raise AdbError('高速战斗点击通道异常，已停止发送')

    def _open(self):
        self._process = self._popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self._reader = Thread(target=self._read_responses, name='duel-tap-ack', daemon=True)
        self._reader.start()
        self._response(b'READY')

    def tap(self, can_send=lambda: True):
        if self._closed:
            raise AdbError('高速战斗点击通道已关闭')
        check_execution()
        if self._process is None:
            # Entry checks and before_attack navigation run before any evdev open.
            self._open()
        if not can_send():
            return False
        started = self._clock()
        packet = base64.b64encode(self.down) + b' ' + base64.b64encode(self.up) + b'\n'
        LOGGER.info('[ADB高速战斗点击开始] time=%s serial=%s points=%s',
                    datetime.now().astimezone().isoformat(timespec='microseconds'), self.serial,
                    self.points, extra={'backend_only': True, 'file_only': True})
        try:
            self._process.stdin.write(packet)
            self._process.stdin.flush()
        except OSError:
            raise AdbError('高速战斗点击发送失败，已停止发送') from None
        self._response(b'OK')
        LOGGER.info('[ADB高速战斗点击完成] serial=%s points=%s elapsed_ms=%.2f',
                    self.serial, self.points, (self._clock() - started) * 1000,
                    extra={'backend_only': True, 'file_only': True})
        return True

    def __exit__(self, error_type, *_):
        try:
            self.close()
        except AdbError:
            if error_type is None:
                raise
            LOGGER.warning('高速战斗点击通道清理未确认，已停止发送', extra={'backend_only': True})

    def close(self):
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    process.stdin.write(b'STOP\n')
                    process.stdin.flush()
                    if process.wait(timeout=2) != 0:
                        raise AdbError('高速战斗点击通道清理未确认')
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=2)
                    raise AdbError('高速战斗点击通道清理超时，已关闭连接') from None
        finally:
            process.stdin.close()
            process.stdout.close()
            if self._reader is not None:
                self._reader.join(timeout=1)


@contextmanager
def fast_duel_taps(client, points):
    channel = None
    try:
        capabilities = client._command('shell', 'getevent', '-lp').stdout
        # ELFCLASS32/64 and little endian; do not infer ABI from the host OS.
        elf = client._command('shell', 'od', '-An', '-t', 'u1', '-j', '4', '-N', '2',
                              '/system/bin/base64').stdout.split()
        bits = {'1': 32, '2': 64}.get(elf[0]) if len(elf) == 2 and elf[1] == '1' else None
        device = find_touch_device(capabilities, bits, len(points))
        if device is not None:
            script = f'test -w {shlex.quote(device.path)}'
            client._command('shell', script)
            channel = DuelTapChannel(client.adb_path, client.serial, device, points)
    except AdbError:
        pass
    if channel is None:
        LOGGER.info('当前设备不支持高速战斗多点触摸，沿用 ADB 坐标连点',
                    extra={'backend_only': True})
        yield None
        return
    LOGGER.info('战斗启用持续 ADB 多点触摸：每个按钮目标每秒 20 次，整轮抬起后确认',
                extra={'backend_only': True})
    with channel:
        yield channel.tap
