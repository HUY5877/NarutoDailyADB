from pathlib import Path

import cv2
import numpy as np
import pytest

from naruto_daily.secret_realm.ninja_selection import find_ninja_selection_return


FIXTURES = Path(__file__).parent / 'fixtures'


def encode(image):
    success, png = cv2.imencode('.png', image)
    assert success
    return png.tobytes()


def selected_image():
    return cv2.imdecode(np.frombuffer(
        (FIXTURES / 'secret_ninja_selected.png').read_bytes(), np.uint8), cv2.IMREAD_COLOR)


@pytest.mark.parametrize('filename', ['secret_ninja_selected.png', 'secret_ninja_loading.png'])
def test_real_selection_pages_return_button_in_original_coordinates(filename):
    png = (FIXTURES / filename).read_bytes()
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    height, width = image.shape[:2]
    point = find_ninja_selection_return(png)
    assert point is not None
    x, y = point
    assert .07 * width < x < .12 * width
    assert .91 * height < y < .97 * height


@pytest.mark.parametrize('size', [(1920, 1080), (1280, 720), (960, 540)])
def test_selection_matches_at_adb_and_reduced_resolutions(size):
    image = cv2.resize(selected_image(), size, interpolation=cv2.INTER_LINEAR)
    point = find_ninja_selection_return(encode(image))
    assert point is not None
    assert abs(point[0] / size[0] - 54 / 622) < .005
    assert abs(point[1] / size[1] - 329 / 350) < .005


def test_outfit_icon_can_identify_selection_without_info_label():
    image = selected_image()
    image[40:70, 375:482] = 0
    assert find_ninja_selection_return(encode(image)) is not None


@pytest.mark.parametrize('missing_region', ['page_markers', 'confirm', 'return'])
def test_incomplete_selection_evidence_does_not_authorize_click(missing_region):
    image = selected_image()
    if missing_region == 'page_markers':
        image[180:260, 70:165] = 0
        image[40:70, 375:482] = 0
    elif missing_region == 'confirm':
        image[267:310, 430:550] = 0
    else:
        image[309:350, 0:115] = 0
    assert find_ninja_selection_return(encode(image)) is None


@pytest.mark.parametrize('path', sorted(path for path in FIXTURES.glob('*.png')
    if path.name not in {'secret_ninja_selected.png', 'secret_ninja_loading.png'}),
    ids=lambda path: path.stem)
def test_room_settlement_battle_and_other_pages_never_match_selection(path):
    assert find_ninja_selection_return(path.read_bytes()) is None


@pytest.mark.parametrize('png', [None, '', b'', b'not an image', set()])
def test_invalid_screenshot_does_not_authorize_click(png):
    assert find_ninja_selection_return(png) is None


def test_portrait_or_partial_capture_does_not_authorize_click():
    image = selected_image()
    assert find_ninja_selection_return(encode(image[:, :350])) is None
    assert find_ninja_selection_return(encode(cv2.resize(image, (311, 175)))) is None
