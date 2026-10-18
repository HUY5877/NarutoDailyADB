import pytest

from naruto_daily.battle import run_battle, run_battle_without_first_skill
from test_battle import Clock, Client, battle_config


@pytest.mark.parametrize('runner, expected', [
    (run_battle, ['J', 'H', 'L', 'K']),
    (run_battle_without_first_skill, ['H', 'L', 'K']),
])
def test_keyboard_mode_sends_configured_keys_and_stops_before_observing(monkeypatch, runner, expected):
    import naruto_daily.battle.handler as handler
    clock = Clock()
    client = Client(clock)
    keys, held = [], []

    class Keyboard:
        @classmethod
        def from_client(cls, client, options):
            return cls()

        def press(self, key):
            held.append(key)
            keys.append(key)
            clock.advance(30 / len(expected))
            held.remove(key)

    monkeypatch.setattr(handler, 'LDPlayerKeyboard', Keyboard, raising=False)
    original_screenshot = client.screenshot

    def screenshot():
        assert not held, '结算截图之前必须先松开全部按键'
        return original_screenshot()

    client.screenshot = screenshot
    result = runner(client, dict(battle_config(), input_mode='keyboard'), clock=clock,
                    sleep=clock.advance,
                    template_matcher=lambda png, *a: ((50, 50), .99) if png == b'2' else (None, .2))
    assert keys == expected
    assert not client.taps
    assert client.frames == [0, 30]
    assert result.taps == len(expected)


def test_result_on_entry_does_not_even_resolve_the_keyboard_window(monkeypatch):
    import naruto_daily.battle.handler as handler

    class Keyboard:
        @classmethod
        def from_client(cls, *args):
            pytest.fail('结果页不得激活窗口或发送按键')

    monkeypatch.setattr(handler, 'LDPlayerKeyboard', Keyboard, raising=False)
    clock = Clock()
    result = run_battle(Client(clock), dict(battle_config(), input_mode='keyboard'),
                        clock=clock, template_matcher=lambda *a: ((50, 50), .99))
    assert result.taps == 0


def test_failed_keyboard_press_stops_the_batch_without_other_inputs(monkeypatch):
    import naruto_daily.battle.handler as handler
    from naruto_daily.common.keyboard import KeyboardInputError
    clock = Clock()
    keys = []

    class Keyboard:
        @classmethod
        def from_client(cls, client, options):
            return cls()

        def press(self, key):
            keys.append(key)
            raise KeyboardInputError('发送失败')

    monkeypatch.setattr(handler, 'LDPlayerKeyboard', Keyboard)
    with pytest.raises(KeyboardInputError, match='发送失败'):
        run_battle(Client(clock), dict(battle_config(), input_mode='keyboard'),
                   clock=clock, template_matcher=lambda *a: (None, .2))
    assert keys == ['J']


@pytest.mark.parametrize('runner, expected', [
    (run_battle, ['J', 'H', 'L', 'K', 'SPACE', 'R', 'E']),
    (run_battle_without_first_skill, ['H', 'L', 'K', 'SPACE', 'R', 'E']),
])
def test_seven_default_keys_finish_before_result_observation(monkeypatch, runner, expected):
    import naruto_daily.battle.handler as handler
    clock = Clock()
    client = Client(clock)
    keys, held = [], []

    class Keyboard:
        @classmethod
        def from_client(cls, client, options):
            return cls()

        def press(self, key):
            held.append(key)
            keys.append(key)
            clock.advance(5)
            held.remove(key)

    monkeypatch.setattr(handler, 'LDPlayerKeyboard', Keyboard)
    original_screenshot = client.screenshot

    def screenshot():
        assert not held, '七个按键全部释放后才能观察结算'
        return original_screenshot()

    client.screenshot = screenshot
    battle = dict(battle_config(), input_mode='keyboard')
    battle['buttons'].extend([
        dict(name='替身', x=1301, y=966),
        dict(name='通灵', x=1729, y=263),
        dict(name='秘卷', x=1728, y=443),
    ])
    result = runner(client, battle, clock=clock, sleep=clock.advance,
                    template_matcher=lambda png, *a: ((50, 50), .99) if png == b'2' else (None, .2))
    assert keys == expected
    assert not client.taps
    assert not held
    assert client.frames == [0, 5 * len(expected)]
    assert result.taps == len(expected)
