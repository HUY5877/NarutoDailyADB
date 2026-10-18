"""将战斗按键定向发送给与 ADB 设备对应的雷电渲染窗口。"""
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


@dataclass(frozen=True)
class LDPlayerRenderTarget:
    window: LDPlayerWindow
    hwnd: int


_ENUM_WINDOW_PROC = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class Win32Keyboard:
    def __init__(self, api=None):
        if api is None:
            if sys.platform != 'win32':
                raise KeyboardInputError('雷电键盘模式只支持 Windows')
            api = ctypes.WinDLL('user32', use_last_error=True)
        self.api = api
        signatures = {
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'IsChild': ([wintypes.HWND, wintypes.HWND], wintypes.BOOL),
            'EnumChildWindows': ([wintypes.HWND, _ENUM_WINDOW_PROC, wintypes.LPARAM], wintypes.BOOL),
            'MapVirtualKeyW': ([wintypes.UINT, wintypes.UINT], wintypes.UINT),
            'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    def _has_identity(self, hwnd, expected_pid):
        if not self.api.IsWindow(hwnd):
            return False
        pid = wintypes.DWORD()
        self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == expected_pid

    def _is_render(self, window, hwnd):
        if not self._has_identity(hwnd, window.pid) or not self.api.IsChild(window.hwnd, hwnd):
            return False
        class_name = ctypes.create_unicode_buffer(256)
        title = ctypes.create_unicode_buffer(256)
        self.api.GetClassNameW(hwnd, class_name, len(class_name))
        self.api.GetWindowTextW(hwnd, title, len(title))
        return class_name.value == 'RenderWindow' and title.value == 'TheRender'

    def _validate_root(self, window):
        if not self._has_identity(window.hwnd, window.pid):
            raise KeyboardInputError('雷电窗口已关闭或变化，已停止发送按键')

    def prepare_input(self, window):
        self._validate_root(window)
        matches = []

        @_ENUM_WINDOW_PROC
        def collect(hwnd, _):
            if self._is_render(window, hwnd):
                matches.append(hwnd)
            return True

        self.api.EnumChildWindows(window.hwnd, collect, 0)
        self._validate_root(window)
        if len(matches) != 1:
            raise KeyboardInputError('未找到唯一的雷电渲染窗口，已停止发送按键')
        return LDPlayerRenderTarget(window, matches[0])

    def key_event(self, target, code, *, up):
        self._validate_root(target.window)
        if not self._is_render(target.window, target.hwnd):
            raise KeyboardInputError('雷电渲染窗口已关闭或变化，已停止发送按键')
        # 普通按键的重复次数为 1；松键同时设置 previous-state 和 transition 位。
        lparam = 1 | (self.api.MapVirtualKeyW(code, 0) << 16)
        if up:
            lparam |= (1 << 30) | (1 << 31)
        if not self.api.PostMessageW(target.hwnd, 0x101 if up else 0x100, code, lparam):
            error = getattr(ctypes, 'get_last_error', lambda: 0)()
            raise KeyboardInputError(f'发送雷电后台按键失败（Windows 错误 {error}）')


class LDPlayerKeyboard:
    def __init__(self, window, *, backend=None, hold_seconds=.08, sleep=time.sleep, serial=None):
        self.window = window
        self.backend = Win32Keyboard() if backend is None else backend
        self.hold_seconds = float(hold_seconds)
        self.sleep = sleep
        self.serial = serial
        self._lock = Lock()

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
        # 一个适配器内保持完整按下/松开；不同实例使用独立锁及窗口消息队列。
        with self._lock:
            check_execution()
            target = self.backend.prepare_input(self.window)
            check_execution()
            LOGGER.info('雷电后台按键：serial=%s instance=%d hwnd=%d render=%d key=%s',
                        self.serial, self.window.index, self.window.hwnd, target.hwnd, key,
                        extra={'backend_only': True})
            try:
                self.backend.key_event(target, code, up=False)
                execution_sleep(self.hold_seconds, self.sleep)
            finally:
                # 清理不能被总时限检查拦住；中断/超时也必须释放已按下的键。
                self.backend.key_event(target, code, up=True)
