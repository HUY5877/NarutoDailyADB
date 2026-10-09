"""Read a complete text configuration from Cloudflare Workers KV."""
from __future__ import annotations

import re
from typing import Mapping

from cloudflare import Cloudflare

from .environment import EnvironmentConfigError, INHERITED_CONFIG_MARKER


BOOTSTRAP_NAMES = ('CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_KV_NAMESPACE_ID')
API_BASE_URL = 'https://api.cloudflare.com/client/v4'


def fetch_environment(settings: Mapping[str, str]) -> dict[str, str]:
    """List every page, then fetch text values in batches of at most 100."""
    for name in BOOTSTRAP_NAMES:
        if not settings.get(name, '').strip():
            raise EnvironmentConfigError(f'请配置 {name}，用于读取 Cloudflare KV')
    account = settings['CLOUDFLARE_ACCOUNT_ID'].strip()
    namespace = settings['CLOUDFLARE_KV_NAMESPACE_ID'].strip()
    if any(re.fullmatch(r'[0-9a-fA-F]{32}', value) is None for value in (account, namespace)):
        raise EnvironmentConfigError('CLOUDFLARE_ACCOUNT_ID 和 CLOUDFLARE_KV_NAMESPACE_ID 必须为 32 位十六进制 ID')

    try:
        with Cloudflare(api_token=settings['CLOUDFLARE_API_TOKEN'].strip(),
                        base_url=API_BASE_URL, timeout=15.0, max_retries=2) as client:
            names: dict[str, None] = {}
            cursors: set[str] = set()
            cursor = None
            while True:
                page = client.kv.namespaces.keys.list(
                    namespace, account_id=account, limit=1000,
                    **({'cursor': cursor} if cursor else {}),
                )
                for key in page.result:
                    name = key.name
                    if (re.fullmatch(r'[A-Z_][A-Z0-9_]*', name) is None
                            or name in (*BOOTSTRAP_NAMES, INHERITED_CONFIG_MARKER)):
                        raise EnvironmentConfigError('Cloudflare KV 包含无效或保留的环境变量名')
                    names[name] = None
                cursor = page.result_info.cursor if page.result_info else None
                if not cursor:
                    break
                if cursor in cursors:
                    raise EnvironmentConfigError('Cloudflare KV 分页游标重复，未加载配置')
                cursors.add(cursor)

            values: dict[str, str] = {}
            keys = list(names)
            for offset in range(0, len(keys), 100):
                batch = keys[offset:offset + 100]
                result = client.kv.namespaces.bulk_get(
                    namespace, account_id=account, keys=batch, type='text', with_metadata=False,
                )
                returned = result.values if result is not None else None
                if not isinstance(returned, dict):
                    raise EnvironmentConfigError('Cloudflare KV 未返回完整的文本配置')
                for name in batch:
                    value = returned.get(name)
                    if not isinstance(value, str) or '\x00' in value:
                        raise EnvironmentConfigError('Cloudflare KV 配置缺失或不是有效文本，请检查后重启')
                    values[name] = value
            return values
    except EnvironmentConfigError:
        raise
    except Exception as error:
        # API errors may contain response bodies and credentials. Never echo them.
        status = getattr(error, 'status_code', None)
        suffix = f'（HTTP {status}）' if type(status) is int else ''
        raise EnvironmentConfigError(f'Cloudflare KV 配置读取失败{suffix}，请检查网络、Token 权限和命名空间') from None
