"""模型识别主界面入口；奖励候选另用同帧文字复核。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZipFile

import cv2
import numpy as np

from .visual import VisualError, find_template_in_png
from .execution import current_execution, stage_timeout
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.prompt_jev import main_screen_prompt
from naruto_daily.multimodal.diagnostics import is_model_api_failure, model_request_context

LOGGER = logging.getLogger('naruto_daily')
REWARD_CONFIRMATION_THRESHOLD = .5


def button_regions(target):
    """1920×1080 下的候选槽位；裁图、坐标映射与奖励复核共用。"""
    if target == 'equipment':
        regions = [(20 + i * 160, 910, 180 + i * 160, 1080) for i in range(6)]
    elif target == 'equipment_submenu':
        regions = [(50 + i * 150, 750, 215 + i * 150, 920) for i in range(6)]
    else:
        regions = [(left, top, right, bottom) for top, bottom in ((0, 170), (170, 310), (310, 465))
                   for left, right in ((1620, 1770), (1770, 1920))]
        regions += [(1770, 465, 1920, 610), (1770, 610, 1920, 780)]
    return regions


def button_candidates(frame, target):
    """固定 HUD 槽位只用于裁图和坐标映射，是否为目标按钮仍由模型读取当前画面。"""
    height, width = frame.shape[:2]
    if target == 'home':
        return cv2.imencode('.png', frame)[1].tobytes(), {}
    regions = button_regions(target)
    # 预览不改变原始 ADB 坐标；编号在候选之外，不覆盖 UI 文字。
    preview_height = round(height * 960 / width)
    sheet = np.full((preview_height + ((len(regions) + 3) // 4) * 220, 960, 3), 245, np.uint8)
    sheet[:preview_height] = cv2.resize(frame, (960, preview_height))
    centers = {}
    for index, (left, top, right, bottom) in enumerate(regions, 1):
        x1, y1 = round(left * width / 1920), round(top * height / 1080)
        x2, y2 = round(right * width / 1920), round(bottom * height / 1080)
        crop = frame[y1:y2, x1:x2]
        if not crop.size:
            continue
        centers[index] = ((x1 + x2) // 2, (y1 + y2) // 2)
        scale = min(200 / crop.shape[1], 175 / crop.shape[0])
        crop = cv2.resize(crop, (max(1, round(crop.shape[1] * scale)), max(1, round(crop.shape[0] * scale))))
        x, y = ((index - 1) % 4) * 240 + 20, preview_height + ((index - 1) // 4) * 220
        cv2.putText(sheet, str(index), (x, y + 28), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
        sheet[y+35:y+35+crop.shape[0], x:x+crop.shape[1]] = crop
    return cv2.imencode('.png', sheet)[1].tobytes(), centers


def parse_main_screen(result, target, width, height, centers):
    if (not isinstance(result, dict) or result.get('target') != target
            or result.get('page') not in {'main', 'equipment_menu', 'other', 'unknown'}
            or type(result.get('found')) is not bool):
        raise VisualError('主界面模型返回格式错误，未继续点击')
    expected_page = 'equipment_menu' if target == 'equipment_submenu' else 'main'
    if not result['found'] or result['page'] != expected_page:
        return None, 0.0
    if target == 'home':
        return (width // 2, height // 2), 1.0
    candidate = result.get('candidate_id')
    if type(candidate) is not int or candidate not in centers:
        raise VisualError('主界面模型按钮编号无效，未继续点击')
    return centers[candidate], 1.0


def save_rejected_reward(asset_root, png, preview, prompt, result, roi, score, width, height):
    """只保留一份完整拒绝证据；保存失败不能改变未点击的判断。"""
    relative = Path('.data/diagnostics/main-screen/rejected-rewards.zip')
    destination, temporary = asset_root / relative, None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata = dict(candidate_id=result['candidate_id'], roi=roi, score=score,
                        width=width, height=height, recorded_at_utc=datetime.now(timezone.utc).isoformat())
        with NamedTemporaryFile(dir=destination.parent, prefix='rejected-rewards-',
                                suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            with ZipFile(stream, 'w') as archive:
                archive.writestr('input.png', png)
                archive.writestr('candidates.png', preview)
                archive.writestr('prompt.txt', prompt)
                archive.writestr('result.json', json.dumps(result, ensure_ascii=False))
                archive.writestr('metadata.json', json.dumps(metadata, ensure_ascii=False))
        temporary.replace(destination)
        LOGGER.info('奖励候选 %d 拒绝证据已保存：%s', result['candidate_id'], relative.as_posix())
    except Exception as exc:
        LOGGER.warning('奖励候选 %d 拒绝证据保存失败：%s，仍未点击',
                       result['candidate_id'], type(exc).__name__)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class MainScreenRecognition:
    def __init__(self, config, asset_root):
        self.asset_root = Path(asset_root)
        self.model = MultimodalClient(config, asset_root=asset_root)
        self.timeout = config['timeout_seconds']

    def match(self, png, target, timeout_seconds=None):
        budget = stage_timeout(self.timeout if timeout_seconds is None else min(self.timeout, timeout_seconds))
        started = time.monotonic()
        menu = target['main_menu']
        try:
            if budget <= 0:
                raise VisualError('主界面模型识别超时，未继续点击')
            frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise VisualError('主界面截图无法解析')
            height, width = frame.shape[:2]
            preview, centers = button_candidates(frame, menu)
            remaining = stage_timeout(budget - (time.monotonic() - started))
            if remaining <= 0:
                raise VisualError('主界面截图处理超时，未继续点击')
            LOGGER.info('主界面模型识别：%s，调用预算 %.2f 秒', target['name'], budget)
            prompt = main_screen_prompt(menu, centers)
            with model_request_context(f'主界面识别/{menu}'):
                result = self.model.complete_json(preview, prompt, timeout_seconds=remaining)
            execution = current_execution()
            if execution is not None:
                execution.remaining()
            elif time.monotonic() - started >= budget:
                raise VisualError('主界面模型响应已超时，忽略结果，未继续点击')
            center, score = parse_main_screen(result, menu, width, height, centers)
            if menu == 'rewards' and center is not None:
                candidate = result['candidate_id']
                left, top, right, bottom = button_regions(menu)[candidate - 1]
                roi = (round(left * width / 1920), round(top * height / 1080),
                       round(right * width / 1920), round(bottom * height / 1080))
                if (width, height) != (1920, 1080):
                    LOGGER.warning('奖励候选 %d 未通过复核：截图尺寸 %d×%d，仅支持 1920×1080，未点击',
                                   candidate, width, height)
                    center, score = None, 0.0
                else:
                    score = -1.0
                    for template in ('main_reward_label.png', 'main_reward_label_red_dot.png'):
                        _, template_score = find_template_in_png(
                            png, self.asset_root / 'assets' / template, roi, REWARD_CONFIRMATION_THRESHOLD)
                        score = max(score, template_score)
                        if score > REWARD_CONFIRMATION_THRESHOLD:
                            break
                    LOGGER.info('奖励候选 %d 同帧文字复核：相似度 %.3f，要求大于 %.3f，%s',
                                candidate, score, REWARD_CONFIRMATION_THRESHOLD,
                                '通过' if score > REWARD_CONFIRMATION_THRESHOLD else '未通过，未点击')
                    if score <= REWARD_CONFIRMATION_THRESHOLD:
                        center = None
                if center is None:
                    save_rejected_reward(self.asset_root, png, preview, prompt, result, roi, score, width, height)
                if execution is not None:
                    execution.remaining()
                elif time.monotonic() - started >= budget:
                    raise VisualError('奖励候选复核已超时，未继续点击')
        except Exception as exc:
            elapsed = time.monotonic() - started
            LOGGER.error('主界面模型识别失败：目标=%s，异常类型=%s，已耗时 %.2f 秒，本次识别总预算 %.2f 秒，剩余预算 %.2f 秒',
                         menu, type(exc).__name__, elapsed, budget, max(0.0, budget - elapsed),
                         extra={'backend_only': is_model_api_failure(exc)})
            raise
        LOGGER.info('主界面模型识别：%s，%s，耗时 %.2f 秒',
                    target['name'], f'已确认 {center}' if center else '未确认', time.monotonic() - started)
        return center, score
