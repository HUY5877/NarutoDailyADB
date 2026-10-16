"""优先最低进阶装备，从左到右处理六格材料，再检查进阶。"""
import logging
import time
from itertools import count
from pathlib import Path

from naruto_daily.common.execution import current_execution
from naruto_daily.common.model_workflow import ModelWorkflow, require_page
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.multimodal import prompt_jev, prompts

LOGGER = logging.getLogger('naruto_daily')
EQUIPMENT_POINTS = ((275, 360), (525, 360), (780, 360), (275, 610), (525, 610), (780, 610))
MATERIAL_POINTS = tuple((x, 690) for x in (1106, 1244, 1380, 1518, 1656, 1792))
BLANK = (1600, 920)
MATERIAL_BUTTON = (650, 840)
START_SWEEP = (1300, 780)
ADVANCE = (1750, 830)
# 1920×1080 扫荡设置弹层：提示文字右侧、开始扫荡按钮左侧的复选框。
AUTO_STOP = (1110, 785)


def lowest_equipment(data):
    require_page(data, 'equipment')
    levels = data.get('progressions')
    if (not isinstance(levels, list) or len(levels) != 6
            or any(type(n) is not int or not 0 <= n <= 999 for n in levels)):
        raise VisualError('六件装备的进阶等级未读全')
    return min(range(6), key=lambda i: levels[i]) + 1


def _can_advance(data, selected):
    require_page(data, 'equipment')
    if type(data.get('selected_slot')) is not int or data['selected_slot'] != selected:
        raise VisualError('当前选中的装备与目标不一致')
    if type(data.get('can_advance')) is not bool:
        raise VisualError('未确认当前装备能否进阶')
    return data['can_advance']


def _overview(data):
    lowest_equipment(data)
    return data


def _material(data):
    require_page(data, 'material')
    if data.get('action') not in ('sweep', 'synthesize', 'equip'):
        raise VisualError('未确认材料详情中的可用操作按钮')
    return data['action']


def _material_entry(data):
    if isinstance(data, dict) and data.get('page') == 'material_info':
        return 'material_info'
    return _material(data)


def _sweep_setup(data):
    require_page(data, 'sweep_setup')
    stamina = data.get('stamina')
    if type(stamina) is not int or not 0 <= stamina <= 99999:
        raise VisualError('扫荡前体力数值不明确')
    auto_stop = data.get('auto_stop')
    if type(auto_stop) is not bool:
        raise VisualError('材料足够后自动停止的勾选状态不明确')
    return stamina, auto_stop


def _stamina(data):
    stamina, auto_stop = _sweep_setup(data)
    if stamina > 5 and not auto_stop:
        raise VisualError('未确认材料足够后自动停止已勾选')
    return stamina


def _sweep_result(data):
    require_page(data, 'sweep_result')
    runs, stamina = data.get('completed_runs'), data.get('stamina')
    if type(runs) is not int or not 1 <= runs <= 99999:
        raise VisualError('扫荡结果未确认实际完成次数，未继续操作')
    if type(stamina) is not int or not 0 <= stamina <= 99999:
        raise VisualError('扫荡结果体力数值不明确')
    if data.get('animation_finished') is not True:
        raise VisualError('扫荡结果动画尚未确认结束')
    return runs, stamina


def _page(data):
    if not isinstance(data, dict) or data.get('page') not in (
            'equipment', 'material', 'material_info', 'sweep_setup', 'sweep_result'):
        raise VisualError('未确认装备弹层状态')
    return data['page']


def _close_layers(flow, page=None):
    scoped = current_execution() is not None
    expected = {'equipment', page or 'material'}
    if page == 'sweep_setup':
        expected.add('material')

    def closing_page(data):
        observed = _page(data)
        if observed not in expected:
            raise VisualError('装备弹层关闭后出现其他前景，继续观察，未继续点击')
        return observed

    attempts = count() if scoped else range(4)
    for _ in attempts:
        if page is None:
            page = flow.read('装备弹层关闭检查', prompt_jev.EQUIPMENT_PAGE, closing_page if scoped else _page)
        if page == 'equipment':
            return
        flow.tap(BLANK)
        page = None
    raise VisualError('装备弹层未能关闭，已停止')


def _equip_after_synthesis(data):
    button = _material(data)
    if button != 'equip':
        raise VisualError('合成后尚未确认装备按钮，继续观察，未重复合成')
    return button


def _finish_material(flow, button):
    if button == 'synthesize' and current_execution() is not None:
        LOGGER.info('材料已齐，点击一次合成成品，等待装备按钮')
        flow.tap(MATERIAL_BUTTON)
        button = flow.read('合成后装备按钮', prompt_jev.EQUIPMENT_MATERIAL, _equip_after_synthesis)
    elif button == 'synthesize':
        for attempt in range(1, 4):
            LOGGER.info('[材料合成 第%d/3次] 材料已齐，点击合成成品', attempt)
            flow.tap(MATERIAL_BUTTON)
            button = flow.read('合成后装备按钮', prompt_jev.EQUIPMENT_MATERIAL, _material)
            if button == 'synthesize':
                # 点击可能尚未生效；先等画面更新，重新确认后才决定是否重试。
                LOGGER.info('合成后仍显示合成，等待1秒后重新截图确认')
                flow.images.sleep(min(1, flow.remaining()))
                button = flow.read('合成后按钮补查', prompt_jev.EQUIPMENT_MATERIAL, _material)
            if button == 'equip':
                break
            if button != 'synthesize':
                raise VisualError('合成后按钮状态异常，未继续点击')
            if attempt < 3:
                LOGGER.info('补查仍确认合成按钮可用，将重试合成')
        else:
            raise VisualError('合成按钮点击3次后仍未出现装备按钮，已停止')
    if button == 'equip':
        LOGGER.info('将成品装备到当前装备')
        flow.tap(MATERIAL_BUTTON)
        _close_layers(flow)
    else:
        _close_layers(flow, 'material')


def _sweep_close_page(data):
    page = _page(data)
    if page not in ('material', 'sweep_result'):
        raise VisualError('扫荡结果关闭后尚未确认材料详情，继续观察，未继续点击')
    return page


def _dismiss_sweep_result(flow, stamina_before):
    scoped = current_execution() is not None
    deadline = flow.elapsed() + (flow.remaining() if scoped else
                                 min(flow.action['sweep_timeout_seconds'], flow.remaining()))
    # 材料足够后可能根本没有“继续扫荡”，直接核验结果层本身。
    runs, stamina_after = flow.read('扫荡完成结果核验', prompts.EQUIPMENT_SWEEP_RESULT, _sweep_result,
                                    timeout=deadline - flow.elapsed())
    LOGGER.info('本次扫荡已完成%d次，体力读数 %d→%d', runs, stamina_before, stamina_after)
    # “继续扫荡”会在滚动动画期间提前出现、在材料足够时消失。
    # 必须确认前景回到材料详情，不能把按钮暂时消失当成结果层关闭。
    while flow.elapsed() < deadline:
        flow.tap(BLANK)
        page = flow.read('扫荡结果关闭检查', prompt_jev.EQUIPMENT_PAGE, _sweep_close_page if scoped else _page,
                         timeout=deadline - flow.elapsed())
        if page == 'material':
            return
        if page != 'sweep_result':
            raise VisualError('扫荡结果关闭后未回到材料详情，已停止')
        LOGGER.info('扫荡结果弹层仍在，等待动画后再次点击空白')
        delay = min(1, deadline - flow.elapsed())
        if delay > 0:
            flow.images.sleep(delay)
    raise VisualError('等待扫荡结果关闭超时，未继续操作装备')


def _sweep_material(flow):
    """当前材料扫荡至可合成/装备；体力不足时关闭弹层并返回False。"""
    while True:
        # 调用前已由模型确认最前方材料详情的一键扫荡按钮。
        flow.tap(MATERIAL_BUTTON)
        stamina, auto_stop = flow.read('开始扫荡前体力检查', prompts.EQUIPMENT_STAMINA, _sweep_setup)
        if stamina > 5 and not auto_stop:
            LOGGER.info('材料足够后自动停止未勾选，点击复选框后重新截图确认')
            flow.tap(AUTO_STOP)
            # 只点一次；点击后仅重新识别，避免把勾取消。
            stamina = flow.read('材料足够后自动停止勾选复查', prompts.EQUIPMENT_STAMINA, _stamina)
        if stamina <= 5:
            LOGGER.info('体力为%d，不大于5，结束装备任务并返回主界面', stamina)
            _close_layers(flow, 'sweep_setup')
            return False
        flow.tap(START_SWEEP)
        _dismiss_sweep_result(flow, stamina)
        button = flow.read('扫荡后合成检查', prompt_jev.EQUIPMENT_MATERIAL, _material)
        if button != 'sweep':
            _finish_material(flow, button)
            return True
        LOGGER.info('当前材料仍需扫荡，重新打开扫荡设置并检查体力')


def handle_equipment_upgrade(client, action, *, model=None, template_matcher=find_template_in_png,
                             asset_root=Path('.'), sleep=time.sleep):
    flow = ModelWorkflow(client, action, model=model, template_matcher=template_matcher,
                         asset_root=asset_root, sleep=sleep)
    rounds = count(1) if current_execution() is not None else range(1, action['max_rounds'] + 1)
    for round_number in rounds:
        overview = flow.read('最低进阶装备识别', prompts.EQUIPMENT, _overview)
        slot = lowest_equipment(overview)
        levels = tuple(overview['progressions'])
        LOGGER.info('[装备养成 第%d轮] 选择第%d件装备，进阶+%d', round_number, slot, levels[slot - 1])
        flow.tap(EQUIPMENT_POINTS[slot - 1])
        operated = False
        for index, point in enumerate(MATERIAL_POINTS, 1):
            LOGGER.info('[装备养成 第%d轮 材料%d/6] 打开材料格，由模型判断弹层操作', round_number, index)
            flow.tap(point)
            button = flow.read('装备材料详情', prompt_jev.EQUIPMENT_MATERIAL_ENTRY, _material_entry)
            if button == 'material_info':
                LOGGER.info('第%d格已装备，信息卡没有操作按钮，关闭后继续下一格', index)
                _close_layers(flow, 'material_info')
                continue
            LOGGER.info('第%d格材料操作：%s', index, button)
            if button == 'sweep':
                if not _sweep_material(flow):
                    return
            else:
                _finish_material(flow, button)
            operated = True
        can_advance = flow.read('六格遍历后进阶检查', prompt_jev.EQUIPMENT_ADVANCE,
                                lambda data: _can_advance(data, slot))
        if can_advance:
            LOGGER.info('六格材料遍历完成，模型确认进阶按钮可用，执行进阶')
            flow.tap(ADVANCE)
            LOGGER.info('已点击进阶，下一轮重新选择最低进阶装备并遍历六格材料')
        elif not operated:
            LOGGER.info('六格材料均无可用操作，当前装备也不能进阶，结束装备任务')
            return
        else:
            LOGGER.info('六格材料遍历完成，当前不能进阶，下一轮重新选择最低进阶装备')
    raise VisualError(f"装备任务达到最多{action['max_rounds']}轮，已停止")
