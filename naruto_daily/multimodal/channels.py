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
                      'YUWAN_API_KEY', 'YUWAN_MODEL'),
    ChannelDefinition('deepseek', 'https://api.deepseek.com',
                      'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL'),
)
API_KEY_ENV_NAMES = (*tuple(channel.key_variable for channel in CHANNELS), 'JEV_API_KEY')


@dataclass(frozen=True)
class Channel:
    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)


def channel_values(values: Mapping[str, str | None], definition: ChannelDefinition) -> tuple[str, str]:
    return ((values.get(definition.key_variable) or '').strip(),
            (values.get(definition.model_variable) or '').strip())
