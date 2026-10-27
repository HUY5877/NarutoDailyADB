from pathlib import Path
from collections import Counter

import pytest

from naruto_daily.abundance import finish_abundance_result, handle_abundance
from naruto_daily.abundance.handler import COMPLETED_TARGET, SERVICE_POPUP_DISMISS
from naruto_daily.common.visual import VisualError


@pytest.fixture(autouse=True)
def battle_clock(monkeypatch):
    from naruto_daily.abundance import handler
    from naruto_daily.battle import run_battle_without_first_skill
    # 这些流程测试用注入的 sleep 推进时间；实际 ADB 耗时在战斗模块测试中覆盖。
    phase_scope = handler.execution_phase_scope
    monkeypatch.setattr(handler, 'execution_phase_scope',
                        lambda seconds, **kw: phase_scope(seconds, clock=lambda: 0, **kw))
    monkeypatch.setattr(handler, 'run_battle_without_first_skill',
                        lambda *a, **kw: run_battle_without_first_skill(*a, **kw, clock=lambda: 0))


def target(name):
    return dict(name=name, template=name+'.png', left=0, top=0, right=100, bottom=100, threshold=.85)


def action():
    return dict(type='handle_abundance', timeout_seconds=3, pre_click_wait_seconds=1,
        post_click_wait_seconds=1, load_wait_seconds=10,
        challenge_button=target('challenge'), result_return=target('dismiss'),
        battle=dict(start_timeout_seconds=3, timeout_seconds=120, tap_interval_seconds=.25,
                    battle_marker=target('battle'), end_targets=[target('result')],
                    buttons=[dict(name=n,x=i,y=i) for i,n in enumerate(('一技能','二技能','大招','普攻'),1)]))


class Client:
    def __init__(self):
        self.taps=[]
    def screenshot(self): return b'screen'
    def tap(self,x,y): self.taps.append((x,y))


def test_challenge_waits_ten_seconds_before_battle_and_dismisses_result():
    client, events = Client(), []
    original=client.tap
    def tap(x,y):
        original(x,y)
        events.append(('tap',x,y))
    client.tap=tap
    def matcher(png,path,roi,threshold):
        if client.taps and client.taps[-1] == SERVICE_POPUP_DISMISS:
            visible = 'abundance_completed'
        elif not client.taps:
            visible='challenge'
        elif len(client.taps)<3:
            visible='battle'
        else:
            visible=path.stem if path.stem in ('result','dismiss') else None
        return ((50,50),.99) if path.stem==visible else (None,.2)
    summary=handle_abundance(client,action(),template_matcher=matcher,sleep=lambda s:events.append(('sleep',s)))
    assert client.taps[0] == (50, 50)
    assert client.taps[-1] == SERVICE_POPUP_DISMISS
    assert Counter(client.taps[1:-1]) == Counter({(2,2):120, (3,3):120, (4,4):120})
    assert events[:3]==[('sleep',1),('tap',50,50),('sleep',10)]
    assert summary.taps==360


def test_missing_challenge_aborts_before_loading_or_attack():
    client, waits = Client(), []
    with pytest.raises(VisualError):
        handle_abundance(client,action(),template_matcher=lambda *args:(None,.2),sleep=waits.append)
    assert client.taps==[] and 10 not in waits


def test_disappearing_result_prompt_is_not_clicked():
    client, waits = Client(), []
    dismiss_checks=0
    def matcher(png,path,roi,threshold):
        nonlocal dismiss_checks
        if path.stem=='dismiss':
            dismiss_checks+=1
            return ((50,50),.99) if dismiss_checks==1 else (None,.2)
        visible='challenge' if not client.taps else ('battle' if len(client.taps)==1 else 'result')
        return ((50,50),.99) if path.stem==visible else (None,.2)
    with pytest.raises(VisualError):
        handle_abundance(client,action(),template_matcher=matcher,sleep=waits.append)
    assert client.taps[0] == (50,50)
    assert Counter(client.taps[1:]) == Counter({(2,2):120, (3,3):120, (4,4):120})


class AfterBattleModel:
    def __init__(self, page, on_call=None):
        self.page = page
        self.calls = []
        self.on_call = on_call

    def complete_json(self, png, prompt, *, timeout_seconds):
        self.calls.append((png, timeout_seconds))
        if self.on_call:
            self.on_call()
        return {'page': self.page}


def test_service_popup_dismissed_once_then_handed_to_close_button_flow():
    from naruto_daily.return_to_main import return_to_main

    client, events = Client(), []
    client.state = 'service_popup'
    client.screenshot = lambda: client.state.encode()
    def tap(x, y):
        events.append(('tap', x, y))
        if client.state == 'service_popup':
            assert (x, y) == (400, 540)  # 实机确认的弹窗左侧空白处
            client.state = 'abundance'
        else:
            assert client.state == 'abundance' and (x, y) == (1832, 51)
            client.state = 'home'
    client.tap = tap
    def matcher(png, path, *args):
        if path.stem == 'abundance_completed' and png == b'abundance':
            return (960, 800), .99
        # 弹窗显示时叉也能匹配到，必须先收起弹窗。
        if path.stem == 'close' and png != b'home':
            return (1832, 51), .99
        if path.stem == 'home' and png == b'home':
            return (1780, 500), .99
        return None, .2

    model = AfterBattleModel('service_popup')
    finish_abundance_result(client, action(), template_matcher=matcher,
                            model=model, sleep=lambda s: events.append(('sleep', s)))
    assert client.state == 'abundance'
    assert events == [('tap', 400, 540), ('sleep', 1)]
    assert model.calls[0][0] == b'service_popup'

    back = dict(main_page_marker=target('home'), close_target=target('close'),
                max_rounds=3, idle_rounds=2, poll_seconds=1)
    return_to_main(client, back, template_matcher=matcher,
                   sleep=lambda s: events.append(('sleep', s)))
    assert client.state == 'home'
    assert [event for event in events if event[0] == 'tap'] == [
        ('tap', 400, 540), ('tap', 1832, 51)]


def test_already_on_home_goes_to_return_flow_without_clicks():
    client, waits = Client(), []
    model = AfterBattleModel('home')
    finish_abundance_result(client, action(), template_matcher=lambda *a: (None, .2),
                            model=model, sleep=waits.append)
    assert client.taps == [] and waits == []
    assert len(model.calls) == 1


@pytest.mark.parametrize('page', ['unknown', 'battle', None, True, 'other_popup', []])
def test_unconfirmed_page_does_not_receive_blank_click(page):
    client = Client()
    with pytest.raises(VisualError):
        finish_abundance_result(client, action(), template_matcher=lambda *a: (None, .2),
                                model=AfterBattleModel(page), sleep=lambda _: None)
    assert client.taps == []


def test_slow_service_popup_response_cannot_trigger_late_dismissal():
    client = Client()
    now = [0.0]
    model = AfterBattleModel('service_popup', on_call=lambda: now.__setitem__(0, 4.0))
    with pytest.raises(VisualError, match='超时'):
        finish_abundance_result(client, action(), template_matcher=lambda *a: (None, .2),
                                model=model, clock=lambda: now[0], sleep=lambda _: None)
    assert client.taps == []


def test_abundance_passes_shared_model_to_result_handler(monkeypatch):
    from naruto_daily.abundance import handler
    from naruto_daily.battle import BattleSummary
    config = action()
    config['multimodal'] = {'timeout_seconds': 3, 'max_calls': 6}
    model = AfterBattleModel('service_popup')
    monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw: model)
    def battle(*args, **kwargs):
        assert kwargs.get('model') is model
        # 调用公共战斗模块的默认节奏，调用方不再单独传入暂停规则。
        assert set(kwargs) == {'template_matcher', 'asset_root', 'sleep', 'model'}
        return BattleSummary(4, 'multimodal')
    def finish(*args, **kwargs):
        assert kwargs.get('model') is model
    monkeypatch.setattr(handler, 'run_battle_without_first_skill', battle)
    monkeypatch.setattr(handler, 'finish_abundance_result', finish)
    handle_abundance(Client(), config, template_matcher=lambda *a: ((50, 50), .99),
                     sleep=lambda _: None)


def test_normal_result_prompt_uses_confirmed_click_without_model_request():
    client, waits = Client(), []
    model = AfterBattleModel('unknown')
    result_checks = []
    def matcher(png, path, *args):
        if path.stem == 'dismiss' and not client.taps:
            result_checks.append(len(waits))
            return (50, 50), .99
        if path.stem == 'abundance_completed' and client.taps == [SERVICE_POPUP_DISMISS]:
            return (960, 800), .99
        return None, .2
    finish_abundance_result(client, action(), template_matcher=matcher,
                            model=model, sleep=waits.append)
    assert client.taps == [SERVICE_POPUP_DISMISS] and waits == [1, 1]
    assert result_checks[-2:] == [0, 1]
    assert model.calls == []


@pytest.mark.parametrize('with_model', [False, True])
def test_local_completed_marker_returns_before_model_or_result_click(with_model):
    client, waits, checked = Client(), [], []
    model = AfterBattleModel('service_popup') if with_model else None
    def matcher(png, path, roi, threshold):
        checked.append(path.as_posix())
        assert path.as_posix() == COMPLETED_TARGET['template']
        return (960, 800), .99

    finish_abundance_result(client, action(), template_matcher=matcher,
                            model=model, sleep=waits.append)

    assert checked == [COMPLETED_TARGET['template']]
    assert client.taps == [] and waits == []
    assert model is None or model.calls == []


@pytest.mark.parametrize('initial_reward_visible', [False, True])
def test_reward_disappearing_during_confirmation_returns_without_clicking(initial_reward_visible):
    client, waits, checked = Client(), [], []
    client.state = b'result'
    client.screenshot = lambda: client.state

    def sleep(seconds):
        waits.append(seconds)
        client.state = b'completed'

    def matcher(png, path, *args):
        checked.append((png, path.stem))
        if png == b'completed' and path.stem == 'abundance_completed':
            return (960, 800), .99
        if png == b'result' and path.stem == 'dismiss' and initial_reward_visible:
            return (50, 50), .99
        return None, .2

    finish_abundance_result(client, action(), template_matcher=matcher, clock=lambda: 0, sleep=sleep)

    assert waits == [1] and client.taps == []
    assert checked[-1] == (b'completed', 'abundance_completed')


@pytest.mark.parametrize('page', ['result_template', 'service_popup', 'abundance'])
def test_result_flow_waits_locally_for_completed_marker_without_more_clicks(page):
    client, waits, waiting_checks = Client(), [], []
    model = AfterBattleModel(page)
    expected_taps = [] if page == 'abundance' else [SERVICE_POPUP_DISMISS]
    def matcher(png, path, *args):
        ready_to_wait = bool(model.calls) if page == 'abundance' else bool(client.taps)
        if ready_to_wait:
            # 进入完成确认后，只识别本地标记，不能再试探关闭奖励或点击其他位置。
            assert path.as_posix() == COMPLETED_TARGET['template']
            assert client.taps == expected_taps
            waiting_checks.append(len(model.calls))
            if len(waiting_checks) >= 3:
                return (960, 800), .99
        elif page == 'result_template' and path.stem == 'dismiss':
            return (50, 50), .99
        return None, .2

    finish_abundance_result(client, dict(action(), timeout_seconds=5), template_matcher=matcher,
                            model=model, clock=lambda: 0, sleep=waits.append)

    expected_model_calls = 0 if page == 'result_template' else 1
    assert waiting_checks == [expected_model_calls] * 3
    assert len(model.calls) == expected_model_calls
    assert client.taps == expected_taps
    assert len(waits) > (2 if page == 'result_template' else 1 if page == 'service_popup' else 0)


@pytest.mark.parametrize('page', ['result_template', 'service_popup', 'abundance'])
def test_missing_completed_marker_times_out_without_model_retry_or_extra_click(page):
    client, waits, waiting_checks = Client(), [], []
    model = AfterBattleModel(page)
    expected_taps = [] if page == 'abundance' else [SERVICE_POPUP_DISMISS]
    def matcher(png, path, *args):
        ready_to_wait = bool(model.calls) if page == 'abundance' else bool(client.taps)
        if ready_to_wait:
            assert path.as_posix() == COMPLETED_TARGET['template']
            assert client.taps == expected_taps
            waiting_checks.append(len(model.calls))
        elif page == 'result_template' and path.stem == 'dismiss':
            return (50, 50), .99
        return None, .2

    with pytest.raises(VisualError, match='今日已完成挑战.*超时'):
        finish_abundance_result(client, action(), template_matcher=matcher,
                                model=model, clock=lambda: 0, sleep=waits.append)

    expected_model_calls = 0 if page == 'result_template' else 1
    assert len(waiting_checks) >= 2
    assert set(waiting_checks) == {expected_model_calls}
    assert len(model.calls) == expected_model_calls
    assert client.taps == expected_taps
    assert sum(waits) == action()['timeout_seconds']


def test_failed_page_recognition_never_dismisses_an_unconfirmed_popup():
    from naruto_daily.multimodal.client import MultimodalError
    client = Client()
    def fail():
        raise MultimodalError('多模态 API 网络异常或请求超时')
    with pytest.raises(MultimodalError):
        finish_abundance_result(client, action(), template_matcher=lambda *a: (None, .2),
                                model=AfterBattleModel('service_popup', on_call=fail),
                                sleep=lambda _: None)
    assert client.taps == []
