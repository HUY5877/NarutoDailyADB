from urllib.request import Request, urlopen

from naruto_daily.multimodal import transport
from naruto_daily.multimodal.agent import claude_runner


def test_claude_sdk_uses_shared_transport_with_native_retries_disabled(monkeypatch):
    tools = {}
    options = []
    requests = []

    def server(**kwargs):
        tools.update({tool.name: tool for tool in kwargs['tools']})
        return {}

    def request(req, **kwargs):
        requests.append((req, kwargs))
        return transport.ModelResponse(200, 'text/event-stream', b'event: message_stop\ndata: {}\n\n')

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
            assert env['CLAUDE_CODE_MAX_RETRIES'] == '0'
            assert env['API_TIMEOUT_MS'] == '129000'
            assert env['ANTHROPIC_AUTH_TOKEN'] != 'private-key'
            req = Request(env['ANTHROPIC_BASE_URL'] + '/v1/messages', data=b'{"stream":true}',
                          headers={'Authorization': 'Bearer ' + env['ANTHROPIC_AUTH_TOKEN']})
            with urlopen(req, timeout=2) as response:
                assert response.headers['Content-Type'] == 'text/event-stream'
                assert b'message_stop' in response.read()

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
            return dict(base_url='https://example.test', api_key='private-key', model='offline', timeout_seconds=30)

    monkeypatch.setattr(claude_runner, 'create_sdk_mcp_server', server)
    monkeypatch.setattr(claude_runner, 'ClaudeSDKClient', SDK)
    monkeypatch.setattr(transport, 'request_with_retry', request)
    result = claude_runner.run_agent(Device(), Model(), '到达目标', joystick=claude_runner._Joystick(200, 800, 80))
    assert result.reached and result.steps == 0
    assert len(options) == len(requests) == 1
    upstream, kwargs = requests[0]
    assert upstream.full_url == 'https://example.test/v1/messages'
    assert upstream.get_header('Authorization') == 'Bearer private-key'
    assert kwargs['request_timeout'] == 30
