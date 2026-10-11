from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.execution import check_execution, current_execution, stage_timeout
from naruto_daily.common.reward_cards import reward_card_displacement, reward_card_swipe_starts
from naruto_daily.common.visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")
# 1920×1080 奖励页统一参数，所有任务的常规查找和卡片补齐共用。
REWARD_CARD_SWIPE_DISTANCE = 450
REWARD_CARD_SWIPE_DURATION_MS = 600
REWARD_CARD_WAIT_SECONDS = 2
REWARD_CARD_MAX_SWIPES = 10


class _RewardCardSwipes:
    """同一静止视图换起点重试；只有截图确认移动才计入滑动上限。"""

    def __init__(self):
        self.successful = 0
        self.attempts = 0
        self.alignment_used = False
        self._alignment_pending = False
        self._before = None
        self._latest = None
        self._start = None
        self._failed_starts = []

    def observe(self, png):
        self._latest = png
        if self._before is None:
            return
        displacement = reward_card_displacement(self._before, png)
        if displacement is None:
            LOGGER.info('奖励卡片第%d次尝试：位移尚未确认，重新截图后再判断', self.attempts)
            return
        if abs(displacement) > 10:
            self.successful += 1
            self.alignment_used |= self._alignment_pending
            self._failed_starts.clear()
            LOGGER.info('奖励卡片第%d次尝试：实际向%s移动 %.0f 像素，累计成功滑动 %d 次',
                        self.attempts, '左' if displacement < 0 else '右',
                        abs(displacement), self.successful)
        else:
            self._failed_starts.append(self._start)
            LOGGER.info('奖励卡片第%d次尝试：未产生位移（%.0f 像素），不计入成功次数；换卡片内部起点重试',
                        self.attempts, displacement)
        self._before = None

    def swipe(self, client, sleep, *, alignment=False):
        if self._before is not None:
            raise VisualError('上次滑动的位移尚未确认')
        starts = reward_card_swipe_starts(self._latest)
        if not starts:
            raise VisualError('尚未确认可靠的卡片内部起点')
        start = next((point for point in starts if all(
            abs(point[0] - failed[0]) > 30 for failed in self._failed_starts)), None)
        if start is None:
            raise VisualError('多个卡片内部起点均未产生位移，可能已到列表末端，暂停滑动')
        x, y = start
        LOGGER.info('奖励卡片第%d次尝试：从卡片内部 (%d, %d) 左滑至 (%d, %d)，持续%d毫秒；已成功 %d 次',
                    self.attempts + 1, x, y, x - REWARD_CARD_SWIPE_DISTANCE, y,
                    REWARD_CARD_SWIPE_DURATION_MS, self.successful)
        client.swipe(x, y, x - REWARD_CARD_SWIPE_DISTANCE, y, REWARD_CARD_SWIPE_DURATION_MS)
        self.attempts += 1
        self._before, self._start = self._latest, start
        # A missed label on the retry frame must not turn a failed alignment
        # into a new ordinary search and grant another successful alignment.
        self._alignment_pending |= alignment
        sleep(REWARD_CARD_WAIT_SECONDS)


@dataclass(frozen=True)
class NavigationSummary:
    """记录截图确认的成功横向滑动次数，以及目标任务是否已经完成。"""

    swipes: int
    completed: bool


def navigate_to_card(
    client,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> NavigationSummary:
    """横向查找指定任务卡片，并点击该卡片下方对应的前往按钮。"""

    name = action["name"]
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    shared_budget = current_execution()
    max_swipes = action.get("max_swipes", REWARD_CARD_MAX_SWIPES)
    swipes = _RewardCardSwipes()
    latest_png = None
    blocked_observations = 0

    def scan_card(png):
        nonlocal latest_png
        latest_png = png
        center, score = images.match(png, action)
        if center is None:
            return center, score, None, 0.0, None, 0.0
        region = dict(
            left=max(0, center[0] + action["button_left_offset"]),
            top=max(0, center[1] + action["button_top_offset"]),
            right=min(action["right"], center[0] + action["button_right_offset"]),
            bottom=center[1] + action["button_bottom_offset"],
        )
        completed, completed_score = images.match(png, dict(region,
            template=action["completed_template"], threshold=action["completed_threshold"]))
        if completed is not None:
            return center, score, completed, completed_score, None, 0.0
        button, button_score = images.match(png, dict(region,
            template=action["button_template"], threshold=action["button_threshold"]))
        return center, score, completed, completed_score, button, button_score

    def check_card():
        # 每次补查都重新定位文字、已完成标记及该卡片的按钮。
        return images.check(scan_card, lambda r: r[2] is not None or r[4] is not None, name)

    def wait_for_card_confirmation(reason):
        LOGGER.info('%s：%s；暂停滑动和点击，1 秒后重新截图，总时限剩余 %.2f 秒',
                    name, reason, shared_budget.remaining())
        images.sleep(1)

    def swipe_or_reobserve(*, alignment=False):
        nonlocal blocked_observations
        try:
            swipes.swipe(client, images.sleep, alignment=alignment)
            blocked_observations = 0
        except VisualError as exc:
            blocked_observations += 1
            if shared_budget is not None:
                wait_for_card_confirmation(str(exc))
            elif blocked_observations < 2:
                LOGGER.info('%s；暂停输入，1 秒后重新截图', exc)
                images.sleep(1)
            else:
                raise

    observation_only = False
    round_number = 0
    while True:
        round_number += 1
        if shared_budget is not None:
            LOGGER.info('[查找%s 第%d轮] 定位卡片→检查已完成标记或前往按钮；达到成功滑动上限后暂停滑动并继续观察；已成功 %d 次，上限 %d 次，总时限剩余 %.2f 秒',
                        name, round_number, swipes.successful, max_swipes, shared_budget.remaining())
        else:
            LOGGER.info('[查找%s 第%d轮] 定位卡片→检查已完成标记或前往按钮；未找到则滑动，达到上限后跳过或报错；已成功 %d 次，常规上限 %d 次',
                        name, round_number, swipes.successful, max_swipes)
        center, score, completed_center, completed_score, button_center, button_score = check_card()
        swipes.observe(latest_png)
        if center is not None:
            LOGGER.info(
                "发现%s，相似度 %.3f，文字中心：(%d, %d)",
                name,
                score,
                center[0],
                center[1],
            )
            if completed_center is not None:
                LOGGER.info(
                    "今日%s已完成，相似度 %.3f，跳过执行",
                    name,
                    completed_score,
                )
                return NavigationSummary(swipes=swipes.successful, completed=True)

            if button_center is None:
                if not swipes.alignment_used and not observation_only and swipes.successful < max_swipes:
                    LOGGER.info(
                        "找到%s文字，但按钮和已完成标记都不完整，额外左滑一次后重新判断",
                        name,
                    )
                    swipe_or_reobserve(alignment=True)
                    continue
                if shared_budget is not None:
                    observation_only = True
                    wait_for_card_confirmation('已找到卡片文字，尚未确认对应按钮或已完成标记')
                    continue
                if action.get("completed_skip_type"):
                    LOGGER.info(
                        "额外左滑后仍未找到可执行按钮，跳过%s这个可选任务",
                        name,
                    )
                    return NavigationSummary(swipes=swipes.successful, completed=True)
                raise VisualError(
                    f"找到{name}，但未找到它下方的立刻前往，最后相似度 {button_score:.3f}"
                )
            LOGGER.info(
                "发现对应的立刻前往，相似度 %.3f，点击前等待 %.2f 秒",
                button_score,
                float(action.get("pre_click_wait_seconds", 1)),
            )
            images.sleep(float(action.get("pre_click_wait_seconds", 1)))
            _, _, completed_center, completed_score, confirmed_center, confirmed_score = check_card()
            swipes.observe(latest_png)
            if completed_center is not None:
                LOGGER.info("今日%s已完成，相似度 %.3f，取消点击并跳过执行", name, completed_score)
                return NavigationSummary(swipes=swipes.successful, completed=True)
            if confirmed_center is None:
                if shared_budget is not None:
                    observation_only = True
                    wait_for_card_confirmation('点击前复查尚未确认目标，取消本次点击')
                    continue
                raise VisualError(
                    f"找到{name}的立刻前往，但点击前复查及补查均未确认，"
                    f"最后相似度 {confirmed_score:.3f}"
                )
            LOGGER.info(
                "再次确认对应的立刻前往，相似度 %.3f，点击坐标：(%d, %d)",
                confirmed_score,
                confirmed_center[0],
                confirmed_center[1],
            )
            client.tap(*confirmed_center)
            LOGGER.info(
                "点击立刻前往后等待页面响应 %.2f 秒",
                float(action.get("post_click_wait_seconds", 1)),
            )
            images.sleep(float(action.get("post_click_wait_seconds", 1)))
            return NavigationSummary(swipes=swipes.successful, completed=False)

        if shared_budget is not None and (observation_only or swipes.alignment_used or swipes.successful >= max_swipes):
            observation_only = True
            wait_for_card_confirmation('尚未确认目标卡片，继续观察当前页面')
            continue
        if swipes.alignment_used:
            if action.get("completed_skip_type"):
                LOGGER.info(
                    "额外左滑后未再找到%s，跳过这个可选任务",
                    name,
                )
                return NavigationSummary(swipes=swipes.successful, completed=True)
            raise VisualError(f"额外左滑后未再找到{name}，最后相似度 {score:.3f}")

        if swipes.successful >= max_swipes:
            if action.get("completed_skip_type"):
                LOGGER.info(
                    "滑动 %d 次后仍未找到%s，跳过这个可选任务",
                    max_swipes,
                    name,
                )
                return NavigationSummary(swipes=swipes.successful, completed=True)
            raise VisualError(f"滑动 {max_swipes} 次后仍未找到{name}，最后相似度 {score:.3f}")

        LOGGER.info(
            "当前页面未找到%s，相似度 %.3f，准备左滑；已成功滑动 %d/%d 次",
            name,
            score,
            swipes.successful,
            max_swipes,
        )
        swipe_or_reobserve()


def _confirm_reward_page(images, deadline):
    """入口点击不等于进入成功；等待卡片布局出现后才允许后续操作。"""
    deadline = min(deadline, images.elapsed + 10)
    while images.elapsed < deadline:
        # This bounded postcondition must not inherit check()'s observation
        # retries, which intentionally use the entire daily execution budget.
        check_execution()
        ready = bool(reward_card_swipe_starts(images.client.screenshot()))
        check_execution()
        if images.elapsed >= deadline:
            break
        if ready:
            LOGGER.info('已确认奖励任务页卡片布局，继续执行')
            return
        if images.elapsed < deadline:
            LOGGER.info('尚未确认奖励任务页卡片布局，继续截图观察（剩余 %.2f 秒）',
                        deadline - images.elapsed)
            images.sleep(min(1, deadline - images.elapsed))
    raise VisualError('点击奖励入口后未确认奖励任务页，停止后续卡片查找和点击')


def required_template_tap(client, action, *, template_matcher=find_template_in_png,
                          asset_root=Path("."), sleep=time.sleep):
    """等待、复查并点击必需按钮；主界面目标通过 ImageRecognition 使用模型。"""
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    is_main = 'main_menu' in action
    if is_main and current_execution() is None:
        images.main_call_limit = action['multimodal']['max_calls']
    timeout_seconds = stage_timeout(float(action["timeout_seconds"]))
    poll_seconds = float(action["poll_seconds"])
    pre_click_wait_seconds = float(action.get("pre_click_wait_seconds", 1))
    post_click_wait_seconds = float(action.get("post_click_wait_seconds", 1))
    deadline = images.elapsed + timeout_seconds
    round_number = 0
    while True:
        round_number += 1
        LOGGER.info('[等待点击%s 第%d轮] 查找按钮→等待后复查→点击；奖励入口另需确认任务页；未确认则轮询，超时停止；剩余等待 %.2f 秒',
                    action['name'], round_number, max(0, deadline - images.elapsed))
        if is_main:
            images.main_call_phase = '查找'
        center, score = images.find(action, deadline=deadline)
        if center is not None:
            if is_main:
                LOGGER.info('模型已确认%s，点击前等待 %.2f 秒', action['name'], pre_click_wait_seconds)
            else:
                LOGGER.info("发现%s，相似度 %.3f，点击前等待 %.2f 秒",
                            action["name"], score, pre_click_wait_seconds)
            images.sleep(pre_click_wait_seconds)
            if is_main:
                images.main_call_phase = '点击前复查'
            confirmed_center, confirmed_score = images.find(action, deadline=deadline)
            if confirmed_center is None:
                LOGGER.info('等待后未确认%s，本轮不点击旧坐标', action['name'])
            else:
                if is_main:
                    LOGGER.info('模型复查确认%s，点击坐标 %s', action['name'], confirmed_center)
                else:
                    LOGGER.info("再次确认%s，相似度 %.3f，点击坐标 (%d, %d)",
                                action["name"], confirmed_score, confirmed_center[0], confirmed_center[1])
                client.tap(*confirmed_center)
                LOGGER.info(
                    "点击完成，等待 %.2f 秒让页面响应",
                    post_click_wait_seconds,
                )
                images.sleep(post_click_wait_seconds)
                if action.get('main_menu') == 'rewards':
                    _confirm_reward_page(images, deadline)
                break
        if images.elapsed >= deadline:
            if is_main:
                raise VisualError(f"等待模型确认{action['name']}超时，未继续点击")
            raise VisualError(f"等待{action['name']}超时，最后相似度 {score:.3f}")
        delay = min(poll_seconds, deadline - images.elapsed)
        if is_main:
            LOGGER.info('模型尚未确认%s，%.2f 秒后重试', action['name'], delay)
        else:
            LOGGER.info("等待%s，当前相似度 %.3f，%.2f 秒后重试", action["name"], score, delay)
        images.sleep(delay)
