import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.recruit import handler

from naruto_daily.runner import ActionRunner


class RecordingClient:
    def __init__(self):
        self.taps = []
        self.swipes = []
        self.screenshot_count = 0

    def screenshot(self):
        self.screenshot_count += 1
        return f"招募画面{self.screenshot_count}".encode()

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, *args):
        self.swipes.append(args)


def recruit_action():
    return {
        "type": "handle_free_recruit",
        "multimodal": {"base_url": "${MULTIMODAL_BASE_URL}", "model": "${MULTIMODAL_MODEL}",
                       "timeout_seconds": 20, "max_calls": 6},
        "timeout_seconds": 40,
        "poll_seconds": 1,
        "advanced_tab": {
            "name": "高级招募标签", "template": "advanced_tab.png",
            "left": 0, "top": 150, "right": 300, "bottom": 350, "threshold": 0.90,
        },
        "advanced_red_dot": {
            "name": "高级招募红点", "template": "recruit_red_dot.png",
            "left": 170, "top": 180, "right": 270, "bottom": 300, "threshold": 0.95,
        },
        "ordinary_tab": {
            "name": "普通招募标签", "template": "ordinary_tab.png",
            "left": 0, "top": 780, "right": 300, "bottom": 1030, "threshold": 0.90,
        },
        "ordinary_red_dot": {
            "name": "普通招募红点", "template": "recruit_red_dot.png",
            "left": 170, "top": 800, "right": 270, "bottom": 980, "threshold": 0.95,
        },
        "free_button": {
            "name": "免费招募按钮",
            "left": 900, "top": 800, "right": 1500, "bottom": 910, "click_x": 1330, "click_y": 865,
        },
        "result_confirm": {
            "name": "招募结果确定按钮", "template": "recruit_confirm.png",
            "left": 350, "top": 700, "right": 800, "bottom": 1000, "threshold": 0.90,
        },
    }


def test_recruit_processes_advanced_then_ordinary_free_draw(caplog, monkeypatch):
    replies = iter([
        dict(category=category, free_count=1, button_text="免费1抽", center=list(center))
        for category, center in [("高级招募", (1280, 880)), ("高级招募", (1281, 881)),
                                 ("普通招募", (1200, 850)), ("普通招募", (1201, 851))]
    ])
    monkeypatch.setattr(handler, "MultimodalClient", lambda *a, **kw:
        SimpleNamespace(complete_json=lambda *a, **kw: next(replies)))
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99), ((121, 251), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((600, 870), 0.99), ((601, 871), 0.99),
        ((120, 900), 0.99),
        ((218, 880), 0.99),
        ((218, 880), 0.99),
        ((120, 900), 0.99), ((121, 901), 0.99),
        ((218, 880), 0.99),
        ((218, 880), 0.99),
        ((218, 880), 0.99),
        ((218, 880), 0.99),
        ((600, 870), 0.99), ((601, 871), 0.99),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == [
        (121, 251),
        (1330, 865),
        (601, 871),
        (121, 901),
        (1330, 865),
        (601, 871),
    ]
    assert waits == [1.0] * 14
    assert "高级招募免费次数处理完成" in caplog.text
    assert "普通招募免费次数处理完成" in caplog.text


def test_recruit_skips_each_category_without_red_dot(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        (None, 0.30), (None, 0.30),
        ((120, 900), 0.99),
        (None, 0.25), (None, 0.25),
    ])

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=lambda _: None,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == []
    assert "未识别到高级招募红点，跳过本项" in caplog.text
    assert "未识别到普通招募红点，跳过本项" in caplog.text
    assert "相似度 0.250，阈值 0.950，搜索区域 (170, 830)-(270, 890)" in caplog.text
    assert client.swipes == []


def test_recruit_skips_category_when_red_dot_disappears_during_wait(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        (None, 0.40), (None, 0.40),
        ((120, 900), 0.99),
        (None, 0.25), (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == []
    assert waits == [1.0] * 3
    assert "等待后高级招募红点已消失，跳过高级招募" in caplog.text


def test_recruit_does_not_click_free_button_when_red_dot_disappears_after_tab(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99), ((121, 251), 0.99),
        ((218, 230), 0.99),
        (None, 0.40), (None, 0.40),
        ((120, 900), 0.99),
        (None, 0.25), (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == [(121, 251)]
    assert waits == [1.0] * 5
    assert "点击高级招募标签后红点已消失，不点击免费按钮" in caplog.text


def test_recruit_does_not_click_tab_when_red_dot_disappears_during_tab_wait(caplog):
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99),
        ((121, 251), 0.99),
        (None, 0.40), ((121, 251), 0.99), (None, 0.40),
        ((120, 900), 0.99),
        (None, 0.25), (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == []
    assert waits == [1.0] * 4
    assert "点击高级招募标签前高级招募红点已消失，取消点击" in caplog.text


def test_recruit_does_not_click_free_button_when_red_dot_disappears_during_button_wait(caplog, monkeypatch):
    monkeypatch.setattr(handler, "MultimodalClient", lambda *a, **kw:
        SimpleNamespace(complete_json=lambda *a, **kw:
            dict(category="高级招募", free_count=1, button_text="免费1抽", center=[1280, 880])))
    client = RecordingClient()
    results = iter([
        ((120, 250), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((120, 250), 0.99),
        ((121, 251), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        ((218, 230), 0.99),
        (None, 0.40),
        ((120, 900), 0.99),
        (None, 0.25), (None, 0.25),
    ])
    waits = []

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        ActionRunner(
            client,
            sleep=waits.append,
            template_matcher=lambda *args: next(results),
            asset_root=Path("assets"),
        ).run([recruit_action()])

    assert client.taps == [(121, 251)]
    assert waits == [1.0] * 5
    assert "点击免费招募按钮前高级招募红点未确认，取消点击" in caplog.text


def test_ordinary_search_swipes_up_in_small_steps_and_checks_after_each_step():
    client, waits, checked_at_swipes = RecordingClient(), [], []

    def matcher(png, path, roi, threshold):
        checked_at_swipes.append(len(client.swipes))
        if len(client.swipes) == 3:
            # 已滚到旧的固定识别区域上方，仍需识别到标签。
            assert roi[1] <= 620
            return (128, 620), .99
        return None, .2

    images = ImageRecognition(client, matcher, sleep=waits.append)
    tab, guard = handler._find_ordinary_targets(images, recruit_action())

    assert client.swipes == [(120, 900, 120, 780, 500)] * 3
    assert checked_at_swipes == [0, 0, 1, 1, 2, 2, 3]
    assert waits == [1.0] * 6
    assert tab['top'] < 620 < tab['bottom']
    assert guard['top'] <= 582 < guard['bottom']
    assert guard['bottom'] < 650  # 不再读取旧区域内其他活动的红点。
    assert client.taps == []


def test_ordinary_search_retry_can_find_label_without_swiping():
    client = RecordingClient()
    replies = iter([(None, .2), ((128, 909), .99)])
    images = ImageRecognition(client, lambda *args: next(replies), sleep=lambda _: None)

    handler._find_ordinary_targets(images, recruit_action())

    assert client.swipes == []
    assert client.screenshot_count == 2


def test_ordinary_search_stops_at_swipe_limit_without_clicking():
    client = RecordingClient()
    images = ImageRecognition(client, lambda *args: (None, .2), sleep=lambda _: None)
    with pytest.raises(VisualError, match='小幅滑动8次仍未找到'):
        handler._find_ordinary_targets(images, recruit_action())
    assert len(client.swipes) == 8
    assert client.taps == []


def test_ordinary_search_does_not_swipe_after_deadline():
    client = RecordingClient()
    action = dict(recruit_action(), timeout_seconds=1)
    images = ImageRecognition(client, lambda *args: (None, .2), sleep=lambda _: None)
    with pytest.raises(VisualError, match='超时|剩余时间不足'):
        handler._find_ordinary_targets(images, action)
    assert client.swipes == []
    assert client.taps == []


def test_ordinary_search_ignores_label_found_after_real_time_deadline(monkeypatch):
    client, now = RecordingClient(), [0.0]
    monkeypatch.setattr(handler.time, 'monotonic', lambda: now[0])

    def matcher(*args):
        now[0] += 41
        return (128, 909), .99

    images = ImageRecognition(client, matcher, sleep=lambda _: None)
    with pytest.raises(VisualError, match='超时'):
        handler._find_ordinary_targets(images, recruit_action())
    assert client.swipes == []
    assert client.taps == []


@pytest.mark.parametrize('fixture, expected_dot', [
    ('advanced_recruit_page.png', (219, 871)),
    ('recruit_sidebar_shifted.png', (218, 742)),
])
def test_ordinary_search_reuses_templates_and_anchors_red_dot_to_real_label(fixture, expected_dot):
    from naruto_daily.config import load_settings

    root = Path(__file__).parents[1]
    action = next(a for a in load_settings(root / 'config.example.json').tasks['recruit']
                  if a['type'] == 'handle_free_recruit')
    png = (root / 'tests' / 'fixtures' / fixture).read_bytes()
    client = RecordingClient()
    client.screenshot = lambda: png
    images = ImageRecognition(client, find_template_in_png, root, sleep=lambda _: None)

    tab, guard = handler._find_ordinary_targets(images, action)

    assert images.match(png, tab)[0] is not None
    assert images.match(png, guard)[0] == expected_dot
    assert client.swipes == []


@pytest.mark.parametrize('has_ordinary_dot', [False, True])
def test_hidden_ordinary_tab_is_scrolled_into_view_before_checking_its_red_dot(monkeypatch, has_ordinary_dot):
    client, calls = RecordingClient(), []

    def matcher(png, path, roi, threshold):
        if path.name == 'advanced_tab.png':
            return (120, 250), .99
        if path.name == 'ordinary_tab.png':
            return ((128, 680), .99) if len(client.swipes) == 3 else (None, .2)
        if path.name == 'recruit_confirm.png':
            return (600, 870), .99
        if path.name == 'recruit_red_dot.png':
            # 其他活动的红点一直存在，不能拿它作为普通招募的免费标记。
            dots = [(218, 850)]
            if has_ordinary_dot and len(client.swipes) == 3:
                dots.append((218, 642))
            for x, y in dots:
                if roi[0] <= x < roi[2] and roi[1] <= y < roi[3]:
                    return (x, y), .99
            return None, .2
        raise AssertionError(path)

    def complete_json(*args, **kwargs):
        calls.append(1)
        return dict(category='普通招募', free_count=1, button_text='免费1抽')

    monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw:
                        SimpleNamespace(complete_json=complete_json))
    result = handler.handle_free_recruit(client, recruit_action(), template_matcher=matcher,
                                         sleep=lambda _: None)

    assert len(client.swipes) == 3
    assert result == handler.RecruitSummary(0, int(has_ordinary_dot))
    assert client.taps == ([(128, 680), (1330, 865), (600, 870)] if has_ordinary_dot else [])
    assert len(calls) == (2 if has_ordinary_dot else 0)
