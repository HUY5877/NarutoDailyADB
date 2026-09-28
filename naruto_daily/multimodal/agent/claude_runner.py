"""Claude Agent SDK backend for the screenshot driven navigator."""

from __future__ import annotations

import asyncio
import base64
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)
from pydantic import BaseModel, ConfigDict, Field

from naruto_daily.common.visual import VisualError
from naruto_daily.multimodal.diagnostics import model_request_context, safe_exception_details
from naruto_daily.multimodal.transport import MAX_REQUEST_RETRIES, request_budget
from .completion_watch import CompletionDetected, CompletionWatch

Observation = str
COMPLETION_RETRIES = 3
LOGGER = logging.getLogger('naruto_daily')


def _stop_owned_cli(sdk) -> None:
    """Avoid the SDK's multi-second graceful subprocess shutdown on completion.

    This targets only the transport process created for this navigation session.
    Tool input is already disabled by the completion latch before this is called.
    """
    process = getattr(getattr(sdk, '_transport', None), '_process', None)
    if process is not None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


class AgentError(VisualError):
    """导航无法可靠完成。"""


@dataclass(frozen=True)
class AgentStep:
    observation: str
    action: str


@dataclass(frozen=True)
class AgentResult:
    reached: bool
    steps: int
    status: str
    observation: str
    history: tuple[AgentStep, ...]


class NavigationEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    observation: str = Field(min_length=1, max_length=2000, pattern=r'\S')

    def state(self) -> Literal['pending', 'reached', 'blocked']:
        raise NotImplementedError


class GoalEvidence(NavigationEvidence):
    status: Literal['pending', 'reached', 'blocked']

    def state(self):
        return self.status


@dataclass(frozen=True)
class _Joystick:
    x: int
    y: int
    radius: int
    duration_ms: int = 300

    def move(self, client: Any, direction: str) -> None:
        if direction not in ('w', 'a', 's', 'd'):
            raise AgentError('移动动作只能是单个 w/a/s/d')
        dx, dy = {'w': (0, -1), 'a': (-1, 0), 's': (0, 1), 'd': (1, 0)}[direction]
        client.swipe(self.x, self.y, self.x + dx * self.radius,
                     self.y + dy * self.radius, self.duration_ms)


def run_agent(client, model, goal: str, *, joystick, max_steps: int = 50,
              settle_seconds: float = 0.5, sleep: Callable[[float], None] = __import__('time').sleep,
              evidence_type: type[NavigationEvidence] = GoalEvidence,
              instructions: str = '',
              completion_probe: Callable[[bytes], str | None] | None = None) -> AgentResult:
    """Run one native Claude Agent SDK session with in-process MCP tools."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('目标必须是非空文字')
    if type(max_steps) is not int or not 1 <= max_steps <= 50:
        raise ValueError('max_steps 必须是 1 到 50 的整数')
    if isinstance(settle_seconds, bool) or not isinstance(settle_seconds, (int, float)) \
            or not math.isfinite(settle_seconds) or not 0 <= settle_seconds <= 60:
        raise ValueError('settle_seconds 必须在 0 到 60 秒之间')
    if not isinstance(evidence_type, type) or not hasattr(evidence_type, 'model_fields') or not callable(getattr(evidence_type, 'state', None)):
        raise ValueError('evidence_type 必须定义观察字段和 state 完成条件')
    connection = model.agent_connection()
    timeout = connection['timeout_seconds']
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('模型请求超时必须是正数')

    history: list[AgentStep] = []
    latest_observation = ''
    final_result: AgentResult | None = None
    active_sdk = None
    watch = CompletionWatch(client, completion_probe, on_complete=lambda: _stop_owned_cli(active_sdk))
    initial_png = client.screenshot()

    def result(reached: bool, observation: str) -> AgentResult:
        return AgentResult(reached, len(history), 'reached' if reached else 'max_steps',
                           observation, tuple(history))

    def parse(args: dict[str, Any], *, direction: bool) -> NavigationEvidence:
        nonlocal latest_observation
        if direction:
            expected = {'direction'}
            if set(args) != set(evidence_type.model_fields) | expected:
                raise AgentError('模型导航工具参数不完整或包含未知字段，已停止')
        else:
            if set(args) != set(evidence_type.model_fields):
                raise AgentError('模型完成工具参数不完整或包含未知字段，已停止')
        try:
            evidence = evidence_type.model_validate({k: args[k] for k in evidence_type.model_fields})
        except Exception:
            raise AgentError('模型导航事实格式错误，已停止') from None
        latest_observation = evidence.observation
        state = evidence.state()
        if state == 'blocked':
            raise AgentError(f'当前画面不可导航，停止且不进入战斗：{evidence.observation}')
        if state not in ('pending', 'reached'):
            raise AgentError('导航观察返回无效状态，已停止')
        return evidence

    async def session() -> AgentResult:
        nonlocal final_result, active_sdk

        @tool('move', '观察当前截图事实后，沿一个方向移动一次并返回新截图。',
              {**{name: str for name in evidence_type.model_fields}, 'direction': str})
        async def move(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal final_result
            watch.check_fresh()
            evidence = parse(args, direction=True)
            if evidence.state() == 'reached' or len(history) >= max_steps:
                final_result = result(evidence.state() == 'reached', evidence.observation)
                return {'content': [{'type': 'text', 'text': '目标已确定或已达到步数上限，请调用 finish。'}], 'is_error': True}
            direction = args['direction']
            if direction not in ('w', 'a', 's', 'd'):
                raise AgentError('移动动作只能是单个 w/a/s/d')
            joystick.move(client, direction)
            history.append(AgentStep(evidence.observation, direction))
            sleep(settle_seconds)
            png = client.screenshot()
            watch.check(png)
            return {'content': [
                {'type': 'text', 'text': f'已移动 {len(history)}/{max_steps} 步。只观察这张最新截图后再调用工具。'},
                {'type': 'image', 'data': base64.b64encode(png).decode('ascii'), 'mimeType': 'image/png'},
            ]}

        @tool('finish', '根据当前截图事实结束导航；未完成时不要调用。',
              {name: str for name in evidence_type.model_fields})
        async def finish(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal final_result
            watch.check_fresh()
            evidence = parse(args, direction=False)
            state = evidence.state()
            if state == 'pending' and len(history) < max_steps:
                return {'content': [{'type': 'text', 'text':
                    f'事实仍未满足完成条件；请继续调用 move，剩余 {max_steps - len(history)} 步。'}], 'is_error': True}
            final_result = result(state == 'reached', evidence.observation)
            return {'content': [{'type': 'text', 'text': '导航结果已记录。'}]}

        server = create_sdk_mcp_server(name='navigation', version='1.0.0', tools=[move, finish])
        options = ClaudeAgentOptions(
            model=connection['model'],
            max_turns=max_steps + 1 + COMPLETION_RETRIES,
            system_prompt=(
                '你是截图驱动的游戏导航 agent。每轮必须且只能调用一个导航工具。'
                '每次 move 返回一张新截图；只依据最新截图，不要猜测看不清的事实。'
                '只在事实满足完成条件时调用 finish；pending 时继续 move。\n' + instructions
            ),
            mcp_servers={'navigation': server},
            strict_mcp_config=True,
            tools=[],
            allowed_tools=['mcp__navigation__move', 'mcp__navigation__finish'],
            permission_mode='dontAsk',
            env={'ANTHROPIC_AUTH_TOKEN': connection['api_key'],
                 'ANTHROPIC_BASE_URL': base_url,
                 'ANTHROPIC_MODEL': connection['model'],
                 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
                 'CLAUDE_CODE_MAX_RETRIES': str(MAX_REQUEST_RETRIES),
                 'API_TIMEOUT_MS': str(int(timeout * 1000))},
            cli_path=None,
            cwd='.',
        )
        image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                             'data': base64.b64encode(initial_png).decode('ascii')}}

        async def first_message():
            yield {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'text', 'text': f'目标：{goal.strip()}。最多移动 {max_steps} 步。先观察当前截图。'}, image
            ]}}

        active_sdk = ClaudeSDKClient(options=options)
        async with active_sdk as sdk:
            await sdk.query(first_message())
            async for message in sdk.receive_response():
                if isinstance(message, ResultMessage):
                    break
        if final_result is None:
            raise AgentError('导航模型未调用完成工具，已停止')
        return final_result

    base_url = str(connection['base_url']).rstrip('/')
    if base_url.endswith('/v1'):
        base_url = base_url[:-3].rstrip('/')
    try:
        with model_request_context('导航/ClaudeSDK'):
            watch.check(initial_png)
            return asyncio.run(asyncio.wait_for(
                watch.run(session()), timeout=request_budget(timeout) * (max_steps + 1 + COMPLETION_RETRIES)))
    except CompletionDetected as exc:
        return result(True, exc.reason)
    except AgentError:
        raise
    except Exception as exc:
        LOGGER.error('导航模型调用失败：场景=导航/ClaudeSDK；%s', safe_exception_details(exc))
        raise AgentError('导航模型请求或工具参数异常，已停止') from None

