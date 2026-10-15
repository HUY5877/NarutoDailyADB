"""可复用的 OpenAI 兼容多模态 JSON 调用，场景提示词由调用方传入。"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Callable
from urllib.request import Request

from .transport import MultimodalError, MultimodalRequestError, request_with_retry
from .diagnostics import current_model_scene, safe_exception_details
from naruto_daily.common.environment import (
    EnvironmentConfigError, read_environment, resolve_environment,
)
from .gateway import initialize_gateway
from .decisions import ChoicePrompt, JevDecisionClient
from naruto_daily.common.execution import MODEL_REQUEST_TIMEOUT_SECONDS, current_execution
from naruto_daily.common.visual import NonRetryableVisualError


LOGGER = logging.getLogger('naruto_daily')


class MultimodalConfigurationError(MultimodalError, NonRetryableVisualError):
    """模型连接配置无效，不能通过重新截图恢复。"""


class MultimodalClient:
    def __init__(self, config: dict, *, asset_root=Path('.'), open_request=None,
                 sleep=None, clock=None):
        self._open = open_request
        self._sleep, self._clock = sleep, clock
        try:
            environment = read_environment()
            self._decision_values = {name: environment.get(name) for name in ('JEV_API_KEY', 'JEV_MODEL')}
            self._decisions = None
            self.gateway = initialize_gateway()
            # 任务 JSON 只控制调用时限等业务参数，URL、密钥、模型由渠道决定。
            self.config = resolve_environment({key: value for key, value in config.items()
                                               if key not in ('base_url', 'model')}, environment)
            channel = self.gateway.channels[0]
            self.config.update(base_url=channel.base_url, model=channel.model)
            self._key = channel.api_key
        except EnvironmentConfigError as exc:
            raise MultimodalConfigurationError(f'多模态配置错误（API 密钥或环境变量）：{exc}') from None
        if current_execution() is not None:
            self.config = {**self.config, 'timeout_seconds': min(
                float(self.config['timeout_seconds']), MODEL_REQUEST_TIMEOUT_SECONDS)}

    def agent_connection(self) -> dict:
        """供 Python agent 复用连接配置；包含密钥，禁止写入日志。"""
        timeout = float(self.config['timeout_seconds'])
        if current_execution() is not None:
            timeout = min(timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        return dict(base_url=self.config['base_url'], model=self.config['model'],
                    timeout_seconds=timeout, api_key=self._key,
                    protocol='openai', route_request=self.gateway.route_async_request)

    def complete_json(self, png: bytes, prompt: str, *, timeout_seconds=None,
                      refresh_image: Callable[[], bytes] | None = None) -> dict:
        """首次识别给定截图；提供刷新函数时，每次接口重试都改用新截图。"""
        if isinstance(prompt, ChoicePrompt):
            try:
                if self._decisions is None:
                    self._decisions = JevDecisionClient(
                        self._decision_values, open_request=self._open,
                        sleep=self._sleep, clock=self._clock,
                    )
            except EnvironmentConfigError as exc:
                raise MultimodalConfigurationError(f'JEV 配置错误：{exc}') from None
            return self._decisions.complete_json(
                png, prompt, request_timeout=float(self.config['timeout_seconds']),
                timeout_seconds=timeout_seconds, refresh_image=refresh_image,
            )
        def build_request(image):
            payload = {
                'model': self.config['model'], 'reasoning_effort': 'low', 'max_tokens': 4096,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')
                    }},
                ]}],
            }
            return Request(self.config['base_url'].rstrip('/') + '/chat/completions',
                           data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key})

        # 截图发生在重试等待之后；不能预先缓存下一次请求的图片。
        refresh_request = (lambda: build_request(refresh_image())) if refresh_image is not None else None
        response = request_with_retry(
            build_request(png), request_timeout=float(self.config['timeout_seconds']), timeout_seconds=timeout_seconds,
            open_request=self._open, sleep=self._sleep, clock=self._clock,
            refresh_request=refresh_request,
            route_request=self.gateway.route_request,
        )
        body = response.body

        def log_invalid_response(reason, exc=None):
            LOGGER.error('[多模态结果无效] request_id=%s scene=%s status=%d response_bytes=%d reason=%s%s',
                         response.request_id, current_model_scene(), response.status, len(body), reason,
                         ' ' + safe_exception_details(exc) if exc is not None else '')

        try:
            result = json.loads(body)
            choice = result['choices'][0]
            if choice.get('finish_reason') != 'stop':
                log_invalid_response('generation_not_complete')
                raise MultimodalError('多模态结果未完整生成，已停止本次识别')
            parsed = json.loads(choice['message']['content'])
            if not isinstance(parsed, dict):
                log_invalid_response('result_not_object')
                raise MultimodalError('多模态结果必须是 JSON 对象')
            return parsed
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError) as exc:
            log_invalid_response('invalid_json_or_response_structure', exc)
            raise MultimodalError('多模态 API 返回了无法解析的结果') from None
