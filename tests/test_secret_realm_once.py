"""一次模式先完成成功结算收尾，再结束整个日常；失败和退出照常恢复。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from naruto_daily.battle import BattleSummary
from naruto_daily.common.visual import VisualError
from naruto_daily.login_service.log_events import forward_task_logs
from naruto_daily.runner import ActionRunner
from naruto_daily.secret_realm import handler
from naruto_daily.secret_realm.pre_battle import BattlePreparation
from test_login_service_daily import session_for
from test_secret_realm import secret_action
from test_secret_result import RESULT, ROOM, matcher


class Scenario:
    def __init__(self, rounds, *, finish_room=True):
        self.rounds = iter(rounds)
        self.frame = ROOM
        self.current = None
        self.events = []
        self.ticket_values = iter([99] * (2 * len(rounds)) + [0])
        self.finish_room = finish_room

    def screenshot(self):
        self.events.append(('screenshot', self.frame))
        return self.frame

    def tap(self, x, y):
        self.events.append(('tap', x, y))
        if (x, y) == (1787, 920):
            self.current = next(self.rounds)
        elif (x, y) in {(209, 1010), (960, 900)}:
            self.frame = ROOM if self.finish_room else set()


def install(monkeypatch, scenario):
    monkeypatch.setattr('naruto_daily.runner.handle_free_recruit',
                        lambda device, action, **kwargs: device.tap(10, 10))
    monkeypatch.setattr('naruto_daily.runner.handle_coin_fortune',
                        lambda device, action, **kwargs: device.tap(999, 999))
    monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw: object())

    def tickets(*args, **kwargs):
        scenario.events.append(('tickets',))
        return SimpleNamespace(tickets=next(scenario.ticket_values), deploy_center=(1787, 920))

    def prepare(*args, **kwargs):
        preparation, frame, _ = scenario.current
        if preparation in {BattlePreparation.FINISHED, BattlePreparation.EXITED}:
            scenario.frame = frame
        return preparation

    def battle(*args, **kwargs):
        scenario.events.append(('battle',))
        prepared = kwargs['before_attack'](args[0].screenshot())
        if prepared is not None:
            return prepared
        _, frame, target = scenario.current
        scenario.frame = frame
        return BattleSummary(0, target)

    monkeypatch.setattr(handler, 'wait_for_tickets', tickets)
    monkeypatch.setattr(handler, 'wait_after_deploy', lambda *a, **kw: None)
    monkeypatch.setattr(handler, 'prepare_secret_battle', prepare)
    monkeypatch.setattr(handler, 'run_battle', battle)
    monkeypatch.setattr(handler, 'secret_failure_visible', lambda png: 'failure' in png)


SUCCESS = (BattlePreparation.READY, RESULT, '秘境结算翻牌说明')


@pytest.mark.parametrize('counts', [[0], [99, 0]], ids=['room-zero', 'pre-deploy-zero'])
def test_once_with_no_tickets_normally_finishes_the_whole_daily(monkeypatch, counts):
    scenario = Scenario([])
    scenario.ticket_values = iter(counts)
    install(monkeypatch, scenario)
    events = []
    with forward_task_logs(events.append):
        summary = ActionRunner(scenario, template_matcher=matcher, sleep=lambda _: None,
                               user_options={'secretRealmMode': 'once'}).run([
            {'type': 'handle_free_recruit'}, secret_action(), {'type': 'handle_coin_fortune'},
        ])
    assert summary.action_count == 2
    assert scenario.events.count(('tickets',)) == len(counts)
    assert not any(event[0] == 'battle' for event in scenario.events)
    assert ('tap', 1787, 920) not in scenario.events
    assert ('tap', 999, 999) not in scenario.events
    assert not any('secretRealmCompletedAt' in event for event in events)
    assert events[-1]['message'] == '执行结束：2 个动作'
    assert scenario.frame == ROOM


@pytest.mark.parametrize('options, rounds, ticket_reads', [
    (None, 2, 5), ({'equipmentStamina': False}, 2, 5),
    ({'secretRealmOnce': False}, 2, 5), ({'secretRealmOnce': True}, 1, 2),
    ({'secretRealmMode': 'all'}, 2, 5), ({'secretRealmMode': 'once'}, 1, 2),
])
def test_once_success_finishes_all_actions_after_return_to_room(monkeypatch, options, rounds, ticket_reads):
    scenario = Scenario([SUCCESS, SUCCESS])
    install(monkeypatch, scenario)
    events = []
    with forward_task_logs(events.append):
        result = ActionRunner(scenario, template_matcher=matcher, sleep=lambda _: None,
                              user_options=options).run([
            {'type': 'handle_free_recruit'}, secret_action(), {'type': 'handle_coin_fortune'},
        ])
    assert result.action_count == 2
    assert scenario.frame == ROOM
    assert scenario.events.count(('tap', 1787, 920)) == rounds
    assert scenario.events.count(('tickets',)) == ticket_reads
    assert ('tap', 999, 999) not in scenario.events
    assert scenario.events.count(('tap', 209, 1010)) == rounds
    milestones = [event for event in events if 'secretRealmCompletedAt' in event]
    assert len(milestones) == 1
    if options and (options.get('secretRealmOnce') or options.get('secretRealmMode') == 'once'):
        assert '结算收尾后结束本次任务' in milestones[0]['message']
        assert '继续消耗' not in milestones[0]['message']
    else:
        assert '继续消耗剩余秘境挑战券' in milestones[0]['message']
    assert events[-1]['message'] == '执行结束：2 个动作'


@pytest.mark.parametrize('first, expected_completed', [
    ((BattlePreparation.FINISHED, {'failure'}, None), 2),
    ((BattlePreparation.READY, {'failure'}, 'multimodal'), 2),
    ((BattlePreparation.EXITED, ROOM, None), 1),
    ((BattlePreparation.READY, ROOM, '秘境更换忍者页'), 2),
    ((BattlePreparation.READY, ROOM, '秘境房间出战'), 2),
])
def test_once_continues_after_non_success_until_real_success(monkeypatch, first, expected_completed):
    scenario = Scenario([first, SUCCESS, SUCCESS])
    install(monkeypatch, scenario)
    events = []
    with forward_task_logs(events.append):
        completed = handler.handle_secret_realm(scenario, secret_action(), template_matcher=matcher,
                                                sleep=lambda _: None, secret_realm_once=True)
    assert completed == expected_completed
    assert scenario.events.count(('tap', 1787, 920)) == 2
    assert scenario.events.count(('tickets',)) == 4
    assert scenario.frame == ROOM
    assert sum('secretRealmCompletedAt' in event for event in events) == 1


@pytest.mark.parametrize('first', [
    (BattlePreparation.FINISHED, RESULT, None),
    (BattlePreparation.FINISHED, {'secret_reward_overlay'}, None),
    (BattlePreparation.READY, ROOM, '秘境恭喜你获得奖励弹层'),
])
def test_direct_success_result_also_stops_once_after_cleanup(monkeypatch, first):
    scenario = Scenario([first, SUCCESS])
    install(monkeypatch, scenario)
    assert handler.handle_secret_realm(scenario, secret_action(), template_matcher=matcher,
                                       sleep=lambda _: None, secret_realm_once=True) == 1
    assert scenario.events.count(('tap', 1787, 920)) == 1
    assert scenario.events.count(('tickets',)) == 2
    assert scenario.frame == ROOM


def test_success_without_room_cleanup_still_fails_instead_of_reporting_task_completion(monkeypatch):
    scenario = Scenario([SUCCESS], finish_room=False)
    install(monkeypatch, scenario)
    config = secret_action()
    config['timeout_seconds'] = 8
    events = []
    with forward_task_logs(events.append), pytest.raises(VisualError, match='结算返回房间超时'):
        ActionRunner(scenario, template_matcher=matcher, sleep=lambda _: None,
                     user_options={'secretRealmMode': 'once'}).run([config])
    assert sum('secretRealmCompletedAt' in event for event in events) == 1
    assert not any(event['message'].startswith('执行结束') for event in events)
    assert scenario.events.count(('tap', 1787, 920)) == 1


@pytest.mark.parametrize('room_created', [True, False])
def test_once_confirms_the_room_after_settlement_returned_to_lobby(monkeypatch, room_created):
    class LobbyScenario(Scenario):
        def tap(self, x, y):
            super().tap(x, y)
            if (x, y) == (209, 1010):
                self.frame = {'secret_create_room'}
            elif (x, y) == (1443, 791):
                self.frame = ROOM if room_created else {'secret_create_room'}

    scenario = LobbyScenario([SUCCESS])
    install(monkeypatch, scenario)
    config = secret_action()
    config['timeout_seconds'] = 30
    if room_created:
        assert handler.handle_secret_realm(scenario, config, template_matcher=matcher,
                                           sleep=lambda _: None, secret_realm_once=True) == 1
        assert scenario.frame == ROOM
    else:
        with pytest.raises(VisualError, match='秘境房间出战'):
            handler.handle_secret_realm(scenario, config, template_matcher=matcher,
                                        sleep=lambda _: None, secret_realm_once=True)
    assert scenario.events.count(('tap', 1443, 791)) == 1
    assert scenario.events.count(('tap', 1787, 920)) == 1
    assert scenario.events.count(('tickets',)) == 2


@pytest.mark.parametrize('equipment', [True, False])
def test_daily_session_passes_once_option_and_keeps_equipment_choice(monkeypatch, tmp_path, equipment):
    scenario = Scenario([SUCCESS, SUCCESS])
    install(monkeypatch, scenario)
    config = secret_action()
    options = {'equipmentStamina': equipment, 'secretRealmMode': 'once'}
    session, clock, _ = session_for(
        tmp_path, user_options=options,
        change=lambda payload: payload['tasks'].update(secret_realm=[deepcopy(config)]),
    )
    session.client = scenario
    daily_matcher = session._template_matcher
    session._template_matcher = lambda frame, path, roi, threshold: (
        daily_matcher(frame, path, roi, threshold) if path.stem.startswith('daily_')
        else matcher(frame, path, roi, threshold)
    )
    session._logged_in = True
    assert session.run_daily(clock() + 300) == len(session._daily_actions)
    assert session._runner().user_options == options
    assert scenario.events.count(('tap', 1787, 920)) == 1
    assert scenario.events.count(('tickets',)) == 2
    equipment_x = session.settings.tasks['equipment_upgrade'][0]['left']
    assert (('tap', equipment_x, 0) in scenario.events) is equipment
    assert scenario.frame == ROOM
