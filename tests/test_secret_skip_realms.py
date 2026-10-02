import pytest

from naruto_daily.secret_realm import pre_battle
from test_secret_navigation import Scenario, execute


@pytest.mark.parametrize('realm', ['阴阳秘境', '毒风秘境', '落岩秘境'])
def test_skipped_realms_exit_and_redeploy_after_fresh_tickets_without_navigation_or_attack(monkeypatch, realm):
    scenario = Scenario(realm=realm)
    # A skipped run may leave the ticket count unchanged; read it again before redeploying.
    scenario.ticket_counts = iter([1, 1, 1, 1, 0])

    def forbidden_navigation(*args, **kwargs):
        pytest.fail('暂时跳过的秘境不应调用导航 agent')

    monkeypatch.setattr(pre_battle, 'navigate_to_poison_seal', forbidden_navigation)
    assert execute(monkeypatch, scenario) == 0

    events = [event[0] for event in scenario.events if event[0] != 'screenshot']
    assert not scenario.nav_frames and scenario.moves == 0
    assert 'battle' not in events
    assert events == [
        'tickets', 'tickets', 'deploy', 'classify', 'pause', 'exit', 'confirm',
        'tickets', 'tickets', 'deploy', 'classify', 'pause', 'exit', 'confirm',
        'tickets',
    ]
    assert scenario.state == 'room'
