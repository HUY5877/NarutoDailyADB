"""Visual navigation public types and Claude Agent SDK dispatch."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from naruto_daily.common.visual import VisualError
from naruto_daily.common.execution import check_execution


class AgentError(VisualError):
    """Navigation cannot complete reliably."""


@dataclass(frozen=True)
class Joystick:
    x: int
    y: int
    radius: int
    duration_ms: int = 300

    def __post_init__(self):
        values = (self.x, self.y, self.radius, self.duration_ms)
        if any(type(value) is not int for value in values):
            raise ValueError('摇杆坐标、半径和移动时长必须是整数')
        if self.radius <= 0 or min(self.x, self.y) < self.radius:
            raise ValueError('摇杆半径必须为正，四向端点不能小于零')
        if not 1 <= self.duration_ms <= 10000:
            raise ValueError('单次移动时长必须在 1 到 10000 毫秒之间')

    def move(self, client, direction: str) -> None:
        check_execution()
        if direction not in ('w', 'a', 's', 'd'):
            raise AgentError('移动动作只能是单个 w/a/s/d')
        dx, dy = {'w': (0, -1), 'a': (-1, 0), 's': (0, 1), 'd': (1, 0)}[direction]
        client.swipe(self.x, self.y, self.x + dx * self.radius,
                     self.y + dy * self.radius, self.duration_ms)


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

    def state(self) -> Literal['pending', 'reached', 'finished', 'blocked']:
        raise NotImplementedError


class GoalEvidence(NavigationEvidence):
    status: Literal['pending', 'reached', 'blocked']

    def state(self):
        return self.status


def run_agent(client, model, goal: str, *, joystick: Joystick, max_steps: int = 50,
              settle_seconds: float = 0.5, sleep: Callable[[float], None] = time.sleep,
              evidence_type: type[NavigationEvidence] = GoalEvidence,
              instructions: str = '',
              completion_probe: Callable[[bytes], str | None] | None = None) -> AgentResult:
    """真实连接使用 Claude Agent SDK；本地 HTTP 替身只用于离线测试。"""
    connection = model.agent_connection()
    base_url = str(connection.get('base_url', ''))
    # Modelink/OpenAI-compatible endpoints use the native Python OpenAI adapter.
    # Claude Messages endpoints omit /v1 and keep the Claude Agent SDK path.
    openai_compatible = (base_url.rstrip('/').endswith('/v1')
                         or base_url.startswith(('http://127.0.0.1:', 'http://localhost:'))
                         or '/' in str(connection.get('model', '')))
    if openai_compatible:
        from .compat_runner import run_agent as run_compat_agent
        return run_compat_agent(client, model, goal, joystick=joystick, max_steps=max_steps,
                                settle_seconds=settle_seconds, sleep=sleep,
                                evidence_type=evidence_type, instructions=instructions,
                                completion_probe=completion_probe)
    from .claude_runner import run_agent as run_claude_agent
    return run_claude_agent(client, model, goal, joystick=joystick, max_steps=max_steps,
                            settle_seconds=settle_seconds, sleep=sleep,
                            evidence_type=evidence_type, instructions=instructions,
                            completion_probe=completion_probe)
