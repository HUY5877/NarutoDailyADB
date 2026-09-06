"""所有游戏图片判断共用的重新截图、一次补查和超时处理。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, TypeVar

from naruto_daily.common.visual import VisualError, find_template_in_png


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

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)
        self.elapsed += seconds

    def match(self, screenshot: bytes, target: dict):
        """只匹配传入的同一帧；刷新和重试由 check 统一负责。"""
        roi = tuple(target[key] for key in ("left", "top", "right", "bottom"))
        return self.matcher(screenshot, self.asset_root / target["template"], roi, float(target["threshold"]))

    def check(self, probe: Callable[[bytes], T], found: Callable[[T], bool], name: str,
              *, deadline: float = float("inf")) -> T:
        result = probe(self.client.screenshot())
        if found(result) or self.elapsed + RETRY_SECONDS > deadline:
            return result
        LOGGER.info("未确认%s，1.00 秒后重新截图补查一次", name)
        self.sleep(RETRY_SECONDS)
        result = probe(self.client.screenshot())
        LOGGER.info("%s补查%s", name, "成功" if found(result) else "仍未确认，本次补查结束，返回当前轮次判断（未点击）")
        return result

    def find(self, target: dict, *, deadline: float = float("inf")):
        return self.check(lambda png: self.match(png, target), lambda result: result[0] is not None,
                          target.get("name", target["template"]), deadline=deadline)

    def either(self, targets: list[dict], *, deadline: float = float("inf")):
        """互斥页面在同一帧依次判断；都未发现才补查整组。"""
        def probe(png):
            results = []
            for target in targets:
                result = self.match(png, target)
                results.append(result)
                if result[0] is not None:
                    return len(results) - 1, results
            return None, results
        return self.check(probe, lambda result: result[0] is not None,
                          " / ".join(t.get("name", t["template"]) for t in targets), deadline=deadline)

    def wait_for(self, target: dict, *, timeout_seconds: float, poll_seconds: float):
        deadline = self.elapsed + timeout_seconds
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
