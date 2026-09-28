"""优先最低进阶装备，循环获取材料、合成、装备和进阶。"""
import logging
import time
from pathlib import Path

from naruto_daily.common.model_workflow import ModelWorkflow, require_page
from naruto_daily.common.visual import VisualError, find_template_in_png
from . import prompts

LOGGER = logging.getLogger('naruto_daily')
EQUIPMENT_POINTS = ((275, 360), (525, 360), (780, 360), (275, 610), (525, 610), (780, 610))
MATERIAL_POINTS = tuple((x, 690) for x in (1106, 1244, 1380, 1518, 1656, 1792))
BLANK = (1600, 920)
MATERIAL_BUTTON = (650, 840)
# 1920×1080 扫荡设置弹层：提示文字右侧、开始扫荡按钮左侧的复选框。
AUTO_STOP = (1110, 785)


def lowest_equipment(data):
    require_page(data, 'equipment')
    levels = data.get('progressions')
    if (not isinstance(levels, list) or len(levels) != 6
            or any(type(n) is not int or not 0 <= n <= 999 for n in levels)):
        raise VisualError('六件装备的进阶等级未读全')
    return min(range(6), key=lambda i: levels[i]) + 1


def first_material(data, selected):
    require_page(data, 'equipment')
    if type(data.get('selected_slot')) is not int or data['selected_slot'] != selected:
        raise VisualError('当前选中的装备与目标不一致')
    index, state = data.get('material_slot'), data.get('material_state')
    if state == 'advance' and index is None and data.get('can_advance') is True:
        return None
    if (state not in ('sweepable', 'craftable', 'equipable')
            or type(index) is not int or not 1 <= index <= 6
            or data.get('can_advance') is not False):
        raise VisualError('未确认首个可扫荡材料或其它可用操作；可扫荡提示可能闪烁，需要重新截图')
    return index


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
    for _ in range(4):
        if page is None:
            page = flow.read('装备弹层关闭检查', prompts.PAGE, _page)
        if page == 'equipment':
            return
        flow.tap(BLANK)
        page = None
    raise VisualError('装备弹层未能关闭，已停止')


def _finish_material(flow, button):
    if button == 'synthesize':
        for attempt in range(1, 4):
            LOGGER.info('[材料合成 第%d/3次] 材料已齐，点击合成成品', attempt)
            flow.tap(MATERIAL_BUTTON)
            button = flow.read('合成后装备按钮', prompts.MATERIAL, _material)
            if button == 'synthesize':
                # 点击可能尚未生效；先等画面更新，重新确认后才决定是否重试。
                LOGGER.info('合成后仍显示合成，等待1秒后重新截图确认')
                flow.images.sleep(min(1, flow.remaining()))
                button = flow.read('合成后按钮补查', prompts.MATERIAL, _material)
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


def _dismiss_sweep_result(flow, stamina_before):
    deadline = flow.elapsed() + min(flow.action['sweep_timeout_seconds'], flow.remaining())
    # 材料足够后可能根本没有“继续扫荡”，直接核验结果层本身。
    runs, stamina_after = flow.read('扫荡完成结果核验', prompts.SWEEP_RESULT, _sweep_result,
                                    timeout=deadline - flow.elapsed())
    LOGGER.info('本次扫荡已完成%d次，体力读数 %d→%d', runs, stamina_before, stamina_after)
    # “继续扫荡”会在滚动动画期间提前出现、在材料足够时消失。
    # 必须确认前景回到材料详情，不能把按钮暂时消失当成结果层关闭。
    while flow.elapsed() < deadline:
        flow.tap(BLANK)
        center, _ = flow.images.match(flow.client.screenshot(), flow.action['sweep_done'])
        flow.remaining()
        if flow.elapsed() >= deadline:
            break
        if center is None:
            page = flow.read('扫荡结果关闭检查', prompts.PAGE, _page,
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


def handle_equipment_upgrade(client, action, *, model=None, template_matcher=find_template_in_png,
                             asset_root=Path('.'), sleep=time.sleep):
    flow = ModelWorkflow(client, action, model=model, template_matcher=template_matcher,
                         asset_root=asset_root, sleep=sleep)
    previous_advance = None
    unchanged_material = None
    for round_number in range(1, action['max_rounds'] + 1):
        overview = flow.read('最低进阶装备识别', prompts.EQUIPMENT, _overview)
        slot = lowest_equipment(overview)
        levels = tuple(overview['progressions'])
        if previous_advance == levels:
            raise VisualError('进阶后装备等级未变化，未重复点击')
        previous_advance = None
        LOGGER.info('[装备养成 第%d轮] 选择第%d件装备，进阶+%d', round_number, slot, levels[slot - 1])
        flow.tap(EQUIPMENT_POINTS[slot - 1])
        info_slots = set()
        def validate_materials(data):
            index = first_material(data, slot)
            if index in info_slots:
                raise VisualError(f'第{index}格已确认是只读成品信息卡，未重复点击')
            return data
        materials = flow.read('首个可扫荡材料识别', prompts.MATERIALS, validate_materials)
        index = first_material(materials, slot)
        signature = (levels, slot, index, materials['material_state'])
        if unchanged_material == signature:
            raise VisualError('合成装备后材料状态未变化，已停止循环')
        unchanged_material = None
        while index is not None:
            LOGGER.info('选中第%d格材料，模型确认操作：%s', index, materials['material_state'])
            flow.tap(MATERIAL_POINTS[index - 1])
            button = flow.read('装备材料详情', prompts.MATERIAL_ENTRY, _material_entry)
            if button != 'material_info':
                break
            LOGGER.info('第%d格打开只读成品信息卡，关闭后重新识别首个可扫荡材料', index)
            info_slots.add(index)
            _close_layers(flow, 'material_info')
            prompt = prompts.MATERIALS + f'\n本轮已确认第{sorted(info_slots)}格仅打开只读成品信息卡，不能再次选择这些格子。'
            materials = flow.read('成品信息卡关闭后重新识别扫荡目标', prompt, validate_materials)
            index = first_material(materials, slot)
            signature = (levels, slot, index, materials['material_state'])
        if index is None:
            LOGGER.info('当前装备六个成品均已齐全，执行进阶')
            flow.button('advance')
            previous_advance = levels
            continue
        if button != 'sweep':
            _finish_material(flow, button)
            unchanged_material = signature
            continue
        flow.button('one_click_sweep')
        stamina, auto_stop = flow.read('开始扫荡前体力检查', prompts.STAMINA, _sweep_setup)
        if stamina > 5 and not auto_stop:
            LOGGER.info('材料足够后自动停止未勾选，点击复选框后重新截图确认')
            flow.tap(AUTO_STOP)
            # 只点一次；点击后仅重新识别，避免延迟或误识别时把勾取消。
            stamina = flow.read('材料足够后自动停止勾选复查', prompts.STAMINA, _stamina)
        if stamina <= 5:
            LOGGER.info('体力为%d，不大于5，结束装备任务并返回主界面', stamina)
            _close_layers(flow, 'sweep_setup')
            return
        flow.button('start_sweep')
        _dismiss_sweep_result(flow, stamina)
        button = flow.read('扫荡后合成检查', prompts.MATERIAL, _material)
        _finish_material(flow, button)
        if button != 'sweep':
            unchanged_material = signature
    raise VisualError(f"装备任务达到最多{action['max_rounds']}轮，已停止")
