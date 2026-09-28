"""毒风快速切入战斗使用真实 HUD；单条血条或其他页面不能命中。"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.secret_realm.boss_detection import poison_boss_visible


FIXTURES = Path(__file__).parent / 'fixtures'


def encode(frame):
    return cv2.imencode('.png', frame)[1].tobytes()


def boss_frame():
    return cv2.imdecode(np.frombuffer((FIXTURES / 'secret_poison_boss_active.png').read_bytes(), np.uint8), 1)


@pytest.mark.parametrize('size', [(1203, 677), (1920, 1080), (1280, 720), (960, 540)])
def test_real_poison_boss_at_supported_scales(size):
    assert poison_boss_visible(encode(cv2.resize(boss_frame(), size)))


@pytest.mark.parametrize('quality', [35, 60, 90])
def test_compressed_actual_boss_still_matches(quality):
    frame = cv2.resize(boss_frame(), (1920, 1080))
    encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()
    assert poison_boss_visible(encoded)


@pytest.mark.parametrize('path', sorted(p for p in FIXTURES.glob('*.png') if p.name != 'secret_poison_boss_active.png'), ids=lambda p: p.name)
def test_existing_other_pages_are_not_poison_boss(path):
    assert not poison_boss_visible(path.read_bytes())


@pytest.mark.parametrize('roi', [(675, 530, 745, 620), (535, 585, 670, 620), (150, 617, 680, 649)])
def test_each_hud_component_is_required(roi):
    frame = cv2.resize(boss_frame(), (1200, 675))
    x1, y1, x2, y2 = roi
    frame[y1:y2, x1:x2] = 0
    assert not poison_boss_visible(encode(frame))


def test_plain_red_line_and_player_health_bar_are_not_boss():
    frame = np.zeros((675, 1200, 3), np.uint8)
    cv2.rectangle(frame, (170, 623), (715, 640), (0, 0, 255), -1)
    cv2.rectangle(frame, (65, 30), (175, 43), (0, 0, 255), -1)
    assert not poison_boss_visible(encode(frame))


def test_single_red_line_is_insufficient_even_with_name_and_emblem():
    frame = cv2.resize(boss_frame(), (1200, 675))
    frame[617:649, 150:680] = 0
    cv2.line(frame, (170, 630), (675, 630), (0, 0, 255), 1)
    assert not poison_boss_visible(encode(frame))


@pytest.mark.parametrize('brightness', [1.0, .75, .6])
def test_real_failure_overlay_wins_over_retained_boss_hud(brightness):
    # 合成回归：真实失败区域覆盖真实 BOSS 场景，HUD 刻意保留。
    frame = (cv2.resize(boss_frame(), (1200, 675)) * brightness).astype(np.uint8)
    failed = cv2.imdecode(np.frombuffer((FIXTURES / 'secret_poison_unreleased_failure.png').read_bytes(), np.uint8), 1)
    frame[160:420, 390:810] = cv2.resize(failed[160:430, 360:820], (420, 260))
    assert not poison_boss_visible(encode(frame))


@pytest.mark.parametrize('filename', ['secret_pause_menu.png', 'secret_exit_dialog.png',
                                     'secret_result.png', 'secret_reward_overlay.png'])
def test_actual_end_or_pause_page_cannot_be_overridden_by_boss_hud(filename):
    frame = cv2.imdecode(np.frombuffer((FIXTURES / filename).read_bytes(), np.uint8), 1)
    frame = cv2.resize(frame, (1200, 675))
    # 保留原画面的暂停/结算区域，同时模拟底层 BOSS HUD 尚未消失。
    frame[535:650, 150:760] = cv2.resize(boss_frame(), (1200, 675))[535:650, 150:760]
    assert not poison_boss_visible(encode(frame))


@pytest.mark.parametrize('png', [b'', b'not an image'])
def test_invalid_image_is_conservative(png):
    assert not poison_boss_visible(png)
