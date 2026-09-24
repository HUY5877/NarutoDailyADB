from pathlib import Path

import pytest

from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.equipment.handler import handle_equipment_upgrade, lowest_equipment, first_material, _finish_material
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

    def complete_json(self, png, prompt, **kwargs):
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer


def target(name):
    return dict(name=name, template=name + '.png', left=0, top=0, right=1920,
                bottom=1080, threshold=.9)


def action():
    return dict(timeout_seconds=1800, max_rounds=100, poll_seconds=1,
                post_click_wait_seconds=1, sweep_timeout_seconds=120,
                multimodal=dict(max_calls=2),
                **{key: target(key) for key in ('one_click_sweep', 'start_sweep', 'sweep_done', 'advance')})


CENTERS = {'one_click_sweep': (650, 840), 'start_sweep': (1300, 780),
           'sweep_done': (1300, 900), 'advance': (1750, 830)}
AUTO_STOP = (1110, 785)


def matcher(png, path, roi, threshold):
    if path.stem == 'sweep_done' and png == b'(1600, 920)':
        return None, .1
    return CENTERS[path.stem], 1


def overview(levels=None):
    return dict(page='equipment', progressions=levels or [85, 45, 23, 85, 2, 78], selected_slot=5)


def materials(states=None, advance=False):
    return dict(page='equipment', selected_slot=5,
                materials=states or ['sweepable', 'filled', 'filled', 'filled', 'filled', 'sweepable'],
                can_advance=advance)


def material(button):
    return dict(page='material', action=button)


def setup(stamina, auto_stop=True):
    return dict(page='sweep_setup', stamina=stamina, auto_stop=auto_stop)


def sweep_result(stamina, runs=1, animation_finished=True):
    return dict(page='sweep_result', stamina=stamina, completed_runs=runs,
                animation_finished=animation_finished)


@pytest.mark.parametrize('next_stamina', [330, 335])
def test_completed_sweeps_do_not_require_stamina_to_decrease_between_rounds(next_stamina):
    client, _ = run(
        overview(), materials(), material('sweep'), setup(330),
        sweep_result(325), dict(page='material'), material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(next_stamina),
        sweep_result(next_stamina - 5), dict(page='material'), material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment'),
    )
    assert client.taps.count(CENTERS['start_sweep']) == 2


def test_zero_completed_runs_never_dismisses_or_restarts_sweep():
    client = Client()
    with pytest.raises(VisualError, match='实际完成'):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(), material('sweep'), setup(330),
            sweep_result(330, runs=0), sweep_result(330, runs=0)),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps[-1] == CENTERS['start_sweep']
    assert client.taps.count(CENTERS['start_sweep']) == 1


def test_animation_is_confirmed_finished_before_first_result_dismissal():
    client = Client()
    checks = []

    class AnimationModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            answer = super().complete_json(png, prompt, **kwargs)
            if isinstance(answer, dict) and 'animation_finished' in answer:
                checks.append(client.taps.count((1600, 920)))
            return answer

    handle_equipment_upgrade(client, action(), model=AnimationModel(
        overview(), materials(), material('sweep'), setup(330),
        sweep_result(210, runs=24, animation_finished=False), sweep_result(210, runs=24),
        dict(page='material'), material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment')),
        template_matcher=matcher, sleep=lambda _: None)
    assert checks == [0, 0]
    assert client.taps.count(CENTERS['start_sweep']) == 1


def test_hidden_continue_button_does_not_mean_result_layer_was_closed():
    client = Client()

    class ForegroundModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            answer = super().complete_json(png, prompt, **kwargs)
            if isinstance(answer, dict) and answer.get('action') == 'synthesize':
                assert client.taps.count((1600, 920)) == 2
            return answer

    handle_equipment_upgrade(client, action(), model=ForegroundModel(
        overview(), materials(), material('sweep'), setup(330), sweep_result(210, runs=24),
        dict(page='sweep_result'), dict(page='material'),
        material('synthesize'), material('equip'), dict(page='equipment'),
        overview(), materials(['filled'] * 5 + ['sweepable']), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment')),
        template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(CENTERS['start_sweep']) == 1


@pytest.mark.parametrize('bad', [
    sweep_result(210, runs=True), sweep_result(210, runs=-1), sweep_result(210, runs='24'),
    sweep_result(None), sweep_result(True), sweep_result(210, animation_finished=None),
    sweep_result(210, animation_finished='true'), dict(page='material'),
])
def test_uncertain_sweep_result_cannot_trigger_close_or_material_click(bad):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(), material('sweep'), setup(330), bad, bad),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps[-1] == CENTERS['start_sweep']
    assert client.taps.count(CENTERS['start_sweep']) == 1


def test_late_sweep_result_does_not_dismiss_or_start_material_operations(monkeypatch):
    from naruto_daily.common import model_workflow
    now = [0]
    monkeypatch.setattr(model_workflow.time, 'monotonic', lambda: now[0])
    client = Client()

    class LateResult(Model):
        def complete_json(self, png, prompt, **kwargs):
            answer = super().complete_json(png, prompt, **kwargs)
            if isinstance(answer, dict) and answer.get('page') == 'sweep_result':
                assert kwargs['timeout_seconds'] <= action()['sweep_timeout_seconds']
                now[0] = 150
            return answer

    with pytest.raises(VisualError, match='超时'):
        handle_equipment_upgrade(client, action(), model=LateResult(
            overview(), materials(), material('sweep'), setup(330), sweep_result(210, runs=24)),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps[-1] == CENTERS['start_sweep']


def run(*answers, config=None, match=matcher):
    client = Client()
    result = handle_equipment_upgrade(client, config or action(), model=Model(*answers),
                                      template_matcher=match, sleep=lambda _: None)
    return client, result


def test_minimum_progression_and_ties_use_row_major_order():
    assert lowest_equipment(overview()) == 5
    assert lowest_equipment(overview([1, 1, 1, 1, 1, 1])) == 1


@pytest.mark.parametrize('levels', [[True, 1, 2, 3, 4, 5], [1, 2], [1, 2, 3, None, 4, 5]])
def test_unreadable_equipment_is_not_guessed(levels):
    with pytest.raises(VisualError):
        lowest_equipment(overview(levels))


def test_material_selection_uses_leftmost_missing_slot_and_requires_selected_equipment():
    assert first_material(materials(['filled', 'sweepable', 'filled', 'sweepable', 'filled', 'filled']), 5) == 2
    assert first_material(materials(['filled'] * 6, True), 5) is None
    with pytest.raises(VisualError):
        first_material(materials(), 1)
    with pytest.raises(VisualError):
        first_material(materials(['filled'] * 6, False), 5)


def test_filled_material_info_is_closed_and_next_missing_slot_is_opened():
    before = materials(['filled', 'equipable', 'filled', 'sweepable', 'sweepable', 'sweepable'])
    after = materials(['filled', 'filled', 'filled', 'sweepable', 'sweepable', 'sweepable'])
    client, _ = run(
        overview(), before, dict(page='material_info'), dict(page='equipment'), after,
        material('sweep'), setup(4), dict(page='material'), dict(page='equipment'),
    )
    assert client.taps == [
        (525, 610), (1244, 690), (1600, 920), (1518, 690),
        CENTERS['one_click_sweep'], (1600, 920), (1600, 920),
    ]


def test_last_filled_material_info_leads_to_advance_after_verification():
    client, _ = run(
        overview(), materials(['filled'] * 5 + ['equipable']),
        dict(page='material_info'), dict(page='equipment'), materials(['filled'] * 6, True),
        overview([85, 45, 23, 85, 3, 78]), materials(), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment'),
    )
    assert client.taps[:4] == [(525, 610), (1792, 690), (1600, 920), CENTERS['advance']]
    assert client.taps.count((1792, 690)) == 1


def test_multiple_filled_info_cards_continue_forward_without_reopening_them():
    client, _ = run(
        overview(), materials(['filled', 'equipable', 'equipable', 'sweepable', 'filled', 'filled']),
        dict(page='material_info'), dict(page='material_info'), dict(page='equipment'),
        materials(['filled', 'filled', 'equipable', 'sweepable', 'filled', 'filled']),
        dict(page='material_info'), dict(page='equipment'),
        materials(['filled', 'filled', 'filled', 'sweepable', 'filled', 'filled']),
        material('sweep'), setup(4), dict(page='material'), dict(page='equipment'),
    )
    assert client.taps == [
        (525, 610), (1244, 690), (1600, 920), (1600, 920),
        (1380, 690), (1600, 920), (1518, 690), CENTERS['one_click_sweep'],
        (1600, 920), (1600, 920),
    ]


def test_info_verification_cannot_reopen_an_earlier_confirmed_filled_slot():
    client = Client()
    with pytest.raises(VisualError, match='已确认的成品格'):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(['filled', 'equipable', 'filled', 'sweepable', 'filled', 'filled']),
            dict(page='material_info'), dict(page='equipment'),
            materials(['equipable', 'filled', 'filled', 'sweepable', 'filled', 'filled'])),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == [(525, 610), (1244, 690), (1600, 920)]


@pytest.mark.parametrize('state', ['equipable', 'sweepable', 'craftable'])
def test_material_info_does_not_skip_slot_without_filled_confirmation(state):
    client = Client()
    before = materials(['filled', 'equipable', 'filled', 'sweepable', 'sweepable', 'sweepable'])
    after = materials(['filled', state, 'filled', 'sweepable', 'sweepable', 'sweepable'])
    with pytest.raises(VisualError, match='第2格.*已装备'):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), before, dict(page='material_info'), dict(page='equipment'), after),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == [(525, 610), (1244, 690), (1600, 920)]


@pytest.mark.parametrize('after', [
    dict(materials(), selected_slot=1), dict(page='unknown'), dict(page='material_info'),
])
def test_material_info_requires_fresh_same_equipment_verification(after):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(), dict(page='material_info'), dict(page='equipment'), after, after),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == [(525, 610), (1106, 690), (1600, 920)]


def test_unknown_material_entry_is_not_treated_as_read_only_info():
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(), dict(page='unknown'), dict(page='unknown')),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == [(525, 610), (1106, 690)]


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
    client, _ = run(overview(), materials(), material('sweep'), setup(stamina, auto_stop),
                    dict(page='material'), dict(page='equipment'))
    assert CENTERS['start_sweep'] not in client.taps
    assert AUTO_STOP not in client.taps
    assert client.taps[-2:] == [(1600, 920)] * 2


def test_sweep_synthesize_equip_reselect_then_stop():
    client, _ = run(
        overview(), materials(), material('sweep'), setup(6),
        sweep_result(1), dict(page='material'),
        material('synthesize'), material('equip'), dict(page='equipment'),
        overview(), materials(['filled'] * 5 + ['sweepable']), material('sweep'), setup(1),
        dict(page='material'), dict(page='equipment'),
    )
    assert client.taps.count(CENTERS['start_sweep']) == 1
    assert client.taps.count((525, 610)) == 2
    assert client.taps.count((650, 840)) == 4  # 两次打开扫荡、合成、装备
    assert CENTERS['sweep_done'] not in client.taps  # 继续扫荡只作完成标记
    assert AUTO_STOP not in client.taps  # 已勾选时不得再次切换


def test_unchecked_auto_stop_is_clicked_once_and_freshly_confirmed_before_sweep():
    events = []

    class RecordingClient(Client):
        def screenshot(self):
            png = super().screenshot()
            events.append(('screenshot', png))
            return png

        def tap(self, x, y):
            events.append(('tap', (x, y)))
            super().tap(x, y)

    class RecordingModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            answer = super().complete_json(png, prompt, **kwargs)
            events.append(('answer', answer))
            return answer

    client = RecordingClient()
    handle_equipment_upgrade(client, action(), model=RecordingModel(
        overview(), materials(), material('sweep'), setup(20, False),
        setup(20, False), setup(18), sweep_result(13), dict(page='material'),
        material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment')),
        template_matcher=matcher, sleep=lambda _: None)

    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps.count(CENTERS['start_sweep']) == 1
    checkbox_click = events.index(('tap', AUTO_STOP))
    sweep_click = events.index(('tap', CENTERS['start_sweep']))
    between = events[checkbox_click + 1:sweep_click]
    assert [event[1] for event in between if event[0] == 'answer'] == [setup(20, False), setup(18)]
    assert sum(event == ('screenshot', repr(AUTO_STOP).encode()) for event in between) >= 2
    assert not any(event[0] == 'tap' for event in between)


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
            overview(), materials(), material('sweep'), setup(20, False),
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
            overview(), materials(), material('sweep'), setup(20, False), bad, bad),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps[-1] == AUTO_STOP
    assert CENTERS['start_sweep'] not in client.taps


@pytest.mark.parametrize('stamina', [0, 5])
@pytest.mark.parametrize('auto_stop', [False, True])
def test_stamina_falling_to_limit_during_checkbox_recheck_closes_without_sweep(stamina, auto_stop):
    client, _ = run(
        overview(), materials(), material('sweep'), setup(20, False), setup(stamina, auto_stop),
        dict(page='material'), dict(page='equipment'),
    )
    assert client.taps.count(AUTO_STOP) == 1
    assert CENTERS['start_sweep'] not in client.taps
    assert client.taps[-2:] == [(1600, 920)] * 2


def test_sweep_result_logs_stamina_from_checkbox_recheck(caplog):
    caplog.set_level('INFO', logger='naruto_daily')
    client, _ = run(
        overview(), materials(), material('sweep'), setup(20, False), setup(10),
        sweep_result(5), dict(page='material'), material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment'),
    )
    assert '体力读数 10→5' in caplog.text
    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps.count(CENTERS['start_sweep']) == 1


def test_lower_stamina_on_checkbox_recheck_allows_next_sweep():
    client, _ = run(
        overview(), materials(), material('sweep'), setup(20),
        sweep_result(15), dict(page='material'),
        material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(20, False), setup(10),
        sweep_result(5), dict(page='material'),
        material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment'),
    )
    assert client.taps.count(AUTO_STOP) == 1
    assert client.taps.count(CENTERS['start_sweep']) == 2


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
            overview(), materials(), material('sweep'), setup(20, False), setup(20)),
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
            overview(), materials(), material('sweep'), setup(20, False)),
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


def test_all_materials_ready_advances_then_selects_new_lowest():
    client, _ = run(overview(), materials(['filled'] * 6, True),
                    overview([85, 45, 23, 85, 3, 78]), materials(), material('sweep'), setup(4),
                    dict(page='material'), dict(page='equipment'))
    assert client.taps.count(CENTERS['advance']) == 1


def test_advance_without_change_does_not_repeat_click():
    client = Client()
    with pytest.raises(VisualError, match='进阶.*未变化'):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(['filled'] * 6, True), overview()),
            template_matcher=matcher, sleep=lambda _: None)
    assert client.taps.count(CENTERS['advance']) == 1


@pytest.mark.parametrize('bad', [dict(page='material', stamina=200, auto_stop=True),
                               setup(True), setup(None), setup(200, None), setup(200, 'true'),
                               setup(200, 0), setup(200, 1), dict(page='sweep_setup', stamina=200)])
def test_uncertain_stamina_or_wrong_layer_never_starts_sweep(bad):
    client = Client()
    with pytest.raises(VisualError):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(), material('sweep'), bad, bad),
            template_matcher=matcher, sleep=lambda _: None)
    assert CENTERS['start_sweep'] not in client.taps
    assert AUTO_STOP not in client.taps


def test_missing_completion_marker_can_still_confirm_finished_result():
    def missing_done(png, path, roi, threshold):
        return (None, .1) if path.stem == 'sweep_done' else matcher(png, path, roi, threshold)
    client, _ = run(
        overview(), materials(), material('sweep'), setup(330),
        sweep_result(210, runs=24), dict(page='material'),
        material('synthesize'), material('equip'), dict(page='equipment'),
        overview(), materials(['filled'] * 5 + ['sweepable']), material('sweep'), setup(4),
        dict(page='material'), dict(page='equipment'), match=missing_done)
    assert client.taps.count(CENTERS['start_sweep']) == 1


def test_missing_result_never_dismisses_or_restarts_active_sweep():
    client = Client()
    def missing_done(png, path, roi, threshold):
        return (None, .1) if path.stem == 'sweep_done' else matcher(png, path, roi, threshold)
    with pytest.raises(VisualError, match='扫荡完成结果核验'):
        handle_equipment_upgrade(client, action(), model=Model(
            overview(), materials(), material('sweep'), setup(10),
            dict(page='unknown'), dict(page='unknown')),
            template_matcher=missing_done, sleep=lambda _: None)
    assert client.taps[-1] == CENTERS['start_sweep']


@pytest.mark.parametrize('slots', [[], [1, 2, 3], [2, 3]])
def test_takeout_clicks_leftmost_claimable_box_once_and_verifies(slots):
    client = Client()
    answers = [dict(page='takeout', claimable_slots=slots)]
    if slots:
        answers.append(dict(page='takeout', claimable_slots=[]))
    handle_ichiraku_takeout(client, action(), model=Model(*answers), sleep=lambda _: None)
    assert client.taps == ([] if not slots else [({1: 450, 2: 880, 3: 1310}[slots[0]], 520)])


def test_takeout_unrecognized_page_does_not_click():
    client = Client()
    with pytest.raises(VisualError):
        handle_ichiraku_takeout(client, action(), model=Model(dict(page='unknown'), dict(page='unknown')),
                               sleep=lambda _: None)
    assert not client.taps


def test_sweep_with_partial_materials_reopens_and_checks_stamina_again():
    client, _ = run(overview(), materials(), material('sweep'), setup(33),
                    sweep_result(5, runs=6), dict(page='material'),
                    material('sweep'), dict(page='equipment'),
                    overview(), materials(), material('sweep'), setup(4),
                    dict(page='material'), dict(page='equipment'))
    assert client.taps.count(CENTERS['start_sweep']) == 1
    assert client.taps.count(CENTERS['one_click_sweep']) == 2


def test_ignored_result_dismiss_is_retried_before_reading_material():
    client = Client()
    def delayed_dismiss(png, path, roi, threshold):
        if path.stem == 'sweep_done':
            return (None, .1) if client.taps.count((1600, 920)) >= 2 else (CENTERS['sweep_done'], 1)
        return matcher(png, path, roi, threshold)
    class GuardModel(Model):
        def complete_json(self, png, prompt, **kwargs):
            if '根据左面板底部' in prompt and CENTERS['start_sweep'] in client.taps:
                assert client.taps.count((1600, 920)) >= 2, '扫荡结果弹层仍未关闭'
            return super().complete_json(png, prompt, **kwargs)
    handle_equipment_upgrade(client, action(), model=GuardModel(
        overview(), materials(), material('sweep'), setup(6), sweep_result(1), dict(page='material'),
        material('sweep'), dict(page='equipment'),
        overview(), materials(), material('sweep'), setup(4), dict(page='material'), dict(page='equipment')),
        template_matcher=delayed_dismiss, sleep=lambda _: None)


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


def test_takeout_reward_overlay_dismissed_after_single_claim():
    client = Client()
    handle_ichiraku_takeout(client, action(), model=Model(
        dict(page='takeout', claimable_slots=[1, 2, 3]), dict(page='reward'),
        dict(page='takeout', claimable_slots=[])), sleep=lambda _: None)
    assert client.taps == [(450, 520), (1550, 740)]


def test_takeout_claims_remaining_box_if_game_does_not_claim_all_at_once():
    client = Client()
    handle_ichiraku_takeout(client, action(), model=Model(
        dict(page='takeout', claimable_slots=[2, 3]), dict(page='takeout', claimable_slots=[3]),
        dict(page='takeout', claimable_slots=[])), sleep=lambda _: None)
    assert client.taps == [(880, 520), (1310, 520)]


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
