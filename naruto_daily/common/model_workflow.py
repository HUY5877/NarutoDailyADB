"""日常页面流程共用的限时模型判断和模板等待。"""
from __future__ import annotations

import logging
import time
from itertools import count
from pathlib import Path

from .recognition import ImageRecognition
from .visual import NonRetryableVisualError, VisualError, find_template_in_png
from .execution import current_execution, retry_observation, stage_timeout
from naruto_daily.multimodal.client import MultimodalClient, MultimodalRequestError
from naruto_daily.multimodal.diagnostics import model_request_context

LOGGER = logging.getLogger('naruto_daily')


class ModelWorkflow:
    def __init__(self, client, action, *, model=None, template_matcher=find_template_in_png,
                 asset_root=Path('.'), sleep=time.sleep):
        self.client, self.action = client, action
        self.images = ImageRecognition(client, template_matcher, asset_root, sleep)
        self.model = model if model is not None else MultimodalClient(action['multimodal'], asset_root=asset_root)
        self.started = time.monotonic()

    def elapsed(self):
        return max(self.images.elapsed, time.monotonic() - self.started)

    def remaining(self):
        execution = current_execution()
        if execution is not None:
            return execution.remaining()
        remaining = self.action['timeout_seconds'] - self.elapsed()
        if remaining <= 0:
            raise VisualError('日常页面处理超时，已停止点击')
        return remaining

    def read(self, name, prompt, validate, *, timeout=None):
        deadline = (None if timeout is None or current_execution() is not None
                    else self.elapsed() + min(timeout, self.remaining()))

        def remaining_budget():
            remaining = self.remaining()
            if deadline is not None:
                remaining = min(remaining, deadline - self.elapsed())
                if remaining <= 0:
                    raise VisualError(f'{name}识别超时，已停止')
            return remaining

        # An uncertain frame is recoverable within the shared daily deadline.
        # Keep the local call cap for standalone observations without that budget.
        max_calls = None if current_execution() is not None else self.action['multimodal']['max_calls']
        for attempt in count(1):
            remaining_budget()
            attempt_started = time.monotonic()
            attempt_label = f'第{attempt}次' if max_calls is None else f'第{attempt}/{max_calls}次'
            try:
                LOGGER.info('[%s %s] 开始截图', name, attempt_label)
                png = self.client.screenshot()
                remaining = remaining_budget()
                configured_timeout = self.action['multimodal'].get('timeout_seconds')
                call_timeout = (min(remaining, configured_timeout)
                                if isinstance(configured_timeout, (int, float)) else remaining)
                LOGGER.info('[%s %s] 模型请求开始：%s %.2f 秒，调用超时 %.2f 秒',
                            name, attempt_label,
                            '任务总时限剩余' if current_execution() is not None else '阶段剩余',
                            remaining, call_timeout)
                request_started = time.monotonic()
                with model_request_context(name):
                    options = {'refresh_image': self.client.screenshot} if current_execution() is not None else {}
                    result = self.model.complete_json(png, prompt, timeout_seconds=remaining, **options)
                LOGGER.info('[%s %s] 模型请求返回：耗时 %.2f 秒',
                            name, attempt_label, time.monotonic() - request_started)
                remaining_budget()  # 迟到的模型响应不能触发后续点击。
                parsed = validate(result)
                LOGGER.info('[%s 第%d次] %s', name, attempt, parsed)
                return parsed
            except MultimodalRequestError as exc:
                LOGGER.error('[%s] 接口请求失败：%s；停止识别，不再叠加重试', name, exc)
                raise
            except NonRetryableVisualError:
                raise
            except VisualError as exc:
                exhausted = max_calls is not None and attempt >= max_calls
                LOGGER.info('[%s %s] 识别未确认：%s：%s；本次耗时 %.2f 秒；%s',
                            name, attempt_label, type(exc).__name__, exc,
                            time.monotonic() - attempt_started,
                            '已达重试上限，停止识别' if exhausted else
                            '总时限内继续确认，1秒后重新截图' if max_calls is None else '将重新截图重试')
                if exhausted:
                    raise VisualError(f'{name}多次识别未确认，已停止：{exc}') from exc
                self.images.sleep(min(1, remaining_budget()))

    def tap(self, point):
        self.remaining()
        LOGGER.info('点击坐标：(%d, %d)', *point)
        self.client.tap(*point)
        self.images.sleep(min(float(self.action.get('post_click_wait_seconds', 1)), self.remaining()))

    def button(self, key, *, click=True, timeout=20):
        target = self.action[key]
        deadline = self.elapsed() + min(stage_timeout(timeout), self.remaining())

        def observe():
            return self.images.match(self.client.screenshot(), target)

        while self.elapsed() < deadline:
            center, score = retry_observation(observe, name=target['name'])
            self.remaining()
            if self.elapsed() >= deadline:
                break
            if center is not None:
                LOGGER.info('确认%s，相似度 %.3f', target['name'], score)
                if click:
                    self.tap(center)
                return
            delay = min(self.action.get('poll_seconds', 1), deadline - self.elapsed())
            if delay > 0:
                self.images.sleep(delay)
        raise VisualError(f"等待{target['name']}超时，未继续点击")


def require_page(data, page):
    if not isinstance(data, dict) or data.get('page') != page:
        raise VisualError(f'未确认前景为{page}页面')
