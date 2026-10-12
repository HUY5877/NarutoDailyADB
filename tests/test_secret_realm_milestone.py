"""首次秘境成功只依赖明确奖励结算，并继续处理余券。"""

import logging
from types import SimpleNamespace

import pytest

from naruto_daily.battle.handler import BattleSummary
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from naruto_daily.secret_realm import handler
from naruto_daily.secret_realm.pre_battle import BattlePreparation
from test_secret_realm import secret_action
from test_secret_result import Client, RESULT, ROOM, matcher


@pytest.mark.parametrize('frame, expected', [
    (RESULT, True),
    ({'secret_reward_overlay'}, True),
    ({'failure', 'secret_result_return', 'secret_reward_overlay'}, False),
    (ROOM | {'secret_reward_overlay'}, False),
    ({'secret_create_room', 'secret_reward_overlay'}, False),
    ({'ninja', 'secret_result_return', 'secret_reward_overlay'}, False),
    ({'secret_result_return'}, False),
])
def test_result_milestone_requires_success_foreground(monkeypatch, frame, expected):
    monkeypatch.setattr(handler, 'secret_failure_visible', lambda png: 'failure' in png)
    monkeypatch.setattr(handler, 'find_ninja_selection_return',
                        lambda png: (150, 1000) if 'ninja' in png else None)
    client, reported = Client([frame, frame, ROOM]), []
    handler.finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                                 secret_action(), result_confirmed=True,
                                 on_success=lambda: reported.append(True))
    assert bool(reported) is expected


def test_active_exit_never_reports_success_even_with_result_markers():
    client, reported = Client([RESULT, RESULT, ROOM]), []
    handler.finish_secret_result(client, ImageRecognition(client, matcher, sleep=lambda _: None),
                                 secret_action(), exited_battle=True,
                                 on_success=lambda: reported.append(True))
    assert not reported


@pytest.mark.parametrize('preparation, result_frame, end_target, expected', [
    (BattlePreparation.READY, ROOM, '秘境结算翻牌说明', True),
    (BattlePreparation.READY, ROOM, '秘境恭喜你获得奖励弹层', True),
    (BattlePreparation.READY, RESULT, 'multimodal', True),
    (BattlePreparation.FINISHED, RESULT, None, True),
    (BattlePreparation.FINISHED, {'secret_reward_overlay'}, None, True),
    (BattlePreparation.FINISHED, {'failure'}, None, False),
    (BattlePreparation.READY, {'failure'}, 'multimodal', False),
    (BattlePreparation.READY, ROOM, '秘境房间出战', False),
    (BattlePreparation.READY, ROOM, '秘境更换忍者页', False),
    (BattlePreparation.EXITED, ROOM, None, False),
])
def test_first_success_reports_once_and_remaining_tickets_continue(
        monkeypatch, caplog, preparation, result_frame, end_target, expected):
    class Scenario:
        frame = ROOM
        deployments = 0

        def screenshot(self):
            return self.frame

        def tap(self, x, y):
            if (x, y) == (1787, 920):
                self.deployments += 1
            else:
                self.frame = ROOM

    client = Scenario()
    counts = iter([2, 2, 1, 1, 0])
    monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw: object())
    monkeypatch.setattr(handler, 'wait_for_tickets', lambda *a, **kw:
                        SimpleNamespace(tickets=next(counts), deploy_center=(1787, 920)))
    monkeypatch.setattr(handler, 'wait_after_deploy', lambda *a, **kw: None)
    monkeypatch.setattr(handler, 'secret_failure_visible', lambda png: 'failure' in png)

    def prepare(*args, **kwargs):
        if preparation is BattlePreparation.FINISHED:
            client.frame = result_frame
        return preparation

    def battle(*args, **kwargs):
        client.frame = result_frame
        return BattleSummary(0, end_target)

    monkeypatch.setattr(handler, 'prepare_secret_battle', prepare)
    monkeypatch.setattr(handler, 'run_battle', battle)
    caplog.set_level(logging.INFO, logger='naruto_daily')
    completed = handler.handle_secret_realm(client, secret_action(), template_matcher=matcher,
                                           sleep=lambda _: None)
    milestones = [record for record in caplog.records
                  if getattr(record, 'secret_realm_completed', False)]
    assert len(milestones) == (1 if expected else 0)
    assert client.deployments == 2
    assert completed == (0 if preparation is BattlePreparation.EXITED else 2)


def test_failure_before_first_battle_has_no_completion_milestone(monkeypatch, caplog):
    monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw: object())
    monkeypatch.setattr(handler, 'wait_for_tickets',
                        lambda *a, **kw: (_ for _ in ()).throw(VisualError('读取失败')))
    caplog.set_level(logging.INFO, logger='naruto_daily')
    with pytest.raises(VisualError):
        handler.handle_secret_realm(Client([ROOM]), secret_action(),
                                    template_matcher=matcher, sleep=lambda _: None)
    assert not any(getattr(record, 'secret_realm_completed', False) for record in caplog.records)
