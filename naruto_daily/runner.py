from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .adb_client import AdbClient
from .visual import VisualError, find_template_in_png
from .popup import cleanup_popups
from .authorization import finish_login_authorization
from .navigation import navigate_to_card
from .prayer import handle_organization_prayer


LOGGER = logging.getLogger("naruto_daily")
ACTION_NAMES = {
    "tap": "点击",
    "swipe": "滑动",
    "back": "返回",
    "wait": "等待",
    "optional_template_tap": "识别后点击",
    "popup_cleanup": "循环清理弹窗",
    "required_template_tap": "等待识别后点击",
    "finish_login_authorization": "处理可选第二次授权",
    "find_card_and_click": "横向查找任务卡片",
    "handle_organization_prayer": "领取昨日奖励并执行铜币祈福",
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
            else float(action.get("timeout_seconds", 0))
            if action.get("type") in {"required_template_tap", "finish_login_authorization"}
            else float(action.get("max_swipes", 0)) * float(action.get("wait_seconds", 0))
            if action.get("type") == "find_card_and_click"
            else 3 * float(action.get("timeout_seconds", 0))
            + (float(action.get("max_claims", 0)) + 2) * float(action.get("wait_seconds", 0))
            if action.get("type") == "handle_organization_prayer"
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
            elif action_type == "required_template_tap":
                template_path = self.asset_root / action["template"]
                roi = (action["left"], action["top"], action["right"], action["bottom"])
                timeout_seconds = float(action["timeout_seconds"])
                poll_seconds = float(action["poll_seconds"])
                elapsed = 0.0
                while True:
                    center, score = self.template_matcher(
                        self.client.screenshot(), template_path, roi, float(action["threshold"])
                    )
                    if center is not None:
                        LOGGER.info(
                            "发现%s，相似度 %.3f，点击坐标 (%d, %d)",
                            action["name"], score, center[0], center[1],
                        )
                        self.client.tap(*center)
                        break
                    if elapsed >= timeout_seconds:
                        raise VisualError(f"等待{action['name']}超时，最后相似度 {score:.3f}")
                    delay = min(poll_seconds, timeout_seconds - elapsed)
                    LOGGER.info(
                        "等待%s，当前相似度 %.3f，%.2f 秒后重试",
                        action["name"], score, delay,
                    )
                    self.sleep(delay)
                    elapsed += delay
            elif action_type == "finish_login_authorization":
                finish_login_authorization(
                    self.client,
                    action,
                    template_matcher=self.template_matcher,
                    asset_root=self.asset_root,
                    sleep=self.sleep,
                )
            elif action_type == "find_card_and_click":
                navigation = navigate_to_card(
                    self.client,
                    action,
                    template_matcher=self.template_matcher,
                    asset_root=self.asset_root,
                    sleep=self.sleep,
                )
                if navigation.completed:
                    LOGGER.info("目标任务今日已完成，跳过后续动作")
                    break
            elif action_type == "handle_organization_prayer":
                handle_organization_prayer(
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
