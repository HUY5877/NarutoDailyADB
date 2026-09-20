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
    states = data.get('materials')
    if (not isinstance(states, list) or len(states) != 6
            or any(s not in ('sweepable', 'filled', 'craftable', 'equipable') for s in states)
            or type(data.get('can_advance')) is not bool):
        raise VisualError('装备材料状态未读全')
    missing = next((i + 1 for i, state in enumerate(states) if state != 'filled'), None)
    if missing is None and not data['can_advance']:
        raise VisualError('材料已齐但没有确认可用的进阶按钮')
    return missing


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


def _stamina(data):
    require_page(data, 'sweep_setup')
    stamina = data.get('stamina')
    if type(stamina) is not int or not 0 <= stamina <= 99999:
        raise VisualError('扫荡前体力数值不明确')
    if data.get('auto_stop') is not True:
        raise VisualError('未确认材料足够后自动停止已勾选')
    return stamina


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


def _dismiss_sweep_result(flow):
    deadline = flow.elapsed() + min(flow.action['sweep_timeout_seconds'], flow.remaining())
    flow.button('sweep_done', click=False, timeout=deadline - flow.elapsed())
    # 结果动画开始时按钮已出现，此时第一次空白点击可能被游戏忽略。
    # 只有重新截图确认结果层消失后，才允许读取背景的合成按钮。
    while flow.elapsed() < deadline:
        flow.tap(BLANK)
        center, _ = flow.images.match(flow.client.screenshot(), flow.action['sweep_done'])
        flow.remaining()
        if flow.elapsed() >= deadline:
            break
        if center is None:
            return
        LOGGER.info('扫荡结果弹层仍在，等待动画后再次点击空白')
        flow.images.sleep(min(1, deadline - flow.elapsed()))
    raise VisualError('等待扫荡结果关闭超时，未继续操作装备')


def handle_equipment_upgrade(client, action, *, model=None, template_matcher=find_template_in_png,
                             asset_root=Path('.'), sleep=time.sleep):
    flow = ModelWorkflow(client, action, model=model, template_matcher=template_matcher,
                         asset_root=asset_root, sleep=sleep)
    previous_advance = None
    previous_sweep = None
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
        def validate_materials(data):
            first_material(data, slot)
            return data
        materials = flow.read('装备成品从左到右识别', prompts.MATERIALS, validate_materials)
        index = first_material(materials, slot)
        signature = (levels, slot, tuple(materials['materials']))
        if unchanged_material == signature:
            raise VisualError('合成装备后材料状态未变化，已停止循环')
        unchanged_material = None
        while index is not None:
            flow.tap(MATERIAL_POINTS[index - 1])
            button = flow.read('装备材料详情', prompts.MATERIAL_ENTRY, _material_entry)
            if button != 'material_info':
                break
            LOGGER.info('第%d格打开成品信息卡，关闭后复查是否已装备', index)
            _close_layers(flow, 'material_info')
            materials = flow.read('成品信息卡关闭后材料复查', prompts.MATERIALS, validate_materials)
            if materials['materials'][index - 1] != 'filled':
                raise VisualError(f'第{index}格信息卡已关闭，但未确认已装备，已停止')
            next_index = first_material(materials, slot)
            if next_index is not None and next_index <= index:
                raise VisualError('已确认的成品格再次显示未装备，已停止重复点击')
            LOGGER.info('确认第%d格成品已装备，继续检查后面的材料', index)
            index = next_index
            signature = (levels, slot, tuple(materials['materials']))
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
        stamina = flow.read('开始扫荡前体力检查', prompts.STAMINA, _stamina)
        if stamina <= 5:
            LOGGER.info('体力为%d，不大于5，结束装备任务并返回主界面', stamina)
            _close_layers(flow, 'sweep_setup')
            return
        if previous_sweep is not None and stamina >= previous_sweep:
            raise VisualError('扫荡后体力未减少，未重复启动扫荡')
        previous_sweep = stamina
        flow.button('start_sweep')
        _dismiss_sweep_result(flow)
        button = flow.read('扫荡后合成检查', prompts.MATERIAL, _material)
        _finish_material(flow, button)
        if button != 'sweep':
            unchanged_material = signature
    raise VisualError(f"装备任务达到最多{action['max_rounds']}轮，已停止")
