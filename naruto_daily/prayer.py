from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class PrayerSummary:
    """记录昨日奖励处理情况和成功领取数量。"""

    yesterday_opened: bool
    claims: int


def _match(client, target: dict, template_matcher: Callable, asset_root: Path):
    template_path = Path(asset_root) / target["template"]
    roi = (target["left"], target["top"], target["right"], target["bottom"])
    return template_matcher(client.screenshot(), template_path, roi, float(target["threshold"]))


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


def handle_organization_prayer(
    client,
    action: dict,
    *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."),
    sleep: Callable[[float], None] = time.sleep,
) -> PrayerSummary:
    """领取昨日祈福奖励，并执行一次 6000 铜币焚香祈福。"""

    timeout_seconds = float(action["timeout_seconds"])
    poll_seconds = float(action["poll_seconds"])
    wait_seconds = float(action["wait_seconds"])
    pre_click_wait = float(action.get("pre_click_wait_seconds", 3))
    post_click_wait = float(action.get("post_click_wait_seconds", 3))
    coin_prayer = action["coin_prayer"]

    _wait_for_target(
        client,
        coin_prayer,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        template_matcher=template_matcher,
        asset_root=asset_root,
        sleep=sleep,
    )

    yesterday_center, yesterday_score = _match(
        client,
        action["yesterday_reward"],
        template_matcher,
        asset_root,
    )
    yesterday_opened = yesterday_center is not None
    claims = 0

    if not yesterday_opened:
        LOGGER.info("未发现昨日奖励，相似度 %.3f，跳过奖励领取", yesterday_score)
    else:
        LOGGER.info(
            "发现昨日奖励，相似度 %.3f，点击前等待 %.2f 秒",
            yesterday_score,
            pre_click_wait,
        )
        sleep(pre_click_wait)
        confirmed_yesterday, confirmed_yesterday_score = _match(
            client,
            action["yesterday_reward"],
            template_matcher,
            asset_root,
        )
        if confirmed_yesterday is None:
            LOGGER.info(
                "等待后昨日奖励已消失，相似度 %.3f，跳过奖励领取",
                confirmed_yesterday_score,
            )
            yesterday_opened = False
        else:
            LOGGER.info(
                "再次确认昨日奖励，相似度 %.3f，点击坐标：(%d, %d)",
                confirmed_yesterday_score,
                confirmed_yesterday[0],
                confirmed_yesterday[1],
            )
            client.tap(*confirmed_yesterday)
            sleep(post_click_wait)

    if yesterday_opened:
        _wait_for_target(
            client,
            action["panel_marker"],
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            template_matcher=template_matcher,
            asset_root=asset_root,
            sleep=sleep,
        )

        for claim_number in range(1, int(action["max_claims"]) + 1):
            claim_center, claim_score = _match(
                client,
                action["claim_button"],
                template_matcher,
                asset_root,
            )
            if claim_center is None:
                LOGGER.info("没有更多可领取的昨日奖励，当前相似度 %.3f", claim_score)
                break
            LOGGER.info(
                "发现第 %d 项昨日奖励，相似度 %.3f，点击前等待 %.2f 秒",
                claim_number,
                claim_score,
                pre_click_wait,
            )
            sleep(pre_click_wait)
            confirmed_claim, confirmed_claim_score = _match(
                client,
                action["claim_button"],
                template_matcher,
                asset_root,
            )
            if confirmed_claim is None:
                LOGGER.info(
                    "等待后第 %d 项昨日奖励已消失，相似度 %.3f，不点击旧坐标",
                    claim_number,
                    confirmed_claim_score,
                )
                continue
            LOGGER.info(
                "再次确认第 %d 项昨日奖励，相似度 %.3f，点击坐标：(%d, %d)",
                claim_number,
                confirmed_claim_score,
                confirmed_claim[0],
                confirmed_claim[1],
            )
            client.tap(*confirmed_claim)
            claims += 1
            sleep(post_click_wait)

        LOGGER.info("昨日奖励领取结束，共领取 %d 项，点击空白处关闭界面", claims)
        client.tap(action["dismiss_x"], action["dismiss_y"])
        sleep(wait_seconds)

    coin_center = _wait_for_target(
        client,
        coin_prayer,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        template_matcher=template_matcher,
        asset_root=asset_root,
        sleep=sleep,
    )
    LOGGER.info("发现 6000 铜币焚香祈福，点击前等待 %.2f 秒", pre_click_wait)
    sleep(pre_click_wait)
    confirmed_coin, confirmed_coin_score = _match(
        client,
        coin_prayer,
        template_matcher,
        asset_root,
    )
    if confirmed_coin is None:
        raise VisualError(
            f"6000 铜币焚香祈福在点击前等待后已消失，最后相似度 {confirmed_coin_score:.3f}"
        )
    LOGGER.info(
        "再次确认 6000 铜币焚香祈福，相似度 %.3f，点击坐标：(%d, %d)",
        confirmed_coin_score,
        confirmed_coin[0],
        confirmed_coin[1],
    )
    client.tap(*confirmed_coin)
    LOGGER.info("点击铜币祈福后等待页面响应 %.2f 秒", post_click_wait)
    sleep(post_click_wait)
    return PrayerSummary(yesterday_opened=yesterday_opened, claims=claims)
