"""固定渠道地址及配置名称；渠道顺序决定 Redis 轮询编号。"""
from dataclasses import dataclass, field
import json
import re
from typing import Mapping

from naruto_daily.common.environment import EnvironmentConfigError


MAX_CHANNEL_KEYS = 20
MAX_API_KEY_LENGTH = 512
MAX_MODEL_LENGTH = 200
_CONTROL_CHARACTERS = re.compile(r'[\x00-\x1f\x7f-\x9f]')


@dataclass(frozen=True)
class ChannelDefinition:
    name: str
    base_url: str
    key_variable: str
    model_variable: str

    @property
    def enabled_variable(self) -> str:
        return self.key_variable.removesuffix('_API_KEY') + '_ENABLED'


CHANNELS = (
    ChannelDefinition('yuwan', 'https://llm-proxy.yuwan-game.com/v1',
                      'YUWAN_API_KEY', 'YUWAN_MODEL'),
    ChannelDefinition('deepseek', 'https://api.deepseek.com',
                      'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL'),
    ChannelDefinition('openai', 'https://api.openai.com/v1',
                      'OPENAI_API_KEY', 'OPENAI_MODEL'),
)
API_KEY_ENV_NAMES = (*tuple(channel.key_variable for channel in CHANNELS), 'JEV_API_KEY')


@dataclass(frozen=True)
class Channel:
    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)


def parse_api_keys(value: str | None, variable: str) -> tuple[str, ...]:
    """兼容历史单 key；有 JSON 语法的配置必须是完整的字符串数组。"""
    if value is None or isinstance(value, str) and not value.strip():
        return ()
    error = f'{variable} 必须为单个密钥或 JSON 字符串数组，最多 {MAX_CHANNEL_KEYS} 个非空密钥，每个最多 {MAX_API_KEY_LENGTH} 字符且不能含控制字符'
    if not isinstance(value, str):
        raise EnvironmentConfigError(error)
    text = value.strip()
    try:
        keys = json.loads(text)
    except json.JSONDecodeError:
        if text.startswith(('[', '{', '"')):
            raise EnvironmentConfigError(error) from None
        keys = [value]
    if not isinstance(keys, list) or len(keys) > MAX_CHANNEL_KEYS:
        raise EnvironmentConfigError(error)
    if any(not isinstance(key, str) or not key.strip() or len(key.strip()) > MAX_API_KEY_LENGTH
           or _CONTROL_CHARACTERS.search(key) for key in keys):
        raise EnvironmentConfigError(error)
    return tuple(key.strip() for key in keys)


def channel_enabled(values: Mapping[str, str | None], definition: ChannelDefinition) -> bool:
    value = values.get(definition.enabled_variable)
    if value is None:
        return True  # 缺省是否启用仍由 key 是否非空决定。
    if not isinstance(value, str) or value.strip() not in ('true', 'false'):
        raise EnvironmentConfigError(f'{definition.enabled_variable} 必须为 true 或 false')
    return value.strip() == 'true'


def channel_values(values: Mapping[str, str | None], definition: ChannelDefinition) -> tuple[tuple[str, ...], str]:
    if not channel_enabled(values, definition):
        return (), ''
    keys = parse_api_keys(values.get(definition.key_variable), definition.key_variable)
    if not keys:
        return (), ''
    model = values.get(definition.model_variable) or ''
    if not isinstance(model, str) or not model.strip() or len(model.strip()) > MAX_MODEL_LENGTH or _CONTROL_CHARACTERS.search(model):
        raise EnvironmentConfigError(f'请配置有效渠道 {definition.name} 的 {definition.model_variable}（1–{MAX_MODEL_LENGTH} 字符且不能含控制字符）')
    return keys, model.strip()
