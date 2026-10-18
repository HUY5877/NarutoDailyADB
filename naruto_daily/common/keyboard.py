"""将战斗按键发送给与 ADB 设备对应的雷电前台窗口。"""
from __future__ import annotations

import ctypes
import csv
from ctypes import wintypes
from dataclasses import dataclass
import logging
from pathlib import Path
import sys
from threading import Lock
import time

from .adb_client import _run_subprocess
from .execution import check_execution, execution_sleep
from .visual import NonRetryableVisualError


DEFAULT_BATTLE_KEYS = {
    '一技能': 'J', '二技能': 'H', '大招': 'L', '普攻': 'K',
    '替身': 'SPACE', '通灵': 'R', '秘卷': 'E',
}
LOGGER = logging.getLogger('naruto_daily.keyboard')
_INPUT_LOCK = Lock()


class KeyboardInputError(NonRetryableVisualError):
    """目标窗口或键盘输入不可用；不能改发坐标点击。"""


def normalize_key(key):
    if not isinstance(key, str) or not key.isascii():
        raise ValueError('按键必须是单个英文字母、数字或 SPACE')
    value = key.upper()
    if value != 'SPACE' and not (len(value) == 1 and value.isascii() and value.isalnum()):
        raise ValueError('按键必须是单个英文字母、数字或 SPACE')
    return value


@dataclass(frozen=True)
class LDPlayerWindow:
    index: int
    hwnd: int
    pid: int


class _KeyboardInput(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [('ki', _KeyboardInput), ('mi', _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('data', _InputUnion)]


class Win32Keyboard:
    def __init__(self):
        if sys.platform != 'win32':
            raise KeyboardInputError('雷电键盘模式只支持 Windows')
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        signatures = {
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'IsIconic': ([wintypes.HWND], wintypes.BOOL),
            'ShowWindow': ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            'SetForegroundWindow': ([wintypes.HWND], wintypes.BOOL),
            'GetForegroundWindow': ([], wintypes.HWND),
            'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            'GetAsyncKeyState': ([ctypes.c_int], ctypes.c_short),
            'MapVirtualKeyW': ([wintypes.UINT, wintypes.UINT], wintypes.UINT),
            'SendInput': ([wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int], wintypes.UINT),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    def prepare_input(self, window, code):
        pid = wintypes.DWORD()
        self.api.GetWindowThreadProcessId(window.hwnd, ctypes.byref(pid))
        if not self.api.IsWindow(window.hwnd) or pid.value != window.pid:
            raise KeyboardInputError('雷电窗口已关闭或变化，已停止发送按键')
        if self.api.IsIconic(window.hwnd):
            self.api.ShowWindow(window.hwnd, 9)  # SW_RESTORE
        if self.api.GetForegroundWindow() != window.hwnd:
            self.api.SetForegroundWindow(window.hwnd)
        if self.api.GetForegroundWindow() != window.hwnd:
            raise KeyboardInputError('无法激活雷电窗口，请将目标雷电放到前台后重试')
        if any(self.api.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            raise KeyboardInputError('当前有修饰键按住，已停止发送战斗按键')
        if self.api.GetAsyncKeyState(code) & 0x8000:
            raise KeyboardInputError('目标按键已被按住，已停止发送战斗按键')

    def key_event(self, code, *, up):
        event = _Input(type=1)  # INPUT_KEYBOARD；使用原生虚拟键而非输入文本。
        event.data.ki = _KeyboardInput(wVk=code, wScan=self.api.MapVirtualKeyW(code, 0),
                                       dwFlags=2 if up else 0)
        if self.api.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
            raise KeyboardInputError(f'发送雷电键盘事件失败（Windows 错误 {ctypes.get_last_error()}）')


class LDPlayerKeyboard:
    def __init__(self, window, *, backend=None, hold_seconds=.08, sleep=time.sleep, serial=None):
        self.window = window
        self.backend = Win32Keyboard() if backend is None else backend
        self.hold_seconds = float(hold_seconds)
        self.sleep = sleep
        self.serial = serial

    @classmethod
    def from_client(cls, client, options, *, backend=None, run_command=_run_subprocess, sleep=time.sleep):
        serial = client.serial
        if not serial:
            raise KeyboardInputError('雷电键盘模式必须指定已选中的 ADB serial')
        directory = Path(client.adb_path).parent
        console = next((directory / name for name in ('ldconsole.exe', 'dnconsole.exe')
                        if (directory / name).is_file()), None)
        if console is None:
            raise KeyboardInputError('ADB 同目录下未找到 ldconsole.exe 或 dnconsole.exe')

        def run(*arguments):
            check_execution()
            try:
                result = run_command([str(console), *arguments])
            except OSError as exc:
                raise KeyboardInputError(f'读取雷电窗口失败：{exc}') from exc
            check_execution()
            if result.returncode:
                raise KeyboardInputError('读取雷电实例信息失败：' + (result.stderr.strip() or result.stdout.strip()))
            return result.stdout.strip()

        candidates = []
        requested = options.get('instance_index')
        for fields in csv.reader(run('list2').splitlines()):
            try:
                index, hwnd, running, pid = (int(fields[i]) for i in (0, 2, 4, 5))
            except (ValueError, IndexError):
                continue
            if running != 1 or hwnd <= 0 or pid <= 0 or (requested is not None and index != requested):
                continue
            actual_serial = run('adb', '--index', str(index), '--command', 'get-serialno')
            if actual_serial == serial:
                candidates.append(LDPlayerWindow(index, hwnd, pid))
        if not candidates:
            raise KeyboardInputError(f'未找到与 ADB 设备 {serial} 匹配的运行中雷电窗口')
        if len(candidates) != 1:
            raise KeyboardInputError('ADB 设备未对应唯一雷电窗口，请指定 keyboard.instance_index')
        return cls(candidates[0], backend=backend, hold_seconds=options.get('hold_seconds', .08),
                   sleep=sleep, serial=serial)

    def press(self, key):
        key = normalize_key(key)
        code = 32 if key == 'SPACE' else ord(key)
        # SendInput 使用系统键盘流，同一进程内不能交错两次按下/松开。
        with _INPUT_LOCK:
            check_execution()
            self.backend.prepare_input(self.window, code)
            check_execution()
            LOGGER.info('雷电按键：serial=%s instance=%d hwnd=%d key=%s',
                        self.serial, self.window.index, self.window.hwnd, key, extra={'backend_only': True})
            try:
                self.backend.key_event(code, up=False)
                execution_sleep(self.hold_seconds, self.sleep)
            finally:
                # 清理不能被总时限检查拦住；中断/超时也必须释放已按下的键。
                self.backend.key_event(code, up=True)
