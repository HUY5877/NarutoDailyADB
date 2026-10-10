import base64
import io
import json
from http.client import BadStatusLine
from urllib.error import HTTPError, URLError

import pytest

from naruto_daily.common import environment
from naruto_daily.multimodal import client as mm
from naruto_daily.multimodal.channels import CHANNELS


def config(tmp_path):
    return dict(base_url=CHANNELS[0].base_url, model='step-3.7-flash',
                timeout_seconds=20)


def write_model_env(directory, *, key='root-key', model='root-model'):
    (directory / '.env').write_text(
        f'YUWAN_API_KEY="{key}"\nYUWAN_MODEL={model}\n'
        'REDIS_URL=redis://test.invalid/0\n',
        encoding='utf-8')


def response(content, finish='stop'):
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': finish, 'message': {'content': content}}]}).encode())


def test_multimodal_request_sends_image_and_prompt_and_parses_json(tmp_path):
    def open_request(request, timeout):
        payload = json.loads(request.data)
        assert request.full_url == CHANNELS[0].base_url + '/chat/completions'
        assert request.get_header('Authorization') == 'Bearer test-secret'
        assert timeout == pytest.approx(7, abs=.01)
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
    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path, open_request=fail,
                                 sleep=lambda _: None, clock=lambda: 0)
    with pytest.raises(mm.MultimodalError) as raised:
        client.complete_json(b'png', 'prompt')
    assert 'test-secret' not in str(raised.value)


def test_missing_cloud_key_does_not_fall_back_to_local_files(tmp_path, monkeypatch):
    settings = config(tmp_path)
    write_model_env(tmp_path, key='old-root-key')
    service = tmp_path / 'service'
    service.mkdir()
    write_model_env(service, key='service-key')
    monkeypatch.delenv('YUWAN_API_KEY', raising=False)
    with pytest.raises(mm.MultimodalError, match='密钥'):
        mm.MultimodalClient(settings, asset_root=service,
                            open_request=lambda *args, **kwargs: pytest.fail('缺少根配置时不得请求'))


@pytest.mark.parametrize('content,finish,reason', [
    ('private-response', 'stop', 'invalid_json_or_response_structure'),
    ('[]', 'stop', 'result_not_object'),
    ('{"secret": "private-response"}', 'length', 'generation_not_complete'),
])
def test_invalid_response_logs_safe_details_with_transport_request_id(tmp_path, caplog, content, finish, reason):
    import re
    from naruto_daily.multimodal.diagnostics import model_request_context

    caplog.set_level('INFO', logger='naruto_daily')
    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path,
                                 open_request=lambda *a, **k: response(content, finish))
    with model_request_context('主界面识别/home'):
        with pytest.raises(mm.MultimodalError):
            client.complete_json(b'private-screenshot', 'private-prompt')

    assert f'reason={reason}' in caplog.text and 'scene=主界面识别/home' in caplog.text
    assert 'status=200' in caplog.text and 'response_bytes=' in caplog.text
    assert len(set(re.findall(r'request_id=(\w+)', caplog.text))) == 1
    for private in ('test-secret', 'private-response', 'private-screenshot', 'private-prompt'):
        assert private not in caplog.text


def test_client_sends_injected_cloud_model_and_key_despite_local_files_and_cwd(tmp_path, monkeypatch):
    root, service, elsewhere = (tmp_path / name for name in ('project', 'service', 'elsewhere'))
    for directory in (root, service, elsewhere):
        directory.mkdir()
    monkeypatch.setattr(environment, 'PROJECT_ROOT', root)
    write_model_env(root)
    write_model_env(service, key='service-key', model='service-model')
    write_model_env(elsewhere, key='cwd-key', model='cwd-model')
    monkeypatch.setenv('YUWAN_API_KEY', 'environment-key')
    monkeypatch.setenv('YUWAN_MODEL', 'environment-model')
    monkeypatch.chdir(elsewhere)

    def open_request(request, timeout):
        assert request.full_url == CHANNELS[0].base_url + '/chat/completions'
        assert request.get_header('Authorization') == 'Bearer environment-key'
        assert json.loads(request.data)['model'] == 'environment-model'
        return response('{}')

    client = mm.MultimodalClient(
        dict(base_url='https://ignored.test/v1', model='ignored-model', timeout_seconds=20),
        asset_root=service, open_request=open_request,
    )
    assert client.complete_json(b'png', 'prompt') == {}
    connection = client.agent_connection()
    assert callable(connection.pop('route_request'))
    assert connection == dict(base_url=CHANNELS[0].base_url, model='environment-model',
                             api_key='environment-key', timeout_seconds=20, protocol='openai')


@pytest.mark.parametrize('key', ['', '   '])
def test_empty_cloud_key_does_not_fall_back_to_local_file(tmp_path, monkeypatch, key):
    settings = config(tmp_path)
    write_model_env(tmp_path, key='old-root-key')
    monkeypatch.setenv('YUWAN_API_KEY', key)
    with pytest.raises(mm.MultimodalError, match='密钥'):
        mm.MultimodalClient(settings, asset_root=tmp_path)


@pytest.mark.parametrize('name', ['YUWAN_API_KEY', 'YUWAN_MODEL'])
@pytest.mark.parametrize('value', [None, ''])
def test_missing_cloud_variable_never_uses_local_file_fallback(tmp_path, monkeypatch, name, value):
    root, service = tmp_path / 'project', tmp_path / 'service'
    root.mkdir()
    service.mkdir()
    monkeypatch.setattr(environment, 'PROJECT_ROOT', root)
    values = dict(YUWAN_API_KEY='root-key', YUWAN_MODEL='root-model',
                  REDIS_URL='redis://test.invalid/0')
    for variable, setting in values.items():
        monkeypatch.setenv(variable, setting)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    (root / '.env').write_text('\n'.join(f'{variable}={setting}' for variable, setting in values.items()), encoding='utf-8')
    write_model_env(service, key='service-key', model='service-model')

    with pytest.raises(mm.MultimodalError, match=name):
        mm.MultimodalClient(
            dict(timeout_seconds=20),
            asset_root=service, open_request=lambda *a, **k: pytest.fail('缺少根配置时不得请求'))


def test_new_client_reuses_startup_channel_configuration(tmp_path, monkeypatch):
    service = tmp_path / 'service'
    service.mkdir()
    write_model_env(service, key='service-key', model='service-model')
    sent = []

    def open_request(request, timeout):
        sent.append((request.full_url, json.loads(request.data)['model'], request.get_header('Authorization')))
        return response('{}')

    settings = dict(timeout_seconds=20)
    for version in ('first', 'second'):
        monkeypatch.setenv('YUWAN_API_KEY', f'{version}-key')
        monkeypatch.setenv('YUWAN_MODEL', f'{version}-model')
        client = mm.MultimodalClient(settings, asset_root=service, open_request=open_request)
        assert client.complete_json(b'png', 'prompt') == {}

    assert sent == [(CHANNELS[0].base_url + '/chat/completions', 'first-model', 'Bearer first-key')] * 2


@pytest.mark.parametrize('failure_status', [None, 400, 401, 418, 529])
def test_client_rebuilds_image_on_each_retry_preserving_prompt_model_and_authorization(tmp_path, failure_status):
    now = [0]
    events = []
    requests = []
    fresh_frames = iter([b'fresh-1', b'fresh-2', b'fresh-3'])

    def refresh():
        frame = next(fresh_frames)
        events.append(('screenshot', now[0], frame))
        return frame

    def sleep(seconds):
        events.append(('wait', seconds))
        now[0] += seconds

    def open_request(request, timeout):
        payload = json.loads(request.data)
        content = payload['messages'][0]['content']
        frame = base64.b64decode(content[1]['image_url']['url'].split(',')[1])
        requests.append((request, payload, frame))
        events.append(('send', now[0], frame))
        if len(requests) < 4:
            if failure_status is not None:
                raise HTTPError(request.full_url, failure_status, 'test-secret', {}, None)
            raise TimeoutError()
        return response('{"finished":true}')

    settings = config(tmp_path)
    client = mm.MultimodalClient(settings, asset_root=tmp_path, open_request=open_request,
                                 sleep=sleep, clock=lambda: now[0])
    assert client.complete_json(b'initial', 'same prompt', refresh_image=refresh) == {'finished': True}

    assert [item[2] for item in requests] == [b'initial', b'fresh-1', b'fresh-2', b'fresh-3']
    assert events == [('send', 0, b'initial'), ('wait', 10), ('screenshot', 10, b'fresh-1'),
                      ('send', 10, b'fresh-1'), ('wait', 10), ('screenshot', 20, b'fresh-2'),
                      ('send', 20, b'fresh-2'), ('wait', 10), ('screenshot', 30, b'fresh-3'),
                      ('send', 30, b'fresh-3')]
    original = requests[0][1]
    for request, payload, _ in requests:
        assert request.full_url == settings['base_url'] + '/chat/completions'
        assert request.get_header('Authorization') == 'Bearer test-secret'
        assert request.get_header('Content-type') == 'application/json'
        assert payload['model'] == settings['model']
        assert payload['messages'][0]['content'][0] == {'type': 'text', 'text': 'same prompt'}
        # Only the screenshot changes; response format and generation settings stay fixed.
        payload['messages'][0]['content'][1] = original['messages'][0]['content'][1]
        assert payload == original


def test_client_successful_first_attempt_does_not_capture_again(tmp_path):
    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path,
                                 open_request=lambda *a, **k: response('{}'))
    assert client.complete_json(
        b'initial', 'prompt', refresh_image=lambda: pytest.fail('首次请求不得补截图')) == {}


def test_client_refresh_failure_never_resends_old_image(tmp_path):
    sent = []
    refreshes = []
    waits = []

    def open_request(request, timeout):
        sent.append(json.loads(request.data))
        raise TimeoutError()

    def refresh():
        refreshes.append(1)
        raise OSError('screenshot failed')

    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path, open_request=open_request,
                                 sleep=waits.append, clock=lambda: 0)
    with pytest.raises(mm.MultimodalRequestError, match='刷新失败'):
        client.complete_json(b'initial', 'prompt', refresh_image=refresh)

    assert len(sent) == 1 and refreshes == [1] and waits == [10]


def test_client_retry_without_refresh_callback_keeps_original_image(tmp_path):
    sent = []
    waits = []

    def open_request(request, timeout):
        sent.append(request)
        if len(sent) == 1:
            raise TimeoutError()
        return response('{}')

    client = mm.MultimodalClient(config(tmp_path), asset_root=tmp_path, open_request=open_request,
                                 sleep=waits.append, clock=lambda: 0)
    assert client.complete_json(b'initial', 'prompt') == {}
    assert len(sent) == 2 and sent[0].data == sent[1].data
    assert waits == [10]
