"""秘境探险任务，房间刷新后重新核对券数，不对网络重试重复出战。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from naruto_daily.battle import run_battle
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalClient
from .recognition import wait_for_tickets
from .pre_battle import BattlePreparation, prepare_secret_battle
from .deployment import wait_after_deploy
from .result_detection import secret_failure_visible


LOGGER = logging.getLogger('naruto_daily')


def _enter_room(client, images, action):
    deadline = images.elapsed + action['timeout_seconds']
    round_number = 0
    targets = [action['deploy_button'], action['create_room']]
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[秘境建房 第%d轮] 先确认是否已在房间，否则查找创建房间；未确认则继续等候', round_number)
        index, _ = images.either(targets, deadline=deadline)
        if index == 0:
            return
        if index == 1:
            images.sleep(action['pre_click_wait_seconds'])
            index, matches = images.either(targets, deadline=deadline)
            if index == 0:
                return
            if index == 1 and images.elapsed < deadline:
                client.tap(*matches[1][0])
                LOGGER.info('已创建秘境房间，等待 %g 秒刷新', action['post_click_wait_seconds'])
                images.sleep(action['post_click_wait_seconds'])
                return
        images.sleep(min(action['poll_seconds'], max(0, deadline-images.elapsed)))
    raise VisualError('秘境创建房间超时')


def finish_secret_result(client, images, action, *, exited_battle=False, result_confirmed=False):
    """失败和奖励弹层点空白处、翻牌结算点返回，逐轮检查直到回房间。"""
    deadline = images.elapsed + action['timeout_seconds']
    return_clicked = False
    dismiss_enabled = exited_battle or result_confirmed

    def probe(png):
        if images.match(png, action['deploy_button'])[0] is not None:
            return 'room', None
        if images.match(png, action['create_room'])[0] is not None:
            return 'lobby', None
        if images.match(png, action['reward_overlay'])[0] is not None:
            return 'reward', None
        if images.match(png, action['result_marker'])[0] is not None:
            center, _ = images.match(png, action['result_return'])
            if center is not None:
                return 'result', center
        if secret_failure_visible(png):
            return 'failure', None
        return None

    def check():
        return images.check(probe, lambda state: state is not None,
                            '秘境结算返回或房间', deadline=deadline)

    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[秘境结算 第%d轮] %s；回到房间后结束，剩余 %.2f 秒', round_number,
                    '继续检查房间、奖励弹层和结算返回；未返回则点击空白处' if dismiss_enabled else '先确认房间，否则识别奖励弹层或结算返回',
                    deadline-images.elapsed)
        state = check()
        if state and state[0] == 'result' and not return_clicked:
            if images.elapsed + action['pre_click_wait_seconds'] >= deadline:
                break
            images.sleep(action['pre_click_wait_seconds'])
            state = check()
            if state and state[0] == 'result' and images.elapsed < deadline:
                LOGGER.info('再次确认秘境结算页，点击左下角返回 (%d, %d)', *state[1])
                client.tap(*state[1])
                return_clicked = True
                dismiss_enabled = True
                images.sleep(action['post_click_wait_seconds'])
                continue
        if state and state[0] == 'room':
            LOGGER.info('已确认返回秘境房间，继续核对剩余挑战券')
            return
        if state and state[0] == 'lobby':
            LOGGER.info('返回到秘境大厅，重新创建房间后继续核对挑战券')
            _enter_room(client, images, action)
            return
        if state and state[0] in ('reward', 'failure'):
            dismiss_enabled = True
            if state[0] == 'failure':
                LOGGER.info('已确认秘境失败结算，点击空白处回房间后重新核对挑战券')
        if dismiss_enabled and images.elapsed < deadline:
            # 避开奖励物品所在的中间一排；此空白坐标已经实机验证可关闭奖励弹层。
            client.tap(960, 900)
            LOGGER.info('已点击屏幕下方空白处 (960, 900)，继续检查是否回到秘境房间')
        images.sleep(min(action['poll_seconds'], max(0, deadline-images.elapsed)))
    raise VisualError('秘境结算返回房间超时，未继续出战')


def handle_secret_realm(client, action, *, template_matcher=find_template_in_png,
                        asset_root=Path('.'), sleep=time.sleep):
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    _enter_room(client, images, action)
    model = MultimodalClient(action['multimodal'], asset_root=asset_root)
    completed = 0
    while True:
        LOGGER.info('[秘境探险 第%d轮] 读取剩余挑战券；大于0则复查后出战，等战斗结束回房间；为0则结束并执行后续回主界面', completed+1)
        state = wait_for_tickets(images, model, action)
        if state.tickets == 0:
            LOGGER.info('秘境挑战券已用完，共结束 %d 场战斗，进入回主界面步骤', completed)
            return completed
        images.sleep(action['pre_click_wait_seconds'])
        confirmed = wait_for_tickets(images, model, action)
        if confirmed.tickets == 0:
            LOGGER.info('出战前复查挑战券已为0，进入回主界面步骤')
            return completed
        LOGGER.info('复查剩余挑战券 %d 张，点击出战 (%d, %d)', confirmed.tickets, *confirmed.deploy_center)
        client.tap(*confirmed.deploy_center)
        LOGGER.info('已点击出战，等待 %g 秒加载并检查继续挑战提示；加载完成前不再点击出战', action['load_wait_seconds'])
        wait_after_deploy(images, action, confirmed_tickets=confirmed.tickets)
        preparation = prepare_secret_battle(images, model, action)
        if preparation is BattlePreparation.READY:
            # 秘境结算仅停留约20秒，缩短连点窗口以便回房间前识别结束。
            run_battle(client, action['battle'], template_matcher=template_matcher,
                       asset_root=asset_root, sleep=sleep, model=model, check_interval_seconds=18)
        finish_secret_result(client, images, action,
                             exited_battle=preparation is BattlePreparation.EXITED,
                             result_confirmed=preparation is BattlePreparation.FINISHED)
        if preparation is BattlePreparation.EXITED:
            LOGGER.info('已主动退出本次秘境；回房间重新读券后继续')
        else:
            completed += 1
            LOGGER.info('秘境第%d场已结束，等待 %g 秒刷新房间后重新读券；当前券数大于0即可继续', completed, action['post_click_wait_seconds'])
        images.sleep(action['post_click_wait_seconds'])
