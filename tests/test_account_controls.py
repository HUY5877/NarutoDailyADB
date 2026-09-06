import pytest

from naruto_daily.login import account_controls
from naruto_daily.common.adb_client import AdbClient, CommandResult
from naruto_daily.common.visual import VisualError


def test_adb_reads_native_ui_hierarchy():
    calls = []
    def run(args):
        calls.append(args)
        return CommandResult(0, '<hierarchy />' if args[-2] == 'cat' else 'dumped', '')
    client = AdbClient('adb', 'emulator', run_command=run)
    assert client.ui_hierarchy() == '<hierarchy />'
    assert calls[0][-3:] == ['rm', '-f', '/sdcard/naruto_daily_ui.xml']
    assert calls[1][-3:] == ['uiautomator', 'dump', '/sdcard/naruto_daily_ui.xml']


def xml(name, bounds='[147,962][321,1003]'):
    return f'<hierarchy><node package="com.tencent.mobileqq" text="{name}" enabled="true" bounds="{bounds}" /></hierarchy>'


def test_account_control_uses_exact_name_and_native_bounds():
    assert account_controls.find_account_control(xml('离落花开花败'), '离落花开花败', 822) == (234, 982)
    assert account_controls.find_account_control(xml('离落花开败'), '离落花开花败', 822) is None


@pytest.mark.parametrize('bounds', ['[1836,960][1864,1000]', '[100,500][300,550]', '[0,0][9999,9999]'])
def test_controls_outside_account_list_are_rejected(bounds):
    assert account_controls.find_account_control(xml('离落花开花败', bounds), '离落花开花败', 822) is None


def test_malformed_ui_hierarchy_is_not_treated_as_success():
    with pytest.raises(VisualError):
        account_controls.find_account_control('not XML', '离落花开花败', 822)
