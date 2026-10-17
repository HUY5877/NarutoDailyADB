"""JEV 只选择本地候选，业务 JSON 与坐标始终由调用方提供。"""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import logging
from typing import Callable, Mapping
from urllib.request import Request

import cv2
import numpy as np

from naruto_daily.common.environment import EnvironmentConfigError, require_environment
from .diagnostics import current_model_scene, safe_exception_details
from .transport import MultimodalError, request_with_retry
from .prompt_jev import CHOICE_INSTRUCTIONS, CHOICE_STATE_PREFIX, ChoicePrompt


LOGGER = logging.getLogger('naruto_daily')
JEV_ENDPOINT = 'https://openrouter.ai/api/alpha/decisions'
DEFAULT_JEV_MODEL = 'typesafe/jev-1.14'
JEV_MAX_IMAGE_EDGE = 1568


def _prepare_image(png: bytes) -> bytes:
    try:
        frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    except cv2.error:
        raise MultimodalError('JEV 请求图片无法解析') from None
    if frame is None:
        raise MultimodalError('JEV 请求图片无法解析')
    height, width = frame.shape[:2]
    longest_edge = max(width, height)
    if longest_edge <= JEV_MAX_IMAGE_EDGE:
        return png
    scale = JEV_MAX_IMAGE_EDGE / longest_edge
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    resized = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    success, encoded = cv2.imencode('.png', resized)
    if not success:
        raise MultimodalError('JEV 请求图片缩放编码失败')
    return encoded.tobytes()


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
            image = _prepare_image(image)
            LOGGER.info('[多模态路由] channel=jev url=%s model=%s scene=%s',
                        JEV_ENDPOINT, self._model, current_model_scene(),
                        extra={'backend_only': True})
            payload = {
                'model': self._model,
                'state': [
                    CHOICE_STATE_PREFIX + str(prompt),
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
