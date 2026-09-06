import base64
import io
import json
from http.client import BadStatusLine
from urllib.error import HTTPError, URLError

import pytest

from naruto_daily.multimodal import client as mm


def config(tmp_path):
    (tmp_path / 'key').write_text('test-secret', encoding='utf-8')
    return dict(base_url='https://api.stepfun.com/step_plan/v1', model='step-3.7-flash',
                api_key_file='key', timeout_seconds=20)


def response(content, finish='stop'):
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': finish, 'message': {'content': content}}]}).encode())


def test_multimodal_request_sends_image_and_prompt_and_parses_json(tmp_path):
    def open_request(request, timeout):
        payload = json.loads(request.data)
        assert request.full_url == 'https://api.stepfun.com/step_plan/v1/chat/completions'
        assert request.get_header('Authorization') == 'Bearer test-secret'
        assert timeout == 7
        content = payload['messages'][0]['content']
        assert content[0]['text'] == 'read this picture'
        assert base64.b64decode(content[1]['image_url']['url'].split(',')[1]) == b'png'
        return response('{"nickname":"离落花开花败"}')
    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path, open_request=open_request)
    assert client.complete_json(b'png', 'read this picture', timeout_seconds=7) == {'nickname': '离落花开花败'}


@pytest.mark.parametrize('content,finish', [('not json', 'stop'), ('[]', 'stop'), ('{}', 'length')])
def test_malformed_or_truncated_response_is_rejected(tmp_path, content, finish):
    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path,
                                 open_request=lambda *a, **k: response(content, finish))
    with pytest.raises(mm.MultimodalError):
        client.complete_json(b'png', 'prompt')


@pytest.mark.parametrize('error', [HTTPError('https://example.test', 401, 'test-secret', {}, None),
                                 URLError('test-secret'), TimeoutError('test-secret'), BadStatusLine('test-secret')])
def test_api_errors_do_not_expose_credentials(tmp_path, error):
    def fail(*a, **k):
        raise error
    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path, open_request=fail)
    with pytest.raises(mm.MultimodalError) as raised:
        client.complete_json(b'png', 'prompt')
    assert 'test-secret' not in str(raised.value)


def test_missing_key_fails_before_network(tmp_path):
    settings = config(tmp_path)
    (tmp_path / 'key').unlink()
    with pytest.raises(mm.MultimodalError, match='密钥'):
        mm.MultimodalClient(settings, asset_root=tmp_path)
