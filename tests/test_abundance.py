from pathlib import Path
from collections import Counter

import pytest

from naruto_daily.abundance import handle_abundance
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
    assert client.taps[0] == client.taps[-1] == (50,50)
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
