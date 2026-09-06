from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from naruto_daily.common.adb_client import AdbClient
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.multimodal.client import MultimodalClient, MultimodalError
from naruto_daily.multimodal.prompts import ACCOUNT_NICKNAME
from naruto_daily.login.account_controls import find_account_control
from naruto_daily.common.visual import VisualError, find_template_in_png


LOGGER = logging.getLogger("naruto_daily")


@dataclass(frozen=True)
class AuthorizationSummary:
    used_second_authorization: bool


def authorize_account(
    client: AdbClient, action: dict, *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path('.'), sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """模型读取名字，本地严格匹配；图像模板只用于识别弹层和按钮。"""
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    deadline = float(action['timeout_seconds'])
    pre_wait = float(action.get('pre_click_wait_seconds', 3))
    post_wait = float(action.get('post_click_wait_seconds', 3))
    account_chosen = False
    model = MultimodalClient(action['multimodal'], asset_root=asset_root)
    calls = 0

    def extract(png, prompt):
        nonlocal calls
        if calls >= action['multimodal']['max_calls']:
            raise MultimodalError('账号识别达到模型调用上限，已停止登录')
        if images.elapsed >= deadline:
            raise MultimodalError('账号识别已超过等待时间，已停止登录')
        calls += 1
        started = clock()
        try:
            return model.complete_json(png, prompt, timeout_seconds=deadline - images.elapsed)
        finally:
            # 网络和推理时间同样消耗本阶段预算。
            images.elapsed += max(0, clock() - started)

    def probe(png):
        # 列表弹层优先，不能把变暗的背景授权页当成可点击页面。
        picker, _ = images.match(png, action['picker_marker'])
        if picker is not None:
            if account_chosen:
                return None
            started = clock()
            try:
                center = find_account_control(client.ui_hierarchy(), action['account_name'], picker[1] + 25)
            finally:
                images.elapsed += max(0, clock() - started)
            return ('account_option', center, None) if center else None
        agree, score = images.match(png, action['agree_button'])
        if agree is None:
            return None
        result = extract(png, ACCOUNT_NICKNAME)
        if 'nickname' not in result or (result['nickname'] is not None and not isinstance(result['nickname'], str)):
            raise MultimodalError('模型返回的昵称格式不正确')
        nickname = result['nickname'].strip() if result['nickname'] else None
        selected = nickname == action['account_name']
        LOGGER.info('模型读取昵称：%r；目标昵称：%s；字符串匹配：%s',
                    nickname, action['account_name'], '一致' if selected else '未确认')
        if selected:
            return 'agree_button', agree, score
        if not nickname or account_chosen:
            return None
        switch, score = images.match(png, action['switch_account'])
        return ('switch_account', switch, score) if switch else None

    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[账号授权 第%d轮] 检查账号列表或读取昵称；%s；剩余等待 %.2f 秒，模型调用 %d/%d 次',
                    round_number, '已选账号，仅核对昵称后同意' if account_chosen else '昵称一致则同意，不一致则切换账号',
                    max(0, deadline - images.elapsed), calls, action['multimodal']['max_calls'])
        state = images.check(probe, lambda value: value is not None,
                             action['account_name'], deadline=deadline)
        if state is not None:
            if images.elapsed + pre_wait >= deadline:
                LOGGER.info('剩余时间不足以完成点击前等待，结束账号授权循环')
                break
            LOGGER.info('本轮找到可执行动作，等待 %.2f 秒后重新确认页面，确认一致才点击', pre_wait)
            images.sleep(pre_wait)
            confirmed = images.check(probe, lambda value: value is not None,
                                     action['account_name'], deadline=deadline)
            if confirmed is not None and confirmed[0] == state[0] and images.elapsed < deadline:
                key, center, score = confirmed
                name = action['account_name'] + '账号条目' if key == 'account_option' else action[key]['name']
                LOGGER.info('确认%s，点击坐标：(%d, %d)', name, *center)
                client.tap(*center)
                if key == 'account_option':
                    account_chosen = True
                # 即使点击等待设为 0，页面不推进时也必须轮询并消耗超时预算。
                delay = post_wait if key == 'agree_button' else max(post_wait, float(action['poll_seconds']))
                images.sleep(min(delay, max(0, deadline - images.elapsed)))
                if key == 'agree_button':
                    LOGGER.info('已使用账号%s完成第一次授权', action['account_name'])
                    return
                LOGGER.info('本轮账号操作完成，下一轮重新检查页面并核对目标昵称')
                continue
            LOGGER.info('点击前未确认同一个动作，本轮取消点击')
        if images.elapsed < deadline:
            delay = min(float(action['poll_seconds']), deadline - images.elapsed)
            LOGGER.info('本轮未完成授权，等待 %.2f 秒后开始下一轮；达到时间或模型调用上限则停止', delay)
            images.sleep(delay)
    raise VisualError(f"未能确认并授权账号{action['account_name']}，已停止登录")


def finish_login_authorization(
    client: AdbClient, action: dict, *,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."), sleep: Callable[[float], None] = time.sleep,
) -> AuthorizationSummary:
    """在同一截图判断授权或开始游戏，未确认时统一补查一次。"""
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    deadline = images.elapsed + float(action["timeout_seconds"])
    poll_seconds = float(action["poll_seconds"])
    pre_wait = float(action.get("pre_click_wait_seconds", 3))
    post_wait = float(action.get("post_click_wait_seconds", 3))
    used_second_authorization = False
    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[登录授权 第%d轮] %s；开始游戏点击成功后结束；剩余等待 %.2f 秒',
                    round_number, '查找开始游戏按钮' if used_second_authorization else '先找开始游戏，再找第二次授权按钮',
                    max(0, deadline - images.elapsed))
        targets = [action["start_game"]]
        if not used_second_authorization:
            targets.append(action["second_authorization"])
        index, matches = images.either(targets, deadline=deadline)
        if index is not None:
            target = targets[index]
            if index == 0:
                LOGGER.info("%s，发现开始游戏按钮，相似度 %.3f",
                            "第二次授权完成" if used_second_authorization else "无需第二次授权",
                            matches[index][1])
            else:
                LOGGER.info("发现第二次授权页面，相似度 %.3f", matches[index][1])
            LOGGER.info("点击%s前等待 %.2f 秒", target["name"], pre_wait)
            images.sleep(pre_wait)
            # 复查整个分支，开始游戏始终优先，避免页面切换后点旧位置。
            confirmed_index, confirmed = images.either(targets, deadline=deadline)
            if confirmed_index is not None:
                confirmed_target = targets[confirmed_index]
                center, score = confirmed[confirmed_index]
                LOGGER.info("再次确认%s，相似度 %.3f，点击坐标：(%d, %d)",
                            confirmed_target["name"], score, *center)
                client.tap(*center)
                LOGGER.info("点击完成，等待 %.2f 秒让页面响应", post_wait)
                images.sleep(post_wait)
                if confirmed_index == 0:
                    return AuthorizationSummary(used_second_authorization)
                used_second_authorization = True
                LOGGER.info('本轮已点击第二次授权，下一轮等待开始游戏按钮')
                continue
        if images.elapsed >= deadline:
            break
        delay = min(poll_seconds, deadline - images.elapsed)
        LOGGER.info('本轮未确认可点击的登录按钮，等待 %.2f 秒后开始下一轮', delay)
        images.sleep(delay)
    raise VisualError("等待授权完成页面超时")
