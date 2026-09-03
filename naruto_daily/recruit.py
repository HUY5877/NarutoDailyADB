from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class RecruitSummary:
    """记录高级招募和普通招募的免费抽取次数。"""

    advanced_clicks: int
    ordinary_clicks: int


def _match(
    client,
    target: dict,
    template_matcher: Callable,
    asset_root: Path,
    screenshot: bytes | None = None,
):
    template_path = Path(asset_root) / target["template"]
    roi = (target["left"], target["top"], target["right"], target["bottom"])
    return template_matcher(
        client.screenshot() if screenshot is None else screenshot,
        template_path,
        roi,
        float(target["threshold"]),
    )


def _wait_for_target(
    client,
    target: dict,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    template_matcher: Callable,
    asset_root: Path,
    sleep: Callable[[float], None],
):
    elapsed = 0.0
    while True:
        center, score = _match(client, target, template_matcher, asset_root)
        if center is not None:
            LOGGER.info(
                "发现%s，相似度 %.3f，坐标：(%d, %d)",
                target["name"],
                score,
                center[0],
                center[1],
            )
            return center
        if elapsed >= timeout_seconds:
            raise VisualError(f"等待{target['name']}超时，最后相似度 {score:.3f}")
        delay = min(poll_seconds, timeout_seconds - elapsed)
        LOGGER.info(
            "等待%s，当前相似度 %.3f，%.2f 秒后重试",
            target["name"],
            score,
            delay,
        )
        sleep(delay)
        elapsed += delay


def _click_confirmed_target(
    client,
    target: dict,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    pre_click_wait: float,
    post_click_wait: float,
    template_matcher: Callable,
    asset_root: Path,
    sleep: Callable[[float], None],
    guard_target: dict | None = None,
    guard_name: str = "",
) -> bool:
    elapsed = 0.0
    while True:
        center, score = _match(client, target, template_matcher, asset_root)
        if center is not None:
            LOGGER.info(
                "发现%s，相似度 %.3f，点击前等待 %.2f 秒",
                target["name"],
                score,
                pre_click_wait,
            )
            sleep(pre_click_wait)
            elapsed += pre_click_wait
            confirmed_screenshot = client.screenshot()
            confirmed_center, confirmed_score = _match(
                client,
                target,
                template_matcher,
                asset_root,
                confirmed_screenshot,
            )
            if confirmed_center is None:
                LOGGER.info(
                    "等待后%s已消失，相似度 %.3f，不点击旧坐标并继续识别",
                    target["name"],
                    confirmed_score,
                )
            else:
                if guard_target is not None:
                    guard_center, guard_score = _match(
                        client,
                        guard_target,
                        template_matcher,
                        asset_root,
                        confirmed_screenshot,
                    )
                    if guard_center is None:
                        LOGGER.info(
                            "点击%s前%s红点已消失，取消点击；当前相似度 %.3f",
                            target["name"],
                            guard_name,
                            guard_score,
                        )
                        return False
                LOGGER.info(
                    "再次确认%s，相似度 %.3f，点击坐标：(%d, %d)",
                    target["name"],
                    confirmed_score,
                    confirmed_center[0],
                    confirmed_center[1],
                )
                client.tap(*confirmed_center)
                LOGGER.info("点击完成，等待页面响应 %.2f 秒", post_click_wait)
                sleep(post_click_wait)
                return True

        if elapsed >= timeout_seconds:
            raise VisualError(f"等待{target['name']}超时，最后相似度 {score:.3f}")
        delay = min(poll_seconds, timeout_seconds - elapsed)
        LOGGER.info(
            "等待%s，当前相似度 %.3f，%.2f 秒后重试",
            target["name"],
            score,
            delay,
        )
        sleep(delay)
        elapsed += delay


def _confirm_red_dot(
    client,
    target: dict,
    category_name: str,
    *,
    first_score: float,
    pre_click_wait: float,
    template_matcher: Callable,
    asset_root: Path,
    sleep: Callable[[float], None],
) -> bool:
    LOGGER.info(
        "%s存在红点，相似度 %.3f，继续操作前等待 %.2f 秒",
        category_name,
        first_score,
        pre_click_wait,
    )
    sleep(pre_click_wait)
    center, score = _match(client, target, template_matcher, asset_root)
    if center is None:
        LOGGER.info(
            "等待后%s红点已消失，跳过%s；当前相似度 %.3f",
            category_name,
            category_name,
            score,
        )
        return False
    LOGGER.info("再次确认%s红点，相似度 %.3f", category_name, score)
    return True


def handle_free_recruit(
    client,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> RecruitSummary:
    """只在对应招募标签存在红点时点击免费招募，并关闭结果页面。"""

    timeout_seconds = float(action["timeout_seconds"])
    poll_seconds = float(action["poll_seconds"])
    pre_click_wait = float(action.get("pre_click_wait_seconds", 3))
    post_click_wait = float(action.get("post_click_wait_seconds", 3))

    # 高级招募标签同时充当招募页面已经加载完成的标记。
    _wait_for_target(
        client,
        action["advanced_tab"],
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        template_matcher=template_matcher,
        asset_root=asset_root,
        sleep=sleep,
    )

    advanced_clicks = 0
    advanced_red_dot, advanced_score = _match(
        client,
        action["advanced_red_dot"],
        template_matcher,
        asset_root,
    )
    if advanced_red_dot is None:
        LOGGER.info("高级招募没有红点，跳过；当前相似度 %.3f", advanced_score)
    elif _confirm_red_dot(
        client,
        action["advanced_red_dot"],
        "高级招募",
        first_score=advanced_score,
        pre_click_wait=pre_click_wait,
        template_matcher=template_matcher,
        asset_root=asset_root,
        sleep=sleep,
    ):
        tab_clicked = _click_confirmed_target(
            client,
            action["advanced_tab"],
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            pre_click_wait=pre_click_wait,
            post_click_wait=post_click_wait,
            template_matcher=template_matcher,
            asset_root=asset_root,
            sleep=sleep,
            guard_target=action["advanced_red_dot"],
            guard_name="",
        )
        if tab_clicked:
            current_red_dot, current_red_score = _match(
                client,
                action["advanced_red_dot"],
                template_matcher,
                asset_root,
            )
            if current_red_dot is None:
                LOGGER.info(
                    "点击高级招募标签后红点已消失，不点击免费按钮；当前相似度 %.3f",
                    current_red_score,
                )
            else:
                free_clicked = _click_confirmed_target(
                    client,
                    action["free_button"],
                    timeout_seconds=timeout_seconds,
                    poll_seconds=poll_seconds,
                    pre_click_wait=pre_click_wait,
                    post_click_wait=post_click_wait,
                    template_matcher=template_matcher,
                    asset_root=asset_root,
                    sleep=sleep,
                    guard_target=action["advanced_red_dot"],
                    guard_name="高级招募",
                )
                if free_clicked:
                    _click_confirmed_target(
                        client,
                        action["result_confirm"],
                        timeout_seconds=timeout_seconds,
                        poll_seconds=poll_seconds,
                        pre_click_wait=pre_click_wait,
                        post_click_wait=post_click_wait,
                        template_matcher=template_matcher,
                        asset_root=asset_root,
                        sleep=sleep,
                    )
                    advanced_clicks = 1
                    LOGGER.info("高级招募免费次数处理完成")

    ordinary_clicks = 0
    ordinary_red_dot, ordinary_score = _match(
        client,
        action["ordinary_red_dot"],
        template_matcher,
        asset_root,
    )
    if ordinary_red_dot is None:
        LOGGER.info("普通招募没有红点，跳过；当前相似度 %.3f", ordinary_score)
    elif _confirm_red_dot(
        client,
        action["ordinary_red_dot"],
        "普通招募",
        first_score=ordinary_score,
        pre_click_wait=pre_click_wait,
        template_matcher=template_matcher,
        asset_root=asset_root,
        sleep=sleep,
    ):
        tab_clicked = _click_confirmed_target(
            client,
            action["ordinary_tab"],
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            pre_click_wait=pre_click_wait,
            post_click_wait=post_click_wait,
            template_matcher=template_matcher,
            asset_root=asset_root,
            sleep=sleep,
            guard_target=action["ordinary_red_dot"],
            guard_name="",
        )
        if tab_clicked:
            current_red_dot, current_red_score = _match(
                client,
                action["ordinary_red_dot"],
                template_matcher,
                asset_root,
            )
            if current_red_dot is None:
                LOGGER.info(
                    "点击普通招募标签后红点已消失，不点击免费按钮；当前相似度 %.3f",
                    current_red_score,
                )
            else:
                free_clicked = _click_confirmed_target(
                    client,
                    action["free_button"],
                    timeout_seconds=timeout_seconds,
                    poll_seconds=poll_seconds,
                    pre_click_wait=pre_click_wait,
                    post_click_wait=post_click_wait,
                    template_matcher=template_matcher,
                    asset_root=asset_root,
                    sleep=sleep,
                    guard_target=action["ordinary_red_dot"],
                    guard_name="普通招募",
                )
                if free_clicked:
                    _click_confirmed_target(
                        client,
                        action["result_confirm"],
                        timeout_seconds=timeout_seconds,
                        poll_seconds=poll_seconds,
                        pre_click_wait=pre_click_wait,
                        post_click_wait=post_click_wait,
                        template_matcher=template_matcher,
                        asset_root=asset_root,
                        sleep=sleep,
                    )
                    ordinary_clicks = 1
                    LOGGER.info("普通招募免费次数处理完成")

    return RecruitSummary(
        advanced_clicks=advanced_clicks,
        ordinary_clicks=ordinary_clicks,
    )
