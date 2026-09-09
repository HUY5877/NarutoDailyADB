"""可复用的 OpenAI 兼容多模态 JSON 调用，场景提示词由调用方传入。"""
from __future__ import annotations

import base64
import json
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from naruto_daily.common.visual import VisualError
from naruto_daily.common.environment import (
    EnvironmentConfigError, read_environment, require_environment, resolve_environment,
)


class MultimodalError(VisualError):
    """模型调用失败，不得当作已经识别成功。"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不把带密钥的请求自动转发到另一个地址。
        return None


class MultimodalClient:
    def __init__(self, config: dict, *, asset_root=Path('.'), open_request=None):
        self._open = open_request or build_opener(_NoRedirect).open
        try:
            environment = read_environment(Path(asset_root))
            self.config = resolve_environment(config, environment)
            self._key = require_environment(environment, 'MULTIMODAL_API_KEY')
        except EnvironmentConfigError as exc:
            raise MultimodalError(f'多模态配置错误（API 密钥或环境变量）：{exc}') from None
        if not self._key or any(c.isspace() for c in self._key):
            raise MultimodalError('多模态 API 密钥为空或格式不正确')

    def agent_connection(self) -> dict:
        """供 Python agent 复用连接配置；包含密钥，禁止写入日志。"""
        return dict(base_url=self.config['base_url'], model=self.config['model'],
                    timeout_seconds=float(self.config['timeout_seconds']), api_key=self._key)

    def complete_json(self, png: bytes, prompt: str, *, timeout_seconds=None) -> dict:
        timeout = float(self.config['timeout_seconds'])
        if timeout_seconds is not None:
            timeout = min(timeout, float(timeout_seconds))
        if timeout <= 0:
            raise MultimodalError('多模态调用已超过本阶段剩余时间')
        payload = {
            'model': self.config['model'], 'reasoning_effort': 'low', 'max_tokens': 4096,
            'response_format': {'type': 'json_object'},
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {
                    'url': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
                }},
            ]}],
        }
        request = Request(self.config['base_url'].rstrip('/') + '/chat/completions',
                          data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key})
        try:
            with self._open(request, timeout=timeout) as response:
                body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise MultimodalError('多模态响应超过大小限制')
            result = json.loads(body)
            choice = result['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise MultimodalError('多模态结果未完整生成，已停止本次识别')
            parsed = json.loads(choice['message']['content'])
            if not isinstance(parsed, dict):
                raise MultimodalError('多模态结果必须是 JSON 对象')
            return parsed
        except HTTPError as exc:
            raise MultimodalError(f'多模态 API 请求失败（HTTP {exc.code}），请检查模型、密钥或接口额度') from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise MultimodalError('多模态 API 网络异常或请求超时') from None
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
            raise MultimodalError('多模态 API 返回了无法解析的结果') from None
