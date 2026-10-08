"""统一读取项目根目录 .env，不随配置 JSON 或启动目录改变。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MULTIMODAL_ENV_NAMES = (
    'MULTIMODAL_API_KEY', 'MULTIMODAL_BASE_URL', 'MULTIMODAL_MODEL',
    'MULTIMODAL_YUWAN_API_KEY', 'MULTIMODAL_YUWAN_MODEL',
    'MULTIMODAL_DEEPSEEK_API_KEY', 'MULTIMODAL_DEEPSEEK_MODEL',
)


class EnvironmentConfigError(ValueError):
    """环境配置缺失或无法读取。"""


def _read_root_dotenv() -> dict[str, str | None]:
    try:
        return dict(dotenv_values(PROJECT_ROOT / '.env', encoding='utf-8-sig', interpolate=False))
    except (OSError, UnicodeError):
        raise EnvironmentConfigError('无法读取项目根目录中的 .env 文件') from None


def read_multimodal_environment() -> dict[str, str | None]:
    """每次读取根目录文件；缺失值也不得回退到服务或进程中的旧配置。"""
    values = _read_root_dotenv()
    return {name: values.get(name) for name in MULTIMODAL_ENV_NAMES}


def read_environment() -> dict[str, str | None]:
    from naruto_daily.multimodal.channels import legacy_placeholders
    values = _read_root_dotenv()
    # 保留“未配置”与“显式留空”的差别，旧鱼玩 key 才能按需兼容。
    values = legacy_placeholders(values)
    merged = {**values, **os.environ}
    for name in MULTIMODAL_ENV_NAMES:
        merged.pop(name, None)
        if name in values:
            merged[name] = values[name]
    return merged


def require_environment(values: Mapping[str, str | None], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        source = '项目根目录的 .env' if name in MULTIMODAL_ENV_NAMES else '项目根目录的 .env 或进程环境'
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
