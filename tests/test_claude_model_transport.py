from http.server import HTTPServer
import pytest
from naruto_daily.multimodal.agent import claude_runner
from naruto_daily.secret_realm.navigation import PoisonEvidence


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


@pytest.mark.parametrize('tool_name', ['finish', 'move'])
@pytest.mark.parametrize('prior_moves', [0, 1])
def test_claude_finished_realm_stops_movement_and_preserves_result(monkeypatch, tool_name, prior_moves):
    tools = {}
    swipes = []
    pending = dict(observation='人物蓝圈在封印左侧，向右靠近', page='battle',
                   seal_prompt_visible='yes', boss_visible='no', battle_started='no',
                   visible_text='解开封印，召唤 BOSS')
    finished = dict(observation='显示失败及点击任意位置关闭界面', page='result',
                    seal_prompt_visible='yes', boss_visible='no', battle_started='no',
                    visible_text='失败 点击任意位置关闭界面')

    def server(**kwargs):
        tools.update({tool.name: tool for tool in kwargs['tools']})
        return {}

    class SDK:
        def __init__(self, *, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, messages):
            async for _ in messages:
                pass

        async def receive_response(self):
            for _ in range(prior_moves):
                await tools['move'].handler({**pending, 'direction': 'd'})
            args = {**finished, 'direction': 'd'} if tool_name == 'move' else finished
            await tools[tool_name].handler(args)
            # SDK callbacks can arrive after completion. Neither movement nor
            # stale success evidence may replace the recorded result page.
            await tools['move'].handler({**pending, 'direction': 'w'})
            await tools['finish'].handler({**pending, 'seal_prompt_visible': 'no',
                                           'boss_visible': 'yes', 'visible_text': '毒风 BOSS'})
            yield object.__new__(claude_runner.ResultMessage)

    class Device:
        def screenshot(self):
            return b'png'

        def swipe(self, *args):
            swipes.append(args)

    class Model:
        def agent_connection(self):
            return dict(base_url='https://example.test/v1', api_key='private-key',
                        model='offline', timeout_seconds=30)

    monkeypatch.setattr(claude_runner, 'create_sdk_mcp_server', server)
    monkeypatch.setattr(claude_runner, 'ClaudeSDKClient', SDK)
    result = claude_runner.run_agent(
        Device(), Model(), '解封毒风秘境', joystick=claude_runner._Joystick(200, 800, 80),
        evidence_type=PoisonEvidence, sleep=lambda _: None,
        # Finished takes precedence over exhausting the movement allowance.
        max_steps=1,
    )
    assert not result.reached
    assert result.status == 'finished'
    assert result.steps == prior_moves
    assert result.observation == finished['observation']
    assert len(swipes) == prior_moves
    assert [(step.observation, step.action) for step in result.history] == [
        (pending['observation'], 'd')
    ] * prior_moves
