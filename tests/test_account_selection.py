import json
from pathlib import Path

import pytest

from naruto_daily.login import authorization
from naruto_daily.multimodal.client import MultimodalError
from naruto_daily.multimodal.prompts import ACCOUNT_NICKNAME
from naruto_daily.common.visual import VisualError


def action():
    result = dict(type='authorize_account', account_name='离落花开花败', timeout_seconds=40,
                  poll_seconds=1, pre_click_wait_seconds=3, post_click_wait_seconds=3,
                  multimodal=dict(base_url='https://api.stepfun.com/step_plan/v1', model='step-3.7-flash',
                                  api_key_file='.secrets/stepfun_api_key', timeout_seconds=20, max_calls=6))
    for key in ('picker_marker', 'switch_account', 'agree_button'):
        result[key] = dict(name=key, template=key + '.png', left=0, top=0,
                           right=1920, bottom=1080, threshold=0.95)
    return result


class Client:
    def __init__(self, frames):
        self.frames, self.taps, self.screenshots = frames, [], 0
    def screenshot(self):
        self.screenshots += 1
        return self.frames[min(len(self.taps), len(self.frames) - 1)]
    def tap(self, x, y):
        self.taps.append((x, y))
    def ui_hierarchy(self):
        return '<hierarchy><node package="com.tencent.mobileqq" text="离落花开花败" enabled="true" bounds="[147,962][321,1003]" /></hierarchy>'


def matcher(frame, path, roi, threshold):
    points = {'picker_marker': (150, 797), 'switch_account': (960, 919), 'agree_button': (961, 834)}
    return (points[path.stem], 1) if path.stem in frame else (None, 0.1)


AUTH = {'agree_button', 'switch_account'}
PICKER = {'picker_marker'}
TARGET = {'nickname': '离落花开花败'}



@pytest.fixture
def model(monkeypatch):
    class FakeModel:
        replies = []
        calls = []
        def __init__(self, *args, **kwargs):
            pass
        def complete_json(self, png, prompt, **kwargs):
            self.calls.append((png, prompt, kwargs))
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
    monkeypatch.setattr(authorization, 'MultimodalClient', FakeModel, raising=False)
    return FakeModel


@pytest.mark.parametrize('frames,replies,expected', [
    ([AUTH], [TARGET, TARGET], [(961, 834)]),
    ([PICKER, AUTH], [TARGET, TARGET], [(234, 982), (961, 834)]),
    ([AUTH, PICKER, AUTH], [{'nickname': '其他用户'}] * 2 + [TARGET, TARGET],
     [(960, 919), (234, 982), (961, 834)]),
])
def test_model_extracted_name_controls_switching_and_authorization(model, frames, replies, expected):
    model.replies = replies.copy()
    client = Client(frames)
    authorization.authorize_account(client, action(), template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == expected


def test_changed_nickname_before_agree_never_clicks_agree(model):
    model.replies = [TARGET, {'nickname': '其他用户'}, MultimodalError('停止测试')]
    client = Client([AUTH])
    with pytest.raises(VisualError):
        authorization.authorize_account(client, action(), template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == []


@pytest.mark.parametrize('reply', [{'nickname': None}, {'nickname': ''}, {'nickname': '离落花开花败123'}])
def test_unknown_or_nonidentical_name_never_authorizes(model, reply):
    model.replies = [reply] * 6
    client = Client([PICKER, AUTH])
    with pytest.raises(VisualError):
        authorization.authorize_account(client, action(), template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == [(234, 982)]
    assert len(model.calls) <= 6


def test_unknown_name_retries_a_fresh_screenshot_after_one_second(model):
    model.replies = [{'nickname': None}, TARGET, TARGET]
    client, waits = Client([AUTH]), []
    authorization.authorize_account(client, action(), template_matcher=matcher, sleep=waits.append)
    assert waits[0] == 1 and client.screenshots >= 3
    assert client.taps == [(961, 834)]


def test_api_error_stops_without_authorizing(model):
    model.replies = [MultimodalError('HTTP 401')]
    client = Client([AUTH])
    with pytest.raises(VisualError, match='401'):
        authorization.authorize_account(client, action(), template_matcher=matcher, sleep=lambda _: None)
    assert client.taps == []


def test_network_time_counts_towards_authorization_timeout(model, monkeypatch):
    ticks = [0]
    def slow(*a, **k):
        ticks[0] += 50
        return TARGET
    monkeypatch.setattr(model, 'complete_json', slow)
    client = Client([AUTH])
    with pytest.raises(VisualError):
        authorization.authorize_account(client, action(), template_matcher=matcher,
                                        sleep=lambda _: None, clock=lambda: ticks[0])
    assert client.taps == []


@pytest.mark.parametrize('filename', ['qq_account_selected.png', 'qq_account_selected_no_banner.png',
                                      'qq_account_selected_low.png'])
def test_layout_changes_do_not_require_nickname_image_templates(model, filename):
    from naruto_daily.config import load_settings
    from naruto_daily.runner import ActionRunner
    root = Path(__file__).parents[1]
    configured = load_settings(root / 'config.example.json').tasks['login'][3]
    model.replies = [TARGET, TARGET]
    client = Client([(root / 'tests/fixtures' / filename).read_bytes()])
    ActionRunner(client, asset_root=root, sleep=lambda _: None).run([configured])
    assert client.taps == [(961, 834)]
    assert all(call[1] == ACCOUNT_NICKNAME for call in model.calls)


def test_prompts_do_not_reveal_target_answer():
    assert '离落花开花败' not in ACCOUNT_NICKNAME


def test_dry_run_does_not_read_secret_or_call_model():
    from naruto_daily.runner import ActionRunner
    client = Client([AUTH])
    ActionRunner(client, dry_run=True).run([action()])
    assert client.taps == [] and client.screenshots == 0


@pytest.mark.parametrize('key,value', [('base_url', 'http://example.com'), ('model', ''),
                                      ('api_key_file', '../key'), ('timeout_seconds', True), ('max_calls', 100)])
def test_bad_multimodal_configuration_is_rejected(tmp_path, key, value):
    from naruto_daily.config import load_settings, ConfigError
    root = Path(__file__).parents[1]
    payload = json.loads((root / 'config.example.json').read_text(encoding='utf-8'))
    configured = action()
    configured['multimodal'][key] = value
    payload['tasks']['login'][3] = configured
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ConfigError):
        load_settings(path)
