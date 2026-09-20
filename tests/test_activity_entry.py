from pathlib import Path

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.config import load_settings


ROOT = Path(__file__).parents[1]


def activity_target():
    return next(
        action
        for action in load_settings(ROOT / "config.example.json").tasks["ichiraku_takeout"]
        if action.get("template") == "assets/activities_entry.png"
    )


@pytest.mark.parametrize("fixture", [
    "main_page_activity_evening.png",
    "daily_home.png",
    "main_page_activity_icon.png",
])
def test_activity_entry_matches_across_home_backgrounds(fixture):
    target = activity_target()
    png = (ROOT / "tests/fixtures" / fixture).read_bytes()
    center, score = ImageRecognition(None, asset_root=ROOT).match(png, target)
    assert center is not None, score
    assert 1800 <= center[0] <= 1870 and 90 <= center[1] <= 130


@pytest.mark.parametrize("fixture", ["activity_page_close.png", "takeout_live.png"])
def test_activity_entry_rejects_activity_page_close_button(fixture):
    png = (ROOT / "tests/fixtures" / fixture).read_bytes()
    center, score = ImageRecognition(None, asset_root=ROOT).match(png, activity_target())
    assert center is None, (center, score)


def test_activity_entry_rejects_other_home_icons():
    target = dict(activity_target(), top=180, bottom=1080)
    png = (ROOT / "tests/fixtures/main_page_activity_evening.png").read_bytes()
    center, score = ImageRecognition(None, asset_root=ROOT).match(png, target)
    assert center is None, (center, score)
