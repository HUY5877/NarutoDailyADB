from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .adb_client import AdbClient
from .visual import find_template_in_png
from .popup import cleanup_popups


LOGGER = logging.getLogger("naruto_daily")
ACTION_NAMES = {
    "tap": "点击",
    "swipe": "滑动",
    "back": "返回",
    "wait": "等待",
    "optional_template_tap": "识别后点击",
    "popup_cleanup": "循环清理弹窗",
}


class ActionLimitError(ValueError):
    """动作序列超过安全限制。"""


@dataclass(frozen=True)
class RunSummary:
    action_count: int
    wait_seconds: float


class ActionRunner:
    def __init__(
        self,
        client: AdbClient,
        sleep: Callable[[float], None] = time.sleep,
        dry_run: bool = False,
        template_matcher: Callable = find_template_in_png,
        asset_root: Path = Path("."),
    ):
        self.client = client
        self.sleep = sleep
        self.dry_run = dry_run
        self.template_matcher = template_matcher
        self.asset_root = Path(asset_root)

    def run(self, actions: Sequence[dict]) -> RunSummary:
        if len(actions) > 100:
            raise ActionLimitError("动作序列超过 100 个动作")
        wait_seconds = sum(
            float(action.get("seconds", 0))
            if action.get("type") == "wait"
            else float(action.get("rounds", 0)) * len(action.get("targets", [])) * float(action.get("wait_seconds", 0))
            if action.get("type") == "popup_cleanup"
            else 0.0
            for action in actions
        )
        if wait_seconds > 300:
            raise ActionLimitError("动作序列等待时间超过 300 秒")
        LOGGER.info("开始执行，共 %d 个动作", len(actions))
        for index, action in enumerate(actions, start=1):
            LOGGER.info("动作 %d/%d：%s", index, len(actions), ACTION_NAMES.get(action["type"], "未知动作"))
            if self.dry_run:
                LOGGER.info("模拟运行，不发送输入")
                continue
            action_type = action["type"]
            if action_type == "tap":
                LOGGER.info("点击坐标：(%d, %d)", action["x"], action["y"])
                self.client.tap(action["x"], action["y"])
            elif action_type == "swipe":
                self.client.swipe(action["x1"], action["y1"], action["x2"], action["y2"], action["duration_ms"])
            elif action_type == "back":
                LOGGER.info("发送安卓返回键")
                self.client.back()
            elif action_type == "wait":
                LOGGER.info("等待 %.2f 秒", float(action["seconds"]))
                self.sleep(float(action["seconds"]))
            elif action_type == "optional_template_tap":
                template_path = self.asset_root / action["template"]
                roi = (action["left"], action["top"], action["right"], action["bottom"])
                center, score = self.template_matcher(
                    self.client.screenshot(), template_path, roi, float(action["threshold"])
                )
                LOGGER.info("模板相似度：%.3f，阈值：%.3f", score, float(action["threshold"]))
                if center is None:
                    LOGGER.info("未发现领取按钮，跳过")
                else:
                    LOGGER.info("发现领取按钮，点击坐标：(%d, %d)", center[0], center[1])
                    self.client.tap(*center)
            elif action_type == "popup_cleanup":
                cleanup_popups(
                    self.client,
                    action,
                    template_matcher=self.template_matcher,
                    asset_root=self.asset_root,
                    sleep=self.sleep,
                )
            else:
                raise ValueError(f"不支持的动作类型：{action_type!r}")
        LOGGER.info("执行结束：%d 个动作，配置等待 %.2f 秒", len(actions), wait_seconds)
        return RunSummary(len(actions), wait_seconds)
