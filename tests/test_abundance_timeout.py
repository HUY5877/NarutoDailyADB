"""丰饶战斗只占三分钟，超时后保留日常预算并继续下一项。"""
from copy import deepcopy
from threading import Lock
from types import SimpleNamespace

import pytest

from naruto_daily.abundance import handler
from naruto_daily.battle import run_battle_without_first_skill
from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.execution import (
    DailyDeadlineExceeded, check_execution, current_execution, execution_scope,
)
from naruto_daily.common.visual import TemplateConfigurationError, VisualError
from naruto_daily.multimodal.client import MultimodalRequestError
from naruto_daily.runner import ActionRunner
from test_battle import Clock, battle_config, target


NEXT_POINT = (10, 20)


class Device:
    def __init__(self, clock):
        self.clock = clock
        self.lock = Lock()
        self.finish_at = None
        self.screenshot_seconds = 0
        self.screenshot_error = None
        self.attack_times = []
        self.frames = []
        self.events = []

    def screenshot(self):
        check_execution()
        if self.screenshot_error is not None:
            raise self.screenshot_error
        self.frames.append(self.clock())
        self.clock.advance(self.screenshot_seconds)
        check_execution()
        return b'finished' if self.finish_at is not None and self.clock() >= self.finish_at else b'battle'

    def tap(self, x, y):
        with self.lock:
            check_execution()
            point = (x, y)
            if point == NEXT_POINT:
                self.events.append('next')
            else:
                assert point in {(2, 2), (3, 3), (4, 4)}, '丰饶只能点击二技能、大招和普攻'
                self.attack_times.append(self.clock())
            self.clock.advance(1)
            check_execution()


@pytest.fixture
def scene(monkeypatch):
    clock = Clock()
    device = Device(clock)
    action = dict(
        type='handle_abundance', timeout_seconds=20, load_wait_seconds=10,
        challenge_button=target('challenge'), result_return=target('dismiss'),
        battle=dict(battle_config(), timeout_seconds=120),
    )
    finish_calls = []
    template_error = [None]

    def match(png, path, *args):
        if template_error[0] is not None:
            raise template_error[0]
        if path.stem == 'result' and png == b'finished':
            return (50, 50), .99
        return None, .1

    # 挑战入口另有真实截图回归；这里保留加载等待和整个真实战斗循环。
    monkeypatch.setattr(handler, '_click_confirmed', lambda *a, **kw: True)
    monkeypatch.setattr(handler, 'run_battle_without_first_skill',
                        lambda *a, **kw: run_battle_without_first_skill(*a, **kw, clock=clock))
    monkeypatch.setattr(handler, 'finish_abundance_result',
                        lambda *a, **kw: finish_calls.append(clock()))
    return SimpleNamespace(clock=clock, device=device, action=action, match=match,
                           finish_calls=finish_calls, template_error=template_error)


def run_abundance(scene):
    return handler.handle_abundance(scene.device, scene.action,
                                    template_matcher=scene.match, sleep=scene.clock.advance)


def test_abundance_stops_after_180_battle_seconds_with_legacy_config(scene, caplog):
    original = deepcopy(scene.action)
    with caplog.at_level('WARNING', logger='naruto_daily'):
        with execution_scope(3000, clock=scene.clock, sleep=scene.clock.advance) as daily:
            result = run_abundance(scene)
            assert current_execution() is daily
            assert daily.remaining() == 2810

    assert result is None
    assert scene.clock() == 190  # 十秒加载以后，单独计算三分钟战斗。
    assert scene.device.attack_times and max(scene.device.attack_times) < 190
    assert not scene.finish_calls
    assert scene.action == original
    assert any(record.levelname == 'WARNING' and '丰饶' in record.message for record in caplog.records)


@pytest.mark.parametrize('cost', ['screenshot', 'model', 'model_retry'])
def test_battle_observation_and_retry_time_counts_towards_180_seconds(scene, monkeypatch, cost):
    requests = []
    if cost == 'screenshot':
        scene.device.screenshot_seconds = 180
    else:
        class Model:
            def complete_json(self, png, prompt, *, timeout_seconds, **kwargs):
                requests.append((scene.clock(), timeout_seconds))
                assert not scene.device.attack_times
                # 网络请求本身耗时；重试场景还会经过真实 retry_observation 的等待。
                scene.clock.advance(min(30, timeout_seconds) if cost == 'model_retry' else timeout_seconds)
                if cost == 'model_retry':
                    raise MultimodalRequestError('offline timeout', retryable=True)
                return {'finished': False, 'page': 'battle', 'reason': '战斗中'}

        scene.action['multimodal'] = {'timeout_seconds': 30, 'max_calls': 6}
        monkeypatch.setattr(handler, 'MultimodalClient', lambda *a, **kw: Model())

    with execution_scope(3000, clock=scene.clock, sleep=scene.clock.advance) as daily:
        assert run_abundance(scene) is None
        assert current_execution() is daily and daily.remaining() == 2810

    assert scene.clock() == 190
    assert not scene.device.attack_times and not scene.finish_calls
    if cost != 'screenshot':
        assert requests[0] == (10, 180)
    if cost == 'model_retry':
        assert [at for at, _ in requests] == [10, 50, 90, 130, 170]


def recovery_actions(scene):
    return [scene.action, dict(
        type='return_to_main', max_rounds=5, idle_rounds=2, poll_seconds=1,
        pre_click_wait_seconds=1, post_click_wait_seconds=1,
        main_page_marker=target('home'), close_target=target('close'),
    ), dict(type='tap', x=NEXT_POINT[0], y=NEXT_POINT[1])]


def observe_recovery(monkeypatch, scene):
    from naruto_daily import runner
    budgets = []

    def return_home(*args, **kwargs):
        scene.device.events.append('return')
        budgets.append(current_execution())
        # 回主实现沿用原模块；这里只核对一次调用、预算恢复和下一动作。

    monkeypatch.setattr(runner, 'return_to_main', return_home)
    return budgets


def test_timeout_uses_following_return_once_and_then_runs_next_task(scene, monkeypatch):
    budgets = observe_recovery(monkeypatch, scene)
    original = deepcopy(scene.action)
    with execution_scope(3000, clock=scene.clock, sleep=scene.clock.advance) as daily:
        summary = ActionRunner(scene.device, template_matcher=scene.match,
                               sleep=scene.clock.advance).run(recovery_actions(scene))
        assert current_execution() is daily and daily.deadline == 3000
        assert daily.remaining() > 2800

    assert summary.action_count == 3
    assert budgets == [daily]
    assert scene.device.events == ['return', 'next']
    assert not scene.finish_calls
    assert max(scene.device.attack_times) < 190
    assert scene.action == original


def test_normal_battle_keeps_summary_check_interval_and_result_cleanup(scene):
    scene.device.finish_at = 40
    with execution_scope(3000, clock=scene.clock, sleep=scene.clock.advance) as daily:
        summary = run_abundance(scene)
        assert current_execution() is daily and daily.remaining() == 2960

    assert summary.end_target == 'result'
    assert summary.taps == len(scene.device.attack_times) == 30
    assert scene.device.frames == [10, 40]
    assert scene.finish_calls == [40]


@pytest.mark.parametrize('error_type', [AdbError, TemplateConfigurationError, DailyDeadlineExceeded])
def test_unrelated_battle_errors_propagate_without_recovery(scene, monkeypatch, error_type):
    budgets = observe_recovery(monkeypatch, scene)
    failure = error_type('原始故障')
    if error_type is TemplateConfigurationError:
        scene.template_error[0] = failure
    else:
        scene.device.screenshot_error = failure

    with execution_scope(3000, clock=scene.clock, sleep=scene.clock.advance) as daily:
        with pytest.raises(error_type) as caught:
            ActionRunner(scene.device, template_matcher=scene.match,
                         sleep=scene.clock.advance).run(recovery_actions(scene))
        assert current_execution() is daily

    assert caught.value is failure
    assert not budgets and not scene.device.events and not scene.finish_calls


def test_result_visual_error_is_not_treated_as_battle_timeout(scene, monkeypatch):
    scene.device.finish_at = 10
    failure = VisualError('战后结算页面未确认')
    budgets = observe_recovery(monkeypatch, scene)

    def fail_result(*args, **kwargs):
        raise failure

    monkeypatch.setattr(handler, 'finish_abundance_result', fail_result)
    with execution_scope(3000, clock=scene.clock, sleep=scene.clock.advance):
        with pytest.raises(VisualError) as caught:
            ActionRunner(scene.device, template_matcher=scene.match,
                         sleep=scene.clock.advance).run(recovery_actions(scene))

    assert caught.value is failure
    assert not budgets and not scene.device.events and not scene.device.attack_times


@pytest.mark.parametrize('deadline', [100, 190])
def test_daily_deadline_wins_before_or_at_battle_limit(scene, monkeypatch, deadline):
    budgets = observe_recovery(monkeypatch, scene)
    with pytest.raises(DailyDeadlineExceeded) as caught:
        with execution_scope(deadline, clock=scene.clock, sleep=scene.clock.advance):
            ActionRunner(scene.device, template_matcher=scene.match,
                         sleep=scene.clock.advance).run(recovery_actions(scene))

    assert type(caught.value) is DailyDeadlineExceeded
    assert current_execution() is None
    assert scene.clock() == deadline
    assert not budgets and not scene.device.events and not scene.finish_calls
    assert max(scene.device.attack_times) < deadline
