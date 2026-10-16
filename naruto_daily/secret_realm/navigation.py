"""毒风秘境导航规则；画面证据由原生 move/finish 工具直接提交。"""
import re
from typing import Literal

from pydantic import Field

from naruto_daily.multimodal.agent import Joystick, NavigationEvidence, run_agent
from naruto_daily.multimodal.prompts import (
    POISON_EVIDENCE_DESCRIPTIONS, POISON_SEAL_GOAL, POISON_SEAL_INSTRUCTIONS,
)
from .boss_detection import poison_boss_visible


# 固定 1920×1080 布局；中心经毒风未解封场景实机校准，避免右移时向下偏。
SECRET_JOYSTICK = Joystick(x=327, y=825, radius=80, duration_ms=300)


class PoisonEvidence(NavigationEvidence):
    """模型在同一原生工具调用里提供的当前截图事实。"""

    observation: str = Field(
        min_length=1, max_length=2000, pattern=r'\S',
        description=POISON_EVIDENCE_DESCRIPTIONS['observation'])
    page: Literal['battle', 'result', 'other', 'unknown'] = Field(
        description=POISON_EVIDENCE_DESCRIPTIONS['page'])
    seal_prompt_visible: Literal['yes', 'no', 'unknown'] = Field(
        description=POISON_EVIDENCE_DESCRIPTIONS['seal_prompt_visible'])
    boss_visible: Literal['yes', 'no', 'unknown'] = Field(
        description=POISON_EVIDENCE_DESCRIPTIONS['boss_visible'])
    battle_started: Literal['yes', 'no', 'unknown'] = Field(
        description=POISON_EVIDENCE_DESCRIPTIONS['battle_started'])
    visible_text: str = Field(max_length=2000, description=POISON_EVIDENCE_DESCRIPTIONS['visible_text'])

    def state(self) -> Literal['pending', 'reached', 'finished', 'blocked']:
        if self.page == 'result':
            return 'finished'
        if self.page != 'battle':
            return 'blocked'
        # 仅检查模型已抄写文字和事实字段是否矛盾，不增加 OCR 或模型请求。
        text = re.sub(r'\W+', '', self.visible_text).casefold()
        if any(marker in text for marker in ('失败', '胜利', '结算', '点击任意位置关闭')):
            return 'finished'
        if self.seal_prompt_visible != 'no':
            return 'pending'
        if any(marker in text for marker in ('解开封印召唤boss', '解除封印召唤boss',
                                             '待解封', '等待解封', '等待召唤')):
            return 'pending'
        if self.boss_visible == 'yes' or self.battle_started == 'yes':
            return 'reached'
        return 'pending'


def _poison_completion_probe(png: bytes) -> str | None:
    if poison_boss_visible(png):
        return '本地已确认毒风 BOSS 名称、风徽章和血条，立即结束导航并进入战斗'
    return None


def navigate_to_poison_seal(images, model):
    return run_agent(images.client, model, POISON_SEAL_GOAL,
                     joystick=SECRET_JOYSTICK, max_steps=50, sleep=images.sleep,
                     evidence_type=PoisonEvidence, instructions=POISON_SEAL_INSTRUCTIONS,
                     completion_probe=_poison_completion_probe)
