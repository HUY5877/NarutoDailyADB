"""走真实秘境 handler 和 SDK；设备、模型 HTTP 服务与图像匹配为离线边界。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_fakes import tool_call
from naruto_daily.common.visual import VisualError
from naruto_daily.config import load_settings
from naruto_daily.secret_realm import handler, navigation

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def model_connection(monkeypatch, agent_server):
    monkeypatch.setattr(Scenario, 'agent_connection',
                        lambda self: agent_server.connection(self.agent_reply), raising=False)


class Scenario:
    def __init__(self, realm='毒风秘境', reach_after=2, *, fail_navigation=False,
                 fail_exit=False, finish_steps=(), evidence_page='battle', result_after=None,
                 reached_tool='finish', always_finish=False, evidence_override=None):
        self.realm = realm
        self.reach_after = reach_after
        self.fail_navigation = fail_navigation
        self.fail_exit = fail_exit
        self.finish_steps = set(finish_steps)
        self.evidence_page = evidence_page
        self.result_after = result_after
        self.reached_tool = reached_tool
        self.always_finish = always_finish
        self.evidence_override = evidence_override or {}
        self.state = 'room'
        self.moves = 0
        self.events = []
        self.ticket_counts = iter([1, 1, 0])
        self.nav_frames = []

    def screenshot(self):
        frame = f'{self.state}:{self.moves}'.encode()
        self.events.append(('screenshot', frame))
        return frame

    def tap(self, x, y):
        transitions = {(1787, 920): ('deploy', 'battle'), (1860, 50): ('pause', 'pause'),
                       (780, 680): ('exit', 'confirm'), (790, 690): ('confirm', 'room')}
        event, state = transitions[(x, y)]
        self.events.append((event,))
        self.state = state

    def swipe(self, *coordinates):
        assert self.state == 'battle'
        self.moves += 1
        self.events.append(('move', coordinates))

    def complete_json(self, png, prompt, **kwargs):
        """既有 JSON 客户端只读券和副本名，额外视觉复核会直接让测试失败。"""
        assert png == f'{self.state}:{self.moves}'.encode()
        if self.state == 'room':
            self.events.append(('tickets',))
            return {'page': 'room', 'tickets': next(self.ticket_counts)}
        assert self.state == 'battle' and 'realm_name' in prompt, '导航不得额外调用 complete_json'
        self.events.append(('classify',))
        return {'page': 'battle', 'realm_name': self.realm}

    def agent_reply(self, png, prompt):
        assert self.state == 'battle'
        assert png == f'battle:{self.moves}'.encode()
        assert '毒风秘境' in prompt and '封印' in prompt
        self.nav_frames.append(png)
        if self.fail_navigation:
            raise VisualError('导航模型调用失败')
        ready = self.reach_after is not None and self.moves >= self.reach_after
        page = 'result' if self.result_after is not None and self.moves >= self.result_after else self.evidence_page
        evidence = {'page': page, 'seal_prompt_visible': 'no' if ready else 'yes',
                    'boss_visible': 'yes' if ready else 'no', 'battle_started': 'yes' if ready else 'no',
                    'visible_text': '' if ready else '解开「封印」，召唤BOSS',
                    'observation': '封印提示消失，BOSS 已出现' if ready else '中央仍显示待解封提示'}
        evidence.update(self.evidence_override)
        finish_now = self.always_finish or self.moves in self.finish_steps
        self.finish_steps.discard(self.moves)
        name = 'finish' if finish_now else self.reached_tool if ready else 'move'
        args = dict(evidence)
        if name == 'move':
            args['direction'] = 'd'
        self.events.append(('navigation', ready))
        return {'message': {'role': 'assistant', 'content': None,
                            'tool_calls': [tool_call(name, args, f'nav-{len(self.nav_frames)}')]}}

    def matcher(self, png, path, roi, threshold):
        state = png.decode().split(':')[0]
        targets = {
            'room': {'secret_deploy': (1787, 920)},
            'battle': {'secret_battle_pause': (1860, 50)},
            'pause': {'secret_exit_battle': (780, 680)},
            'confirm': {'secret_exit_question': (960, 480), 'secret_exit_confirm': (790, 690)},
        }
        if self.fail_exit and state == 'pause':
            return None, .1
        point = targets.get(state, {}).get(path.stem)
        return point, 1 if point else .1


def execute(monkeypatch, scenario):
    action = next(a for a in load_settings(ROOT / 'config.example.json').tasks['secret_realm']
                  if a['type'] == 'handle_secret_realm')
    action['load_wait_seconds'] = 0
    monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw: scenario)
    def battle(*args, **kwargs):
        if scenario.realm == '毒风秘境':
            assert scenario.reach_after is not None and scenario.moves >= scenario.reach_after, '解封前不能攻击'
            assert [e for e in scenario.events if e[0] != 'screenshot'][-1] == ('navigation', True), \
                '同一次导航事实确认后直接进入战斗，本地只读截图不得额外导航或调用模型'
        scenario.events.append(('battle',))
        scenario.state = 'room'
    monkeypatch.setattr(handler, 'run_battle', battle)
    return handler.handle_secret_realm(scenario, action, template_matcher=scenario.matcher, sleep=lambda _: None)


def test_poison_realm_navigates_then_battles_only_after_fresh_confirmation(monkeypatch):
    scenario = Scenario()
    assert execute(monkeypatch, scenario) == 1
    events = [e[0] for e in scenario.events]
    assert scenario.nav_frames == [b'battle:0', b'battle:1', b'battle:2']
    assert events.index('classify') < events.index('move') < events.index('battle')
    before_battle = scenario.events[:events.index('battle')]
    assert [e for e in before_battle if e[0] != 'screenshot'][-1] == ('navigation', True)
    assert 'pause' not in events and scenario.moves == 2
    assert [e[1] for e in scenario.events if e[0] == 'move'] == [
        (327, 825, 407, 825, 300), (327, 825, 407, 825, 300)]


@pytest.mark.parametrize('final_tool', ['move', 'finish'])
def test_poison_step_limit_exits_then_rereads_tickets_without_counting_completion(monkeypatch, final_tool):
    scenario = Scenario(reach_after=None, finish_steps=(50,) if final_tool == 'finish' else ())
    assert execute(monkeypatch, scenario) == 0
    assert scenario.moves == 50 and len(scenario.nav_frames) == 51
    assert [e[0] for e in scenario.events if e[0] != 'screenshot'][-4:] == ['pause', 'exit', 'confirm', 'tickets']
    assert ('battle',) not in scenario.events


@pytest.mark.parametrize('steps', [0, 2, 50])
@pytest.mark.parametrize('final_tool', ['move', 'finish'])
def test_poison_ready_evidence_starts_battle_without_an_extra_move(monkeypatch, steps, final_tool):
    scenario = Scenario(reach_after=steps, reached_tool=final_tool)
    assert execute(monkeypatch, scenario) == 1
    assert scenario.moves == steps and len(scenario.nav_frames) == steps + 1
    assert ('battle',) in scenario.events and ('pause',) not in scenario.events


def test_yinyang_uses_same_exit_without_navigation_or_attack(monkeypatch):
    scenario = Scenario(realm='阴阳秘境')
    assert execute(monkeypatch, scenario) == 0
    assert not scenario.nav_frames and scenario.moves == 0
    assert ('battle',) not in scenario.events
    assert [e[0] for e in scenario.events if e[0] != 'screenshot'][-4:] == ['pause', 'exit', 'confirm', 'tickets']


def test_other_realm_battles_without_navigation(monkeypatch):
    scenario = Scenario(realm='烈炎秘境')
    assert execute(monkeypatch, scenario) == 1
    assert not scenario.nav_frames and scenario.moves == 0
    assert ('battle',) in scenario.events and ('pause',) not in scenario.events


@pytest.mark.parametrize('error', ['navigation', 'exit'])
def test_failed_poison_navigation_or_exit_stops_without_attack_or_redeploy(monkeypatch, error):
    scenario = Scenario(reach_after=None, fail_navigation=error == 'navigation', fail_exit=error == 'exit')
    with pytest.raises(VisualError):
        execute(monkeypatch, scenario)
    assert ('battle',) not in scenario.events
    assert scenario.events.count(('deploy',)) == 1


def test_poison_pending_finish_retries_same_image_without_extra_visual_request(monkeypatch):
    scenario = Scenario(finish_steps=(0,))
    assert execute(monkeypatch, scenario) == 1
    assert scenario.moves == 2
    assert scenario.nav_frames == [b'battle:0', b'battle:0', b'battle:1', b'battle:2']
    first = scenario.events.index(('navigation', False))
    assert [e for e in scenario.events[first + 1:] if e[0] != 'screenshot'][0] == ('navigation', False), \
        'SDK重试应复用请求截图；本地完成检测不应产生额外模型请求或移动'


def test_poison_repeated_pending_finish_stops_after_three_sdk_retries(monkeypatch):
    scenario = Scenario(reach_after=None, always_finish=True)
    with pytest.raises(VisualError):
        execute(monkeypatch, scenario)
    assert scenario.nav_frames == [b'battle:0'] * 4
    assert scenario.moves == 0 and ('battle',) not in scenario.events and ('pause',) not in scenario.events


def test_poison_three_retries_still_allow_fiftieth_move_and_final_check(monkeypatch):
    scenario = Scenario(reach_after=50, finish_steps=(0, 1, 2))
    assert execute(monkeypatch, scenario) == 1
    assert scenario.moves == 50 and len(scenario.nav_frames) == 54
    assert ('battle',) in scenario.events and ('pause',) not in scenario.events


@pytest.mark.parametrize('page', ['result', 'other', 'unknown'])
@pytest.mark.parametrize('tool', ['move', 'finish'])
def test_poison_nonbattle_evidence_stops_before_move_or_attack(monkeypatch, page, tool):
    scenario = Scenario(reach_after=None, evidence_page=page, always_finish=tool == 'finish')
    with pytest.raises(VisualError):
        execute(monkeypatch, scenario)
    assert scenario.moves == 0 and len(scenario.nav_frames) == 1
    assert all(event[0] not in ('battle', 'pause', 'exit') for event in scenario.events)
    assert scenario.events.count(('deploy',)) == 1


@pytest.mark.parametrize('final_tool', ['move', 'finish'])
def test_poison_nonbattle_on_fiftieth_move_does_not_attempt_exit(monkeypatch, final_tool):
    scenario = Scenario(reach_after=None, result_after=50,
                        finish_steps=(50,) if final_tool == 'finish' else ())
    with pytest.raises(VisualError):
        execute(monkeypatch, scenario)
    assert scenario.moves == 50 and len(scenario.nav_frames) == 51
    assert all(event[0] not in ('battle', 'pause', 'exit') for event in scenario.events)


@pytest.mark.parametrize('seal', ['yes', 'unknown'])
def test_poison_seal_prompt_present_or_unknown_overrides_boss_evidence(monkeypatch, seal):
    scenario = Scenario(reach_after=None, always_finish=True,
                        evidence_override={'seal_prompt_visible': seal, 'boss_visible': 'yes',
                                           'battle_started': 'yes'})
    with pytest.raises(VisualError):
        execute(monkeypatch, scenario)
    assert scenario.moves == 0 and len(scenario.nav_frames) == 4
    assert ('battle',) not in scenario.events


def test_poison_retry_budget_is_shared_across_successful_moves(monkeypatch):
    scenario = Scenario(reach_after=None, finish_steps=(0, 1, 2, 3))
    with pytest.raises(VisualError):
        execute(monkeypatch, scenario)
    assert scenario.moves == 3 and len(scenario.nav_frames) == 7
    assert ('battle',) not in scenario.events and ('pause',) not in scenario.events


def test_poison_native_tool_schemas_use_required_flat_facts(monkeypatch, agent_server):
    scenario = Scenario(reach_after=1)
    assert execute(monkeypatch, scenario) == 1
    facts = {'page', 'boss_visible', 'battle_started', 'seal_prompt_visible',
             'visible_text', 'observation'}
    assert len(agent_server.payloads) == 2
    for payload in agent_server.payloads:
        schemas = {tool['function']['name']: tool['function']['parameters']
                   for tool in payload['tools']}
        assert set(schemas) == {'move', 'finish'}
        for name, schema in schemas.items():
            expected = facts | ({'direction'} if name == 'move' else set())
            assert set(schema['properties']) == expected
            assert set(schema['required']) == expected
            assert 'evidence' not in schema['properties']
            for field in ('seal_prompt_visible', 'boss_visible', 'battle_started'):
                assert schema['properties'][field]['type'] == 'string'
                assert set(schema['properties'][field]['enum']) == {'yes', 'no', 'unknown'}


def test_poison_local_completion_after_move_skips_next_real_sdk_request(monkeypatch, agent_server):
    scenario = Scenario(reach_after=1)
    scenario.state = 'battle'
    monkeypatch.setattr(navigation, 'poison_boss_visible', lambda png: png == b'battle:1')
    images = SimpleNamespace(client=scenario, sleep=lambda _: None)
    result = navigation.navigate_to_poison_seal(images, scenario)
    assert result.reached and result.steps == scenario.moves == 1
    assert [step.action for step in result.history] == ['d']
    assert len(agent_server.payloads) == 1
    assert scenario.nav_frames == [b'battle:0']
