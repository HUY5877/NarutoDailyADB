"""选择与生成分别走独立协议；本地 JEV 配置传递到实际任务子进程。"""
import io
import json
import os

import cv2
import numpy as np
import pytest

from naruto_daily.common import environment
from naruto_daily.multimodal.client import MultimodalClient, MultimodalConfigurationError
from naruto_daily.multimodal.decisions import ChoicePrompt


def test_choice_uses_jev_without_consuming_generation_route(monkeypatch, configured_environment):
    monkeypatch.setenv('JEV_API_KEY', 'jev-private')
    sent = []

    def open_request(request, timeout):
        sent.append(request.full_url)
        assert request.get_header('Authorization') == 'Bearer jev-private'
        assert 'messages' not in json.loads(request.data)
        return io.BytesIO(json.dumps({'answers': {'decision': {'choice': '无法确认'}}}).encode())

    model = MultimodalClient({'timeout_seconds': 10}, open_request=open_request)
    prompt = ChoicePrompt('只判断当前页面', {'无法确认': {'page': 'unknown', 'found': False}})
    frame = cv2.imencode('.png', np.zeros((360, 640, 3), np.uint8))[1].tobytes()
    assert model.complete_json(frame, prompt) == {'page': 'unknown', 'found': False}
    assert len(sent) == 1 and sent[0].endswith('/api/alpha/decisions')
    assert configured_environment.value == 0


def test_missing_jev_key_fails_closed_without_generation_fallback():
    model = MultimodalClient(
        {'timeout_seconds': 10},
        open_request=lambda *args, **kwargs: pytest.fail('缺少 JEV 密钥时禁止请求生成模型'),
    )
    with pytest.raises(MultimodalConfigurationError, match='JEV_API_KEY'):
        model.complete_json(b'png', ChoicePrompt('判断页面', {'unknown': {'page': 'unknown'}}))


def test_coordinate_generation_stays_on_original_gateway(monkeypatch, configured_environment):
    monkeypatch.setenv('JEV_API_KEY', 'jev-private')

    def open_request(request, timeout):
        assert request.full_url.endswith('/chat/completions')
        assert request.get_header('Authorization') == 'Bearer test-secret'
        return io.BytesIO(json.dumps({'choices': [{
            'finish_reason': 'stop', 'message': {'content': '{"center":[100,200]}'},
        }]}).encode())

    model = MultimodalClient({'timeout_seconds': 10}, open_request=open_request)
    assert model.complete_json(b'png', '识别实际按钮中心坐标') == {'center': [100, 200]}
    assert configured_environment.value == 1


def test_local_jev_key_overrides_shared_cloud_and_is_inherited(monkeypatch, tmp_path):
    from naruto_daily.common import cloudflare_kv
    monkeypatch.setattr(environment, '_initialized', False)
    (tmp_path / '.env').write_text('JEV_API_KEY=local-jev-private\nJEV_MODEL=typesafe/jev-1.14\n', encoding='utf-8')
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: {
        'JEV_API_KEY': 'cloud-jev-private', 'JEV_MODEL': 'shared-model',
        'OSS_ACCESS_KEY_SECRET': 'oss-private',
    })
    environment.initialize_environment()
    child = environment.model_environment_for_child()
    assert child['JEV_API_KEY'] == 'local-jev-private'
    assert child['JEV_MODEL'] == 'typesafe/jev-1.14'
    assert child[environment.INHERITED_CONFIG_MARKER] == '1'
    assert 'OSS_ACCESS_KEY_SECRET' not in child
    assert 'CLOUDFLARE_API_TOKEN' not in child
    assert os.environ['JEV_API_KEY'] == 'local-jev-private'


def test_explicit_empty_jev_key_cannot_use_shared_cloud_key(monkeypatch, tmp_path):
    from naruto_daily.common import cloudflare_kv
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setenv('JEV_API_KEY', '')
    (tmp_path / '.env').write_text('JEV_API_KEY=local-jev-private\n', encoding='utf-8')
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: {'JEV_API_KEY': 'cloud-jev-private'})
    environment.initialize_environment()
    assert environment.read_environment()['JEV_API_KEY'] == ''


def test_jev_key_is_redacted_from_task_and_diagnostic_logs(monkeypatch):
    from naruto_daily.login_service.log_events import model_log_secrets, sanitize_log_message
    monkeypatch.setenv('JEV_API_KEY', 'jev-private-without-key-prefix')
    message = sanitize_log_message('失败 jev-private-without-key-prefix', secrets=model_log_secrets())
    assert 'jev-private' not in message and '[REDACTED]' in message
