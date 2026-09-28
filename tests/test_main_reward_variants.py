from pathlib import Path

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import find_template_in_png


ROOT = Path(__file__).parents[1]


def target():
    return dict(name='奖励', template='assets/main_reward_label.png',
                alternate_templates=['assets/main_reward_label_red_dot.png'],
                left=0, top=0, right=136, bottom=166, threshold=.9)


def test_red_dot_variant_matches_without_lowering_threshold():
    png = (ROOT / 'tests/fixtures/reward_icon_red_dot_variant.png').read_bytes()
    assert find_template_in_png(png, ROOT/'assets/main_reward_label.png', (0, 0, 136, 166), .9)[0] is None
    center, score = ImageRecognition(None, asset_root=ROOT).match(png, target())
    assert center == (72, 129) and score > .99


def test_alternatives_share_one_screenshot_and_return_best_failure_score():
    calls = []
    def matcher(png, path, roi, threshold):
        calls.append((png, path.name))
        return None, .85 if len(calls) == 1 else .87
    result = ImageRecognition(None, matcher).match(b'same-frame', target())
    assert result == (None, .87)
    assert calls == [(b'same-frame', 'main_reward_label.png'),
                     (b'same-frame', 'main_reward_label_red_dot.png')]


@pytest.mark.parametrize('fixture,roi', [
    ('main_page_reward_dot.png', (1700, 400, 1920, 700)),
    ('main_page_activity_icon.png', (1700, 400, 1920, 700)),
])
def test_existing_reward_styles_still_match(fixture, roi):
    config = target()
    config.update(zip(('left', 'top', 'right', 'bottom'), roi))
    assert ImageRecognition(None, asset_root=ROOT).match((ROOT/'tests/fixtures'/fixture).read_bytes(), config)[0] is not None


def test_unrelated_main_page_icons_are_not_reward_marker():
    config = target()
    config.update(right=220, bottom=180)
    assert ImageRecognition(None, asset_root=ROOT).match(
        (ROOT/'tests/fixtures/main_page_top_right.png').read_bytes(), config)[0] is None


def test_return_to_main_accepts_red_dot_variant_without_clicking_close(caplog):
    import logging
    from naruto_daily.config import load_settings
    from naruto_daily.return_to_main import return_to_main
    config = next(a for a in load_settings(ROOT/'config.example.json').tasks['login']
                  if a['type'] == 'return_to_main')
    assert config['main_page_marker']['main_menu'] == 'home'
    config['main_page_marker'] = target()
    png = (ROOT/'tests/fixtures/reward_icon_red_dot_variant.png').read_bytes()
    class Client:
        def screenshot(self):
            return png
        def tap(self, *args):
            raise AssertionError('已在主界面时不能点击关闭')
    with caplog.at_level(logging.INFO, logger='naruto_daily'):
        result = return_to_main(Client(), config, asset_root=ROOT, sleep=lambda _: None)
    assert result.clicks == 0
    assert '阈值 0.900' in caplog.text and '模板 2 种' in caplog.text


@pytest.mark.parametrize('alternatives', ['not-a-list', ['../outside.png'], [42]])
def test_invalid_alternative_template_configuration_is_rejected(tmp_path, alternatives):
    import json
    from naruto_daily.config import load_settings, ConfigError
    data = json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
    config = next(a for a in data['tasks']['login'] if a['type'] == 'return_to_main')
    config['main_page_marker'] = dict(target(), alternate_templates=alternatives)
    path = tmp_path/'config.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ConfigError):
        load_settings(path)
