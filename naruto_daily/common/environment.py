"""启动时加载 Cloudflare KV；本地文件提供连接凭据、JEV 和机器配置。"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_ENV_NAMES = (
    'YUWAN_API_KEY', 'YUWAN_MODEL', 'YUWAN_ENABLED',
    'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_ENABLED',
    'OPENAI_API_KEY', 'OPENAI_MODEL', 'OPENAI_ENABLED',
    'JEV_API_KEY', 'JEV_MODEL',
)
JEV_ENV_NAMES = ('JEV_API_KEY', 'JEV_MODEL')
LOCAL_CHANNEL_ENV_NAMES = ('YUWAN_API_KEY', 'YUWAN_MODEL', 'YUWAN_ENABLED', *JEV_ENV_NAMES)
CLOUD_ENV_NAMES = (*CHANNEL_ENV_NAMES, 'REDIS_URL', 'REDIS_QUEUE_KEY',
                   'MULTIMODAL_ROUTING_COUNTER_KEY', 'LDPLAYER_STATE_DIR',
                   'OSS_REGION', 'OSS_BUCKET', 'OSS_ACCESS_KEY_ID',
                   'OSS_ACCESS_KEY_SECRET', 'OSS_STS_TOKEN', 'OSS_KEY_PREFIX',
                   'QQ_BOT_APP_ID', 'QQ_BOT_APP_SECRET', 'QQ_BOT_GROUP_OPENID', 'DATABASE_URL', 'QQ_CAPTURE_API_BASE',
                   'QQ_APK_RELEASE_PREFIX', 'QQ_CAPTURE_APK_SHA256', 'QQ_LOGIN_APK_SHA256',
                   'SLS_ENABLED', 'SLS_ENDPOINT', 'SLS_PROJECT', 'SLS_LOGSTORE',
                   'SLS_ACCESS_KEY_ID', 'SLS_ACCESS_KEY_SECRET', 'SLS_STS_TOKEN')
INHERITED_CONFIG_MARKER = '_NARUTO_CLOUD_CONFIG_LOADED'
_initialized = False
_initialization_lock = Lock()


class EnvironmentConfigError(ValueError):
    """环境配置缺失或无法读取。"""


def resolve_app_env(settings: Mapping[str, str | None]) -> str:
    """Environment selection belongs to the launcher, never remote configuration."""
    profile = settings.get('APP_ENV', 'prod')
    if not isinstance(profile, str) or profile.strip() not in {'prod', 'test'}:
        raise EnvironmentConfigError('APP_ENV 必须为 prod 或 test')
    return profile.strip()


def _read_local_dotenv(path: Path, *, include_local_channel: bool = False) -> dict[str, str | None]:
    try:
        values = dotenv_values(path, encoding='utf-8-sig', interpolate=False)
        return {name: value for name, value in values.items()
                if name not in (*CLOUD_ENV_NAMES, INHERITED_CONFIG_MARKER)
                or include_local_channel and name in LOCAL_CHANNEL_ENV_NAMES}
    except (OSError, UnicodeError):
        raise EnvironmentConfigError('无法读取本地环境配置文件') from None


def initialize_environment(env_file: Path | None = None) -> None:
    """每个进程初始化一次；任务子进程复用父进程传入的配置。"""
    global _initialized
    with _initialization_lock:
        if _initialized:
            return
        if os.environ.get(INHERITED_CONFIG_MARKER) == '1':
            resolve_app_env(os.environ)
            _initialized = True
            return
        local = _read_local_dotenv(env_file or PROJECT_ROOT / '.env', include_local_channel=True)
        profile = resolve_app_env({**local, **os.environ})
        for name, value in local.items():
            if value is not None and name not in LOCAL_CHANNEL_ENV_NAMES:
                os.environ.setdefault(name, value)
        from .cloudflare_kv import fetch_environment

        values = fetch_environment({**os.environ, 'APP_ENV': profile})
        cloud_count = len(values)
        from naruto_daily.multimodal.channels import CHANNELS, channel_enabled, channel_values, parse_api_keys

        # 显式本地开关独立于 key；关闭渠道时不解析其密钥或要求模型。
        channel_enabled(local, CHANNELS[0])
        if local.get('YUWAN_ENABLED') is not None:
            values['YUWAN_ENABLED'] = local['YUWAN_ENABLED']
        yuwan_enabled = channel_enabled(values, CHANNELS[0])
        local_yuwan = bool(parse_api_keys(local.get('YUWAN_API_KEY'), 'YUWAN_API_KEY')) if yuwan_enabled else bool(
            (local.get('YUWAN_API_KEY') or '').strip())
        if local_yuwan:
            selected = {**values, 'YUWAN_API_KEY': local['YUWAN_API_KEY'],
                        'YUWAN_MODEL': local.get('YUWAN_MODEL') or ''}
            channel_values(selected, CHANNELS[0])
            values.update({name: selected[name] for name in ('YUWAN_API_KEY', 'YUWAN_MODEL', 'YUWAN_ENABLED')
                           if name in selected})
        # JEV 本地配置独立于生成模型；子进程只继承已解析的值。
        # 环境变量优先于 .env，明确的空值也不能回退到共享 KV 中的 key。
        for name in JEV_ENV_NAMES:
            value = os.environ.get(name, local.get(name))
            if value is not None:
                values[name] = value
        # Publish only after every page and value has been read successfully.
        # Missing cloud settings must never reuse stale local/process credentials.
        for name in CLOUD_ENV_NAMES:
            os.environ.pop(name, None)
        os.environ.update(values)
        os.environ['APP_ENV'] = profile
        _initialized = True
        logging.getLogger(__name__).info('Cloudflare KV 配置已加载：%d 项；鱼玩本地配置：%s',
                                         cloud_count, '启用' if local_yuwan and yuwan_enabled else '未启用',
                                         extra={'backend_only': True})


def model_environment_for_child() -> dict[str, str]:
    """Pass the selected model/Redis profile; omit OSS, database and CF credentials."""
    names = (*CHANNEL_ENV_NAMES, 'REDIS_URL', 'MULTIMODAL_ROUTING_COUNTER_KEY')
    values = {name: os.environ[name] for name in names if name in os.environ}
    values['APP_ENV'] = resolve_app_env(os.environ)
    values[INHERITED_CONFIG_MARKER] = '1'
    return values


def read_multimodal_environment() -> dict[str, str | None]:
    """读取启动时注入的渠道配置；日志脱敏也可调用，不触发网络请求。"""
    return {name: os.environ.get(name) for name in CHANNEL_ENV_NAMES}


def read_environment() -> dict[str, str | None]:
    initialize_environment()
    if os.environ.get(INHERITED_CONFIG_MARKER) == '1':
        return dict(os.environ)
    return {**_read_local_dotenv(PROJECT_ROOT / '.env'), **os.environ}


def require_environment(values: Mapping[str, str | None], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        source = ('Cloudflare KV 或本地 .env' if name in LOCAL_CHANNEL_ENV_NAMES else
                  'Cloudflare KV' if name in CLOUD_ENV_NAMES else 'Cloudflare KV、本地 .env 或进程环境')
        raise EnvironmentConfigError(f'请在{source}中配置非空的 {name}')
    return value.strip()


def resolve_environment(value: Any, values: Mapping[str, str | None]) -> Any:
    """将 JSON 中完整的 ${变量名} 引用替换为环境值，其余字段保持原样。"""
    if isinstance(value, dict):
        return {key: resolve_environment(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_environment(item, values) for item in value]
    if isinstance(value, str):
        match = re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', value)
        if match:
            return require_environment(values, match[1])
    return value
