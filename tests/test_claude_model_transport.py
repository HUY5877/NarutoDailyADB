from http.server import HTTPServer
import pytest
from naruto_daily.multimodal.agent import claude_runner


@pytest.mark.parametrize('base_url', ['https://example.test', 'https://example.test/v1'])
def test_claude_sdk_connects_directly_without_local_listener(monkeypatch, base_url):
    tools = {}
    options = []

    def server(**kwargs):
        tools.update({tool.name: tool for tool in kwargs['tools']})
        return {}

    def forbidden(*args, **kwargs):
        pytest.fail('模型调用不得启动本地监听端口')

    monkeypatch.setattr(HTTPServer, 'server_bind', forbidden)

    class SDK:
        def __init__(self, *, options):
            self.options = options

        async def __aenter__(self):
            options.append(self.options)
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, messages):
            async for _ in messages:
                pass
            env = self.options.env
            assert env['CLAUDE_CODE_MAX_RETRIES'] == '3'
            assert env['API_TIMEOUT_MS'] == '30000'
            assert env['ANTHROPIC_AUTH_TOKEN'] == 'private-key'
            assert env['ANTHROPIC_BASE_URL'] == 'https://example.test'

        async def receive_response(self):
            await tools['finish'].handler({'observation': '已到达', 'status': 'reached'})
            yield object.__new__(claude_runner.ResultMessage)

    class Device:
        def screenshot(self):
            return b'png'

        def swipe(self, *args):
            raise AssertionError('本次已到达，不得移动')

    class Model:
        def agent_connection(self):
            return dict(base_url=base_url, api_key='private-key', model='offline', timeout_seconds=30)

    monkeypatch.setattr(claude_runner, 'create_sdk_mcp_server', server)
    monkeypatch.setattr(claude_runner, 'ClaudeSDKClient', SDK)
    result = claude_runner.run_agent(Device(), Model(), '到达目标', joystick=claude_runner._Joystick(200, 800, 80))
    assert result.reached and result.steps == 0
    assert len(options) == 1
