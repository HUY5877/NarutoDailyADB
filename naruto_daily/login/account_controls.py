"""从安卓账号列表控件取得精确坐标，不依赖昵称图片或模型猜测坐标。"""
import re
from xml.etree import ElementTree

from naruto_daily.common.visual import VisualError


def find_account_control(xml: str, nickname: str, list_top: int):
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise VisualError('无法读取安卓账号列表控件') from None
    matches = []
    for node in root.iter('node'):
        if (node.get('package') != 'com.tencent.mobileqq' or node.get('text') != nickname
                or node.get('enabled') != 'true'):
            continue
        bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
        if bounds is None:
            continue
        left, top, right, bottom = map(int, bounds.groups())
        if not (0 <= left < right < 960 and list_top <= top < bottom <= 1080):
            continue
        matches.append(((left + right) // 2, (top + bottom) // 2))
    return matches[0] if len(matches) == 1 else None
