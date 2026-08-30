from pathlib import Path

import cv2
import numpy as np

from naruto_daily.visual import find_template_in_png


def encode(image):
    ok, data = cv2.imencode(".png", image)
    assert ok
    return data.tobytes()


def test_find_template_returns_android_center_and_score(tmp_path):
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    template[:, :10] = (0, 128, 255)
    template[5:15, 12:25] = (255, 255, 255)
    screen = np.zeros((200, 300, 3), dtype=np.uint8)
    screen[90:110, 140:170] = template
    template_path = tmp_path / "claim.png"
    cv2.imwrite(str(template_path), template)

    center, score = find_template_in_png(encode(screen), template_path, (100, 60, 220, 150), 0.85)

    assert center == (155, 100)
    assert score >= 0.99


def test_find_template_returns_none_below_threshold(tmp_path):
    template = np.full((20, 30, 3), 255, dtype=np.uint8)
    screen = np.zeros((200, 300, 3), dtype=np.uint8)
    template_path = tmp_path / "claim.png"
    cv2.imwrite(str(template_path), template)

    center, score = find_template_in_png(encode(screen), template_path, (100, 60, 220, 150), 0.85)

    assert center is None
    assert score < 0.85


def test_transparent_template_ignores_popup_background(tmp_path):
    template = np.zeros((24, 24, 4), dtype=np.uint8)
    cv2.line(template, (3, 3), (20, 20), (0, 80, 255, 255), 5)
    cv2.line(template, (20, 3), (3, 20), (0, 80, 255, 255), 5)
    screen = np.full((120, 180, 3), (130, 60, 170), dtype=np.uint8)
    alpha = template[:, :, 3] > 0
    target = screen[50:74, 90:114]
    target[alpha] = template[:, :, :3][alpha]
    template_path = tmp_path / "透明关闭叉.png"
    cv2.imencode(".png", template)[1].tofile(template_path)

    center, score = find_template_in_png(encode(screen), template_path, (60, 30, 140, 100), 0.80)

    assert center == (102, 62)
    assert score >= 0.99


def test_claim_template_rejects_unreached_reward():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "prayer_unreached.png").read_bytes()

    center, score = find_template_in_png(
        screenshot,
        project_root / "assets" / "prayer_claim.png",
        (0, 0, 341, 255),
        0.85,
    )

    assert center is None
    assert score < 0.5


def test_organization_prayer_label_rejects_ninja_recruit_card():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "ninja_recruit_card.png").read_bytes()

    center, score = find_template_in_png(
        screenshot,
        project_root / "assets" / "organization_pray_label.png",
        (0, 0, 400, 220),
        0.85,
    )

    assert center is None
    assert score < 0.6


def test_coin_fortune_label_matches_task_card():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "coin_fortune_card.png").read_bytes()

    center, score = find_template_in_png(
        screenshot,
        project_root / "assets" / "coin_fortune_label.png",
        (0, 300, 373, 430),
        0.85,
    )

    assert center == (187, 365)
    assert score >= 0.99


def test_free_fortune_template_matches_free_button_but_rejects_paid_button():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "coin_fortune_buttons.png").read_bytes()
    template = project_root / "assets" / "free_fortune_once.png"

    free_center, free_score = find_template_in_png(screenshot, template, (0, 0, 450, 188), 0.85)
    paid_center, paid_score = find_template_in_png(screenshot, template, (450, 0, 809, 188), 0.85)

    assert free_center == (252, 85)
    assert free_score >= 0.99
    assert paid_center is None
    assert paid_score < 0.85
