from pathlib import Path
from collections import Counter

import pytest

from naruto_daily.abundance import finish_abundance_result, handle_abundance
from naruto_daily.abundance.handler import SERVICE_POPUP_DISMISS
from naruto_daily.common.visual import VisualError


@pytest.fixture(autouse=True)
def battle_clock(monkeypatch):
    from naruto_daily.abundance import handler
    from naruto_daily.battle import run_battle
    # 这些流程测试用注入的 sleep 推进时间；实际 ADB 耗时在战斗模块测试中覆盖。
    monkeypatch.setattr(handler, 'run_battle', lambda *a, **kw: run_battle(*a, **kw, clock=lambda: 0))


def target(name):
    return dict(name=name, template=name+'.png', left=0, top=0, right=100, bottom=100, threshold=.85)


def action():
    return dict(type='handle_abundance', timeout_seconds=3, pre_click_wait_seconds=1,
        post_click_wait_seconds=1, load_wait_seconds=10,
        challenge_button=target('challenge'), result_return=target('dismiss'),
        battle=dict(start_timeout_seconds=3, timeout_seconds=30, tap_interval_seconds=.25,
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
        if not client.taps:
            visible='challenge'
        elif len(client.taps)<3:
            visible='battle'
        else:
            visible=path.stem if path.stem in ('result','dismiss') else None
        return ((50,50),.99) if path.stem==visible else (None,.2)
    summary=handle_abundance(client,action(),template_matcher=matcher,sleep=lambda s:events.append(('sleep',s)))
    assert client.taps[0] == (50, 50)
    assert client.taps[-1] == SERVICE_POPUP_DISMISS
    assert Counter(client.taps[1:-1]) == Counter({(1,1):80, (2,2):80, (3,3):80, (4,4):80})
    assert events[:3]==[('sleep',1),('tap',50,50),('sleep',10)]
    assert summary.taps==320


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
    assert Counter(client.taps[1:]) == Counter({(1,1):80, (2,2):80, (3,3):80, (4,4):80})


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


@pytest.mark.parametrize('page', ['abundance', 'home'])
def test_already_left_result_goes_to_return_flow_without_clicks(page):
    client, waits = Client(), []
    model = AfterBattleModel(page)
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
        assert kwargs['pause_every_seconds'] == 30
        assert kwargs['pause_seconds'] == 5
        return BattleSummary(4, 'multimodal')
    def finish(*args, **kwargs):
        assert kwargs.get('model') is model
    monkeypatch.setattr(handler, 'run_battle', battle)
    monkeypatch.setattr(handler, 'finish_abundance_result', finish)
    handle_abundance(Client(), config, template_matcher=lambda *a: ((50, 50), .99),
                     sleep=lambda _: None)


def test_normal_result_prompt_uses_confirmed_click_without_model_request():
    client, waits = Client(), []
    model = AfterBattleModel('unknown')
    finish_abundance_result(client, action(), template_matcher=lambda *a: ((50, 50), .99),
                            model=model, sleep=waits.append)
    assert client.taps == [SERVICE_POPUP_DISMISS] and waits == [1, 1]
    assert model.calls == []


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
