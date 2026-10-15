import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.login_service import qr_navigation as navigation
from naruto_daily.login_service.refresh import RefreshRequests


def test_child_only_reads_its_task_and_does_not_repeat_before_upload_ack(monkeypatch):
    task_id, request_id = str(uuid4()), str(uuid4())
    reads = []
    command = {'taskId': task_id, 'requestId': request_id}
    client = SimpleNamespace(get=lambda key: reads.append(key) or json.dumps(command))
    reader = RefreshRequests('unused', 'test:queue', task_id, client=client)
    monkeypatch.setattr('naruto_daily.login_service.refresh.time.monotonic', lambda: 100)
    assert reader.poll() == request_id
    reader.next_poll = 0
    assert reader.poll() is None
    command['taskId'] = str(uuid4())
    command['requestId'] = str(uuid4())
    reader.next_poll = 0
    assert reader.poll() is None
    assert reads == [f'test-test-jev:queue:refresh:{task_id}'] * 3


@pytest.mark.parametrize('page', ['valid_qr', 'expired_qr', 'login'])
def test_refresh_returns_once_from_either_qr_and_never_backs_from_login(monkeypatch, page):
    state = [page]
    actions = []
    device = SimpleNamespace(deadline=200, screenshot=lambda: state[0],
                             back=lambda: (actions.append('back'), state.__setitem__(0, 'login')))
    monkeypatch.setattr(navigation.time, 'time', lambda: 100)
    monkeypatch.setattr(navigation.time, 'sleep', lambda _: None)
    monkeypatch.setattr(navigation, 'authorization_page', lambda frame: frame.endswith('_qr'))
    monkeypatch.setattr(navigation, 'choose_action', lambda *_: navigation.LoginAction('qq_android', 1605, 813))
    monkeypatch.setattr(navigation, 'fetch_login_qr', lambda *_, **kw: actions.append('fetch') or b'new qr')
    assert navigation.refresh_login_qr(device, is_authorized=lambda: False) == b'new qr'
    assert actions == (['fetch'] if page == 'login' else ['back', 'fetch'])


@pytest.mark.parametrize('detected_at', [0, 1])
def test_authorization_wins_before_refresh_or_immediately_before_back(monkeypatch, detected_at):
    observations = iter([False] * detected_at + [True])
    device = SimpleNamespace(deadline=200, screenshot=lambda: 'qr', back=lambda: pytest.fail('must not cancel login'))
    monkeypatch.setattr(navigation.time, 'time', lambda: 100)
    monkeypatch.setattr(navigation, 'authorization_page', lambda _: True)
    assert navigation.refresh_login_qr(device, is_authorized=lambda: next(observations)) is None


def test_authorization_during_new_qr_navigation_never_taps_game_back(monkeypatch):
    device = SimpleNamespace(deadline=200, screenshot=lambda: pytest.fail('no need to fetch QR'))
    monkeypatch.setattr(navigation.time, 'time', lambda: 100)
    assert navigation.fetch_login_qr(device, is_authorized=lambda: True) is None
