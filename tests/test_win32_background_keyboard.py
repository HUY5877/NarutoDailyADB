"""在可注入的 Win32 API 边界验证后台消息，不调用真实窗口或键盘。"""

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from naruto_daily.common import keyboard


ROOT = 100
RENDER = 120
PID = 456


def window(*, pid=PID, parent=None, class_name="RenderWindow", title="TheRender", valid=True):
    return SimpleNamespace(pid=pid, parent=parent, class_name=class_name, title=title, valid=valid)


class FakeUser32:
    """仅替代外部 Win32 调用，候选筛选、身份复查及消息构造由真实后端完成。"""

    def __init__(self):
        self.windows = {
            ROOT: window(parent=None, class_name="LDPlayerMainFrame", title="雷电模拟器"),
            110: window(parent=ROOT, class_name="Container", title=""),
            RENDER: window(parent=110),
            130: window(parent=ROOT, class_name="Edit", title="TheRender"),
            140: window(parent=ROOT, title="OtherRender"),
        }
        self.enumerated = [110, 130, RENDER, 140]
        self.messages = []
        self.post_succeeds = True
        self.scans = {74: 0x24, 72: 0x23, 76: 0x26, 75: 0x25, 32: 0x39, 82: 0x13, 69: 0x12}

        # 普通函数允许生产构造器设置 ctypes 的 argtypes 和 restype。
        def is_window(hwnd):
            return bool(hwnd in self.windows and self.windows[hwnd].valid)

        def get_pid(hwnd, output):
            pid = self.windows[hwnd].pid if is_window(hwnd) else 0
            ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = pid
            return 1 if pid else 0

        def get_class(hwnd, output, capacity):
            value = self.windows[hwnd].class_name if is_window(hwnd) else ""
            output.value = value[: capacity - 1]
            return len(output.value)

        def get_title(hwnd, output, capacity):
            value = self.windows[hwnd].title if is_window(hwnd) else ""
            output.value = value[: capacity - 1]
            return len(output.value)

        def is_child(parent, hwnd):
            seen = set()
            while is_window(hwnd) and hwnd not in seen:
                seen.add(hwnd)
                hwnd = self.windows[hwnd].parent
                if hwnd == parent:
                    return True
            return False

        def enum_children(parent, callback, data):
            assert parent == ROOT
            for hwnd in self.enumerated:
                if not callback(hwnd, data):
                    break
            return True

        def map_key(code, mode):
            assert mode == 0
            return self.scans[code]

        def post_message(hwnd, message, code, lparam):
            self.messages.append((hwnd, message, code, lparam))
            return self.post_succeeds

        self.IsWindow = is_window
        self.GetWindowThreadProcessId = get_pid
        self.GetClassNameW = get_class
        self.GetWindowTextW = get_title
        self.IsChild = is_child
        self.EnumChildWindows = enum_children
        self.MapVirtualKeyW = map_key
        self.PostMessageW = post_message


def setup_backend(api=None):
    api = FakeUser32() if api is None else api
    backend = keyboard.Win32Keyboard(api=api)
    root = keyboard.LDPlayerWindow(0, ROOT, PID)
    return backend, api, root


def test_background_backend_works_without_foreground_or_global_input_apis(monkeypatch):
    monkeypatch.setattr(keyboard.sys, "platform", "linux")

    def forbidden_user32(*args, **kwargs):
        pytest.fail("注入 FakeUser32 时不得读取真实 user32")

    monkeypatch.setattr(keyboard.ctypes, "WinDLL", forbidden_user32, raising=False)
    backend, api, root = setup_backend()
    target = backend.prepare_input(root)
    backend.key_event(target, 74, up=False)
    backend.key_event(target, 74, up=True)

    assert api.messages == [
        (RENDER, 0x100, 74, 0x00240001),
        (RENDER, 0x101, 74, 0xC0240001),
    ]


def test_prepare_selects_the_unique_render_descendant_and_keeps_its_root_identity():
    backend, api, root = setup_backend()
    target = backend.prepare_input(root)

    assert isinstance(target, keyboard.LDPlayerRenderTarget)
    assert target.window == root
    assert target.hwnd == RENDER
    assert api.messages == []


def test_prepare_ignores_matching_class_and_title_from_another_process_or_root():
    api = FakeUser32()
    api.windows[150] = window(parent=ROOT, pid=999)
    api.windows[160] = window(parent=999)
    api.enumerated.extend([150, 160])
    backend, api, root = setup_backend(api)

    target = backend.prepare_input(root)

    assert target.hwnd == RENDER
    assert api.messages == []


@pytest.mark.parametrize("code, down_lparam, up_lparam", [
    (74, 0x00240001, 0xC0240001),
    (72, 0x00230001, 0xC0230001),
    (76, 0x00260001, 0xC0260001),
    (75, 0x00250001, 0xC0250001),
    (32, 0x00390001, 0xC0390001),
    (82, 0x00130001, 0xC0130001),
    (69, 0x00120001, 0xC0120001),
])
def test_each_battle_key_posts_down_and_up_only_to_the_resolved_render(code, down_lparam, up_lparam):
    backend, api, root = setup_backend()
    target = backend.prepare_input(root)

    backend.key_event(target, code, up=False)
    backend.key_event(target, code, up=True)

    assert api.messages == [
        (RENDER, 0x100, code, down_lparam),
        (RENDER, 0x101, code, up_lparam),
    ]


@pytest.mark.parametrize("invalid", [
    "root_missing", "root_pid", "render_missing", "render_pid", "render_class", "render_title", "not_descendant",
])
def test_prepare_refuses_an_unavailable_or_unrelated_render_window(invalid):
    api = FakeUser32()
    if invalid == "root_missing":
        api.windows[ROOT].valid = False
    elif invalid == "root_pid":
        api.windows[ROOT].pid = 999
    elif invalid == "render_missing":
        api.enumerated.remove(RENDER)
    elif invalid == "render_pid":
        api.windows[RENDER].pid = 999
    elif invalid == "render_class":
        api.windows[RENDER].class_name = "OtherWindow"
    elif invalid == "render_title":
        api.windows[RENDER].title = "OtherRender"
    elif invalid == "not_descendant":
        # 模拟枚举到的句柄在身份核对前被重新挂到其它根窗口。
        api.windows[RENDER].parent = 999
    backend, api, root = setup_backend(api)

    with pytest.raises(keyboard.KeyboardInputError):
        backend.prepare_input(root)

    assert api.messages == []


def test_prepare_refuses_multiple_matching_render_windows():
    api = FakeUser32()
    api.windows[150] = window(parent=ROOT)
    api.enumerated.append(150)
    backend, api, root = setup_backend(api)

    with pytest.raises(keyboard.KeyboardInputError):
        backend.prepare_input(root)

    assert api.messages == []


@pytest.mark.parametrize("up", [False, True])
@pytest.mark.parametrize("changed", ["root_missing", "root_pid", "render_missing", "render_pid", "parent", "class", "title"])
def test_every_event_revalidates_the_prepared_target_before_posting(changed, up):
    backend, api, root = setup_backend()
    target = backend.prepare_input(root)
    if changed == "root_missing":
        api.windows[ROOT].valid = False
    elif changed == "root_pid":
        api.windows[ROOT].pid = 999
    elif changed == "render_missing":
        api.windows[RENDER].valid = False
    elif changed == "render_pid":
        api.windows[RENDER].pid = 999
    elif changed == "parent":
        api.windows[RENDER].parent = 999
    elif changed == "class":
        api.windows[RENDER].class_name = "OtherWindow"
    elif changed == "title":
        api.windows[RENDER].title = "OtherRender"

    with pytest.raises(keyboard.KeyboardInputError):
        backend.key_event(target, 74, up=up)

    assert api.messages == []


def test_key_up_does_not_post_to_a_handle_reused_by_another_process():
    backend, api, root = setup_backend()
    target = backend.prepare_input(root)
    backend.key_event(target, 74, up=False)
    api.windows[RENDER].pid = 999

    with pytest.raises(keyboard.KeyboardInputError):
        backend.key_event(target, 74, up=True)

    assert api.messages == [(RENDER, 0x100, 74, 0x00240001)]


@pytest.mark.parametrize("up", [False, True])
def test_post_message_failure_is_reported_as_a_keyboard_input_error(up):
    backend, api, root = setup_backend()
    target = backend.prepare_input(root)
    api.post_succeeds = False

    with pytest.raises(keyboard.KeyboardInputError):
        backend.key_event(target, 74, up=up)
