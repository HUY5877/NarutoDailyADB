"""主界面决斗场入口先做 JEV 有限判断，再由生成渠道定位坐标。"""
import io
import json

import cv2
import numpy as np

from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.prompt_jev import ARENA_ENTRY_PROMPT, ChoicePrompt
from naruto_daily.multimodal.prompts import arena_entry_prompt


def test_arena_visibility_choices_authorize_only_confirmed_main_entry():
    assert isinstance(ARENA_ENTRY_PROMPT, ChoicePrompt)
    assert list(ARENA_ENTRY_PROMPT.choices.values()) == [
        {'page': 'main', 'arena_visible': True},
        {'page': 'main', 'arena_visible': False},
        {'page': 'unknown', 'arena_visible': False},
    ]
    assert all('center' not in result for result in ARENA_ENTRY_PROMPT.choices.values())


def test_arena_coordinate_prompt_uses_actual_complete_image_dimensions():
    prompt = arena_entry_prompt(1600, 900)
    assert type(prompt) is str
    # JSON object 模式的上游接口要求提示词明确包含 json。
    assert 'json' in prompt.lower()
    assert '1600×900' in prompt
    assert '0 <= x < 1600' in prompt and '0 <= y < 900' in prompt
    assert '{"page":"main","name":"决斗场","center":[x,y]}' in prompt
    assert '{"page":"unknown"}' in prompt


def test_arena_visibility_then_coordinate_requests_use_separate_protocols(
        monkeypatch, configured_environment):
    monkeypatch.setenv('JEV_API_KEY', 'jev-private')
    frame = cv2.imencode('.png', np.zeros((90, 160, 3), np.uint8))[1].tobytes()
    sent = []

    def open_request(request, timeout):
        sent.append(request.full_url)
        payload = json.loads(request.data)
        if len(sent) == 1:
            assert request.full_url.endswith('/api/alpha/decisions')
            assert request.get_header('Authorization') == 'Bearer jev-private'
            assert 'messages' not in payload
            assert set(payload['questions']['decision']['criteria']) == set(ARENA_ENTRY_PROMPT.choices)
            label = next(label for label, result in ARENA_ENTRY_PROMPT.choices.items()
                         if result['arena_visible'])
            return io.BytesIO(json.dumps({'answers': {'decision': {'choice': label}}}).encode())
        assert request.full_url.endswith('/chat/completions')
        assert request.get_header('Authorization') == 'Bearer test-secret'
        assert payload['messages'][0]['content'][0]['text'] == arena_entry_prompt(160, 90)
        return io.BytesIO(json.dumps({'choices': [{
            'finish_reason': 'stop',
            'message': {'content': '{"page":"main","name":"决斗场","center":[80,45]}'},
        }]}).encode())

    model = MultimodalClient({'timeout_seconds': 10}, open_request=open_request)
    assert model.complete_json(frame, ARENA_ENTRY_PROMPT) == {'page': 'main', 'arena_visible': True}
    assert configured_environment.value == 0
    assert model.complete_json(frame, arena_entry_prompt(160, 90)) == {
        'page': 'main', 'name': '决斗场', 'center': [80, 45],
    }
    assert len(sent) == 2 and configured_environment.value == 1
