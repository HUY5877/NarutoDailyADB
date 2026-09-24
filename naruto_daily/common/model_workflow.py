"""日常页面流程共用的限时模型判断和模板等待。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .recognition import ImageRecognition
from .visual import VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalClient

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
        remaining = self.action['timeout_seconds'] - self.elapsed()
        if remaining <= 0:
            raise VisualError('日常页面处理超时，已停止点击')
        return remaining

    def read(self, name, prompt, validate):
        max_calls = self.action['multimodal']['max_calls']
        for attempt in range(1, max_calls + 1):
            self.remaining()
            attempt_started = time.monotonic()
            try:
                LOGGER.info('[%s 第%d/%d次] 开始截图', name, attempt, max_calls)
                png = self.client.screenshot()
                remaining = self.remaining()
                configured_timeout = self.action['multimodal'].get('timeout_seconds')
                call_timeout = (min(remaining, configured_timeout)
                                if isinstance(configured_timeout, (int, float)) else remaining)
                LOGGER.info('[%s 第%d/%d次] 模型请求开始：阶段剩余 %.2f 秒，调用超时 %.2f 秒',
                            name, attempt, max_calls, remaining, call_timeout)
                request_started = time.monotonic()
                result = self.model.complete_json(png, prompt, timeout_seconds=remaining)
                LOGGER.info('[%s 第%d/%d次] 模型请求返回：耗时 %.2f 秒',
                            name, attempt, max_calls, time.monotonic() - request_started)
                self.remaining()  # 迟到的模型响应不能触发后续点击。
                parsed = validate(result)
                LOGGER.info('[%s 第%d次] %s', name, attempt, parsed)
                return parsed
            except VisualError as exc:
                LOGGER.info('[%s 第%d/%d次] 识别未确认：%s：%s；本次耗时 %.2f 秒；%s',
                            name, attempt, max_calls, type(exc).__name__, exc,
                            time.monotonic() - attempt_started,
                            '将重新截图重试' if attempt < max_calls else '已达重试上限，停止识别')
                if attempt == max_calls:
                    raise VisualError(f'{name}多次识别未确认，已停止：{exc}') from exc
                self.images.sleep(min(1, self.remaining()))

    def tap(self, point):
        self.remaining()
        LOGGER.info('点击坐标：(%d, %d)', *point)
        self.client.tap(*point)
        self.images.sleep(min(float(self.action.get('post_click_wait_seconds', 1)), self.remaining()))

    def button(self, key, *, click=True, timeout=20):
        target = self.action[key]
        deadline = self.elapsed() + min(timeout, self.remaining())
        while self.elapsed() < deadline:
            center, score = self.images.match(self.client.screenshot(), target)
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
