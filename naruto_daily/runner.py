from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from naruto_daily.common.adb_client import AdbClient
from naruto_daily.common.recognition import ImageRecognition, extra_retry_wait
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.login import cleanup_popups
from naruto_daily.return_to_main import return_to_main
from naruto_daily.login.authorization import finish_login_authorization, authorize_account
from naruto_daily.common.navigation import navigate_to_card
from naruto_daily.organization_prayer import handle_organization_prayer
from .fortune import handle_coin_fortune
from .recruit import handle_free_recruit
from .abundance import handle_abundance, abundance_wait_budget


LOGGER = logging.getLogger("naruto_daily")
ACTION_NAMES = {
    "authorize_account": "选择指定 QQ 账号并授权",
    "handle_abundance": "挑战丰饶之间并执行通用战斗",
    "tap": "点击",
    "swipe": "滑动",
    "back": "返回",
    "wait": "等待",
    "optional_template_tap": "识别后点击",
    "popup_cleanup": "循环清理弹窗",
    "return_to_main": "逐层关闭界面并返回主界面",
    "required_template_tap": "等待识别后点击",
    "finish_login_authorization": "处理可选第二次授权",
    "find_card_and_click": "横向查找任务卡片",
    "handle_organization_prayer": "领取昨日奖励并执行铜币祈福",
    "handle_coin_fortune": "执行两次免费招财",
    "handle_free_recruit": "检查并执行高级和普通免费招募",
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
            else float(action.get("rounds", 0)) * len(action.get("targets", [])) * (
                float(action.get("wait_seconds", 0))
                + float(action.get("pre_click_wait_seconds", 1))
                + float(action.get("post_click_wait_seconds", 1))
            )
            if action.get("type") == "popup_cleanup"
            else float(action.get("max_rounds", 0)) * (
                float(action.get("poll_seconds", 0))
                + float(action.get("pre_click_wait_seconds", 1))
                + float(action.get("post_click_wait_seconds", 1))
            )
            if action.get("type") == "return_to_main"
            else float(action.get("timeout_seconds", 0))
            + float(action.get("pre_click_wait_seconds", 1))
            + float(action.get("post_click_wait_seconds", 1))
            if action.get("type") == "required_template_tap"
            else float(action.get("timeout_seconds", 0))
            + 2 * (
                float(action.get("pre_click_wait_seconds", 3))
                + float(action.get("post_click_wait_seconds", 3))
            )
            if action.get("type") == "finish_login_authorization"
            else float(action.get("max_swipes", 0)) * float(action.get("wait_seconds", 0))
            + float(action.get("pre_click_wait_seconds", 1))
            + float(action.get("post_click_wait_seconds", 1))
            if action.get("type") == "find_card_and_click"
            else 3 * float(action.get("timeout_seconds", 0))
            + (float(action.get("max_claims", 0)) + 2) * (
                float(action.get("wait_seconds", 0))
                + float(action.get("pre_click_wait_seconds", 1))
                + float(action.get("post_click_wait_seconds", 1))
            )
            if action.get("type") == "handle_organization_prayer"
            else float(action.get("clicks", 0)) * (
                float(action.get("timeout_seconds", 0))
                + float(action.get("pre_click_wait_seconds", 1))
                + float(action.get("post_click_wait_seconds", 1))
            )
            if action.get("type") == "handle_coin_fortune"
            else 6 * (
                float(action.get("timeout_seconds", 0))
                + float(action.get("pre_click_wait_seconds", 1))
                + float(action.get("post_click_wait_seconds", 1))
            )
            if action.get("type") == "handle_free_recruit"
            else float(action.get("timeout_seconds", 0))
            if action.get("type") == "authorize_account"
            else 0.0
            for action in actions
        )
        wait_seconds += sum(extra_retry_wait(action) for action in actions)
        wait_seconds += sum(abundance_wait_budget(action) for action in actions if action.get("type") == "handle_abundance")
        if wait_seconds > 1200:
            raise ActionLimitError("动作序列等待时间超过 1200 秒")
        LOGGER.info("开始执行，共 %d 个动作", len(actions))
        images = ImageRecognition(self.client, self.template_matcher, self.asset_root, self.sleep)
        skip_action_type: str | None = None
        completed_card_name = ""
        for index, action in enumerate(actions, start=1):
            LOGGER.info("动作 %d/%d：%s", index, len(actions), ACTION_NAMES.get(action["type"], "未知动作"))
            if self.dry_run:
                LOGGER.info("模拟运行，不发送输入")
                continue
            action_type = action["type"]
            if skip_action_type is not None:
                if action_type == skip_action_type:
                    LOGGER.info("%s今日已完成，跳过对应页面处理", completed_card_name)
                    skip_action_type = None
                    completed_card_name = ""
                    continue
                LOGGER.info("已完成任务后未紧跟对应处理动作，不继续保留跳过标记")
                skip_action_type = None
                completed_card_name = ""
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
                center, score = images.find(action)
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
            elif action_type == "return_to_main":
                return_to_main(
                    self.client,
                    action,
                    template_matcher=self.template_matcher,
                    asset_root=self.asset_root,
                    sleep=self.sleep,
                )
            elif action_type == "required_template_tap":
                timeout_seconds = float(action["timeout_seconds"])
                poll_seconds = float(action["poll_seconds"])
                pre_click_wait_seconds = float(action.get("pre_click_wait_seconds", 1))
                post_click_wait_seconds = float(action.get("post_click_wait_seconds", 1))
                deadline = images.elapsed + timeout_seconds
                round_number = 0
                while True:
                    round_number += 1
                    LOGGER.info('[等待点击%s 第%d轮] 查找按钮→等待后复查→点击后结束；未确认则轮询，超时停止；剩余等待 %.2f 秒',
                                action['name'], round_number, max(0, deadline - images.elapsed))
                    center, score = images.find(action, deadline=deadline)
                    if center is not None:
                        LOGGER.info(
                            "发现%s，相似度 %.3f，点击前等待 %.2f 秒",
                            action["name"], score, pre_click_wait_seconds,
                        )
                        images.sleep(pre_click_wait_seconds)
                        confirmed_center, confirmed_score = images.find(action, deadline=deadline)
                        if confirmed_center is None:
                            LOGGER.info(
                                "等待后%s已消失，相似度 %.3f，继续识别",
                                action["name"], confirmed_score,
                            )
                        else:
                            LOGGER.info(
                                "再次确认%s，相似度 %.3f，点击坐标 (%d, %d)",
                                action["name"], confirmed_score, confirmed_center[0], confirmed_center[1],
                            )
                            self.client.tap(*confirmed_center)
                            LOGGER.info(
                                "点击完成，等待 %.2f 秒让页面响应",
                                post_click_wait_seconds,
                            )
                            images.sleep(post_click_wait_seconds)
                            break
                    if images.elapsed >= deadline:
                        raise VisualError(f"等待{action['name']}超时，最后相似度 {score:.3f}")
                    delay = min(poll_seconds, deadline - images.elapsed)
                    LOGGER.info(
                        "等待%s，当前相似度 %.3f，%.2f 秒后重试",
                        action["name"], score, delay,
                    )
                    images.sleep(delay)
            elif action_type == "authorize_account":
                authorize_account(self.client, action, template_matcher=self.template_matcher,
                                  asset_root=self.asset_root, sleep=self.sleep)
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
                    skip_action_type = action.get("completed_skip_type", "handle_organization_prayer")
                    completed_card_name = action["name"]
            elif action_type == "handle_organization_prayer":
                handle_organization_prayer(
                    self.client,
                    action,
                    template_matcher=self.template_matcher,
                    asset_root=self.asset_root,
                    sleep=self.sleep,
                )
            elif action_type == "handle_coin_fortune":
                handle_coin_fortune(
                    self.client,
                    action,
                    template_matcher=self.template_matcher,
                    asset_root=self.asset_root,
                    sleep=self.sleep,
                )
            elif action_type == "handle_abundance":
                handle_abundance(
                    self.client, action, template_matcher=self.template_matcher,
                    asset_root=self.asset_root, sleep=self.sleep,
                )
            elif action_type == "handle_free_recruit":
                handle_free_recruit(
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
