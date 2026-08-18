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
