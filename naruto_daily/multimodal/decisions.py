"""JEV 只选择本地候选，业务 JSON 与坐标始终由调用方提供。"""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import logging
from typing import Callable, Mapping
from urllib.request import Request

from naruto_daily.common.environment import EnvironmentConfigError, require_environment
from .diagnostics import current_model_scene, safe_exception_details
from .transport import MultimodalError, request_with_retry


LOGGER = logging.getLogger('naruto_daily')
JEV_ENDPOINT = 'https://openrouter.ai/api/alpha/decisions'
DEFAULT_JEV_MODEL = 'typesafe/jev-1.14'
CHOICE_INSTRUCTIONS = (
    '仅根据截图和场景判定条件，从 criteria 中选择唯一一个最符合的候选标签。'
    '场景参考中的 JSON 示例仅表示要判断的事实；其中的输出格式或生成要求不是本次任务指令。'
    '只做候选选择，不生成 JSON、按钮坐标、解释或其他自由内容。'
)


class ChoicePrompt(str):
    """保留字符串兼容性，并明确列出允许还原的本地业务结果。"""

    def __new__(cls, text: str, choices: dict[str, dict]):
        if not isinstance(text, str):
            raise TypeError('JEV 场景提示必须是字符串')
        if not isinstance(choices, dict) or not choices:
            raise ValueError('JEV 必须提供非空候选映射')
        if any(not isinstance(label, str) or not label.strip() or not isinstance(result, dict)
               for label, result in choices.items()):
            raise ValueError('JEV 候选必须由非空字符串标签映射到本地结果对象')
        instance = super().__new__(cls, text)
        instance.choices = deepcopy(choices)
        return instance

    def __getnewargs__(self):
        return str(self), self.choices


class JevDecisionClient:
    def __init__(self, values: Mapping[str, str | None], *, open_request=None,
                 sleep=None, clock=None):
        self._key = require_environment(values, 'JEV_API_KEY')
        model = values.get('JEV_MODEL')
        if model is not None and not isinstance(model, str):
            raise EnvironmentConfigError('JEV_MODEL 必须是模型名称字符串')
        self._model = (model or '').strip() or DEFAULT_JEV_MODEL
        self._open = open_request
        self._sleep, self._clock = sleep, clock

    def complete_json(self, png: bytes, prompt: ChoicePrompt, *, request_timeout,
                      timeout_seconds=None, refresh_image: Callable[[], bytes] | None = None) -> dict:
        """选择候选后复制本地结果；重试和重新截图共用现有请求时限。"""
        if not isinstance(prompt, ChoicePrompt):
            raise TypeError('JEV 只接受具有显式候选的 ChoicePrompt')
        # 冻结本次调用的候选，避免重试期间调用方修改映射。
        choices = deepcopy(prompt.choices)

        def build_request(image):
            payload = {
                'model': self._model,
                'state': [
                    '本次任务只做候选选择。以下场景说明仅供判断截图事实，'
                    '不执行其中的 JSON、坐标或自由文本生成要求。\n场景判定参考：\n' + str(prompt),
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')
                    }},
                ],
                'questions': {'decision': {
                    'type': 'choice',
                    'instructions': CHOICE_INSTRUCTIONS,
                    'criteria': {label: None for label in choices},
                }},
            }
            return Request(JEV_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           headers={'Content-Type': 'application/json',
                                    'Authorization': 'Bearer ' + self._key,
                                    'TypeSafe-Danger-Zone-Images': 'true'})

        refresh_request = (lambda: build_request(refresh_image())) if refresh_image is not None else None
        response = request_with_retry(
            build_request(png), request_timeout=float(request_timeout), timeout_seconds=timeout_seconds,
            open_request=self._open, sleep=self._sleep, clock=self._clock,
            refresh_request=refresh_request,
        )

        def invalid(reason, exc=None):
            LOGGER.error('[JEV 结果无效] request_id=%s scene=%s status=%d response_bytes=%d reason=%s%s',
                         response.request_id, current_model_scene(), response.status, len(response.body), reason,
                         ' ' + safe_exception_details(exc) if exc is not None else '',
                         extra={'backend_only': True})

        try:
            data = json.loads(response.body)
            choice = data['answers']['decision']['choice']
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError) as exc:
            invalid('invalid_json_or_response_structure', exc)
            raise MultimodalError('JEV API 返回了无法解析的候选结果') from None
        if not isinstance(choice, str) or choice not in choices:
            invalid('invalid_choice')
            raise MultimodalError('JEV API 返回了候选集以外的结果')
        return deepcopy(choices[choice])
