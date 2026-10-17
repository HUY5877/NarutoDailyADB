"""用户两张截图验证自动战斗文字匹配，背景不参与模板。

原图 6d15df0f 为关闭自动，2dce3920 为开启自动；均保留原始 1920×1080 像素。
模板取自开启截图的 (874,112,1058,158)，仅白色文字及相邻深色描边不透明，
省略号与战斗背景不参与匹配。
"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.common.visual import find_template_in_png


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures'
TEMPLATE = ROOT / 'assets' / 'squad_assault_auto_battle.png'
ROI = (820, 95, 1120, 180)


def match(png):
    return find_template_in_png(png, TEMPLATE, ROI, .9)


def test_user_enabled_frame_recognizes_auto_battle_text():
    center, score = match((FIXTURES / 'squad_assault_battle_auto_on.png').read_bytes())
    assert center is not None and score > .98


@pytest.mark.parametrize('color', [(0, 0, 0), (240, 240, 240), (180, 70, 20), (20, 80, 180)])
def test_text_survives_different_battle_backgrounds(color):
    image = cv2.imread(str(FIXTURES / 'squad_assault_battle_auto_on.png'))
    text = image[112:158, 874:1058]
    # Independently preserve observed white letters and their surrounding outline.
    # Repaint the rest of the text rectangle; an opaque crop fails this replay.
    foreground = cv2.dilate((text.min(axis=2) >= 190).astype(np.uint8),
                            np.ones((7, 7), np.uint8)).astype(bool)
    text[~foreground] = color
    center, score = match(cv2.imencode('.png', image)[1].tobytes())
    assert center is not None and score > .98


@pytest.mark.parametrize('fixture', [
    'squad_assault_battle_auto_off', 'squad_assault_ready_unchecked',
    'squad_assault_quadruple_dialog', 'squad_assault_limit_unannotated',
    'secret_flame_battle',
])
def test_frames_without_text_do_not_count_as_automatic_battle(fixture):
    center, score = match((FIXTURES / (fixture + '.png')).read_bytes())
    assert center is None and score < .9


def test_bright_plain_background_does_not_replace_the_text_shape():
    image = np.full((1080, 1920, 3), 255, np.uint8)
    center, score = match(cv2.imencode('.png', image)[1].tobytes())
    assert center is None and score < .9
