import json
from pathlib import Path

import pytest

from naruto_daily.common.visual import VisualError
from naruto_daily.config import ConfigError, load_settings
from naruto_daily.return_to_main import return_to_main


def template_target(name, filename, roi):
    return dict(
        name=name, template=filename,
        **dict(zip(("left", "top", "right", "bottom"), roi)), threshold=0.95,
    )


def return_action(*, with_popup=True):
    action = {
        "type": "return_to_main", "max_rounds": 5, "idle_rounds": 2,
        "poll_seconds": 0.5,
        "pre_click_wait_seconds": 3, "post_click_wait_seconds": 2,
        "main_page_marker": template_target(
            "主界面奖励入口", "main.png", (1700, 400, 1920, 700),
        ),
        "close_target": template_target(
            "通用关闭按钮", "close.png", (1650, 0, 1920, 300),
        ),
    }
    if with_popup:
        action["dismiss_popup"] = {
            "marker": template_target(
                "组织争霸赛", "tournament.png", (700, 250, 1700, 650),
            ),
            "x": 1220, "y": 975,
        }
    return action


class PopupClient:
    def __init__(self, state="popup"):
        self.state = state
        self.screenshot_count = 0
        self.taps = []
        self.matches = []

    def screenshot(self):
        self.screenshot_count += 1
        return self.state.encode()

    def tap(self, x, y):
        self.taps.append((x, y))
        self.state = "popup" if self.state == "close_then_popup" else "main"

    def match(self, png, path, roi, threshold):
        state = png.decode()
        self.matches.append((state, path.name))
        if path.name == "main.png" and state == "main":
            return (1800, 500), 0.99
        if path.name == "close.png" and state in {"close", "close_then_popup"}:
            return (1830, 70), 0.99
        if path.name == "tournament.png" and state in {"popup", "close_then_popup"}:
            return (1000, 400), 0.99
        return None, 0.10


def run_return(client, action=None, *, sleep=lambda _: None):
    return return_to_main(
        client, return_action() if action is None else action,
        template_matcher=client.match, sleep=sleep,
    )


def test_tournament_popup_uses_configured_blank_coordinate_then_confirms_main():
    client = PopupClient()
    waits = []

    summary = run_return(client, sleep=waits.append)

    assert client.taps == [(1220, 975)]
    assert summary.clicks == 1
    assert summary.rounds == 2
    assert sum(name == "tournament.png" for _, name in client.matches) == 2
    assert 3.0 in waits and 2.0 in waits
    assert client.matches[-1] == ("main", "main.png")


def test_common_close_is_processed_before_tournament_popup():
    client = PopupClient("close_then_popup")

    summary = run_return(client)

    assert client.taps == [(1830, 70), (1220, 975)]
    assert summary.clicks == 2
    assert summary.rounds == 3
    assert ("close_then_popup", "tournament.png") not in client.matches


def test_missing_tournament_popup_keeps_original_idle_failure_without_clicks():
    client = PopupClient("unknown")
    action = return_action()
    action["idle_rounds"] = 1

    with pytest.raises(VisualError, match="未能确认已经返回主界面"):
        run_return(client, action)

    assert client.taps == []
    assert ("unknown", "tournament.png") in client.matches


def test_tournament_disappearing_during_confirmation_cancels_blank_click():
    client = PopupClient()

    def popup_disappears(seconds):
        if seconds == 3:
            client.state = "main"

    summary = run_return(client, sleep=popup_disappears)

    assert client.taps == []
    assert summary.clicks == 0
    assert summary.rounds == 2
    assert ("main", "tournament.png") in client.matches


def test_dismissing_tournament_resets_idle_rounds_before_waiting_for_main():
    class LoadingClient(PopupClient):
        def tap(self, x, y):
            super().tap(x, y)
            self.state = "loading_after_popup"

    client = LoadingClient("loading_before_popup")
    polls = []

    def finish_loading(seconds):
        if seconds == 0.5:
            polls.append(seconds)
            client.state = "popup" if len(polls) == 1 else "main"

    summary = run_return(client, sleep=finish_loading)

    assert client.taps == [(1220, 975)]
    assert summary.clicks == 1
    assert summary.rounds == 4
    assert polls == [0.5, 0.5]


def test_visible_main_page_does_not_search_or_click_tournament_popup():
    client = PopupClient("main")

    summary = run_return(client)

    assert client.taps == []
    assert summary.rounds == 1
    assert client.matches == [("main", "main.png")]


def test_return_without_optional_popup_keeps_existing_close_behavior():
    client = PopupClient("close")

    summary = run_return(client, return_action(with_popup=False))

    assert client.taps == [(1830, 70)]
    assert summary.clicks == 1
    assert summary.rounds == 2
    assert all(name != "tournament.png" for _, name in client.matches)


def test_return_without_optional_popup_keeps_existing_idle_failure():
    client = PopupClient("unknown")
    action = return_action(with_popup=False)
    action["idle_rounds"] = 1

    with pytest.raises(VisualError, match="连续 1 轮既未发现主界面标记，也未发现关闭按钮"):
        run_return(client, action)

    assert client.taps == []
    assert all(name != "tournament.png" for _, name in client.matches)


def write_settings(tmp_path, action):
    payload = {
        "adb_path": "adb.exe", "serial": None,
        "screen": {"width": 1920, "height": 1080},
        "tasks": {
            "login": [action], "organization_pray": [], "sign_in": [],
            "stamina": [], "recruit": [], "abundance": [],
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("with_popup", [False, True])
def test_return_configuration_accepts_optional_tournament_popup(tmp_path, with_popup):
    action = return_action(with_popup=with_popup)

    settings = load_settings(write_settings(tmp_path, action))

    assert settings.tasks["login"][0] == action


@pytest.mark.parametrize("key,value", [
    ("x", -1), ("x", 1920), ("x", True), ("x", 1.5),
    ("y", -1), ("y", 1080), ("y", False), ("y", "975"),
])
def test_tournament_popup_configuration_rejects_invalid_click_coordinates(tmp_path, key, value):
    action = return_action()
    action["dismiss_popup"][key] = value

    with pytest.raises(ConfigError, match="坐标"):
        load_settings(write_settings(tmp_path, action))


@pytest.mark.parametrize("key", ["marker", "x", "y"])
def test_tournament_popup_configuration_requires_marker_and_both_coordinates(tmp_path, key):
    action = return_action()
    del action["dismiss_popup"][key]

    with pytest.raises(ConfigError, match="缺少字段"):
        load_settings(write_settings(tmp_path, action))


@pytest.mark.parametrize("change,error", [
    (lambda popup: popup.update(marker=[]), "模板目标对象"),
    (lambda popup: popup.update(extra=True), "未知字段"),
    (lambda popup: popup["marker"].update(extra=True), "未知字段"),
    (lambda popup: popup["marker"].pop("template"), "缺少字段"),
    (lambda popup: popup["marker"].update(name=""), "name"),
    (lambda popup: popup["marker"].update(template="../private.png"), "相对路径"),
    (lambda popup: popup["marker"].update(threshold=0), "threshold"),
    (lambda popup: popup["marker"].update(left=-1), "坐标"),
    (lambda popup: popup["marker"].update(right=700), "right"),
    (lambda popup: popup["marker"].update(bottom=1081), "bottom"),
])
def test_tournament_popup_configuration_validates_marker_and_fields(tmp_path, change, error):
    action = return_action()
    change(action["dismiss_popup"])

    with pytest.raises(ConfigError, match=error):
        load_settings(write_settings(tmp_path, action))


@pytest.mark.parametrize("value", [None, [], "tournament"])
def test_tournament_popup_configuration_requires_an_object(tmp_path, value):
    action = return_action()
    action["dismiss_popup"] = value

    with pytest.raises(ConfigError):
        load_settings(write_settings(tmp_path, action))


def test_example_enables_tournament_dismissal_only_for_login():
    root = Path(__file__).parents[1]
    settings = load_settings(root / "config.example.json")
    configured = [
        (task, action)
        for task, actions in settings.tasks.items()
        for action in actions
        if "dismiss_popup" in action
    ]

    assert len(configured) == 1
    task, action = configured[0]
    assert task == "login" and action["type"] == "return_to_main"
    assert (action["dismiss_popup"]["x"], action["dismiss_popup"]["y"]) == (1220, 975)
