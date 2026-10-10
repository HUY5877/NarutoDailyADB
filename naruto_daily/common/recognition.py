"""所有游戏图片判断共用的重新截图、一次补查和超时处理。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, TypeVar

from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.execution import current_execution, retry_observation, stage_timeout


LOGGER = logging.getLogger("naruto_daily")
T = TypeVar("T")
RETRY_SECONDS = 1.0


class ImageRecognition:
    def __init__(self, client, matcher=find_template_in_png, asset_root=Path("."), sleep=time.sleep):
        self.client = client
        self.matcher = matcher
        self.asset_root = Path(asset_root)
        self._sleep = sleep
        self.elapsed = 0.0
        self._started = time.monotonic()
        self._execution = current_execution()
        self._execution_started_remaining = self._execution.remaining() if self._execution is not None else None
        self._use_wall_clock = False
        self._main_models = {}
        self._checking = 0
        # 同一动作内累计两阶段的次数，切换阶段或进入下一轮都不清零。
        self.main_calls = {'查找': 0, '点击前复查': 0}
        self.main_call_phase = '查找'
        self.main_call_limit = None

    @property
    def elapsed(self):
        if self._execution is not None:
            return max(self._elapsed, self._execution_started_remaining - self._execution.remaining())
        return max(self._elapsed, time.monotonic() - self._started) if self._use_wall_clock else self._elapsed

    @elapsed.setter
    def elapsed(self, value):
        self._elapsed = value

    def sleep(self, seconds: float) -> None:
        if self._execution is not None:
            self._execution.sleep(seconds)
        else:
            self._sleep(seconds)
        self._elapsed += seconds

    def match(self, screenshot: bytes, target: dict, *, timeout_seconds=None):
        """模板只匹配传入帧；模型接口重试时重新截图并重建候选坐标。"""
        if 'main_menu' in target:
            from .main_screen import MainScreenRecognition
            self._use_wall_clock = True
            phase = self.main_call_phase
            if self.main_call_limit is not None and self.main_calls[phase] >= self.main_call_limit:
                raise VisualError(f"{target['name']}的{phase}已达到模型调用上限，未继续点击")
            key = tuple(sorted(target['multimodal'].items()))
            if key not in self._main_models:
                self._main_models[key] = MainScreenRecognition(target['multimodal'], self.asset_root)
            first_frame = True

            def observe():
                nonlocal first_frame
                png = screenshot if first_frame else self.client.screenshot()
                first_frame = False
                return self._main_models[key].match(
                    png, target, stage_timeout(timeout_seconds) if self._execution is not None else timeout_seconds,
                )

            # check/either 必须整组重拍，避免前后目标分别使用新旧两帧。
            result = observe() if self._checking else retry_observation(observe, name=target['name'])
            self.main_calls[phase] += 1
            if self.main_call_limit is not None:
                LOGGER.info('%s：%s模型调用 %d/%d', target['name'], phase,
                            self.main_calls[phase], self.main_call_limit)
            return result
        roi = tuple(target[key] for key in ("left", "top", "right", "bottom"))
        best = (None, -1.0)
        for template in [target["template"], *target.get("alternate_templates", [])]:
            result = self.matcher(screenshot, self.asset_root / template, roi, float(target["threshold"]))
            if result[0] is not None:
                return result
            if result[1] > best[1]:
                best = result
        return best

    def check(self, probe: Callable[[bytes], T], found: Callable[[T], bool], name: str,
              *, deadline: float = float("inf")) -> T:
        def observe():
            execution = current_execution()
            if execution is not None:
                execution.remaining()
            self._checking += 1
            try:
                result = probe(self.client.screenshot())
            finally:
                self._checking -= 1
            if execution is not None:
                execution.remaining()
            return result

        if self._execution is not None:
            deadline = self.elapsed + self._execution.remaining()
        result = retry_observation(observe, name=name)
        if found(result) or self.elapsed + RETRY_SECONDS > deadline:
            return result
        LOGGER.info("未确认%s，1.00 秒后重新截图补查一次", name)
        self.sleep(RETRY_SECONDS)
        result = retry_observation(observe, name=name)
        LOGGER.info("%s补查%s", name, "成功" if found(result) else "仍未确认，本次补查结束，返回当前轮次判断（未点击）")
        return result

    def find(self, target: dict, *, deadline: float = float("inf")):
        self._use_wall_clock |= 'main_menu' in target
        return self.check(lambda png: self.match(png, target, timeout_seconds=deadline-self.elapsed),
                          lambda result: result[0] is not None,
                          target.get("name", target.get("template", "主界面")), deadline=deadline)

    def either(self, targets: list[dict], *, deadline: float = float("inf")):
        """互斥页面在同一帧依次判断；都未发现才补查整组。"""
        self._use_wall_clock |= any('main_menu' in target for target in targets)
        def probe(png):
            results = []
            for target in targets:
                result = self.match(png, target, timeout_seconds=deadline-self.elapsed)
                results.append(result)
                if result[0] is not None:
                    return len(results) - 1, results
            return None, results
        return self.check(probe, lambda result: result[0] is not None,
                          " / ".join(t.get("name", t.get("template", "主界面")) for t in targets), deadline=deadline)

    def wait_for(self, target: dict, *, timeout_seconds: float, poll_seconds: float):
        deadline = self.elapsed + stage_timeout(timeout_seconds)
        round_number = 0
        while True:
            round_number += 1
            LOGGER.info('[等待%s 第%d轮] 截图识别，未找到则补查；成功返回，超时停止；剩余等待 %.2f 秒',
                        target['name'], round_number, max(0, deadline - self.elapsed))
            center, score = self.find(target, deadline=deadline)
            if center is not None:
                LOGGER.info("发现%s，相似度 %.3f，坐标：(%d, %d)", target["name"], score, *center)
                return center
            if self.elapsed >= deadline:
                raise VisualError(f"等待{target['name']}超时，最后相似度 {score:.3f}")
            delay = min(poll_seconds, deadline - self.elapsed)
            LOGGER.info('本轮未找到%s，等待 %.2f 秒后开始下一轮截图识别', target['name'], delay)
            self.sleep(delay)
