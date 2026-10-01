from types import SimpleNamespace

import pytest

from naruto_daily.battle import run_battle
from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from test_battle import Clock, Client, battle_config


@pytest.mark.parametrize('finished,page', [
    (False, 'unknown'), (True, 'unknown'),
    (True, 'battle'), (False, 'finished'),
])
def test_uncertain_or_conflicting_state_recaptures_past_call_limit_without_attacking(finished, page):
    clock = Clock()
    client = Client(clock)

    class Model:
        config = {'max_calls': 3}
        calls = 0

        def complete_json(self, png, *args, **kwargs):
            self.calls += 1
            assert client.taps == []
            assert png == str(self.calls).encode()
            if self.calls <= 8:
                return {'finished': finished, 'page': page, 'reason': '当前画面无法确认'}
            return {'finished': True, 'page': 'finished', 'reason': '明确的结算页'}

    model = Model()
    with execution_scope(3000, clock=clock, sleep=clock.advance):
        result = run_battle(client, dict(battle_config(), timeout_seconds=.1),
                            template_matcher=lambda *args: (None, .1),
                            sleep=clock.advance, clock=clock, model=model)

    assert result.end_target == 'multimodal' and result.taps == 0
    assert model.calls == 9 and len(client.frames) == 9
    assert client.taps == []


def test_uncertain_state_resumes_full_battle_window_only_after_confirmed_battle():
    clock = Clock()
    client = Client(clock)
    tap_times = []
    tap = client.tap

    def record_tap(x, y):
        tap_times.append(clock())
        tap(x, y)

    client.tap = record_tap

    class Model:
        calls = 0

        def complete_json(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                assert client.taps == []
                return {'finished': False, 'page': 'unknown', 'reason': '特效遮挡'}
            if self.calls == 3:
                assert client.taps == []
                return {'finished': False, 'page': 'battle', 'reason': '战斗中'}
            return {'finished': True, 'page': 'finished', 'reason': '结算页'}

    model = Model()
    result = run_battle(client, battle_config(), model=model,
                        template_matcher=lambda *args: (None, .1),
                        sleep=clock.advance, clock=clock, check_interval_seconds=18)

    assert client.frames == [0, 1, 2, 20]
    assert result.taps == 72 and model.calls == 4
    assert min(tap_times) == 2 and all(2 <= at < 20 for at in tap_times)


@pytest.mark.parametrize('local_detector', [False, True])
def test_unknown_retry_checks_new_frame_for_local_end_before_another_model_call(local_detector):
    clock = Clock()
    client = Client(clock)
    model = SimpleNamespace(complete_json=lambda *args, **kwargs: {
        'finished': False, 'page': 'unknown', 'reason': '无法确认',
    })

    def matcher(png, *args):
        return ((50, 50), .99) if not local_detector and png == b'4' else (None, .1)

    detector = (lambda png: '更换忍者页' if png == b'4' else None) if local_detector else None
    result = run_battle(client, battle_config(), model=model, template_matcher=matcher,
                        sleep=clock.advance, clock=clock, end_detector=detector)

    assert len(client.frames) == 4 and client.taps == []
    assert result.end_target == ('更换忍者页' if local_detector else 'result')


def test_persistently_unknown_state_uses_remaining_shared_deadline_without_another_capture():
    clock = Clock()
    client = Client(clock)
    model = SimpleNamespace(complete_json=lambda *args, **kwargs: {
        'finished': False, 'page': 'unknown', 'reason': '无法确认',
    })

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(3000, clock=clock, sleep=clock.advance):
            # Earlier task phases have already used all but 2.5 seconds.
            clock.advance(2997.5)
            run_battle(client, dict(battle_config(), timeout_seconds=.1), model=model,
                       template_matcher=lambda *args: (None, .1),
                       sleep=clock.advance, clock=clock)

    assert clock() == 3000 and client.taps == []
    assert client.frames == [2997.5, 2998.5, 2999.5]


@pytest.mark.parametrize('page', ['unknown', 'finished', 'battle'])
def test_late_classification_cannot_finish_or_resume_after_shared_deadline(page):
    clock = Clock()
    client = Client(clock)

    def complete(*args, **kwargs):
        clock.advance(3)
        return {'finished': page == 'finished', 'page': page, 'reason': '迟到结果'}

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(2, clock=clock, sleep=clock.advance):
            run_battle(client, battle_config(),
                       model=SimpleNamespace(complete_json=complete),
                       template_matcher=lambda *args: (None, .1),
                       sleep=clock.advance, clock=clock)

    assert client.taps == [] and client.frames == [0]
