import asyncio
import logging
import re

import httpx2 as httpx
import pytest

from naruto_daily.multimodal import transport
from naruto_daily.multimodal.diagnostics import model_request_context


URL = 'https://example.test/v1/chat/completions'


def request_ids(messages):
    return set(re.findall(r'\brequest_id=([^\s,;，；]+)', '\n'.join(messages)))


@pytest.mark.parametrize('error_class', [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_async_timeout_logs_original_transport_type_and_stable_retry_id(
        monkeypatch, caplog, error_class):
    calls, waits = [], []
    caplog.set_level(logging.INFO, logger='naruto_daily')

    async def wait(seconds):
        waits.append(seconds)

    async def handle(request):
        calls.append(request)
        raise error_class('private-key https://private.invalid', request=request)

    async def run():
        with model_request_context('毒风封印导航'):
            async with transport.ModelAsyncClient(
                    request_timeout=30, transport=httpx.MockTransport(handle)) as client:
                with pytest.raises(httpx.ReadTimeout):
                    await client.post(URL, json={'prompt': 'private-prompt'},
                                      headers={'Authorization': 'Bearer private-key'})

    monkeypatch.setattr(transport.asyncio, 'sleep', wait)
    asyncio.run(run())

    assert len(calls) == 4 and waits == [10, 10, 10]
    messages = [item.getMessage() for item in caplog.records
                if 'request_id=' in item.getMessage()]
    final_log = next(item for item in messages if 'stop_reason=retries_exhausted' in item)
    assert f'error_type={error_class.__name__}' in final_log
    assert f'error_kind={error_class.__name__.removesuffix("Timeout").lower()}_timeout' in final_log
    assert 'scene=毒风封印导航' in final_log and 'attempt=4/4' in final_log
    assert 'retries=3/3' in final_log
    assert len(request_ids(messages)) == 1
    for secret in ['private-key', 'private.invalid', 'private-prompt']:
        assert secret not in caplog.text


def test_async_concurrent_scenes_keep_separate_request_ids_and_restore_outer_context(caplog):
    caplog.set_level(logging.INFO, logger='naruto_daily')

    async def run():
        entered = []
        both_entered = asyncio.Event()

        async def handle(request):
            entered.append(request)
            if len(entered) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            return httpx.Response(200, json={'ok': True})

        async with transport.ModelAsyncClient(
                request_timeout=30, transport=httpx.MockTransport(handle)) as client:
            async def identify(scene):
                with model_request_context(scene):
                    response = await client.post(URL, json={'prompt': 'private-prompt'})
                    assert response.status_code == 200

            with model_request_context('外层场景'):
                await asyncio.gather(identify('登录收尾'), identify('导航观察'))
                response = await client.post(URL, json={})
                assert response.status_code == 200

    asyncio.run(run())

    messages = [item.getMessage() for item in caplog.records
                if 'request_id=' in item.getMessage()]
    ids = []
    for scene in ['登录收尾', '导航观察', '外层场景']:
        scene_messages = [item for item in messages if f'scene={scene}' in item]
        assert len(scene_messages) >= 2
        found = request_ids(scene_messages)
        assert len(found) == 1
        ids.extend(found)
        assert any('status=200' in item or 'http_status=200' in item for item in scene_messages)
    assert len(set(ids)) == 3
    assert 'private-prompt' not in caplog.text
