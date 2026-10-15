"""秘境的两个入口；进入大厅后统一交给 handler 建房、战斗和结算。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import cv2
import numpy as np

from naruto_daily.common.navigation import navigate_to_card, required_template_tap
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.common.execution import current_execution, stage_timeout
from naruto_daily.multimodal.client import MultimodalClient, MultimodalError
from naruto_daily.multimodal.prompts import secret_entry_prompt
from naruto_daily.return_to_main import return_to_main

LOGGER = logging.getLogger('naruto_daily')


def entry_candidates(png):
    """提出白字深色描边的竖排文字区域；不判断文字含义、不匹配模板。"""
    frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise VisualError('秘境入口截图无法解码')
    height, width = frame.shape[:2]
    scale = width / 1920
    low, high = frame.min(axis=2), frame.max(axis=2)
    dark = cv2.dilate((high < 95).astype(np.uint8), np.ones((max(1, round(9*scale)),)*2, np.uint8))
    mask = ((low > 175) & ((high.astype(int)-low) < 65) & (dark > 0)).astype(np.uint8)*255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                           np.ones((max(1, round(27*scale)), max(1, round(9*scale))), np.uint8))
    boxes = []
    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, w, h = cv2.boundingRect(contour)
        if (90*scale <= h <= 400*scale and 15*scale <= w <= 180*scale
                and h > w*1.5 and y > height*.11 and y+h < height*.84):
            boxes.append((x, y, w, h))
    boxes.sort(key=lambda box: (box[0], box[1]))
    if not boxes or len(boxes) > 32:
        return None, {}
    columns = min(8, len(boxes))
    sheet = np.full((((len(boxes)+columns-1)//columns)*300, columns*180, 3), 245, np.uint8)
    centers = {}
    for index, (x, y, w, h) in enumerate(boxes, start=1):
        centers[index] = (x+w//2, y+h//2)
        # 给模型看文字周围少量背景，编号在图片外，不遮挡文字。
        pad = max(4, round(10*scale))
        crop = frame[max(0,y-pad):min(height,y+h+pad), max(0,x-pad):min(width,x+w+pad)]
        ratio = min(160/crop.shape[1], 255/crop.shape[0])
        crop = cv2.resize(crop, (max(1,round(crop.shape[1]*ratio)), max(1,round(crop.shape[0]*ratio))))
        sx, sy = ((index-1)%columns)*180, ((index-1)//columns)*300
        cv2.putText(sheet, str(index), (sx+70,sy+28), cv2.FONT_HERSHEY_SIMPLEX, .8, (0,0,0), 2)
        sheet[sy+40:sy+40+crop.shape[0],sx+10:sx+10+crop.shape[1]] = crop
    return cv2.imencode('.png', sheet)[1].tobytes(), centers


def parse_entry_center(result, page, name, centers):
    """模型只选当前截图中的候选编号，原图坐标由代码持有。"""
    if not isinstance(result, dict) or result.get('page') != page or result.get('name') != name:
        return None
    candidate_id = result.get('candidate_id')
    if type(candidate_id) is not int:
        return None
    return centers.get(candidate_id)


def _locate_and_tap(images, model, action, page, name, *, clock=time.monotonic):
    deadline = images.elapsed + stage_timeout(action['timeout_seconds'])
    calls = 0

    def probe(png):
        nonlocal calls
        if images.elapsed >= deadline:
            raise MultimodalError(f'{name}定位超时')
        if current_execution() is None and calls >= action['multimodal']['max_calls']:
            raise MultimodalError(f'{name}定位达到模型调用上限，未点击')
        preview, centers = entry_candidates(png)
        if not centers:
            return None
        start = clock()
        try:
            result = model.complete_json(preview, secret_entry_prompt(page, name, centers),
                                         timeout_seconds=stage_timeout(deadline-images.elapsed))
            calls += 1
        finally:
            if current_execution() is None:
                images.elapsed += max(0, clock()-start)
        if images.elapsed >= deadline:
            raise MultimodalError(f'{name}定位超时，忽略迟到结果')
        center = parse_entry_center(result, page, name, centers)
        call_progress = (f'{calls} 次（由总时限约束）' if current_execution() is not None
                         else f"{calls}/{action['multimodal']['max_calls']} 次")
        LOGGER.info('模型定位%s：%s；调用 %s', name, center or '未确认', call_progress)
        return center

    def check():
        return images.check(probe, lambda p: p is not None, name, deadline=deadline)

    round_number = 0
    while images.elapsed < deadline:
        round_number += 1
        LOGGER.info('[秘境入口：%s 第%d轮] 模型查找→等待后重新截图定位→点击一次；未找到则补查和轮询，剩余 %.2f 秒',
                    name, round_number, deadline-images.elapsed)
        center = check()
        if center is not None:
            if current_execution() is None and images.elapsed + action['pre_click_wait_seconds'] >= deadline:
                break
            images.sleep(action['pre_click_wait_seconds'])
            center = check()
            if center is not None and images.elapsed < deadline:
                LOGGER.info('确认%s，点击最新坐标 (%d, %d)，等待 %g 秒加载',
                            name, *center, action['post_click_wait_seconds'])
                images.client.tap(*center)
                images.sleep(action['post_click_wait_seconds'])
                return
        images.sleep(min(action['poll_seconds'], max(0, deadline-images.elapsed)))
    raise VisualError(f'等待{name}入口超时，未继续进入秘境')


def enter_from_rewards(client, action, **kwargs):
    return_to_main(client, action['return_to_main'], **kwargs)
    required_template_tap(client, action['reward_button'], **kwargs)
    result = navigate_to_card(client, action['card'], **kwargs)
    return not result.completed


def enter_from_ninja_challenge(client, action, *, template_matcher=find_template_in_png,
                              asset_root=Path('.'), sleep=time.sleep):
    return_to_main(client, action['return_to_main'], template_matcher=template_matcher,
                   asset_root=asset_root, sleep=sleep)
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    swipe = action['swipe']
    LOGGER.info('秘境独立入口：主界面向左滑动一次，等待 %g 秒', action['post_click_wait_seconds'])
    client.swipe(*(swipe[k] for k in ('x1', 'y1', 'x2', 'y2', 'duration_ms')))
    images.sleep(action['post_click_wait_seconds'])
    model = MultimodalClient(action['multimodal'], asset_root=asset_root)
    _locate_and_tap(images, model, action, 'main', '忍者挑战')
    _locate_and_tap(images, model, action, 'ninja_challenge', '秘境探险')
    LOGGER.info('秘境入口已点击，等待公共建房流程确认大厅或房间')
    return True


def enter_secret_realm(client, action, *, template_matcher=find_template_in_png,
                       asset_root=Path('.'), sleep=time.sleep):
    kwargs = dict(template_matcher=template_matcher, asset_root=asset_root, sleep=sleep)
    if action['method'] == 'rewards':
        return enter_from_rewards(client, action, **kwargs)
    if action['method'] == 'ninja_challenge':
        return enter_from_ninja_challenge(client, action, **kwargs)
    raise VisualError('不支持的秘境入口方式')
