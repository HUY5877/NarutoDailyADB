from types import SimpleNamespace

import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope


class Backend:
    def __init__(self):
        self.events = []
        self.focus_error = None

    def prepare_input(self, window, code):
        if self.focus_error:
            raise self.focus_error
        self.events.append(('focus', window.hwnd))

    def key_event(self, code, *, up):
        self.events.append(('up' if up else 'down', code))


def keyboard(backend, sleep=lambda _: None):
    from naruto_daily.common.keyboard import LDPlayerKeyboard, LDPlayerWindow
    return LDPlayerKeyboard(LDPlayerWindow(0, 123, 456), backend=backend, sleep=sleep)


@pytest.mark.parametrize('key, code', [('j', 74), ('K', 75), ('SPACE', 32)])
def test_press_dispatches_a_complete_native_key_pair(key, code):
    backend = Backend()
    waits = []
    keyboard(backend, waits.append).press(key)
    assert backend.events == [('focus', 123), ('down', code), ('up', code)]
    assert waits == [.08]


@pytest.mark.parametrize('error', [RuntimeError('中断'), KeyboardInterrupt()])
def test_interrupted_hold_still_releases_the_key(error):
    backend = Backend()

    def interrupted(_):
        raise error

    with pytest.raises(type(error)):
        keyboard(backend, interrupted).press('J')
    assert backend.events[-2:] == [('down', 74), ('up', 74)]


def test_expired_execution_releases_key_and_does_not_dispatch_another():
    backend = Backend()
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    device = keyboard(backend, advance)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(.04, clock=lambda: now[0]):
            device.press('J')
    assert backend.events[-2:] == [('down', 74), ('up', 74)]


def test_focus_failure_does_not_send_keyboard_input():
    from naruto_daily.common.keyboard import KeyboardInputError
    backend = Backend()
    backend.focus_error = KeyboardInputError('请把雷电放到前台')
    with pytest.raises(KeyboardInputError, match='前台'):
        keyboard(backend).press('K')
    assert backend.events == []


def test_interrupt_during_key_down_also_attempts_release():
    class InterruptedBackend(Backend):
        def key_event(self, code, *, up):
            super().key_event(code, up=up)
            if not up:
                raise KeyboardInterrupt()

    backend = InterruptedBackend()
    with pytest.raises(KeyboardInterrupt):
        keyboard(backend).press('J')
    assert backend.events[-2:] == [('down', 74), ('up', 74)]


@pytest.mark.parametrize('serial, index', [('emulator-5554', 0), ('emulator-5556', 1)])
@pytest.mark.parametrize('title', ['雷电', '"雷电,模拟器"'])
def test_window_is_resolved_against_the_adb_serial(tmp_path, serial, index, title):
    from naruto_daily.common.keyboard import LDPlayerKeyboard
    (tmp_path / 'ldconsole.exe').touch()
    client = SimpleNamespace(adb_path=tmp_path / 'adb.exe', serial=serial)

    def run(command):
        if command[-1] == 'list2':
            return SimpleNamespace(returncode=0, stdout=f'0,{title},123,124,1,456,5\n1,{title},223,224,1,556,6', stderr='')
        selected = command[command.index('--index') + 1]
        return SimpleNamespace(returncode=0, stdout=f'emulator-{5554 + 2 * int(selected)}\n', stderr='')

    device = LDPlayerKeyboard.from_client(client, {}, backend=Backend(), run_command=run)
    assert device.window.index == index
    assert device.window.hwnd == (123 if index == 0 else 223)


def test_explicit_instance_cannot_target_a_different_adb_device(tmp_path):
    from naruto_daily.common.keyboard import LDPlayerKeyboard, KeyboardInputError
    (tmp_path / 'ldconsole.exe').touch()

    def run(command):
        output = '0,雷电,123,124,1,456,5' if command[-1] == 'list2' else 'emulator-5554'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    client = SimpleNamespace(adb_path=tmp_path / 'adb.exe', serial='emulator-5556')
    with pytest.raises(KeyboardInputError, match='匹配'):
        LDPlayerKeyboard.from_client(client, {'instance_index': 0}, backend=Backend(), run_command=run)


def test_ambiguous_serial_never_selects_the_first_window(tmp_path):
    from naruto_daily.common.keyboard import LDPlayerKeyboard, KeyboardInputError
    (tmp_path / 'ldconsole.exe').touch()

    def run(command):
        output = '0,雷电,123,124,1,456,5\n1,雷电,223,224,1,556,6' if command[-1] == 'list2' else 'emulator-5554'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    with pytest.raises(KeyboardInputError, match='唯一'):
        LDPlayerKeyboard.from_client(SimpleNamespace(adb_path=tmp_path / 'adb.exe', serial='emulator-5554'), {},
                                     backend=Backend(), run_command=run)
