from pathlib import Path

import cv2
import numpy as np

from naruto_daily.common.visual import find_template_in_png


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


def test_generic_close_template_rejects_real_main_page_top_right_icons():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "main_page_top_right.png").read_bytes()

    center, score = find_template_in_png(
        screenshot,
        project_root / "assets" / "promo_close.png",
        (0, 0, 220, 180),
        0.95,
    )

    assert center is None
    assert score < 0.95


def test_ninja_recruit_label_matches_only_its_task_card():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "ninja_recruit_card.png").read_bytes()

    center, score = find_template_in_png(
        screenshot,
        project_root / "assets" / "ninja_recruit_label.png",
        (0, 0, 400, 220),
        0.85,
    )

    assert center == (232, 100)
    assert score >= 0.99


def test_recruit_sidebar_templates_match_labels_and_dedicated_red_dots():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "advanced_recruit_page.png").read_bytes()

    advanced_tab, advanced_tab_score = find_template_in_png(
        screenshot,
        project_root / "assets" / "advanced_recruit_tab.png",
        (0, 150, 300, 350),
        0.90,
    )
    advanced_dot, advanced_dot_score = find_template_in_png(
        screenshot,
        project_root / "assets" / "recruit_red_dot.png",
        (170, 180, 270, 300),
        0.95,
    )
    ordinary_tab, ordinary_tab_score = find_template_in_png(
        screenshot,
        project_root / "assets" / "ordinary_recruit_tab.png",
        (0, 780, 300, 1030),
        0.90,
    )
    ordinary_dot, ordinary_dot_score = find_template_in_png(
        screenshot,
        project_root / "assets" / "recruit_red_dot.png",
        (170, 800, 270, 980),
        0.95,
    )

    assert advanced_tab == (126, 263)
    assert advanced_tab_score >= 0.99
    assert advanced_dot == (219, 230)
    assert advanced_dot_score >= 0.99
    assert ordinary_tab == (128, 909)
    assert ordinary_tab_score >= 0.99
    assert ordinary_dot == (219, 871)
    assert ordinary_dot_score >= 0.95


def test_recruit_confirm_template_matches_left_confirm_and_rejects_paid_choices():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "recruit_result.png").read_bytes()
    template = project_root / "assets" / "recruit_confirm.png"

    confirm_center, confirm_score = find_template_in_png(
        screenshot,
        template,
        (350, 700, 800, 1000),
        0.90,
    )
    paid_center, paid_score = find_template_in_png(
        screenshot,
        template,
        (800, 700, 1500, 1000),
        0.90,
    )

    assert confirm_center == (598, 843)
    assert confirm_score >= 0.99
    assert paid_center is None
    assert paid_score < 0.90


def test_fortune_close_is_found_only_after_extending_recruit_return_area_downward():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "fortune_page_close.png").read_bytes()
    template = project_root / "assets" / "promo_close.png"

    old_center, old_score = find_template_in_png(
        screenshot,
        template,
        (1700, 0, 1920, 180),
        0.95,
    )
    extended_center, extended_score = find_template_in_png(
        screenshot,
        template,
        (1650, 0, 1920, 300),
        0.95,
    )

    assert old_center is None
    assert old_score < 0.95
    assert extended_center == (1754, 175)
    assert extended_score >= 0.99


def test_extended_fortune_close_area_still_rejects_main_page_activity_button():
    project_root = Path(__file__).parents[1]
    screenshot = (project_root / "tests" / "fixtures" / "main_page_activity_icon.png").read_bytes()

    center, score = find_template_in_png(
        screenshot,
        project_root / "assets" / "promo_close.png",
        (1650, 0, 1920, 300),
        0.95,
    )

    assert center is None
    assert score < 0.95


def test_configured_ordinary_recruit_targets_cover_both_sidebar_layouts():
    from naruto_daily.config import load_settings

    root = Path(__file__).parents[1]
    settings = load_settings(root / "config.example.json")
    action = next(a for a in settings.tasks["recruit"] if a["type"] == "handle_free_recruit")
    for filename, expected_dot, expected_tab in (
        ("advanced_recruit_page.png", (219, 871), (128, 909)),
        ("recruit_sidebar_shifted.png", (218, 742), (127, 780)),
    ):
        png = (root / "tests/fixtures" / filename).read_bytes()
        for key, expected in (("ordinary_red_dot", expected_dot), ("ordinary_tab", expected_tab)):
            target = action[key]
            center, score = find_template_in_png(
                png, root / target["template"],
                tuple(target[k] for k in ("left", "top", "right", "bottom")), target["threshold"],
            )
            assert center == expected, (filename, key, center, score)


def test_shifted_sidebar_still_rejects_missing_advanced_dot():
    from naruto_daily.config import load_settings

    root = Path(__file__).parents[1]
    settings = load_settings(root / "config.example.json")
    action = next(a for a in settings.tasks["recruit"] if a["type"] == "handle_free_recruit")
    png = (root / "tests/fixtures/recruit_sidebar_shifted.png").read_bytes()
    for key in ("advanced_red_dot",):
        target = action[key]
        center, score = find_template_in_png(
            png, root / target["template"],
            tuple(target[k] for k in ("left", "top", "right", "bottom")), target["threshold"],
        )
        assert center is None, (key, center, score)
