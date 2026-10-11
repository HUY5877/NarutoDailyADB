import json
import logging
from pathlib import Path

import pytest

from naruto_daily.common.navigation import navigate_to_card
from naruto_daily.common.visual import VisualError


pytestmark = pytest.mark.usefixtures('reward_swipe_vision')


class NavigationClient:
    def __init__(self):
        self.taps = []
        self.swipes = []

    def screenshot(self):
        return b"screen"

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, x1, y1, x2, y2, duration_ms):
        self.swipes.append((x1, y1, x2, y2, duration_ms))


def card_action():
    return {
        "type": "find_card_and_click",
        "name": "组织祈福",
        "template": "organization_pray_label.png",
        "left": 0, "top": 300, "right": 1920, "bottom": 480,
        "threshold": 0.85,
        "button_template": "go_now_button.png",
        "button_threshold": 0.85,
        "completed_template": "completed_stamp.png",
        "completed_threshold": 0.90,
        "button_left_offset": -140, "button_top_offset": 80,
        "button_right_offset": 140, "button_bottom_offset": 260,
        "max_swipes": 6,
        "swipe_x1": 1650, "swipe_y1": 550,
        "swipe_x2": 350, "swipe_y2": 550,
        "swipe_duration_ms": 500,
        "wait_seconds": 1,
        "pre_click_wait_seconds": 3,
        "post_click_wait_seconds": 3,
    }


def reward_card_actions():
    payload = json.loads((Path(__file__).parents[1] / 'config.example.json').read_text(encoding='utf-8'))

    def walk(value):
        if isinstance(value, dict):
            if value.get('type') == 'find_card_and_click':
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    return list(walk(payload['tasks']))


@pytest.mark.parametrize('action', reward_card_actions(), ids=lambda a: a['name'])
@pytest.mark.parametrize('alignment', [False, True])
def test_every_reward_task_uses_same_swipe_for_search_and_alignment(action, alignment):
    assert not {'swipe_x1', 'swipe_x2', 'swipe_y1', 'swipe_y2',
                'swipe_duration_ms', 'wait_seconds', 'max_swipes'} & action.keys()
    client, waits = NavigationClient(), []

    def matcher(png, path, roi, threshold):
        if not client.swipes:
            if alignment and path.name == Path(action['template']).name:
                return (1823, 400), .99
            return None, .1
        if path.name == Path(action['template']).name:
            return (562, 400), .99
        if path.name == Path(action['completed_template']).name:
            return (562, 580), .99
        raise AssertionError('已完成任务不应查询前往按钮')

    summary = navigate_to_card(client, action, template_matcher=matcher, sleep=waits.append)
    assert client.swipes == [(1450, 550, 1000, 550, 600)]
    assert waits == [1, 2]  # 未命中补查1秒，共用滑后等待2秒。
    assert summary.completed and summary.swipes == 1
    assert client.taps == []


def test_reward_search_default_limit_is_shared_by_all_tasks():
    assert len(reward_card_actions()) == 6
    for action in reward_card_actions():
        client = NavigationClient()
        summary = navigate_to_card(client, action, template_matcher=lambda *args: (None, .1),
                                   sleep=lambda _: None)
        assert summary.completed and summary.swipes == 10
        assert client.swipes == [(1450, 550, 1000, 550, 600)] * 10


def test_default_limit_allows_ten_successes_after_failed_attempts(monkeypatch):
    from naruto_daily.common import navigation
    client = NavigationClient()
    monkeypatch.setattr(navigation, 'reward_card_swipe_starts',
                        lambda png: [(1260, 550), (1640, 550), (880, 550)])
    monkeypatch.setattr(navigation, 'reward_card_displacement',
                        lambda before, after: 0.0 if len(client.swipes) in (1, 4) else -520.0)
    summary = navigate_to_card(client, reward_card_actions()[0],
                               template_matcher=lambda *args: (None, .1), sleep=lambda _: None)
    assert summary.swipes == 10 and len(client.swipes) == 12
    assert client.swipes[0][:2] != client.swipes[1][:2]
    assert client.swipes[3][:2] != client.swipes[4][:2]


def test_failed_alignment_keeps_its_intent_when_the_label_temporarily_disappears(monkeypatch):
    from naruto_daily.common import navigation
    client = NavigationClient()
    monkeypatch.setattr(navigation, 'reward_card_swipe_starts',
                        lambda png: [(1260, 550), (1640, 550)])
    monkeypatch.setattr(navigation, 'reward_card_displacement',
                        lambda before, after: 0.0 if len(client.swipes) == 1 else -520.0)

    def matcher(png, path, *args):
        if len(client.swipes) == 1:
            return None, .1
        if path.name == 'organization_pray_label.png':
            return (1820, 492), .99
        if len(client.swipes) >= 3 and path.name == 'completed_stamp.png':
            return (1820, 650), .99
        return None, .1

    result = navigate_to_card(client, dict(card_action(), completed_skip_type='handle_organization_prayer'),
                              template_matcher=matcher, sleep=lambda _: None)
    assert len(client.swipes) == 2 and result.swipes == 1
    assert not client.taps


def test_pre_click_frame_can_confirm_the_pending_successful_swipe(monkeypatch):
    from naruto_daily.common import navigation
    client = NavigationClient()
    movements = iter([None, -520.0])
    monkeypatch.setattr(navigation, 'reward_card_displacement', lambda *args: next(movements))

    def matcher(png, path, *args):
        if not client.swipes:
            return None, .1
        if path.name == 'organization_pray_label.png':
            return (562, 394), .99
        if path.name == 'go_now_button.png':
            return (562, 580), .99
        return None, .1

    result = navigate_to_card(client, card_action(), template_matcher=matcher, sleep=lambda _: None)
    assert result.swipes == 1 and len(client.swipes) == 1
    assert client.taps == [(562, 580)]


def test_card_visible_now_clicks_corresponding_button_without_swiping(caplog):
    client = NavigationClient()
    results = iter([
        ((562, 394), 0.99),
        (None, 0.3),
        ((548, 585), 0.98),
        ((562, 394), 0.99),
        (None, 0.3),
        ((549, 586), 0.99),
    ])
    regions = []
    waits = []

    def matcher(png, path, roi, threshold):
        regions.append(roi)
        return next(results)

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = navigate_to_card(
            client,
            card_action(),
            template_matcher=matcher,
            asset_root=Path("assets"),
            sleep=waits.append,
        )

    assert client.taps == [(549, 586)]
    assert client.swipes == []
    assert regions[1] == (422, 474, 702, 654)
    assert regions[2] == (422, 474, 702, 654)
    assert regions[4] == (422, 474, 702, 654)
    assert regions[5] == (422, 474, 702, 654)
    assert waits == [3.0, 3.0]
    assert summary.swipes == 0
    assert "发现组织祈福" in caplog.text
    assert "发现对应的立刻前往" in caplog.text


def test_card_after_two_pages_swipes_then_clicks():
    client = NavigationClient()
    results = iter([
        (None, 0.3),
        (None, 0.3),
        (None, 0.3),
        (None, 0.3),
        ((562, 394), 0.99),
        (None, 0.3),
        ((560, 580), 0.96),
        ((562, 394), 0.99),
        (None, 0.3),
        ((561, 581), 0.98),
    ])
    waits = []

    summary = navigate_to_card(
        client,
        card_action(),
        template_matcher=lambda *args: next(results),
        sleep=waits.append,
    )

    assert client.swipes == [(1450, 550, 1000, 550, 600)] * 2
    assert waits == [1.0, 2.0, 1.0, 2.0, 3.0, 3.0]
    assert client.taps == [(561, 581)]
    assert summary.swipes == 2


def test_card_missing_after_max_swipes_raises_without_clicking():
    client = NavigationClient()

    with pytest.raises(VisualError, match="滑动 6 次后仍未找到组织祈福"):
        navigate_to_card(
            client,
            card_action(),
            template_matcher=lambda *args: (None, 0.20),
            sleep=lambda _: None,
        )

    assert client.taps == []
    assert len(client.swipes) == 6


def test_card_label_without_corresponding_button_raises_without_clicking():
    client = NavigationClient()
    results = iter([
        ((1823, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((1823, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((562, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((562, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
    ])

    with pytest.raises(VisualError, match="找到组织祈福，但未找到它下方的立刻前往"):
        navigate_to_card(
            client,
            card_action(),
            template_matcher=lambda *args: next(results),
            sleep=lambda _: None,
        )

    assert client.taps == []
    assert client.swipes == [(1450, 550, 1000, 550, 600)]


def test_optional_card_partially_visible_gets_only_one_extra_swipe_then_detects_completed(caplog):
    client = NavigationClient()
    action = card_action()
    action["completed_skip_type"] = "handle_organization_prayer"
    results = iter([
        ((1823, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((1823, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((562, 394), 0.99),
        ((562, 580), 0.98),
    ])

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = navigate_to_card(
            client,
            action,
            template_matcher=lambda *args: next(results),
            sleep=lambda _: None,
        )

    assert client.taps == []
    assert client.swipes == [(1450, 550, 1000, 550, 600)]
    assert summary.swipes == 1
    assert summary.completed is True
    assert "额外左滑一次" in caplog.text
    assert "今日组织祈福已完成" in caplog.text


def test_optional_card_still_unusable_after_one_extra_swipe_is_skipped(caplog):
    client = NavigationClient()
    action = card_action()
    action["completed_skip_type"] = "handle_organization_prayer"
    results = iter([
        ((1823, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((1823, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((562, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((562, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
    ])

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = navigate_to_card(
            client,
            action,
            template_matcher=lambda *args: next(results),
            sleep=lambda _: None,
        )

    assert client.taps == []
    assert client.swipes == [(1450, 550, 1000, 550, 600)]
    assert summary.completed is True
    assert "额外左滑后仍未找到可执行按钮" in caplog.text


def test_optional_card_missing_after_max_swipes_is_skipped(caplog):
    client = NavigationClient()
    action = card_action()
    action["completed_skip_type"] = "handle_organization_prayer"

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = navigate_to_card(
            client,
            action,
            template_matcher=lambda *args: (None, 0.20),
            sleep=lambda _: None,
        )

    assert client.taps == []
    assert len(client.swipes) == 6
    assert summary.completed is True
    assert "仍未找到组织祈福，跳过这个可选任务" in caplog.text


def test_card_button_disappearing_during_pre_wait_does_not_click_old_coordinate():
    client = NavigationClient()
    results = iter([
        ((562, 394), 0.99),
        (None, 0.3),
        ((548, 585), 0.98),
        ((562, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
        ((562, 394), 0.99),
        (None, 0.3),
        (None, 0.3),
    ])

    with pytest.raises(VisualError, match="点击前复查及补查均未确认"):
        navigate_to_card(
            client,
            card_action(),
            template_matcher=lambda *args: next(results),
            sleep=lambda _: None,
        )

    assert client.taps == []


def test_completed_card_after_two_pages_stops_without_clicking(caplog):
    client = NavigationClient()
    results = iter([
        (None, 0.3),
        (None, 0.3),
        (None, 0.3),
        (None, 0.3),
        ((1185, 424), 0.99),
        ((1185, 590), 0.98),
    ])

    with caplog.at_level(logging.INFO, logger="naruto_daily"):
        summary = navigate_to_card(
            client,
            card_action(),
            template_matcher=lambda *args: next(results),
            sleep=lambda _: None,
        )

    assert client.swipes == [(1450, 550, 1000, 550, 600)] * 2
    assert client.taps == []
    assert summary.swipes == 2
    assert summary.completed is True
    assert "今日组织祈福已完成" in caplog.text
