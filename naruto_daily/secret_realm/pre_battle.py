"""秘境战斗入口检查：读取当前帧名称，跳过指定秘境并保留毒风导航。"""
from __future__ import annotations

import logging
import re
import time
from enum import Enum

from naruto_daily.common.visual import NonRetryableVisualError, VisualError
from naruto_daily.multimodal.client import MultimodalError
from naruto_daily.multimodal.prompts import SECRET_BATTLE_REALM
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.common.execution import current_execution, stage_timeout
from .navigation import navigate_to_poison_seal


LOGGER = logging.getLogger('naruto_daily')
# 从名单移除毒风秘境后，仍可使用下方保留的 agent 导航流程。
SKIPPED_REALMS = frozenset({'阴阳秘境', '毒风秘境'})
PAUSE = dict(name='秘境战斗暂停按钮', template='assets/secret_battle_pause.png',
             left=1760, top=0, right=1920, bottom=150, threshold=.85)
EXIT = dict(name='退出战斗', template='assets/secret_exit_battle.png',
            left=600, top=570, right=970, bottom=800, threshold=.85)
QUESTION = dict(name='退出本次战斗确认提示', template='assets/secret_exit_question.png',
                left=650, top=390, right=1270, bottom=550, threshold=.85)
CONFIRM = dict(name='退出战斗确定按钮', template='assets/secret_exit_confirm.png',
               left=600, top=570, right=970, bottom=800, threshold=.85)


class BattlePreparation(Enum):
    READY = 'ready'
    EXITED = 'exited'
    FINISHED = 'finished'


def _wait_and_click(images, target, action, *, guard=None):
    deadline = images.elapsed + stage_timeout(action['timeout_seconds'])

    def probe(png):
        if guard is not None and images.match(png, guard)[0] is None:
            return None
        return images.match(png, target)[0]

    def check():
        return images.check(probe, lambda center: center is not None, target['name'], deadline=deadline)

    while images.elapsed < deadline:
        center = check()
        if center is not None:
            if current_execution() is None and images.elapsed + action['pre_click_wait_seconds'] >= deadline:
                break
            images.sleep(action['pre_click_wait_seconds'])
            center = check()
            if center is not None and images.elapsed < deadline:
                images.client.tap(*center)
                LOGGER.info('已点击%s (%d, %d)，等待下一步界面', target['name'], *center)
                images.sleep(action['post_click_wait_seconds'])
                return
        images.sleep(min(action['poll_seconds'], max(0, deadline-images.elapsed)))
    raise VisualError(f'等待{target["name"]}超时，未继续点击或攻击')


def identify_battle_realm(images, model, action, *, clock=time.monotonic, png=None):
    """战斗模块提供当前帧；未确认名称时停止输入，由调用方重新观察。"""
    deadline = images.elapsed + stage_timeout(action['timeout_seconds'])
    calls = 0

    def probe(png):
        nonlocal calls
        # 载入后已经结算或回房间时，交给原战斗/结算流程收尾。
        if any(images.match(png, target)[0] is not None for target in action['battle']['end_targets']):
            return '已结束'
        if images.elapsed >= deadline:
            raise MultimodalError('秘境副本名称识别超时')
        if current_execution() is None and calls >= action['multimodal']['max_calls']:
            raise MultimodalError('秘境副本名称识别达到模型调用上限，未攻击或退出')
        start = clock()
        try:
            with model_request_context('秘境名称识别'):
                result = model.complete_json(png, SECRET_BATTLE_REALM,
                                             timeout_seconds=stage_timeout(deadline-images.elapsed))
            calls += 1
        finally:
            if current_execution() is None:
                images.elapsed += max(0, clock()-start)
        if images.elapsed >= deadline:
            raise MultimodalError('秘境副本名称识别超时，忽略迟到结果')
        name = result.get('realm_name') if isinstance(result, dict) and result.get('page') == 'battle' else None
        if not isinstance(name, str) or not 3 <= len(name.strip()) <= 20 or not name.strip().endswith('秘境'):
            name = None
        call_progress = (f'{calls} 次（由总时限约束）' if current_execution() is not None
                         else f"{calls}/{action['multimodal']['max_calls']} 次")
        LOGGER.info('模型识别秘境副本：%s；调用 %s', name or '未确认', call_progress)
        return name.strip() if name else None

    if png is not None:
        name = probe(png)
        if name is None:
            raise MultimodalError('秘境副本名称尚未确认，保持停止点击并重新截图')
        return name

    while images.elapsed < deadline:
        name = images.check(probe, lambda name: name is not None, '秘境副本名称', deadline=deadline)
        if name is not None:
            return name
        images.sleep(min(action['poll_seconds'], max(0, deadline-images.elapsed)))
    raise MultimodalError('秘境副本名称识别超时，未攻击或退出')


def exit_secret_battle(images, action):
    """复用阴阳秘境的暂停→退出→确认流程；回房间由结算模块继续确认。"""
    try:
        _wait_and_click(images, PAUSE, action)
        _wait_and_click(images, EXIT, action)
        _wait_and_click(images, CONFIRM, action, guard=QUESTION)
    except VisualError as error:
        # A partially executed exit must not be replayed as a read-only retry.
        raise NonRetryableVisualError(str(error)) from None


def prepare_secret_battle(images, model, action, *, clock=time.monotonic, png=None) -> BattlePreparation:
    """区分可开始战斗、已主动退出和导航期间已出现结算。"""
    name = identify_battle_realm(images, model, action, clock=clock, png=png)
    if name == '已结束':
        return BattlePreparation.FINISHED
    # “落岩”可能被模型读成“溶岩”或“熔岩”，名称含“岩”就统一退出。
    if name in SKIPPED_REALMS or re.search(r'岩', name):
        LOGGER.info('确认%s，暂时跳过，执行暂停→退出战斗→确定', name)
        exit_secret_battle(images, action)
        return BattlePreparation.EXITED
    if name == '毒风秘境':
        LOGGER.info('确认毒风秘境，先调用导航 agent 走到封印处，再开始战斗')
        try:
            result = navigate_to_poison_seal(images, model)
        except VisualError as error:
            raise NonRetryableVisualError(str(error)) from None
        if result.status == 'finished':
            LOGGER.info('毒风导航期间已出现失败或胜利结算，停止导航并返回房间重新检查挑战券：%s', result.observation)
            return BattlePreparation.FINISHED
        if result.reached:
            LOGGER.info('毒风 agent 本轮证据满足解封条件：%s；共移动 %d 步，进入战斗模块', result.observation, result.steps)
            return BattlePreparation.READY
        if result.status == 'max_steps':
            LOGGER.info('毒风导航 %d 步后仍未到达封印，执行暂停→退出战斗→确定', result.steps)
            exit_secret_battle(images, action)
            return BattlePreparation.EXITED
        raise NonRetryableVisualError('毒风导航未确认到达，停止且不进入战斗')
    return BattlePreparation.READY
