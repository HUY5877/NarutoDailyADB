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


def finish_secret_result(client, images, action):
    """同一帧确认结算内容和返回按钮；点击一次后等待回房间。"""
    deadline = images.elapsed + action['timeout_seconds']
    clicked = False

    def probe(png):
        if images.match(png, action['deploy_button'])[0] is not None:
            return 'room', None
        if images.match(png, action['create_room'])[0] is not None:
            return 'lobby', None
        if images.match(png, action['result_marker'])[0] is not None:
            center, _ = images.match(png, action['result_return'])
            if center is not None:
                return 'result', center
        return None

    def check():
        return images.check(probe, lambda state: state is not None,
                            '秘境结算返回或房间', deadline=deadline)

    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[秘境结算 第%d轮] %s；回到房间后结束，剩余 %.2f 秒', round_number,
                    '已点击返回，等待页面刷新，不重复点击' if clicked else '先确认房间，否则识别结算页和左下角返回',
                    deadline-images.elapsed)
        state = check()
        if state and state[0] == 'result' and not clicked:
            if images.elapsed + action['pre_click_wait_seconds'] >= deadline:
                break
            images.sleep(action['pre_click_wait_seconds'])
            state = check()
            if state and state[0] == 'result' and images.elapsed < deadline:
                LOGGER.info('再次确认秘境结算页，点击左下角返回 (%d, %d)', *state[1])
                client.tap(*state[1])
                clicked = True
                images.sleep(action['post_click_wait_seconds'])
                continue
        if state and state[0] == 'room':
            LOGGER.info('已确认返回秘境房间，继续核对剩余挑战券')
            return
        if state and state[0] == 'lobby':
            LOGGER.info('返回到秘境大厅，重新创建房间后继续核对挑战券')
            _enter_room(client, images, action)
            return
        images.sleep(min(action['poll_seconds'], max(0, deadline-images.elapsed)))
    raise VisualError('秘境结算返回房间超时，未继续出战')


def handle_secret_realm(client, action, *, template_matcher=find_template_in_png,
                        asset_root=Path('.'), sleep=time.sleep):
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    _enter_room(client, images, action)
    model = MultimodalClient(action['multimodal'], asset_root=asset_root)
    completed = 0
    previous_count = None
    while True:
        LOGGER.info('[秘境探险 第%d轮] 读取剩余挑战券；大于0则复查后出战，等战斗结束回房间；为0则结束并执行后续回主界面', completed+1)
        state = wait_for_tickets(images, model, action, previous_count=previous_count)
        if state.tickets == 0:
            LOGGER.info('秘境挑战券已用完，共完成 %d 场，进入回主界面步骤', completed)
            return completed
        images.sleep(action['pre_click_wait_seconds'])
        confirmed = wait_for_tickets(images, model, action)
        if confirmed.tickets == 0:
            LOGGER.info('出战前复查挑战券已为0，进入回主界面步骤')
            return completed
        LOGGER.info('复查剩余挑战券 %d 张，点击出战 (%d, %d)', confirmed.tickets, *confirmed.deploy_center)
        client.tap(*confirmed.deploy_center)
        LOGGER.info('已点击出战，等待 %g 秒加载；加载完成前不再点击出战', action['load_wait_seconds'])
        images.sleep(action['load_wait_seconds'])
        run_battle(client, action['battle'], template_matcher=template_matcher, asset_root=asset_root, sleep=sleep)
        finish_secret_result(client, images, action)
        completed += 1
        previous_count = confirmed.tickets
        LOGGER.info('秘境第%d场已结束，等待 %g 秒刷新房间券数', completed, action['post_click_wait_seconds'])
        images.sleep(action['post_click_wait_seconds'])
