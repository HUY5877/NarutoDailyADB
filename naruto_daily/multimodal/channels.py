"""固定渠道地址及配置名称；渠道顺序决定 Redis 轮询编号。"""
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class ChannelDefinition:
    name: str
    base_url: str
    key_variable: str
    model_variable: str


CHANNELS = (
    ChannelDefinition('yuwan', 'https://llm-proxy.yuwan-game.com/v1',
                      'MULTIMODAL_YUWAN_API_KEY', 'MULTIMODAL_YUWAN_MODEL'),
    ChannelDefinition('deepseek', 'https://api.deepseek.com',
                      'MULTIMODAL_DEEPSEEK_API_KEY', 'MULTIMODAL_DEEPSEEK_MODEL'),
)
API_KEY_ENV_NAMES = ('MULTIMODAL_API_KEY', *(channel.key_variable for channel in CHANNELS))


@dataclass(frozen=True)
class Channel:
    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)


def channel_values(values: Mapping[str, str | None], definition: ChannelDefinition) -> tuple[str, str]:
    # 旧配置只作为鱼玩渠道的迁移兼容；显式留空新 key 会禁用鱼玩。
    legacy_key = values.get('MULTIMODAL_API_KEY') if definition.name == 'yuwan' else None
    legacy_model = values.get('MULTIMODAL_MODEL') if definition.name == 'yuwan' else None
    return ((values.get(definition.key_variable, legacy_key) or '').strip(),
            (values.get(definition.model_variable, legacy_model) or '').strip())


def legacy_placeholders(values: Mapping[str, str | None]) -> dict[str, str | None]:
    """让旧任务 JSON 的两个占位符继续解析；实际发送只使用选中的渠道。"""
    resolved = dict(values)
    for definition in CHANNELS:
        key, model = channel_values(values, definition)
        if key:
            if not (resolved.get('MULTIMODAL_BASE_URL') or '').strip():
                resolved['MULTIMODAL_BASE_URL'] = definition.base_url
            if not (resolved.get('MULTIMODAL_MODEL') or '').strip():
                resolved['MULTIMODAL_MODEL'] = model
            break
    return resolved
