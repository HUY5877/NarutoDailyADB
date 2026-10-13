from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.execution import current_execution, stage_timeout
from naruto_daily.multimodal.client import MultimodalClient
from .free_button import click_free_recruit
from naruto_daily.common.visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")

# 1920×1080：只在左侧列表慢滑约一个标签的高度，避免惯性滚动越过目标。
ORDINARY_SCROLL = (120, 900, 120, 780, 500)
ORDINARY_MAX_SWIPES = 8


def _find_ordinary_targets(images: ImageRecognition, action: dict) -> tuple[dict, dict]:
    target = dict(action['ordinary_tab'], top=200)
    execution = current_execution()
    deadline = images.elapsed + float(action['timeout_seconds'])
    wall_deadline = time.monotonic() + float(action['timeout_seconds'])
    swipes = 0
    while True:
        remaining = (execution.remaining() if execution is not None else
                     min(deadline - images.elapsed, wall_deadline - time.monotonic()))
        if remaining <= 0:
            raise VisualError('查找普通招募标签超时，未继续滑动或点击')
        LOGGER.info('[查找普通招募 第%d轮] 重新识别左侧标签；已小幅滑动 %d/%d 次',
                    swipes + 1, swipes, ORDINARY_MAX_SWIPES)
        center, score = images.find(target, deadline=images.elapsed + remaining)
        remaining = (execution.remaining() if execution is not None else
                     min(deadline - images.elapsed, wall_deadline - time.monotonic()))
        if remaining <= 0:
            raise VisualError('查找普通招募标签超时，未继续滑动或点击')
        if center is not None:
            # 随标签实际位置收紧两个区域，防止将活动标签的红点当成普通招募红点。
            y = center[1]
            tab = dict(target, top=max(200, y - 55), bottom=min(target['bottom'], y + 55))
            guard = dict(action['ordinary_red_dot'], top=max(200, y - 70), bottom=y - 10)
            LOGGER.info('找到普通招募标签，相似度 %.3f，坐标 (%d, %d)，只检查该标签旁红点',
                        score, *center)
            return tab, guard
        if swipes >= ORDINARY_MAX_SWIPES:
            if execution is not None:
                LOGGER.info('普通招募尚未确认，停止增加滑动，等待后重新截图；总时限剩余 %.2f 秒', remaining)
                images.sleep(min(max(1.0, float(action['poll_seconds'])), remaining))
                continue
            raise VisualError(f'小幅滑动{swipes}次仍未找到普通招募标签，已停止')
        # 手指短距离上移，使列表向下展示后续活动；每次都等页面停稳再截图。
        wait = max(1.0, float(action['poll_seconds']))
        if remaining <= ORDINARY_SCROLL[-1] / 1000 + wait:
            if execution is not None:
                images.sleep(min(wait, remaining))
                continue
            raise VisualError('查找普通招募标签剩余时间不足，未继续滑动')
        swipes += 1
        LOGGER.info('普通招募尚未显示，手指向上短滑120像素，让列表向下滚，第%d/%d次；停稳后重新识别',
                    swipes, ORDINARY_MAX_SWIPES)
        images.client.swipe(*ORDINARY_SCROLL)
        if current_execution() is None:
            images.elapsed += ORDINARY_SCROLL[-1] / 1000
        images.sleep(wait)


@dataclass(frozen=True)
class RecruitSummary:
    advanced_clicks: int
    ordinary_clicks: int


def _click_confirmed_target(
    client, target: dict, *, images: ImageRecognition,
    timeout_seconds: float, poll_seconds: float,
    pre_click_wait: float, post_click_wait: float,
    guard_target: dict | None = None, guard_name: str = "",
) -> bool:
    deadline = images.elapsed + stage_timeout(timeout_seconds)

    def confirm(png):
        button = images.match(png, target)
        guard = images.match(png, guard_target) if guard_target is not None else ((0, 0), 1.0)
        return button, guard

    round_number = 0
    while True:
        round_number += 1
        LOGGER.info('[招募：%s 第%d轮] 查找按钮→等待后复查%s→点击；未确认则轮询，超时停止；剩余等待 %.2f 秒',
                    target['name'], round_number, '按钮和红点' if guard_target else '按钮', max(0, deadline - images.elapsed))
        center, score = images.find(target, deadline=deadline)
        if center is not None:
            LOGGER.info("发现%s，相似度 %.3f，点击前等待 %.2f 秒", target["name"], score, pre_click_wait)
            images.sleep(pre_click_wait)
            # 补查必须重新识别按钮和红点；不允许跨截图拼接点击条件。
            button, guard = images.check(confirm,
                lambda r: r[0][0] is not None and r[1][0] is not None,
                target["name"], deadline=deadline)
            if guard[0] is None:
                LOGGER.info("点击%s前%s红点已消失，取消点击；当前相似度 %.3f",
                            target["name"], guard_name, guard[1])
                return False
            if button[0] is not None:
                LOGGER.info("再次确认%s，相似度 %.3f，点击坐标：(%d, %d)",
                            target["name"], button[1], *button[0])
                client.tap(*button[0])
                LOGGER.info("点击完成，等待页面响应 %.2f 秒", post_click_wait)
                images.sleep(post_click_wait)
                return True
        if images.elapsed >= deadline:
            raise VisualError(f"等待{target['name']}超时，最后相似度 {score:.3f}")
        delay = min(poll_seconds, deadline - images.elapsed)
        LOGGER.info('本轮未确认%s，等待 %.2f 秒后重新查找', target['name'], delay)
        images.sleep(delay)


def _confirm_recruit_result(
    client, action: dict, *, images: ImageRecognition,
    timeout_seconds: float, poll_seconds: float,
    pre_click_wait: float, post_click_wait: float,
) -> bool:
    """忍者展示是可选的第一层，普通结果确定是必需的最后一层。"""
    options = dict(images=images, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
                   pre_click_wait=pre_click_wait, post_click_wait=post_click_wait)
    result_target = action["result_confirm"]
    if "ninja_result_confirm" not in action:
        return _click_confirmed_target(client, result_target, **options)

    targets = [action["ninja_result_confirm"], result_target]
    deadline = images.elapsed + stage_timeout(timeout_seconds)
    while images.elapsed < deadline:
        LOGGER.info('识别招募结果页面：获得忍者时先关闭展示页，再关闭普通结果页')
        index, _ = images.either(targets, deadline=deadline)
        if index is not None:
            images.sleep(pre_click_wait)
            if images.elapsed >= deadline:
                break
            # 点击前同时复查两层，页面切换时按最新截图选择按钮。
            index, results = images.either(targets, deadline=deadline)
            if images.elapsed >= deadline:
                break
            if index is not None:
                center, score = results[index]
                LOGGER.info('再次确认%s，相似度 %.3f，点击坐标：(%d, %d)',
                            targets[index]['name'], score, *center)
                client.tap(*center)
                images.sleep(post_click_wait)
                if len(targets) == 2 and index == 0:
                    LOGGER.info('获得忍者展示页已关闭，等待招募结果动画结束，再确认普通结果页')
                    targets = [result_target]
                    continue
                return True
        if images.elapsed >= deadline:
            break
        images.sleep(min(poll_seconds, deadline - images.elapsed))
    raise VisualError('等待招募结果最终确定按钮超时，未完成结果确认')


def handle_free_recruit(
    client, action: dict, *, template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."), sleep: Callable[[float], None] = time.sleep,
) -> RecruitSummary:
    """只在同一帧确认对应红点和免费按钮后点击，所有识别共用一次补查。"""
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    timeout = float(action["timeout_seconds"])
    poll = float(action["poll_seconds"])
    pre_wait = float(action.get("pre_click_wait_seconds", 1))
    post_wait = float(action.get("post_click_wait_seconds", 1))
    images.wait_for(action["advanced_tab"], timeout_seconds=timeout, poll_seconds=poll)
    clicks = {"advanced": 0, "ordinary": 0}
    model = None
    for round_number, (key, name) in enumerate((("advanced", "高级招募"), ("ordinary", "普通招募")), 1):
        tab, guard = action[key + '_tab'], action[key + '_red_dot']
        if key == 'ordinary':
            tab, guard = _find_ordinary_targets(images, action)
        LOGGER.info('[免费招募 第%d/2轮：%s] 检查红点；有红点则切换标签→模型确认免费按钮→招募→关闭结果，未确认红点则跳过本项',
                    round_number, name)
        center, score = images.find(guard)
        if center is None:
            LOGGER.info("未识别到%s红点，跳过本项；相似度 %.3f，阈值 %.3f，搜索区域 (%d, %d)-(%d, %d)",
                        name, score, guard["threshold"], guard["left"], guard["top"], guard["right"], guard["bottom"])
            continue
        LOGGER.info("%s存在红点，相似度 %.3f，继续操作前等待 %.2f 秒", name, score, pre_wait)
        images.sleep(pre_wait)
        center, score = images.find(guard)
        if center is None:
            LOGGER.info("等待后%s红点已消失，跳过%s；当前相似度 %.3f", name, name, score)
            continue
        LOGGER.info("再次确认%s红点，相似度 %.3f", name, score)
        options = dict(images=images, timeout_seconds=timeout, poll_seconds=poll,
                       pre_click_wait=pre_wait, post_click_wait=post_wait)
        if not _click_confirmed_target(client, tab, guard_target=guard, guard_name=name, **options):
            continue
        center, score = images.find(guard)
        if center is None:
            LOGGER.info("点击%s标签后红点已消失，不点击免费按钮；当前相似度 %.3f", name, score)
            continue
        if model is None:
            model = MultimodalClient(action["multimodal"], asset_root=asset_root)
        if not click_free_recruit(client, action["free_button"], category=name, guard=guard,
                                  model=model, max_calls=action["multimodal"]["max_calls"], **options):
            continue
        _confirm_recruit_result(client, action, **options)
        clicks[key] = 1
        LOGGER.info("%s免费次数处理完成", name)
    return RecruitSummary(clicks["advanced"], clicks["ordinary"])
