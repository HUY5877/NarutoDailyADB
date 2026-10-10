import pytest

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.multimodal.client import MultimodalError


TARGET = dict(name="免费招募按钮", left=900, top=700, right=1500, bottom=910)
GUARD = dict(name="普通招募红点", template="red.png", left=170, top=700, right=270, bottom=980, threshold=.95)


def reply(count=1, center=(1115, 865), category="普通招募", text=None):
    return dict(category=category, free_count=count, button_text=text or f"免费{count}抽", center=list(center))


class Client:
    def __init__(self):
        self.frames = []
        self.taps = []

    def screenshot(self):
        png = str(len(self.frames)).encode()
        self.frames.append(png)
        return png

    def tap(self, x, y):
        self.taps.append((x, y))


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete_json(self, png, prompt, **kwargs):
        self.calls.append((png, prompt, kwargs))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return result


def run(replies, *, category="普通招募", guards=None, timeout=20, max_calls=6, clock=lambda: 0):
    from naruto_daily.recruit.free_button import click_free_recruit
    client = Client()
    model = Model(replies)
    sleeps, matched = [], []
    guards = iter(guards) if guards is not None else None

    def matcher(png, path, roi, threshold):
        assert path.name == "red.png", "免费按钮不应再调用模板匹配"
        matched.append(png)
        found = next(guards) if guards is not None else True
        return ((218, 742), 1) if found else (None, .1)

    images = ImageRecognition(client, matcher, sleep=sleeps.append)
    result = click_free_recruit(client, TARGET, category=category, guard=GUARD,
        images=images, model=model, timeout_seconds=timeout, poll_seconds=1,
        pre_click_wait=1, post_click_wait=1, max_calls=max_calls, clock=clock)
    return result, client, model, sleeps, matched


@pytest.mark.parametrize("count", [1, 2, 5, 10])
@pytest.mark.parametrize("category", ["普通招募", "高级招募"])
def test_free_draw_clicks_latest_model_coordinates_after_confirmation(count, category):
    result, client, model, sleeps, matched = run([
        reply(count, category=category), reply(count, center=(1125, 860), category=category),
    ], category=category)
    assert result is True
    assert client.taps == [(1125, 860)]
    assert (1330, 865) not in client.taps  # 旧固定坐标在普通招募中落到收费十连抽。
    assert [call[0] for call in model.calls] == matched == client.frames
    assert sleeps == [1, 1]


@pytest.mark.parametrize("result", [
    reply(count=0), reply(category="高级招募"), reply(text="10:49:14后增加一次免费"),
    reply(text="免费5抽", count=1), reply(text="1抽"), reply(text="10抽"),
])
def test_paid_countdown_wrong_category_or_invalid_button_is_never_clicked(result):
    from naruto_daily.recruit.free_button import parse_free_recruit
    assert parse_free_recruit(result, TARGET, "普通招募") is None


@pytest.mark.parametrize("result", [reply(count=True), reply(count="1"), {**reply(), "button_text": None}, {}])
def test_malformed_model_output_fails_closed(result):
    from naruto_daily.recruit.free_button import parse_free_recruit
    with pytest.raises(MultimodalError):
        parse_free_recruit(result, TARGET, "普通招募")


def test_first_miss_gets_one_second_retry_then_fresh_confirmation():
    result, client, model, sleeps, matched = run([reply(count=0), reply(), reply()])
    assert result and client.taps == [(1115, 865)]
    assert len(model.calls) == 3
    assert sleeps == [1, 1, 1]


def test_red_dot_disappears_before_click_cancels_without_model_reuse():
    result, client, model, sleeps, matched = run([reply()], guards=[True, False])
    assert result is False and client.taps == []
    assert len(model.calls) == 1


def test_no_red_dot_does_not_call_model():
    result, client, model, sleeps, matched = run([], guards=[False])
    assert result is False and not client.taps and not model.calls


def test_model_call_limit_stops_unrecognized_button():
    with pytest.raises(MultimodalError, match="调用上限"):
        run([reply(count=0)] * 2, max_calls=2)


def test_model_network_error_is_not_treated_as_a_match():
    with pytest.raises(MultimodalError, match="网络"):
        run([MultimodalError("网络异常")])


def test_late_model_response_cannot_trigger_click():
    ticks = iter([0, 0, 0, 30])
    with pytest.raises(MultimodalError, match="超时"):
        run([reply(), reply()], clock=lambda: next(ticks))


@pytest.mark.parametrize("center", [[900, 700], [1499, 909], [1115, 865]])
def test_model_coordinates_are_full_screen_pixels_inside_button_region(center):
    from naruto_daily.recruit.free_button import parse_free_recruit
    assert parse_free_recruit(reply(count=2, center=center), TARGET, "普通招募") == (2, tuple(center))


INVALID_CENTERS = [
    None, [], [1115], [1115, 865, 1], (1115, 865),
    "1115,865", {"x": 1115, "y": 865}, [True, 865], [1115, False],
    ["1115", 865], [1115, "865"], [1115.0, 865], [1115, 865.0],
    [899, 865], [1500, 865], [1115, 699], [1115, 910], [1650, 870],
]


@pytest.mark.parametrize("category", ["普通招募", "高级招募"])
@pytest.mark.parametrize("center", INVALID_CENTERS)
@pytest.mark.parametrize("invalid_on_confirmation", [False, True])
def test_invalid_model_center_fails_closed_without_tapping(category, center, invalid_on_confirmation):
    from naruto_daily.recruit.free_button import click_free_recruit
    client = Client()
    images = ImageRecognition(client, lambda *a: ((218, 742), 1), sleep=lambda _: None)
    invalid = {**reply(category=category), "center": center}
    model = Model(([reply(category=category)] if invalid_on_confirmation else []) + [invalid])
    with pytest.raises(MultimodalError):
        click_free_recruit(client, TARGET, category=category, guard=GUARD, images=images,
            model=model, timeout_seconds=20, poll_seconds=1, pre_click_wait=1, post_click_wait=1,
            max_calls=4, clock=lambda: 0)
    assert client.taps == []


@pytest.mark.parametrize("category", ["普通招募", "高级招募"])
def test_free_button_without_model_center_is_rejected(category):
    from naruto_daily.recruit.free_button import parse_free_recruit
    result = reply(category=category)
    del result["center"]
    with pytest.raises(MultimodalError):
        parse_free_recruit(result, TARGET, category)


@pytest.mark.parametrize("result", [
    dict(category="普通招募", free_count=0, button_text=None),
    dict(category="高级招募", free_count=1, button_text="免费"),
    dict(category="普通招募", free_count=10, button_text="10抽"),
    dict(category="普通招募", free_count=1, button_text="4:21:15后增加一次免费"),
])
def test_unavailable_paid_or_wrong_category_needs_no_center(result):
    from naruto_daily.recruit.free_button import parse_free_recruit
    assert parse_free_recruit(result, TARGET, "普通招募") is None


def test_free_draw_disappears_during_confirmation_does_not_click(monkeypatch):
    from naruto_daily.recruit.free_button import click_free_recruit
    client = Client()
    images = ImageRecognition(client, lambda *a: ((218, 742), 1), sleep=lambda _: None)
    model = Model([reply(), reply(count=0), reply(count=0), reply(count=0)])
    with pytest.raises(MultimodalError):
        click_free_recruit(client, TARGET, category="普通招募", guard=GUARD, images=images,
            model=model, timeout_seconds=20, poll_seconds=1, pre_click_wait=1, post_click_wait=1,
            max_calls=4, clock=lambda: 0)
    assert client.taps == []


@pytest.mark.parametrize("change", [
    {"click_x": 1330}, {"click_y": 865}, {"click_x": 1330, "click_y": 865},
    {"click_x": 1650}, {"click_y": 930}, {"click_x": True}, {"right": 900},
])
def test_config_rejects_fixed_free_button_coordinates_or_invalid_region(tmp_path, change):
    import json
    from pathlib import Path
    from naruto_daily.config import load_settings, ConfigError
    root = Path(__file__).parents[1]
    data = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    action = next(a for a in data["tasks"]["recruit"] if a["type"] == "handle_free_recruit")
    action["free_button"].update(change)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_recruit_dry_run_does_not_create_model_or_read_key(monkeypatch):
    from pathlib import Path
    from naruto_daily.config import load_settings
    from naruto_daily.runner import ActionRunner
    from naruto_daily.recruit import handler
    def fail(*args, **kwargs):
        raise AssertionError("dry-run 不应调用模型")
    monkeypatch.setattr(handler, "MultimodalClient", fail)
    root = Path(__file__).parents[1]
    action = next(a for a in load_settings(root / "config.example.json").tasks["recruit"]
                  if a["type"] == "handle_free_recruit")
    client = Client()
    ActionRunner(client, dry_run=True).run([action])
    assert not client.frames and not client.taps


def test_advanced_button_with_only_free_text_counts_as_one_draw():
    from naruto_daily.recruit.free_button import parse_free_recruit
    assert parse_free_recruit(reply(category="高级招募", text="免费"), TARGET, "高级招募") == (1, (1115, 865))
    assert parse_free_recruit(reply(category="高级招募", count=5, text="免费"), TARGET, "高级招募") is None
