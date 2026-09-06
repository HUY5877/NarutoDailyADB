"""从配置目录的 .env 和进程环境读取配置，不修改全局环境。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values


class EnvironmentConfigError(ValueError):
    """环境配置缺失或无法读取。"""


def read_environment(directory: Path) -> dict[str, str | None]:
    try:
        values = dotenv_values(Path(directory) / '.env', encoding='utf-8-sig', interpolate=False)
    except (OSError, UnicodeError):
        raise EnvironmentConfigError('无法读取配置目录中的 .env 文件') from None
    return {**values, **os.environ}


def require_environment(values: Mapping[str, str | None], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise EnvironmentConfigError(f'请在 .env 或进程环境中配置非空的 {name}')
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
