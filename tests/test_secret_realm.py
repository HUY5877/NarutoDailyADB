import pytest

from naruto_daily.multimodal.client import MultimodalError


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
    from naruto_daily.config import load_settings
    root = Path(__file__).parents[1]
    return next(a for a in load_settings(root / "config.example.json").tasks["secret_realm"]
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
    return calls


def test_secret_realm_runs_two_battles_then_zero_returns_to_main(monkeypatch):
    from naruto_daily import runner
    from naruto_daily.secret_realm import handler
    client, sleeps, battles, returned = SecretClient(), [], [], []
    calls = fake_model(monkeypatch, [2, 2, 1, 1, 0])
    monkeypatch.setattr(handler, "run_battle", lambda *a, **kw: battles.append(a[1]))
    monkeypatch.setattr(runner, "return_to_main", lambda *a, **kw: returned.append(True))
    runner.ActionRunner(client, template_matcher=secret_matcher, sleep=sleeps.append).run([
        secret_action(), {"type": "return_to_main"}])
    assert client.taps == [(1443, 791), (1787, 920), (1787, 920)]
    assert len(battles) == 2 and returned == [True] and len(calls) == 5
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


def test_failed_battle_does_not_repeat_deploy(monkeypatch):
    from naruto_daily.secret_realm import handler
    from naruto_daily.common.visual import VisualError
    client = SecretClient()
    fake_model(monkeypatch, [2, 2])
    def fail(*a, **kw):
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
    monkeypatch.setattr(handler, "run_battle", lambda *a, **kw: battles.append(True))
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher,
                                       sleep=lambda _: None) == expected_battles
    assert len(battles) == expected_battles
    assert client.taps.count((1787, 920)) == expected_battles
    assert len(calls) == expected_battles * 2 + 1


def test_unreadable_count_never_becomes_zero_or_deploys(monkeypatch):
    from naruto_daily.secret_realm import handler
    client = SecretClient()
    fake_model(monkeypatch, [None] * 6)
    with pytest.raises(MultimodalError, match="调用上限"):
        handler.handle_secret_realm(client, secret_action(), template_matcher=secret_matcher, sleep=lambda _: None)
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


def test_skipped_yinyang_does_not_require_ticket_decrease_or_count_as_completed(monkeypatch):
    from naruto_daily.secret_realm import handler
    client, battles = SecretClient(), []
    fake_model(monkeypatch, [1, 1, 1, 1, 0])
    skipped = iter([True, False])
    monkeypatch.setattr(handler, 'prepare_secret_battle', lambda *a, **kw: next(skipped), raising=False)
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw: battles.append(True))
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
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw: battles.append(True))
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
        return False
    monkeypatch.setattr(handler, 'prepare_secret_battle', classify)
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw: battles.append(True))
    assert handler.handle_secret_realm(client, secret_action(), template_matcher=detect,
                                       sleep=lambda _: None) == 1
    assert client.taps == [(1443, 791), (1787, 920), (950, 700)] and len(battles) == 1
