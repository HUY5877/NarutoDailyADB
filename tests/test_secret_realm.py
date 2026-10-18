import pytest

from naruto_daily.multimodal.client import MultimodalError
from naruto_daily.secret_realm.pre_battle import BattlePreparation


@pytest.mark.parametrize('count', [0, 1, 34])
def test_ticket_count_uses_total_not_expiring_count(count):
    from naruto_daily.secret_realm.recognition import parse_tickets
    assert parse_tickets({'page': 'room', 'tickets': count, 'expiring_today': 4}) == count


@pytest.mark.parametrize('result', [
    {'page': 'room', 'tickets': None}, {'page': 'loading', 'tickets': 0},
    {'page': 'result', 'tickets': 34},
])
def test_unknown_or_wrong_page_is_not_zero_tickets(result):
    from naruto_daily.secret_realm.recognition import parse_tickets
    assert parse_tickets(result) is None


@pytest.mark.parametrize('count', [True, '34', -1, 3.4])
def test_malformed_ticket_count_is_rejected(count):
    from naruto_daily.secret_realm.recognition import parse_tickets
    with pytest.raises(MultimodalError):
        parse_tickets({'page': 'room', 'tickets': count})


class SecretClient:
    def __init__(self):
        self.created = False
        self.taps = []

    def screenshot(self):
        return b"room" if self.created else b"lobby"

    def tap(self, x, y):
        self.taps.append((x, y))
        self.created = True


def secret_action():
    from pathlib import Path
    from battle_replay import load_legacy_adb_settings
    root = Path(__file__).parents[1]
    return next(a for a in load_legacy_adb_settings(root / "config.example.json").tasks["secret_realm"]
                if a["type"] == "handle_secret_realm")


def secret_matcher(png, path, roi, threshold):
    if path.stem == "secret_deploy" and png == b"room":
        return (1787, 920), 1
    if path.stem == "secret_create_room" and png == b"lobby":
        return (1443, 791), 1
    return None, .1


def fake_model(monkeypatch, counts):
    from types import SimpleNamespace
    from naruto_daily.secret_realm import handler
    counts = iter(counts)
    calls = []
    def complete(png, prompt, **kwargs):
        assert png == b"room"
        calls.append(png)
        return {"page": "room", "tickets": next(counts)}
    monkeypatch.setattr(handler, "MultimodalClient", lambda *a, **kw: SimpleNamespace(complete_json=complete))
    # Ticket-only scenarios reuse a room frame; their fake battle supplies the
    # READY boundary rather than treating that placeholder as a loaded arena.
    monkeypatch.setattr(handler, 'prepare_secret_battle', lambda *a, **kw: BattlePreparation.READY)
    return calls


def fake_battle_after_prepare(args, kwargs, attack):
    """A fake battle must run the same pre-input gate as the real battle module."""
    prepared = kwargs['before_attack'](args[0].screenshot())
    if prepared is not None:
        return prepared
    return attack()


def test_secret_realm_runs_two_battles_then_zero_completes_without_returning_to_main(monkeypatch, caplog):
    import logging
    from naruto_daily import runner
    from naruto_daily.secret_realm import handler
    client, sleeps, battles, returned = SecretClient(), [], [], []
    calls = fake_model(monkeypatch, [2, 2, 1, 1, 0])
    monkeypatch.setattr(handler, "run_battle", lambda *a, **kw:
                        fake_battle_after_prepare(a, kw, lambda: battles.append((a[1], kw))))
    monkeypatch.setattr(runner, "return_to_main", lambda *a, **kw: returned.append(True))
    caplog.set_level(logging.INFO, logger='naruto_daily')
    summary = runner.ActionRunner(client, template_matcher=secret_matcher, sleep=sleeps.append).run([
        secret_action(), {"type": "return_to_main"}, {"type": "tap", "x": 1, "y": 1}])
    assert client.taps == [(1443, 791), (1787, 920), (1787, 920)]
    assert len(battles) == 2 and returned == [] and len(calls) == 5
    assert summary.action_count == 1
    final = caplog.records[-1]
    assert final.getMessage() == '执行结束：1 个动作'
    assert final.action_index == final.action_total == 1
    assert all(kwargs['check_interval_seconds'] == 18 for _, kwargs in battles)
    # 两次9秒加载现在分段等待，期间检查出战提示。
    assert sum(sleeps) == pytest.approx(42, abs=.1)


def test_zero_tickets_never_deploys_or_starts_battle(monkeypatch):
    from naruto_daily.secret_realm import handler
    client = SecretClient()
    fake_model(monkeypatch, [0])
    def fail(*a, **kw):
        raise AssertionError("零券不能进入战斗")
    monkeypatch.setattr(handler, "run_battle", fail)
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher, sleep=lambda _: None) == 0
    assert client.taps == [(1443, 791)]


@pytest.mark.parametrize('counts', [[0], [1, 0]], ids=['initial-zero', 'pre-deploy-zero'])
def test_confirmed_zero_finishes_run_without_closing_room(monkeypatch, counts):
    from naruto_daily import runner
    from naruto_daily.secret_realm import handler

    client = SecretClient()
    calls = fake_model(monkeypatch, counts)

    def unexpected(*args, **kwargs):
        raise AssertionError('确认零券后不能出战、调用回主界面或继续后续动作')

    monkeypatch.setattr(handler, 'run_battle', unexpected)
    monkeypatch.setattr(runner, 'return_to_main', unexpected)
    summary = runner.ActionRunner(client, template_matcher=secret_matcher, sleep=lambda _: None).run([
        {'type': 'wait', 'seconds': 0}, secret_action(), {'type': 'return_to_main'},
        {'type': 'tap', 'x': 1, 'y': 1},
    ])

    assert summary.action_count == 2
    assert client.taps == [(1443, 791)]
    assert len(calls) == len(counts)


def test_failed_battle_does_not_repeat_deploy(monkeypatch):
    from naruto_daily.secret_realm import handler
    from naruto_daily.common.visual import VisualError
    client = SecretClient()
    fake_model(monkeypatch, [2, 2])
    def fail(*a, **kw):
        prepared = kw['before_attack'](a[0].screenshot())
        if prepared is not None:
            return prepared
        raise VisualError("未确认战斗结束")
    monkeypatch.setattr(handler, "run_battle", fail)
    with pytest.raises(VisualError):
        handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher, sleep=lambda _: None)
    assert client.taps == [(1443, 791), (1787, 920)]


@pytest.mark.parametrize('counts, expected_battles', [
    ([31, 31, 31, 31, 0], 2),
    ([31, 31, 32, 32, 0], 2),
    ([31] * 8 + [0], 4),
], ids=['unchanged', 'increased', 'repeated-unchanged'])
def test_positive_tickets_continue_without_requiring_a_decrease(monkeypatch, counts, expected_battles):
    from naruto_daily.secret_realm import handler
    client, battles = SecretClient(), []
    calls = fake_model(monkeypatch, counts)
    monkeypatch.setattr(handler, "run_battle", lambda *a, **kw:
                        fake_battle_after_prepare(a, kw, lambda: battles.append(True)))
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher,
                                       sleep=lambda _: None) == expected_battles
    assert len(battles) == expected_battles
    assert client.taps.count((1787, 920)) == expected_battles
    assert len(calls) == expected_battles * 2 + 1


def test_unreadable_count_never_becomes_zero_or_deploys(monkeypatch):
    from naruto_daily import runner
    client = SecretClient()
    fake_model(monkeypatch, [None] * 6)
    with pytest.raises(MultimodalError, match="调用上限"):
        runner.ActionRunner(client, template_matcher=secret_matcher, sleep=lambda _: None).run([
            secret_action(), {'type': 'tap', 'x': 1, 'y': 1},
        ])
    assert client.taps == [(1443, 791)]


def test_secret_realm_is_last_in_login_and_all_tasks():
    from pathlib import Path
    from naruto_daily.config import load_settings
    from naruto_daily.tasks import build_task, build_all
    settings = load_settings(Path(__file__).parents[1] / "config.example.json")
    secret = settings.tasks["secret_realm"]
    assert build_task(settings, "login")[-len(secret):] == secret
    assert build_all(settings)[-len(secret):] == secret
    assert secret[-1]["type"] == "return_to_main"


def test_tickets_become_zero_before_deploy_cancels_battle(monkeypatch):
    from naruto_daily.secret_realm import handler
    client = SecretClient()
    fake_model(monkeypatch, [1, 0])
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher, sleep=lambda _: None) == 0
    assert client.taps == [(1443, 791)]


def test_each_battle_result_returns_before_next_ticket_read(monkeypatch):
    from naruto_daily.secret_realm import handler
    class ResultClient(SecretClient):
        result = False
        def screenshot(self):
            return b'result' if self.result else super().screenshot()
        def tap(self, x, y):
            super().tap(x, y)
            if (x, y) == (209, 1010):
                self.result = False
    client = ResultClient()
    def battle(*a, **kw):
        prepared = kw['before_attack'](a[0].screenshot())
        if prepared is not None:
            return prepared
        client.result = True
    def detect(png, path, roi, threshold):
        if png == b'result':
            points = {'secret_result_marker': (537, 837), 'secret_result_return': (209, 1010)}
            return (points[path.stem], 1) if path.stem in points else (None, .1)
        return secret_matcher(png, path, roi, threshold)
    fake_model(monkeypatch, [2, 2, 1, 1, 0])
    monkeypatch.setattr(handler, 'run_battle', battle)
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=detect, sleep=lambda _: None) == 2
    assert client.taps == [(1443, 791), (1787, 920), (209, 1010), (1787, 920), (209, 1010)]


def test_model_finished_battle_with_unmatched_result_templates_dismisses_then_reads_tickets(monkeypatch):
    from types import SimpleNamespace
    from naruto_daily.secret_realm import handler

    class OverlayClient(SecretClient):
        overlay = False

        def screenshot(self):
            return b'countdown-overlay' if self.overlay else super().screenshot()

        def tap(self, x, y):
            super().tap(x, y)
            if (x, y) == (1787, 920):
                self.overlay = True
            elif (x, y) == (960, 900):
                self.overlay = False

    client, calls = OverlayClient(), []
    counts = iter([1, 1, 0])

    def complete(png, prompt, **kwargs):
        calls.append(png)
        if png == b'countdown-overlay':
            return {'page': 'finished', 'finished': True, 'reason': '倒计时遮挡奖励标题，战斗已结束'}
        assert png == b'room', '必须先回到房间，再读取挑战券'
        return {'page': 'room', 'tickets': next(counts)}

    monkeypatch.setattr(handler, 'MultimodalClient',
                        lambda *args, **kwargs: SimpleNamespace(complete_json=complete))
    monkeypatch.setattr(handler, 'prepare_secret_battle', lambda *args, **kwargs: BattlePreparation.READY)
    config = secret_action()
    config['timeout_seconds'] = 8

    # 保留真实战斗检查、结算和读券链路；本地模板均不能识别倒计时遮挡的奖励弹层。
    assert handler.handle_secret_realm(client, config, template_matcher=secret_matcher,
                                       sleep=lambda _: None) == 1
    assert client.taps == [(1443, 791), (1787, 920), (960, 900)]
    assert calls == [b'room', b'room', b'countdown-overlay', b'room']


def test_skipped_yinyang_does_not_require_ticket_decrease_or_count_as_completed(monkeypatch):
    from naruto_daily.secret_realm import handler
    client, battles = SecretClient(), []
    fake_model(monkeypatch, [1, 1, 1, 1, 0])
    skipped = iter([BattlePreparation.EXITED, BattlePreparation.READY])
    monkeypatch.setattr(handler, 'prepare_secret_battle', lambda *a, **kw: next(skipped), raising=False)
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw:
                        fake_battle_after_prepare(a, kw, lambda: battles.append(True)))
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher,
                                       sleep=lambda _: None) == 1
    assert client.taps == [(1443, 791), (1787, 920), (1787, 920)]
    assert len(battles) == 1


def test_failed_yinyang_detection_or_exit_never_starts_attacking(monkeypatch):
    from naruto_daily.secret_realm import handler
    client, battles = SecretClient(), []
    fake_model(monkeypatch, [1, 1])
    def fail(*a, **kw):
        raise MultimodalError('退出未确认')
    monkeypatch.setattr(handler, 'prepare_secret_battle', fail, raising=False)
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw:
                        fake_battle_after_prepare(a, kw, lambda: battles.append(True)))
    with pytest.raises(MultimodalError, match='退出未确认'):
        handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher, sleep=lambda _: None)
    assert client.taps == [(1443, 791), (1787, 920)] and battles == []


def test_deploy_warning_is_confirmed_before_realm_detection_and_battle(monkeypatch):
    from naruto_daily.secret_realm import handler
    class WarningClient(SecretClient):
        warning = False
        def screenshot(self):
            return b'warning' if self.warning else super().screenshot()
        def tap(self, x, y):
            super().tap(x, y)
            if (x, y) == (1787, 920):
                self.warning = True
            elif (x, y) == (950, 700):
                self.warning = False
    client, battles = WarningClient(), []
    def detect(png, path, roi, threshold):
        if png == b'warning':
            points = {'secret_deploy_warning_text': (950, 480), 'secret_deploy_warning_confirm': (950, 700)}
            return (points[path.stem], 1) if path.stem in points else (None, .1)
        return secret_matcher(png, path, roi, threshold)
    fake_model(monkeypatch, [1, 1, 0])
    def classify(*a, **kw):
        assert not client.warning, '应先确认提示再进行副本名称识别'
        return BattlePreparation.READY
    monkeypatch.setattr(handler, 'prepare_secret_battle', classify)
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw:
                        fake_battle_after_prepare(a, kw, lambda: battles.append(True)))
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=detect,
                                       sleep=lambda _: None) == 1
    assert client.taps == [(1443, 791), (1787, 920), (950, 700)] and len(battles) == 1


@pytest.mark.parametrize('next_tickets', [0, 1])
def test_navigation_failure_returns_to_room_and_rereads_tickets_before_redeploy(monkeypatch, next_tickets):
    from naruto_daily.secret_realm import handler

    class FailureClient(SecretClient):
        result = False

        def screenshot(self):
            return b'failure' if self.result else super().screenshot()

        def tap(self, x, y):
            super().tap(x, y)
            if (x, y) == (960, 900):
                self.result = False

    client, battles = FailureClient(), []
    counts = [1, 1, next_tickets] + ([1, 0] if next_tickets else [])
    calls = fake_model(monkeypatch, counts)
    preparations = iter([BattlePreparation.FINISHED, BattlePreparation.READY])

    def prepare(*args, **kwargs):
        preparation = next(preparations)
        if preparation is BattlePreparation.FINISHED:
            client.result = True
        return preparation

    def battle(*args, **kwargs):
        prepared = kwargs['before_attack'](args[0].screenshot())
        if prepared is not None:
            return prepared
        battles.append(True)
        client.result = True

    monkeypatch.setattr(handler, 'prepare_secret_battle', prepare)
    monkeypatch.setattr(handler, 'run_battle', battle)
    monkeypatch.setattr(handler, 'secret_failure_visible', lambda png: png == b'failure')
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher,
                                       sleep=lambda _: None) == 1 + next_tickets
    assert len(battles) == next_tickets
    assert len(calls) == 3 + 2 * next_tickets
    assert client.taps == [(1443, 791), (1787, 920), (960, 900)] + \
        ([(1787, 920), (960, 900)] if next_tickets else [])


@pytest.mark.parametrize('next_tickets', [0, 1])
def test_ninja_selection_returns_to_room_and_rechecks_tickets_before_next_battle(monkeypatch, next_tickets):
    from naruto_daily.battle.handler import BattleSummary
    from naruto_daily.secret_realm import handler

    class NinjaClient(SecretClient):
        selecting = False

        def screenshot(self):
            return b'ninja' if self.selecting else super().screenshot()

        def tap(self, x, y):
            super().tap(x, y)
            if (x, y) == (150, 1000):
                self.selecting = False

    client, battles = NinjaClient(), []
    counts = [1, 1, next_tickets] + ([1, 0] if next_tickets else [])
    calls = fake_model(monkeypatch, counts)
    monkeypatch.setattr(handler, 'prepare_secret_battle', lambda *a, **kw: BattlePreparation.READY)
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda png: (150, 1000) if png == b'ninja' else None)

    def battle(*args, **kwargs):
        prepared = kwargs['before_attack'](args[0].screenshot())
        if prepared is not None:
            return prepared
        battles.append(True)
        client.selecting = True
        assert kwargs['end_detector'](client.screenshot()) == '秘境更换忍者页'
        assert kwargs['end_detector'](b'room') is None
        return BattleSummary(0, '秘境更换忍者页')

    monkeypatch.setattr(handler, 'run_battle', battle)
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher,
                                       sleep=lambda _: None) == 1 + next_tickets
    assert len(battles) == 1 + next_tickets
    assert len(calls) == 3 + 2 * next_tickets
    assert client.taps == [(1443, 791), (1787, 920), (150, 1000)] + \
        ([(1787, 920), (150, 1000)] if next_tickets else [])


def test_ninja_battle_end_does_not_authorize_blank_taps_on_unknown_transition(monkeypatch):
    from naruto_daily.battle.handler import BattleSummary
    from naruto_daily.secret_realm import handler

    class TransitionClient(SecretClient):
        unknown_frames = 0

        def screenshot(self):
            if self.unknown_frames:
                self.unknown_frames -= 1
                return b'unknown-transition'
            return super().screenshot()

    client = TransitionClient()
    calls = fake_model(monkeypatch, [1, 1, 0])
    monkeypatch.setattr(handler, 'prepare_secret_battle', lambda *a, **kw: BattlePreparation.READY)

    def battle(*args, **kwargs):
        prepared = kwargs['before_attack'](args[0].screenshot())
        if prepared is not None:
            return prepared
        client.unknown_frames = 2
        return BattleSummary(0, '秘境更换忍者页')

    monkeypatch.setattr(handler, 'run_battle', battle)
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher,
                                       sleep=lambda _: None) == 1
    assert client.taps == [(1443, 791), (1787, 920)]
    assert len(calls) == 3
