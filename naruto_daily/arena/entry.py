"""逐屏定位决斗场入口；确认忍术对战后交还战斗循环。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import cv2
import numpy as np

from naruto_daily.common.model_workflow import ModelWorkflow
from naruto_daily.common.visual import NonRetryableVisualError, VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalRequestError
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.multimodal.prompts import arena_entry_prompt

LOGGER = logging.getLogger('naruto_daily')
RESET_SWIPES = 5
MAX_SEARCH_SWIPES = 15
TITLE_CONFIRM = {
    'name': '称号弹窗确定按钮',
    'template': 'assets/arena_title_confirm.png',
    'left': 0, 'top': 540, 'right': 1920, 'bottom': 1080,
    'threshold': .85,
}
# 主功能栏上方的场景空白带；完整长滑动，不能退化成点击。
SCENE_SWIPE = (600, 865, 1320, 865, 650)


def parse_arena_center(result, width, height):
    """仅接受当前完整截图内、远离固定工具栏的整数坐标。"""
    if (not isinstance(result, dict) or result.get('page') != 'main'
            or result.get('name') != '决斗场'):
        raise VisualError('模型未确认主界面的决斗场入口')
    center = result.get('center')
    if (not isinstance(center, list) or len(center) != 2
            or any(type(value) is not int for value in center)):
        raise VisualError('决斗场入口坐标必须是两个整数像素')
    x, y = center
    if not (width * .05 <= x < width * .90 and height * .18 <= y < height * .82):
        raise VisualError('决斗场入口坐标超出可点击场景区域')
    return x, y


def _frame_size(png):
    frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise VisualError('决斗场入口截图无法解析')
    height, width = frame.shape[:2]
    if (width, height) != (1920, 1080):
        raise NonRetryableVisualError('决斗场入口和战斗模块要求 1920×1080 安卓画面，已停止操作')
    return width, height


def _observe_entry(flow):
    """当前搜索位置只定位一次，未取得安全坐标则继续搜索。"""
    flow.remaining()
    png = flow.client.screenshot()
    size = _frame_size(png)
    remaining = flow.remaining()
    center = None
    try:
        with model_request_context('决斗场主界面入口坐标'):
            result = flow.model.complete_json(png, arena_entry_prompt(*size), timeout_seconds=remaining)
        center = parse_arena_center(result, *size)
        LOGGER.info('模型定位决斗场入口：(%d, %d)', *center)
    except NonRetryableVisualError:
        raise
    except MultimodalRequestError as exc:
        if not exc.retryable:
            raise
        LOGGER.warning('本屏决斗场入口坐标请求失败：%s', exc, extra={'backend_only': True})
    except VisualError as exc:
        LOGGER.info('本屏未取得决斗场入口坐标：%s', exc)
    flow.remaining()  # 迟到的响应不能触发点击或滑动。
    return center, size


def _swipe_scene(flow, size, *, right):
    width, height = size
    x1, y1, x2, y2, duration = SCENE_SWIPE
    points = (round(x1 * width / 1920), round(y1 * height / 1080),
              round(x2 * width / 1920), round(y2 * height / 1080))
    if not right:
        points = (points[2], points[3], points[0], points[1])
    flow.remaining()
    flow.client.swipe(*points, duration)
    flow.images.sleep(min(flow.action['post_click_wait_seconds'], flow.remaining()))


def _dismiss_title_popups(flow):
    consecutive_misses = 0
    while consecutive_misses < 5:
        flow.images.sleep(min(1, flow.remaining()))
        flow.remaining()
        center, score = flow.images.match(flow.client.screenshot(), TITLE_CONFIRM)
        flow.remaining()
        if center is None:
            consecutive_misses += 1
        else:
            LOGGER.info('OpenCV 确认称号弹窗确定按钮，相似度 %.3f，点击 (%d, %d)', score, *center)
            flow.client.tap(*center)
            consecutive_misses = 0


def _click_ninjutsu_battle(flow):
    target = flow.action['battle_entry']
    while True:
        flow.remaining()
        center, score = flow.images.match(flow.client.screenshot(), target)
        flow.remaining()
        if center is not None:
            flow.images.sleep(min(flow.action['pre_click_wait_seconds'], flow.remaining()))
            center, score = flow.images.match(flow.client.screenshot(), target)
            flow.remaining()
            if center is not None:
                LOGGER.info('OpenCV 确认%s，相似度 %.3f', target['name'], score)
                flow.tap(center)
                return
        LOGGER.info('尚未确认%s，相似度 %.3f，等待后重新截图', target['name'], score)
        flow.images.sleep(min(flow.action['poll_seconds'], flow.remaining()))


def enter_arena(client, action, *, template_matcher=find_template_in_png,
                asset_root=Path('.'), sleep=time.sleep, model=None):
    """右滑五次归位，至多左滑十五次搜索，只在好友切磋可见时成功返回。"""
    flow = ModelWorkflow(client, action, model=model, template_matcher=template_matcher,
                         asset_root=asset_root, sleep=sleep)
    # 主界面已由前置流程确认，归位前只检查画面尺寸。
    size = _frame_size(client.screenshot())
    for index in range(RESET_SWIPES):
        LOGGER.info('主界面向右滑动归位 %d/%d', index + 1, RESET_SWIPES)
        _swipe_scene(flow, size, right=True)

    max_swipes = min(action['max_swipes'], MAX_SEARCH_SWIPES)
    swipes = 0
    while True:
        LOGGER.info('从左向右查找决斗场，已滑动 %d/%d 次', swipes, max_swipes)
        center, size = _observe_entry(flow)
        if center is not None:
            flow.tap(center)
            break
        if swipes >= max_swipes:
            raise VisualError(f'从左向右滑动 {max_swipes} 次后仍未确认决斗场入口，已停止')
        _swipe_scene(flow, size, right=False)
        swipes += 1

    _dismiss_title_popups(flow)
    _click_ninjutsu_battle(flow)
    flow.button('ready_marker', click=False)
    flow.remaining()
    LOGGER.info('已确认好友切磋，交给原有决斗场战斗模块')
