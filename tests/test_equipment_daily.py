from pathlib import Path

import pytest

from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.equipment import prompts
from naruto_daily.equipment.handler import (
    handle_equipment_upgrade, lowest_equipment, _finish_material,
    MATERIAL_POINTS, EQUIPMENT_POINTS, BLANK, MATERIAL_BUTTON, AUTO_STOP, START_SWEEP, ADVANCE,
)
from naruto_daily.ichiraku import handle_ichiraku_takeout


class Client:
    def __init__(self):
        self.taps = []

    def screenshot(self):
        return repr(self.taps[-1] if self.taps else None).encode()

    def tap(self, x, y):
        self.taps.append((x, y))


class Model:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.prompts = []

    def complete_json(self, png, prompt, **kwargs):
        self.prompts.append(prompt)
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer


def action():
    return dict(timeout_seconds=1800, max_rounds=100, poll_seconds=1,
                post_click_wait_seconds=1, sweep_timeout_seconds=120,
                multimodal=dict(max_calls=2))


CENTERS = {'one_click_sweep': MATERIAL_BUTTON, 'start_sweep': START_SWEEP,
           'sweep_done': (1300, 900), 'advance': ADVANCE}


def matcher(*args, **kwargs):
    raise AssertionError('装备流程应全部由模型判断，不能调用图片模板')


def overview(levels=None):
    return dict(page='equipment', progressions=levels or [85, 45, 23, 85, 2, 78], selected_slot=5)


def material(button):
    return dict(page='material', action=button)


def setup(stamina, auto_stop=True):
    return dict(page='sweep_setup', stamina=stamina, auto_stop=auto_stop)


def sweep_result(stamina, runs=1, animation_finished=True):
    return dict(page='sweep_result', stamina=stamina, completed_runs=runs,
                animation_finished=animation_finished)


def advance(ready=False, selected=5):
    return dict(page='equipment', selected_slot=selected, can_advance=ready)


def filled_slots(count=6):
    return [state for _ in range(count) for state in
            (dict(page='material_info'), dict(page='equipment'))]


def run(*answers, config=None):
    client = Client()
    model = Model(*answers)
    handle_equipment_upgrade(client, config or action(), model=model,
                             template_matcher=matcher, sleep=lambda _: None)
    return client, model


def test_all_six_slots_are_opened_left_to_right_before_one_advance_check(caplog):
    with caplog.at_level('INFO', logger='naruto_daily'):
        client, model = run(overview(), *filled_slots(), advance(False))
    assert client.taps == [EQUIPMENT_POINTS[4]] + [p for slot in MATERIAL_POINTS for p in (slot, BLANK)]
    assert model.prompts.count(prompts.MATERIAL_ENTRY) == 6
    assert model.prompts.count(prompts.ADVANCE) == 1
    assert model.prompts[-1] == prompts.ADVANCE
    assert '材料6/6' in caplog.text


def test_mixed_slots_synthesize_sweep_equip_then_advance_and_select_new_lowest():
    client, model = run(
        overview(), *filled_slots(1),
        material('synthesize'), material('equip'), dict(page='equipment'),
        material('sweep'), setup(20), sweep_result(15), dict(page='material'),
        material('synthesize'), material('equip'), dict(page='equipment'),
        material('equip'), dict(page='equipment'), *filled_slots(2), advance(True),
        overview([85, 45, 23, 85, 50, 78]), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment'),
    )
    assert [p for p in client.taps if p in MATERIAL_POINTS] == [*MATERIAL_POINTS, MATERIAL_POINTS[0]]
    assert [p for p in client.taps if p in EQUIPMENT_POINTS] == [EQUIPMENT_POINTS[4], EQUIPMENT_POINTS[2]]
    advance_index = client.taps.index(ADVANCE)
    assert client.taps[advance_index - 2:advance_index] == [MATERIAL_POINTS[5], BLANK]
    assert client.taps.count(START_SWEEP) == 1
    assert client.taps.count(MATERIAL_BUTTON) == 7
    assert client.taps.count(ADVANCE) == 1
    assert model.prompts.count(prompts.ADVANCE) == 1
    assert model.prompts[:model.prompts.index(prompts.ADVANCE)].count(prompts.MATERIAL_ENTRY) == 6


def test_no_advance_after_material_operation_starts_another_full_round():
    client, model = run(
        overview(), material('equip'), dict(page='equipment'), *filled_slots(5), advance(False),
        overview(), *filled_slots(), advance(False),
    )
    assert [p for p in client.taps if p in MATERIAL_POINTS] == list(MATERIAL_POINTS) * 2
    assert model.prompts.count(prompts.ADVANCE) == 2
    assert ADVANCE not in client.taps


def test_read_only_card_must_close_before_the_next_slot_is_opened():
    client, _ = run(overview(), dict(page='material_info'), dict(page='material_info'),
                    dict(page='equipment'), *filled_slots(5), advance(False))
    assert client.taps[:5] == [EQUIPMENT_POINTS[4], MATERIAL_POINTS[0], BLANK, BLANK, MATERIAL_POINTS[1]]


@pytest.mark.parametrize('bad', [dict(page='unknown'), dict(page='equipment'),
                               dict(page='material', action='unknown')])
def test_unknown_material_entry_does_not_count_as_equipped_or_sweepable(bad):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(overview(), bad, bad),
                                 template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == [EQUIPMENT_POINTS[4], MATERIAL_POINTS[0]]


@pytest.mark.parametrize('bad', [advance(None), advance('true'), advance(1),
                               advance(True, 1), advance(True, True), dict(page='material')])
def test_uncertain_advance_or_wrong_equipment_never_clicks_advance(bad):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(overview(), *filled_slots(), bad, bad),
                                 template_matcher=matcher, sleep=lambda _: None)
    assert [p for p in client.taps if p in MATERIAL_POINTS] == list(MATERIAL_POINTS)
    assert ADVANCE not in client.taps


@pytest.mark.parametrize('advance_rounds', [1, 2])
def test_advance_without_level_change_continues_full_material_round(advance_rounds):
    client, model = run(
        *([overview(), *filled_slots(), advance(True)] * advance_rounds),
        overview(), *filled_slots(), advance(False),
    )
    round_taps = [EQUIPMENT_POINTS[4]] + [p for slot in MATERIAL_POINTS for p in (slot, BLANK)]
    assert client.taps == (round_taps + [ADVANCE]) * advance_rounds + round_taps
    assert model.prompts.count(prompts.ADVANCE) == advance_rounds + 1


def test_sweep_with_partial_materials_rechecks_stamina_on_same_slot_then_finishes():
    client, model = run(
        overview(), material('sweep'), setup(30), sweep_result(20), dict(page='material'),
        material('sweep'), setup(20), sweep_result(10), dict(page='material'),
        material('synthesize'), material('equip'), dict(page='equipment'),
        *filled_slots(5), advance(False), overview(), *filled_slots(), advance(False),
    )
    assert client.taps.count(START_SWEEP) == 2
    first_next_slot = client.taps.index(MATERIAL_POINTS[1])
    assert client.taps[:first_next_slot].count(MATERIAL_POINTS[0]) == 1
    assert model.prompts.count(prompts.STAMINA) == 2


def test_partial_sweep_low_stamina_exits_without_skipping_to_next_slot_or_advancing():
    client, model = run(
        overview(), material('sweep'), setup(33), sweep_result(5, runs=6), dict(page='material'),
        material('sweep'), setup(5), dict(page='material'), dict(page='equipment'),
    )
    assert [p for p in client.taps if p in MATERIAL_POINTS] == [MATERIAL_POINTS[0]]
    assert client.taps.count(START_SWEEP) == 1
    assert client.taps.count(MATERIAL_BUTTON) == 2
    assert prompts.ADVANCE not in model.prompts


def test_unchecked_auto_stop_is_confirmed_once_before_starting_sweep(caplog):
    client = Client()
    observed = []

    class RecordingModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            if prompt == prompts.STAMINA:
                observed.append((png, len(client.taps)))
            return super().complete_json(png, prompt, **kwargs)

    with caplog.at_level('INFO', logger='naruto_daily'):
        handle_equipment_upgrade(client, action(), model=RecordingModel(
            overview(), material('sweep'), setup(20, False), setup(20, False), setup(10),
            sweep_result(5), dict(page='material'), material('sweep'), setup(5),
            dict(page='material'), dict(page='equipment')),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps.count(START_SWEEP) == 1
    assert observed[1] == observed[2] == (repr(AUTO_STOP).encode(), 4)
    assert client.taps.index(START_SWEEP) == 4
    assert '体力读数 10→5' in caplog.text


@pytest.mark.parametrize('bad', [
    sweep_result(10, runs=0), sweep_result(10, runs=True), sweep_result(10, runs='2'),
    sweep_result(None), sweep_result(True), sweep_result(10, animation_finished=None),
    sweep_result(10, animation_finished='true'), dict(page='material'), dict(page='unknown'),
])
def test_uncertain_sweep_result_never_dismisses_or_restarts_sweep(bad):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), material('sweep'), setup(20), bad, bad),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps[-1] == START_SWEEP
    assert BLANK not in client.taps
    assert client.taps.count(START_SWEEP) == 1


def test_animation_finishes_and_result_layer_closes_before_reading_material():
    client = Client()
    checks = []

    class GuardModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            if prompt == prompts.SWEEP_RESULT:
                checks.append(client.taps.count(BLANK))
            if prompt == prompts.MATERIAL:
                assert client.taps.count(BLANK) == 2
            return super().complete_json(png, prompt, **kwargs)

    handle_equipment_upgrade(client, action(), model=GuardModel(
        overview(), material('sweep'), setup(30),
        sweep_result(5, runs=5, animation_finished=False), sweep_result(5, runs=5),
        dict(page='sweep_result'), dict(page='material'), material('sweep'), setup(5),
        dict(page='material'), dict(page='equipment')),
        template_matcher=matcher, sleep=lambda _: None)
    assert checks == [0, 0]
    assert client.taps.count(START_SWEEP) == 1


def test_late_sweep_result_cannot_dismiss_or_click_material(monkeypatch):
    from naruto_daily.common import model_workflow
    now = [0]
    monkeypatch.setattr(model_workflow.time, 'monotonic', lambda: now[0])
    client = Client()

    class LateModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            if prompt == prompts.SWEEP_RESULT:
                now[0] = 150
            return super().complete_json(png, prompt, **kwargs)

    with pytest.raises(VisualError, match='超时'):
        handle_equipment_upgrade(client, action(), model=LateModel(
            overview(), material('sweep'), setup(20), sweep_result(10)),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps[-1] == START_SWEEP


def test_round_limit_stops_repeated_unapplied_material_operation():
    client = Client()
    with pytest.raises(VisualError, match='最多2轮'):
        handle_equipment_upgrade(client, dict(action(), max_rounds=2), model=Model(*(
            [overview(), material('equip'), dict(page='equipment'), *filled_slots(5), advance(False)] * 2)),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(MATERIAL_BUTTON) == 2


@pytest.mark.parametrize('legacy_templates', [False, True])
def test_equipment_config_accepts_model_only_and_legacy_templates(tmp_path, legacy_templates):
    import json
    from naruto_daily.config import load_settings
    root = Path(__file__).parents[1]
    payload = json.loads((root / 'config.example.json').read_text(encoding='utf-8'))
    step = next(a for a in payload['tasks']['equipment_upgrade'] if a['type'] == 'handle_equipment_upgrade')
    assert not {'one_click_sweep', 'start_sweep', 'sweep_done', 'advance'} & step.keys()
    if legacy_templates:
        for key in ('one_click_sweep', 'start_sweep', 'sweep_done', 'advance'):
            step[key] = dict(name=key, template='assets/equipment_' + key + '.png',
                             left=1550, top=740, right=1920, bottom=930, threshold=.9)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert load_settings(path).tasks['equipment_upgrade']


def test_minimum_progression_and_ties_use_row_major_order():
    assert lowest_equipment(overview()) == 5
    assert lowest_equipment(overview([1, 1, 1, 1, 1, 1])) == 1


@pytest.mark.parametrize('levels', [[True, 1, 2, 3, 4, 5], [1, 2], [1, 2, 3, None, 4, 5]])
def test_unreadable_equipment_is_not_guessed(levels):
    with pytest.raises(VisualError):
        lowest_equipment(overview(levels))


def test_material_info_after_synthesis_does_not_trigger_entry_recovery():
    client = Client()
    flow = ModelWorkflow(client, action(), model=Model(
        dict(page='material_info'), dict(page='material_info')), sleep=lambda _: None)
    with pytest.raises(VisualError):
        _finish_material(flow, 'synthesize')
    assert client.taps == [(650, 840)]


@pytest.mark.parametrize('stamina', [0, 4, 5])
@pytest.mark.parametrize('auto_stop', [False, True])
def test_low_stamina_dismisses_both_layers_without_starting(stamina, auto_stop):
    client, _ = run(overview(), material('sweep'), setup(stamina, auto_stop),
                    dict(page='material'), dict(page='equipment'))
    assert CENTERS['start_sweep'] not in client.taps
    assert AUTO_STOP not in client.taps
    assert client.taps[-2:] == [(1600, 920)] * 2


@pytest.mark.parametrize('max_calls', [1, 2, 3])
def test_persistently_unchecked_auto_stop_stops_without_repeating_checkbox(max_calls):
    client = Client()
    recheck_frames = []

    class RecordingModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            if AUTO_STOP in client.taps:
                recheck_frames.append(png)
            return super().complete_json(png, prompt, **kwargs)

    config = dict(action(), multimodal=dict(max_calls=max_calls))
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, config, model=RecordingModel(
            overview(), material('sweep'), setup(20, False),
            *[setup(20, False)] * max_calls), template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(AUTO_STOP) == 1
    assert CENTERS['start_sweep'] not in client.taps
    assert recheck_frames == [repr(AUTO_STOP).encode()] * max_calls


@pytest.mark.parametrize('bad', [
    dict(page='material', stamina=20, auto_stop=True), setup(None),
    setup(20, None), setup(20, 'true'),
])
def test_uncertain_auto_stop_recheck_never_starts_or_repeats_checkbox(bad):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), material('sweep'), setup(20, False), bad, bad),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps[-1] == AUTO_STOP
    assert CENTERS['start_sweep'] not in client.taps


@pytest.mark.parametrize('stamina', [0, 5])
@pytest.mark.parametrize('auto_stop', [False, True])
def test_stamina_falling_to_limit_during_checkbox_recheck_closes_without_sweep(stamina, auto_stop):
    client, _ = run(
        overview(), material('sweep'), setup(20, False), setup(stamina, auto_stop),
        dict(page='material'), dict(page='equipment'),
    )
    assert client.taps.count(AUTO_STOP) == 1
    assert CENTERS['start_sweep'] not in client.taps
    assert client.taps[-2:] == [(1600, 920)] * 2


def test_late_auto_stop_confirmation_cannot_start_sweep(monkeypatch):
    from naruto_daily.common import model_workflow
    now = [0]
    monkeypatch.setattr(model_workflow.time, 'monotonic', lambda: now[0])
    client = Client()
    rechecks = []

    class LateModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            if AUTO_STOP in client.taps:
                rechecks.append(png)
                now[0] = 2000
            return super().complete_json(png, prompt, **kwargs)

    with pytest.raises(VisualError, match='超时'):
        handle_equipment_upgrade(client, action(), model=LateModel(
            overview(), material('sweep'), setup(20, False), setup(20)),
            template_matcher=matcher, sleep=lambda _: None)
    assert rechecks == [repr(AUTO_STOP).encode()]
    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps[-1] == AUTO_STOP
    assert CENTERS['start_sweep'] not in client.taps


def test_checkbox_click_wait_exhausting_timeout_stops_before_recheck_or_sweep():
    client = Client()
    # 前三次点击各等1秒，勾选后最后0.5秒耗尽剩余时间。
    config = dict(action(), timeout_seconds=3.5)
    with pytest.raises(VisualError, match='超时'):
        handle_equipment_upgrade(client, config, model=Model(
            overview(), material('sweep'), setup(20, False)),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps[-1] == AUTO_STOP
    assert CENTERS['start_sweep'] not in client.taps


def test_delayed_synthesis_result_is_rechecked_without_repeating_click():
    client = Client()
    observations = []
    sleeps = []

    class DelayedModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            observations.append((len(client.taps), sum(sleeps)))
            return super().complete_json(png, prompt, **kwargs)

    flow = ModelWorkflow(client, action(), model=DelayedModel(
        material('synthesize'), material('equip'), dict(page='equipment')),
        sleep=sleeps.append)
    _finish_material(flow, 'synthesize')

    assert client.taps == [(650, 840), (650, 840)]  # 合成一次、装备一次
    assert observations[:2] == [(1, 1), (1, 2)]  # 等待后补查，不重复合成


def test_ignored_synthesis_click_retries_only_after_fresh_confirmation():
    client = Client()
    observations = []

    class IgnoredClickModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            observations.append(len(client.taps))
            return super().complete_json(png, prompt, **kwargs)

    flow = ModelWorkflow(client, action(), model=IgnoredClickModel(
        material('synthesize'), material('synthesize'), material('equip'),
        dict(page='equipment')), sleep=lambda _: None)
    _finish_material(flow, 'synthesize')

    assert client.taps == [(650, 840)] * 3  # 两次合成点击、一次装备
    assert observations == [1, 1, 2, 3]


def test_persistently_ignored_synthesis_stops_after_three_clicks():
    client = Client()
    flow = ModelWorkflow(client, action(), model=Model(*[material('synthesize')] * 6),
                         sleep=lambda _: None)
    with pytest.raises(VisualError, match='3次.*装备'):
        _finish_material(flow, 'synthesize')
    assert client.taps == [(650, 840)] * 3


def test_third_synthesis_click_can_succeed_on_final_recheck():
    client = Client()
    flow = ModelWorkflow(client, action(), model=Model(
        *[material('synthesize')] * 5, material('equip'), dict(page='equipment')),
        sleep=lambda _: None)
    _finish_material(flow, 'synthesize')
    assert client.taps == [(650, 840)] * 4  # 最多三次合成，再装备一次


@pytest.mark.parametrize('unexpected', [material('sweep'), material('unknown'), dict(page='equipment')])
def test_synthesis_recheck_does_not_retry_unexpected_or_unknown_state(unexpected):
    client = Client()
    observations = []

    class UnexpectedModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            observations.append(len(client.taps))
            return super().complete_json(png, prompt, **kwargs)

    flow = ModelWorkflow(client, action(), model=UnexpectedModel(
        material('synthesize'), unexpected, unexpected), sleep=lambda _: None)
    with pytest.raises(VisualError):
        _finish_material(flow, 'synthesize')
    assert client.taps == [(650, 840)]
    assert len(observations) >= 2  # 必须经过等待后的实际补查


@pytest.mark.parametrize('late_button', ['synthesize', 'equip'])
def test_synthesis_recheck_timeout_never_triggers_another_click(monkeypatch, late_button):
    from naruto_daily.common import model_workflow
    now = [0]
    monkeypatch.setattr(model_workflow.time, 'monotonic', lambda: now[0])

    class LateRecheckModel:
        calls = 0

        def complete_json(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return material('synthesize')
            now[0] = 2000
            return material(late_button)

    client = Client()
    model = LateRecheckModel()
    flow = ModelWorkflow(client, action(), model=model, sleep=lambda _: None)
    with pytest.raises(VisualError, match='超时'):
        _finish_material(flow, 'synthesize')
    assert model.calls == 2
    assert client.taps == [(650, 840)]


def test_synthesis_recheck_wait_exhausting_timeout_stops_before_model_or_click():
    client = Client()
    # 点击后等1秒，剩余0.5秒供补查等待使用，到期后不得再请求模型。
    config = dict(action(), timeout_seconds=1.5)
    flow = ModelWorkflow(client, config, model=Model(material('synthesize')),
                         sleep=lambda _: None)
    with pytest.raises(VisualError, match='超时'):
        _finish_material(flow, 'synthesize')
    assert client.taps == [(650, 840)]


@pytest.mark.parametrize('bad', [dict(page='material', stamina=200, auto_stop=True),
                               setup(True), setup(None), setup(200, None), setup(200, 'true'),
                               setup(200, 0), setup(200, 1), dict(page='sweep_setup', stamina=200)])
def test_uncertain_stamina_or_wrong_layer_never_starts_sweep(bad):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), material('sweep'), bad, bad),
            template_matcher=matcher, sleep=lambda _: None)
    assert CENTERS['start_sweep'] not in client.taps
    assert AUTO_STOP not in client.taps


@pytest.mark.parametrize('slots', [[], [1], [1, 2, 3], [2, 3]])
def test_takeout_clicks_all_three_boxes_and_repeats_if_any_remain(slots):
    client = Client()
    answers = [dict(page='takeout', claimable_slots=slots)]
    if slots:
        answers.append(dict(page='takeout', claimable_slots=[]))
    handle_ichiraku_takeout(client, action(), model=Model(*answers), sleep=lambda _: None)
    assert client.taps == [(450, 520), (880, 520), (1310, 520)] * (2 if slots else 1)


def test_takeout_unrecognized_page_does_not_start_another_round():
    client = Client()
    with pytest.raises(VisualError):
        handle_ichiraku_takeout(client, action(), model=Model(dict(page='unknown'), dict(page='unknown')),
                               sleep=lambda _: None)
    assert client.taps == [(450, 520), (880, 520), (1310, 520)]


def test_late_model_response_cannot_trigger_click(monkeypatch):
    from naruto_daily.common import model_workflow
    now = [0]
    monkeypatch.setattr(model_workflow.time, 'monotonic', lambda: now[0])
    class LateModel:
        def complete_json(self, *args, **kwargs):
            now[0] += 2000
            return overview()
    client = Client()
    with pytest.raises(VisualError, match='超时'):
        handle_equipment_upgrade(client, action(), model=LateModel(), sleep=lambda _: None)
    assert not client.taps


def test_takeout_reward_overlay_dismissed_after_three_clicks():
    client = Client()
    handle_ichiraku_takeout(client, action(), model=Model(
        dict(page='reward'),
        dict(page='takeout', claimable_slots=[])), sleep=lambda _: None)
    assert client.taps == [(450, 520), (880, 520), (1310, 520), (1550, 740)]


def test_takeout_repeats_three_clicks_after_reward_closes_with_remaining_box():
    client = Client()
    handle_ichiraku_takeout(client, action(), model=Model(
        dict(page='reward'), dict(page='takeout', claimable_slots=[3]),
        dict(page='takeout', claimable_slots=[])), sleep=lambda _: None)
    points = [(450, 520), (880, 520), (1310, 520)]
    assert client.taps == points + [(1550, 740)] + points


@pytest.mark.parametrize('task_name', ['login', 'all'])
def test_daily_order_begins_with_takeout_then_equipment(task_name):
    from naruto_daily.config import load_settings
    from naruto_daily.tasks import build_all, build_task
    settings = load_settings(Path(__file__).parents[1] / 'config.example.json')
    actions = build_all(settings) if task_name == 'all' else build_task(settings, task_name)
    prefix = settings.tasks['login'] + settings.tasks['ichiraku_takeout'] + settings.tasks['equipment_upgrade']
    assert actions[:len(prefix)] == prefix
    assert actions[len(prefix):len(prefix) + len(settings.tasks['organization_pray'])] == settings.tasks['organization_pray']
    assert settings.tasks['ichiraku_takeout'][-1]['type'] == 'return_to_main'
    assert settings.tasks['equipment_upgrade'][-1]['type'] == 'return_to_main'
    farm = build_task(settings, 'secret_realm_farm')
    assert not any(a['type'] in ('handle_ichiraku_takeout', 'handle_equipment_upgrade') for a in farm)


@pytest.mark.parametrize('name', ['ichiraku_takeout', 'equipment_upgrade'])
def test_new_modules_dispatch_and_continue_to_next_task(monkeypatch, name):
    import naruto_daily.runner as runner
    seen = []
    monkeypatch.setattr(runner, 'handle_' + name, lambda *a, **kw: seen.append(name))
    client = Client()
    runner.ActionRunner(client).run([dict(type='handle_' + name), dict(type='tap', x=1, y=2)])
    assert seen == [name]
    assert client.taps == [(1, 2)]


@pytest.mark.parametrize('template,frame', [
    ('activities_entry', 'daily_home'), ('equipment_entry', 'daily_home'),
    ('equipment_submenu', 'equipment_submenu'), ('ichiraku_tab', 'takeout_live'),
    ('equipment_advance', 'equipment_all_ready'), ('equipment_one_click_sweep', 'equipment_material'),
    ('equipment_start_sweep', 'equipment_sweep_setup'), ('equipment_sweep_done', 'equipment_sweep_result'),
])
def test_templates_match_original_adb_resolution(template, frame):
    root = Path(__file__).parents[1]
    png = (root / 'tests' / 'fixtures' / (frame + '.png')).read_bytes()
    center, score = find_template_in_png(png, root / 'assets' / (template + '.png'), (0, 0, 1920, 1080), .9)
    assert center is not None, score


@pytest.mark.parametrize('field,value', [('max_rounds', True), ('max_rounds', 0),
                                      ('sweep_timeout_seconds', -1), ('timeout_seconds', 0)])
def test_equipment_rejects_invalid_limits(tmp_path, field, value):
    import json
    from naruto_daily.config import ConfigError, load_settings
    root = Path(__file__).parents[1]
    settings = load_settings(root / 'config.example.json')
    payload = dict(adb_path=str(settings.adb_path), serial=None,
                   screen=dict(width=1920, height=1080), tasks=settings.tasks)
    step = next(a for a in payload['tasks']['equipment_upgrade'] if a['type'] == 'handle_equipment_upgrade')
    step[field] = value
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ConfigError, match=field):
        load_settings(path)
