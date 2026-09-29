from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.config import load_settings
from naruto_daily.secret_realm.pre_battle import BattlePreparation


ROOT = Path(__file__).parents[1]


def action():
    return next(a for a in load_settings(ROOT / 'config.example.json').tasks['secret_realm']
                if a['type'] == 'handle_secret_realm')


class BattleClient:
    """用参考图片验证匹配；页面切换是模拟的，不等同于实机战斗回放。"""
    def __init__(self, realm='yinyang', stuck=False):
        self.state = f'secret_{realm}_battle'
        self.stuck = stuck
        self.taps = []

    def screenshot(self):
        return (ROOT / 'tests/fixtures' / (self.state + '.png')).read_bytes()

    def tap(self, x, y):
        self.taps.append((x, y))
        if self.stuck:
            return
        if self.state == 'secret_yinyang_battle':
            assert 1800 < x < 1920 and 0 < y < 130
            self.state = 'secret_pause_menu'
        elif self.state == 'secret_pause_menu':
            assert 630 < x < 930 and 620 < y < 740
            self.state = 'secret_exit_dialog'
        elif self.state == 'secret_exit_dialog':
            assert 630 < x < 930 and 620 < y < 740
            self.state = 'secret_result'
        else:
            raise AssertionError('退出确认后不应继续点击')


def test_yinyang_uses_model_then_pause_exit_confirm_on_reference_frames():
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    client = BattleClient()
    model = SimpleNamespace(complete_json=lambda *a, **kw: {'page': 'battle', 'realm_name': '阴阳秘境'})
    assert prepare_secret_battle(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None),
                              model, action()) is BattlePreparation.EXITED
    assert len(client.taps) == 3 and client.state == 'secret_result'


def test_other_realm_proceeds_to_battle_without_pause_or_exit():
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    client = BattleClient('flame')
    model = SimpleNamespace(complete_json=lambda *a, **kw: {'page': 'battle', 'realm_name': '烈炎秘境'})
    assert prepare_secret_battle(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None),
                                 model, action()) is BattlePreparation.READY
    assert client.taps == []


@pytest.mark.parametrize('reply', [
    {}, {'page': 'unknown', 'realm_name': '阴阳秘境'},
    {'page': 'battle', 'realm_name': None}, {'page': 'battle', 'realm_name': True},
    {'page': 'battle', 'realm_name': '阴阳四象偶忍'},
])
def test_unknown_realm_retries_then_stops_without_clicking(reply):
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    client = BattleClient()
    model = SimpleNamespace(complete_json=lambda *a, **kw: reply)
    with pytest.raises(VisualError, match='调用上限|超时'):
        prepare_secret_battle(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None), model, action())
    assert client.taps == []


def test_missing_pause_menu_stops_after_one_pause_click():
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    client = BattleClient(stuck=True)
    config = action()
    config['timeout_seconds'] = 12
    model = SimpleNamespace(complete_json=lambda *a, **kw: {'page': 'battle', 'realm_name': '阴阳秘境'})
    with pytest.raises(VisualError, match='超时'):
        prepare_secret_battle(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None), model, config)
    assert len(client.taps) == 1


def test_late_model_result_does_not_trigger_exit():
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    client = BattleClient()
    ticks = iter([0, 100])
    model = SimpleNamespace(complete_json=lambda *a, **kw: {'page': 'battle', 'realm_name': '阴阳秘境'})
    with pytest.raises(VisualError, match='超时'):
        prepare_secret_battle(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None), model, action(),
                            clock=lambda: next(ticks))
    assert client.taps == []


def test_confirm_button_requires_exit_question_on_same_screenshot():
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    from naruto_daily.common.visual import find_template_in_png
    client = BattleClient()
    config = action()
    config['timeout_seconds'] = 12
    frames = {}
    original_screenshot = client.screenshot
    def screenshot():
        png = original_screenshot()
        if client.state == 'secret_exit_dialog':
            token = str(len(frames)).encode()
            frames[token] = png
            return token
        return png
    client.screenshot = screenshot
    def matcher(png, path, roi, threshold):
        if png in frames:
            # 奇数帧只有提示，偶数帧只有确定；不能跨帧拼接点击。
            missing = 'secret_exit_question' if int(png) % 2 == 0 else 'secret_exit_confirm'
            if path.stem == missing:
                return None, .1
            png = frames[png]
        return find_template_in_png(png, path, roi, threshold)
    model = SimpleNamespace(complete_json=lambda *a, **kw: {'page': 'battle', 'realm_name': '阴阳秘境'})
    with pytest.raises(VisualError, match='确定.*超时'):
        prepare_secret_battle(ImageRecognition(client, matcher, asset_root=ROOT, sleep=lambda _: None), model, config)
    assert len(client.taps) == 2 and client.state == 'secret_exit_dialog'


def test_obscured_name_retries_new_frame_before_quitting():
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    client = BattleClient()
    replies = iter([{'page': 'unknown', 'realm_name': None},
                    {'page': 'battle', 'realm_name': '阴阳秘境'}])
    waits = []
    model = SimpleNamespace(complete_json=lambda *a, **kw: next(replies))
    assert prepare_secret_battle(ImageRecognition(client, asset_root=ROOT, sleep=waits.append),
                                 model, action()) is BattlePreparation.EXITED
    assert waits[0] == 1 and len(client.taps) == 3


def test_already_settled_does_not_read_name_or_click_pause():
    from naruto_daily.secret_realm.pre_battle import prepare_secret_battle
    client = BattleClient()
    client.state = 'secret_result'
    def fail(*a, **kw):
        raise AssertionError('结算页无需再调用模型')
    assert prepare_secret_battle(ImageRecognition(client, asset_root=ROOT, sleep=lambda _: None),
                                 SimpleNamespace(complete_json=fail), action()) is BattlePreparation.READY
    assert client.taps == []
