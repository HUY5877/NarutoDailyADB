from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from naruto_daily.common.adb_client import AdbClient
from naruto_daily.common.execution import action_with_execution_budget, check_execution, execution_sleep
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import NonRetryableVisualError, VisualError, find_template_in_png
from naruto_daily.login import cleanup_popups
from naruto_daily.return_to_main import return_to_main
from naruto_daily.login.authorization import finish_login_authorization, authorize_account
from naruto_daily.common.navigation import navigate_to_card, required_template_tap
from naruto_daily.login_service.user_options import UserOptions, parse_user_options
from naruto_daily.secret_realm.enter import enter_secret_realm
from naruto_daily.organization_prayer import handle_organization_prayer
from .fortune import handle_coin_fortune
from .recruit import handle_free_recruit
from .abundance import handle_abundance
from .secret_realm import handle_secret_realm
from .survival_challenge import handle_survival_challenge
from .squad_assault import handle_squad_assault
from .ichiraku import handle_ichiraku_takeout
from .equipment import handle_equipment_upgrade


LOGGER = logging.getLogger("naruto_daily")
ACTION_NAMES = {
    "main_menu_tap": "大模型识别主界面入口后点击",
    "handle_ichiraku_takeout": "判断并领取一乐外卖",
    "handle_equipment_upgrade": "优先最低装备扫荡材料、合成和进阶",
    "enter_secret_realm": "进入秘境探险",
    "handle_secret_realm": "创建秘境房间并按挑战券循环出战",
    "authorize_account": "选择指定 QQ 账号并授权",
    "handle_abundance": "挑战丰饶之间并执行通用战斗",
    "handle_survival_challenge": "执行生存挑战扫荡并按重置次数循环",
    "handle_squad_assault": "取消四倍奖励并循环匹配小队突袭",
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


@dataclass(frozen=True)
class RunSummary:
    action_count: int


class ActionRunner:
    def __init__(
        self,
        client: AdbClient,
        sleep: Callable[[float], None] = time.sleep,
        dry_run: bool = False,
        template_matcher: Callable = find_template_in_png,
        asset_root: Path = Path("."),
        user_options: UserOptions | None = None,
    ):
        self.client = client
        self.sleep = lambda seconds: execution_sleep(seconds, sleep)
        self.dry_run = dry_run
        self.template_matcher = template_matcher
        self.asset_root = Path(asset_root)
        self.user_options = parse_user_options({} if user_options is None else user_options)

    def run(self, actions: Sequence[dict]) -> RunSummary:
        LOGGER.info(
            "开始执行，共 %d 个动作", len(actions),
            extra={"action_index": 0, "action_total": len(actions)},
        )
        images = ImageRecognition(self.client, self.template_matcher, self.asset_root, self.sleep)
        action_count = len(actions)
        skip_action_type: str | None = None
        completed_card_name = ""
        for index, action in enumerate(actions, start=1):
            check_execution()
            action = action_with_execution_budget(action)
            LOGGER.info(
                "动作 %d/%d：%s", index, len(actions), ACTION_NAMES.get(action["type"], "未知动作"),
                extra={"action_index": index, "action_total": len(actions)},
            )
            if self.dry_run:
                LOGGER.info("模拟运行，不发送输入")
                continue
            action_type = action["type"]
            if self.user_options['secretRealmMode'] == 'skip' and action_type in {
                    'enter_secret_realm', 'handle_secret_realm'}:
                LOGGER.info('已按用户设置跳过秘境探险动作，继续其余日常')
                continue
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
            elif action_type in {"required_template_tap", "main_menu_tap"}:
                required_template_tap(self.client, action, template_matcher=self.template_matcher,
                                      asset_root=self.asset_root, sleep=self.sleep)
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
                    if self.user_options.get('mainTask') == 'arena_weekly':
                        raise NonRetryableVisualError('决斗场卡片未能点击前往，停止本次周胜任务')
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
            elif action_type == "enter_secret_realm":
                entered = enter_secret_realm(self.client, action, template_matcher=self.template_matcher,
                                             asset_root=self.asset_root, sleep=self.sleep)
                if not entered:
                    skip_action_type = "handle_secret_realm"
                    completed_card_name = "秘境探险"
            elif action_type == "handle_secret_realm":
                handle_secret_realm(self.client, action, template_matcher=self.template_matcher,
                                    asset_root=self.asset_root, sleep=self.sleep,
                                    secret_realm_once=self.user_options['secretRealmMode'] == 'once')
                # 秘境处理器正常返回代表已用完券，或已按用户设置完成首次成功及结算收尾。
                action_count = index
                LOGGER.info('秘境处理已完成，结束本次任务，跳过后续 %d 个动作', len(actions) - index)
                break
            elif action_type == "handle_abundance":
                handle_abundance(
                    self.client, action, template_matcher=self.template_matcher,
                    asset_root=self.asset_root, sleep=self.sleep,
                )
            elif action_type == "handle_survival_challenge":
                handle_survival_challenge(
                    self.client,
                    action,
                    template_matcher=self.template_matcher,
                    asset_root=self.asset_root,
                    sleep=self.sleep,
                )
            elif action_type == "handle_squad_assault":
                handle_squad_assault(
                    self.client, action, template_matcher=self.template_matcher,
                    asset_root=self.asset_root, sleep=self.sleep,
                )
            elif action_type in ("handle_ichiraku_takeout", "handle_equipment_upgrade"):
                handler = handle_ichiraku_takeout if action_type == "handle_ichiraku_takeout" else handle_equipment_upgrade
                handler(self.client, action, template_matcher=self.template_matcher,
                        asset_root=self.asset_root, sleep=self.sleep)
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
        check_execution()
        LOGGER.info(
            "执行结束：%d 个动作", action_count,
            extra={"action_index": action_count, "action_total": action_count},
        )
        return RunSummary(action_count)
