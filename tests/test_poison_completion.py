"""原生导航工具中的毒风证据规则；不将模型文字字段校验声称为 OCR。"""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naruto_daily.secret_realm import navigation


def evidence(**changes):
    reply = dict(page='battle', seal_prompt_visible='no', boss_visible='yes',
                 battle_started='no', visible_text='毒风秘境', observation='封印提示消失，画面出现 BOSS。')
    reply.update(changes)
    return navigation.PoisonEvidence.model_validate(reply)


@pytest.mark.parametrize('page', ['result', 'other', 'unknown'])
def test_non_battle_page_blocks_even_when_other_fields_claim_success(page):
    assert evidence(page=page, battle_started='yes').state() == 'blocked'


@pytest.mark.parametrize('visible', ['yes', 'unknown'])
def test_visible_or_unknown_seal_instruction_prevents_completion(visible):
    assert evidence(seal_prompt_visible=visible, battle_started='yes').state() == 'pending'


@pytest.mark.parametrize('boss_visible,battle_started', [('no', 'no'), ('unknown', 'no'),
                                                        ('no', 'unknown'), ('unknown', 'unknown')])
def test_absent_instruction_without_positive_evidence_is_pending(boss_visible, battle_started):
    assert evidence(boss_visible=boss_visible, battle_started=battle_started,
                    observation='人物脚下有蓝色选中圈。').state() == 'pending'


@pytest.mark.parametrize('boss_visible,battle_started', [('yes', 'no'), ('yes', 'unknown'),
                                                        ('no', 'yes'), ('unknown', 'yes'), ('yes', 'yes')])
def test_absent_instruction_and_positive_evidence_reach_goal(boss_visible, battle_started):
    assert evidence(boss_visible=boss_visible, battle_started=battle_started).state() == 'reached'


@pytest.mark.parametrize('visible_text', ['解开「封印」，召唤BOSS', '解开 『封印』， 召唤 boss',
                                        '解除[封印]，召唤BOSS', '待解封', '等待解封', '等待召唤BOSS'])
def test_transcribed_pending_instruction_overrides_conflicting_no_flag(visible_text):
    # 只核对模型已提供字段之间的一致性，不从图片执行文字识别。
    assert evidence(visible_text=visible_text, battle_started='yes').state() == 'pending'


@pytest.mark.parametrize('visible_text', ['失败', '胜利', '点击任意位置关闭界面', '战斗结算', '结算奖励'])
@pytest.mark.parametrize('seal_prompt_visible', ['no', 'yes'])
def test_transcribed_ending_text_blocks_a_conflicting_battle_page(visible_text, seal_prompt_visible):
    assert evidence(visible_text=visible_text, seal_prompt_visible=seal_prompt_visible,
                    battle_started='yes').state() == 'blocked'


def test_reward_preview_text_alone_does_not_block_an_active_battle():
    assert evidence(visible_text='奖励预览').state() == 'reached'


def test_manually_transcribed_user_failure_frame_is_blocked():
    # 对应保留的 secret_poison_unreleased_failure.png；这里只测试人工抄写事实的处理。
    result = evidence(page='result', seal_prompt_visible='yes', boss_visible='no',
                      visible_text='毒风秘境 解开「封印」，召唤BOSS 失败 点击任意位置关闭界面',
                      observation='失败弹层覆盖战场，待解封提示仍在。')
    assert result.state() == 'blocked'


def test_empty_visible_text_is_allowed_but_does_not_imply_completion():
    assert evidence(visible_text='', boss_visible='no').state() == 'pending'


@pytest.mark.parametrize('field', ['seal_prompt_visible', 'boss_visible', 'battle_started'])
@pytest.mark.parametrize('value', [True, False, None, 'True', 'False', 0, 1],
                         ids=['bool-true', 'bool-false', 'null', 'string-True', 'string-False', 'zero', 'one'])
def test_fact_fields_reject_boolean_null_and_numeric_variants(field, value):
    with pytest.raises(ValidationError):
        evidence(**{field: value})


@pytest.mark.parametrize('field,value', [('page', None), ('page', 'victory'),
                                       ('observation', ''), ('observation', '  '),
                                       ('observation', 123), ('observation', '字' * 2001),
                                       ('visible_text', None), ('visible_text', 123),
                                       ('visible_text', '字' * 2001)],
                         ids=['page-null', 'page-invalid', 'observation-empty', 'observation-blank',
                              'observation-integer', 'observation-too-long', 'visible-text-null',
                              'visible-text-integer', 'visible-text-too-long'])
def test_malformed_evidence_is_not_coerced_into_valid_facts(field, value):
    with pytest.raises(ValidationError):
        evidence(**{field: value})


@pytest.mark.parametrize('field', ['page', 'seal_prompt_visible', 'boss_visible', 'battle_started',
                                  'visible_text', 'observation'])
def test_every_evidence_field_is_required(field):
    reply = dict(page='battle', seal_prompt_visible='no', boss_visible='yes',
                 battle_started='no', visible_text='', observation='BOSS 已出现。')
    del reply[field]
    with pytest.raises(ValidationError):
        navigation.PoisonEvidence.model_validate(reply)


@pytest.mark.parametrize('reply', [None, [], {'reached': True}])
def test_unstructured_or_self_declared_success_is_not_evidence(reply):
    with pytest.raises(ValidationError):
        navigation.PoisonEvidence.model_validate(reply)


def test_extra_reached_field_is_rejected():
    with pytest.raises(ValidationError):
        evidence(reached=True)


def test_poison_navigation_passes_native_evidence_rules_without_preflight_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('毒风包装器不得自行截图或调用独立 JSON 模型')
    images = SimpleNamespace(client=SimpleNamespace(screenshot=forbidden), sleep=lambda _: None)
    model = SimpleNamespace(complete_json=forbidden)
    result = SimpleNamespace(reached=True, steps=3)
    calls = []
    def run(client, actual_model, goal, *, joystick, max_steps, sleep, evidence_type, instructions):
        assert client is images.client and actual_model is model
        assert max_steps == 50 and sleep is images.sleep
        assert (joystick.x, joystick.y, joystick.radius, joystick.duration_ms) == (320, 840, 80, 300)
        assert evidence_type is navigation.PoisonEvidence
        assert instructions == navigation.POISON_SEAL_INSTRUCTIONS
        assert goal == navigation.POISON_SEAL_GOAL
        calls.append('run')
        return result
    monkeypatch.setattr(navigation, 'run_agent', run)
    assert navigation.navigate_to_poison_seal(images, model) is result
    assert calls == ['run']
