"""Module observations share the deadline without replaying confirmed inputs."""
from contextlib import nullcontext
import re

import pytest

from naruto_daily.common.execution import DailyDeadlineExceeded, execution_scope
from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.visual import VisualError
from test_execution import Clock


class Device:
    def __init__(self):
        self.frames = []
        self.taps = []

    def screenshot(self):
        frame = str(len(self.frames) + 1).encode()
        self.frames.append(frame)
        return frame

    def tap(self, *point):
        self.taps.append(point)


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.frames = []

    def complete_json(self, frame, prompt, **kwargs):
        self.frames.append(frame)
        return next(self.replies)


def target(name):
    return dict(name=name, template=name + '.png', left=0, top=0,
                right=1920, bottom=1080, threshold=.9)


def action():
    return dict(timeout_seconds=2, multimodal=dict(max_calls=1), poll_seconds=1,
                pre_click_wait_seconds=1, post_click_wait_seconds=1)


def test_secret_entry_can_confirm_past_call_cap_and_clicks_latest_frame(monkeypatch):
    from naruto_daily.secret_realm import enter

    clock, device = Clock(), Device()
    reply = dict(page='main', name='忍者挑战', candidate_id=1)
    model = Model([{}] * 4 + [reply, reply])
    monkeypatch.setattr(enter, 'entry_candidates', lambda frame:
                        (frame, {1: (int(frame) * 10, 500)}))
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        enter._locate_and_tap(ImageRecognition(device), model, action(), 'main', '忍者挑战')

    assert device.taps == [(60, 500)]
    assert model.frames == device.frames == [b'1', b'2', b'3', b'4', b'5', b'6']


def test_secret_tickets_can_be_read_past_call_cap_without_deploying():
    from naruto_daily.secret_realm.recognition import wait_for_tickets

    clock, device = Clock(), Device()
    model = Model([dict(page='room', tickets=None)] * 4 + [dict(page='room', tickets=3)])
    config = dict(action(), deploy_button=target('deploy'))

    def matcher(frame, *args):
        return (int(frame) * 10, 500), 1

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        state = wait_for_tickets(ImageRecognition(device, matcher), model, config)

    assert state.tickets == 3 and state.deploy_center == (50, 500)
    assert model.frames == device.frames == [b'1', b'2', b'3', b'4', b'5']
    assert device.taps == []


def test_secret_realm_name_can_be_read_past_call_cap_without_attacking():
    from naruto_daily.secret_realm.pre_battle import identify_battle_realm

    clock, device = Clock(), Device()
    model = Model([dict(page='battle', realm_name=None)] * 4 +
                  [dict(page='battle', realm_name='烈炎秘境')])
    config = dict(action(), battle=dict(end_targets=[]))
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        name = identify_battle_realm(ImageRecognition(device), model, config)

    assert name == '烈炎秘境'
    assert model.frames == device.frames == [b'1', b'2', b'3', b'4', b'5']
    assert device.taps == []


def test_free_recruit_can_confirm_past_call_cap_and_clicks_only_latest_free_button():
    from naruto_daily.recruit.free_button import click_free_recruit

    clock, device = Clock(), Device()
    reply = dict(category='普通招募', free_count=1, button_text='免费1抽', center=[1100, 800])
    missing = dict(reply, free_count=0)
    model = Model([missing] * 4 + [reply, dict(reply, center=[1200, 800])])
    matcher = lambda *args: ((200, 700), 1)
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        done = click_free_recruit(device, target('free'), category='普通招募',
                                 guard=target('red'), images=ImageRecognition(device, matcher),
                                 model=model, timeout_seconds=2, poll_seconds=1,
                                 pre_click_wait=1, post_click_wait=1, max_calls=1)

    assert done is True and device.taps == [(1200, 800)]
    assert model.frames == device.frames == [b'1', b'2', b'3', b'4', b'5', b'6']


def test_abundance_reobserves_unknown_result_until_page_is_confirmed():
    from naruto_daily.abundance.handler import finish_abundance_result

    clock, device = Clock(), Device()
    model = Model([dict(page='unknown')] * 4 + [dict(page='home')])
    config = dict(action(), result_return=target('return'))
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        finish_abundance_result(device, config, model=model,
                                template_matcher=lambda *args: (None, 0), clock=clock)

    assert model.frames == [b'2', b'3', b'4', b'5', b'6']
    assert device.taps == []


def test_survival_reobserves_unknown_reset_count_without_repeating_sweep():
    from naruto_daily.survival_challenge.handler import _read_reset_count

    clock, device = Clock(), Device()
    model = Model([dict(reset_count=None)] * 4 + [dict(reset_count=3)])
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        count = _read_reset_count(model, device, timeout_seconds=2)

    assert count == 3
    assert model.frames == device.frames == [b'1', b'2', b'3', b'4', b'5']
    assert device.taps == []


@pytest.mark.parametrize('module', ['abundance', 'survival'])
def test_unknown_post_action_result_stops_at_shared_deadline_without_input(module):
    from naruto_daily.abundance.handler import finish_abundance_result
    from naruto_daily.survival_challenge.handler import _read_reset_count

    clock, device = Clock(), Device()
    model = Model([dict(page='unknown', reset_count=None)] * 10)
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(3, clock=clock, sleep=clock.sleep):
            if module == 'abundance':
                finish_abundance_result(device, dict(action(), result_return=target('return')),
                                        model=model, template_matcher=lambda *args: (None, 0), clock=clock)
            else:
                _read_reset_count(model, device, timeout_seconds=2)

    assert clock() == 3 and device.taps == []
    assert len(model.frames) == len(set(model.frames))


@pytest.mark.parametrize('invalid', [None, dict(finished='false', page='battle', reason='格式不明确')])
def test_battle_reobserves_invalid_completion_without_resuming_attacks(invalid):
    from naruto_daily.battle import run_battle
    from test_battle import battle_config

    clock, device = Clock(), Device()
    model = Model([invalid] * 4 + [dict(finished=True, page='finished', reason='已完成')])
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        summary = run_battle(device, battle_config(), model=model, clock=clock,
                             template_matcher=lambda *args: (None, 0))

    assert summary.end_target == 'multimodal' and summary.taps == 0
    assert model.frames == device.frames == [b'1', b'2', b'3', b'4', b'5']
    assert device.taps == []


def test_battle_rechecks_local_result_on_retry_before_another_model_request():
    from naruto_daily.battle import run_battle
    from test_battle import battle_config

    clock, device = Clock(), Device()
    model = Model([dict(finished='unknown', page='unknown', reason='无法确认')])

    def matcher(frame, *args):
        return ((50, 50), 1) if frame == b'2' else (None, 0)

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        summary = run_battle(device, battle_config(), model=model, clock=clock,
                             template_matcher=matcher)

    assert summary.end_target == 'result' and summary.taps == 0
    assert device.frames == [b'1', b'2'] and model.frames == [b'1']
    assert device.taps == []


def test_battle_unconfirmed_completion_stops_at_deadline_without_attacks():
    from naruto_daily.battle import run_battle
    from test_battle import battle_config

    clock, device = Clock(), Device()
    model = Model([dict(finished=None, page='unknown', reason='无法确认')] * 10)
    with pytest.raises(DailyDeadlineExceeded), execution_scope(3, clock=clock, sleep=clock.sleep):
        run_battle(device, battle_config(), model=model, clock=clock,
                   template_matcher=lambda *args: (None, 0))

    assert clock() == 3 and device.taps == []
    assert model.frames == device.frames == [b'1', b'2', b'3']


class PrayerDevice(Device):
    def __init__(self):
        super().__init__()
        self.state = 'home'
        self.coin_rechecks = 0

    def tap(self, *point):
        super().tap(*point)
        self.state = {(300, 200): 'rewards', (400, 300): 'claimed',
                      (1500, 200): 'after_rewards', (500, 700): 'prayed'}[point]

    def match(self, frame, path, *args):
        name = path.stem
        if name == 'coin_prayer':
            if self.state == 'home':
                return (500, 700), 1
            if self.state == 'after_rewards':
                self.coin_rechecks += 1
                if self.coin_rechecks not in (2, 3):
                    return (500, 700), 1
        if self.state == 'home' and name == 'yesterday_reward':
            return (300, 200), 1
        if self.state == 'rewards' and name == 'yesterday_panel':
            return (950, 160), 1
        if self.state == 'rewards' and name == 'prayer_claim':
            return (400, 300), 1
        return None, 0


def prayer_after_rewards(device, clock):
    from naruto_daily.organization_prayer import handle_organization_prayer
    from test_prayer import prayer_action

    config = dict(prayer_action(), max_claims=1, pre_click_wait_seconds=1,
                  post_click_wait_seconds=1)
    return handle_organization_prayer(device, config, template_matcher=device.match,
                                      sleep=clock.sleep)


def test_prayer_reobserves_disappeared_coin_button_without_reclaiming_rewards():
    clock, device = Clock(), PrayerDevice()
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        summary = prayer_after_rewards(device, clock)

    assert summary.yesterday_opened is True and summary.claims == 1
    assert device.state == 'prayed'
    assert device.taps == [(300, 200), (400, 300), (1500, 200), (500, 700)]


def test_prayer_disappeared_coin_button_preserves_failure_without_global_scope():
    clock, device = Clock(), PrayerDevice()
    with pytest.raises(VisualError):
        prayer_after_rewards(device, clock)

    assert device.taps == [(300, 200), (400, 300), (1500, 200)]


@pytest.mark.parametrize('invalid', [None, dict(nickname=None)])
def test_account_authorization_reads_past_call_cap_and_confirms_latest_nickname(monkeypatch, invalid):
    from naruto_daily.login import authorization

    clock, device = Clock(), Device()
    model = Model([invalid] * 4 + [dict(nickname='测试用户')] * 2)
    monkeypatch.setattr(authorization, 'MultimodalClient', lambda *args, **kwargs: model)
    config = dict(action(), account_name='测试用户', picker_marker=target('picker'),
                  switch_account=target('switch'), agree_button=target('agree'))

    def matcher(frame, path, *args):
        return ((int(frame) * 10, 500), 1) if path.stem == 'agree' else (None, 0)

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        authorization.authorize_account(device, config, template_matcher=matcher, clock=clock)

    assert device.taps == [(60, 500)]
    assert model.frames == device.frames == [b'1', b'2', b'3', b'4', b'5', b'6']


def test_survival_continues_after_round_cap_until_fresh_zero_count():
    from test_survival_challenge import Client, Model as ResetModel, action as survival_action, run

    clock, device = Clock(), Client(transfer=False)
    config, model = dict(survival_action(), max_rounds=1, wait_seconds=1), ResetModel([1, 0])
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        run(config, device, model, [])

    assert [state for _, _, state in device.taps] == [
        'map', 'ready', 'confirm_first', 'confirm_second', 'map', 'reset_confirm',
        'map', 'ready', 'confirm_first', 'confirm_second',
    ]
    assert model.calls == 2 and device.state == 'map'


def test_survival_round_cap_preserves_failure_without_global_scope():
    from test_survival_challenge import Client, Model as ResetModel, action as survival_action, run

    device, model = Client(transfer=False), ResetModel([1, 0])
    with pytest.raises(VisualError, match='最多'):
        run(dict(survival_action(), max_rounds=1, wait_seconds=1), device, model, [])

    assert [state for _, _, state in device.taps] == [
        'map', 'ready', 'confirm_first', 'confirm_second', 'map', 'reset_confirm',
    ]
    assert model.calls == 1


def test_survival_zero_resources_completes_without_resetting():
    from test_survival_challenge import Client, Model as ResetModel, action as survival_action, run

    clock, device = Clock(), Client(transfer=False)
    config, model = dict(survival_action(), max_rounds=1, wait_seconds=1), ResetModel([0])
    with execution_scope(30, clock=clock, sleep=clock.sleep):
        run(config, device, model, [])

    assert [state for _, _, state in device.taps] == ['map', 'ready', 'confirm_first', 'confirm_second']
    assert model.calls == 1


def test_survival_pending_sweep_wait_reaches_original_deadline_before_stopping():
    from test_survival_challenge import Client, Model as ResetModel, action as survival_action, run

    clock, device = Clock(), Client(transfer=False)
    model = ResetModel([0])
    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(30, clock=clock, sleep=clock.sleep):
            run(survival_action(), device, model, [])

    assert clock() == 30
    assert [state for _, _, state in device.taps] == ['map', 'ready', 'confirm_first', 'confirm_second']
    assert model.calls == 0


def test_survival_pending_sweep_wait_keeps_legacy_early_timeout_without_scope():
    from test_survival_challenge import Client, Model as ResetModel, action as survival_action, run

    device, model, waits = Client(transfer=False), ResetModel([0]), []
    with pytest.raises(VisualError, match='扫荡完成超时'):
        run(dict(survival_action(), timeout_seconds=30), device, model, waits)

    assert sum(waits) == 4
    assert model.calls == 0
    assert [state for _, _, state in device.taps] == ['map', 'ready', 'confirm_first', 'confirm_second']


def test_survival_transfer_rechecks_invalid_frames_before_single_dismiss():
    from naruto_daily.survival_challenge.handler import _dismiss_transfer_if_present, TRANSFER_DISMISS

    clock, device = Clock(), Device()
    observed = []

    def matcher(frame, *args):
        observed.append((frame, tuple(device.taps)))
        if len(observed) <= 4:
            raise VisualError('截图无法解码')
        return ((None, 0) if device.taps else ((100, 100), 1))

    with execution_scope(20, clock=clock, sleep=clock.sleep):
        _dismiss_transfer_if_present(device, ImageRecognition(device, matcher), target('transfer'))

    assert device.frames == [b'1', b'2', b'3', b'4', b'5', b'6']
    assert all(taps == () for _, taps in observed[:5])
    assert observed[-1][1] == (TRANSFER_DISMISS,)
    assert device.taps == [TRANSFER_DISMISS]
    assert clock() == 5


def test_survival_transfer_invalid_frames_reach_deadline_without_any_tap():
    from naruto_daily.survival_challenge.handler import _dismiss_transfer_if_present

    clock, device = Clock(), Device()

    def matcher(*args):
        raise VisualError('截图无法解码')

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(3, clock=clock, sleep=clock.sleep):
            _dismiss_transfer_if_present(device, ImageRecognition(device, matcher), target('transfer'))

    assert clock() == 3
    assert device.frames == [b'1', b'2', b'3']
    assert device.taps == []


def test_survival_transfer_late_confirmation_cannot_dismiss():
    from naruto_daily.survival_challenge.handler import _dismiss_transfer_if_present

    clock, device = Clock(), Device()

    def matcher(*args):
        clock.now = 3
        return (100, 100), 1

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(3, clock=clock, sleep=clock.sleep):
            _dismiss_transfer_if_present(device, ImageRecognition(device, matcher), target('transfer'))

    assert device.taps == []


def test_survival_transfer_invalid_frame_still_fails_once_without_scope():
    from naruto_daily.survival_challenge.handler import _dismiss_transfer_if_present

    device = Device()

    def matcher(*args):
        raise VisualError('截图无法解码')

    with pytest.raises(VisualError, match='截图无法解码'):
        _dismiss_transfer_if_present(device, ImageRecognition(device, matcher), target('transfer'))

    assert device.frames == [b'1']
    assert device.taps == []


def test_survival_start_without_confirmed_reset_observes_new_page_after_retry_cap():
    from naruto_daily.survival_challenge.handler import _start_sweep_and_wait
    from test_survival_challenge import action as survival_action

    clock, device = Clock(), Device()

    def matcher(frame, path, *args):
        # The reset button is absent: a blocked START alone cannot justify fallback.
        visible = 'start_sweep' if clock() < 12 else 'ready'
        return ((1100, 900), 1) if path.stem == visible else (None, 0)

    with execution_scope(30, clock=clock, sleep=clock.sleep):
        images = ImageRecognition(device, matcher)
        stage = _start_sweep_and_wait(device, images, survival_action(), deadline=30,
                                      poll_seconds=1, post_click_wait=1, elapsed=lambda: images.elapsed)

    assert stage == 'ready'
    assert clock() == 12
    assert device.taps == [(1100, 900)] * 3
    assert len(device.frames) > 10
    assert len(device.frames) == len(set(device.frames))


def test_survival_start_without_confirmed_reset_waits_to_deadline_after_retry_cap():
    from naruto_daily.survival_challenge.handler import _start_sweep_and_wait
    from test_survival_challenge import action as survival_action

    clock, device = Clock(), Device()

    def matcher(frame, path, *args):
        # Keep reset unconfirmed throughout the original shared deadline.
        return ((1100, 900), 1) if path.stem == 'start_sweep' else (None, 0)

    with pytest.raises(DailyDeadlineExceeded):
        with execution_scope(20, clock=clock, sleep=clock.sleep):
            images = ImageRecognition(device, matcher)
            _start_sweep_and_wait(device, images, survival_action(), deadline=20,
                                  poll_seconds=1, post_click_wait=1, elapsed=lambda: images.elapsed)

    assert clock() == 20
    assert device.taps == [(1100, 900)] * 3
    assert len(device.frames) > 15


@pytest.mark.parametrize('module', ['authorization', 'recruit', 'secret_entry', 'secret_exit'])
@pytest.mark.parametrize('scoped', [True, False])
def test_click_prewait_reaches_scope_deadline_without_early_failure_or_input(monkeypatch, module, scoped):
    clock, device = Clock(), Device()
    config = dict(action(), pre_click_wait_seconds=3)
    context = execution_scope(2, clock=clock, sleep=clock.sleep) if scoped else nullcontext()

    with pytest.raises(DailyDeadlineExceeded if scoped else VisualError), context:
        if module == 'authorization':
            from naruto_daily.login import authorization

            model = Model([dict(nickname='测试用户')])
            monkeypatch.setattr(authorization, 'MultimodalClient', lambda *args, **kwargs: model)
            config.update(account_name='测试用户', picker_marker=target('picker'),
                          switch_account=target('switch'), agree_button=target('agree'))
            matcher = lambda frame, path, *args: ((200, 500), 1) if path.stem == 'agree' else (None, 0)
            authorization.authorize_account(device, config, template_matcher=matcher,
                                             sleep=clock.sleep, clock=clock)
        elif module == 'recruit':
            from naruto_daily.recruit.free_button import click_free_recruit

            model = Model([dict(category='普通招募', free_count=1, button_text='免费1抽', center=[1100, 800])])
            click_free_recruit(device, target('free'), category='普通招募', guard=target('guard'),
                               images=ImageRecognition(device, lambda *args: ((200, 500), 1), sleep=clock.sleep),
                               model=model, timeout_seconds=2, poll_seconds=1, pre_click_wait=3,
                               post_click_wait=1, max_calls=1, clock=clock)
        elif module == 'secret_entry':
            from naruto_daily.secret_realm import enter

            model = Model([dict(page='main', name='忍者挑战', candidate_id=1)])
            monkeypatch.setattr(enter, 'entry_candidates', lambda frame: (frame, {1: (200, 500)}))
            enter._locate_and_tap(ImageRecognition(device, sleep=clock.sleep), model, config,
                                  'main', '忍者挑战', clock=clock)
        else:
            from naruto_daily.secret_realm.pre_battle import _wait_and_click

            _wait_and_click(ImageRecognition(device, lambda *args: ((200, 500), 1), sleep=clock.sleep),
                            target('pause'), config)

    assert device.taps == [] and device.frames == [b'1']
    assert clock() == (2 if scoped else 0)


@pytest.mark.parametrize('module', ['authorization', 'recruit', 'secret_entry', 'secret_realm', 'secret_tickets'])
@pytest.mark.parametrize('scoped', [True, False])
def test_model_observation_log_shows_only_active_call_limit(monkeypatch, caplog, module, scoped):
    clock, device = Clock(), Device()
    config = dict(action(), timeout_seconds=10, multimodal=dict(max_calls=3))
    context = execution_scope(30, clock=clock, sleep=clock.sleep) if scoped else nullcontext()
    caplog.set_level('INFO', logger='naruto_daily')

    with context:
        if module == 'authorization':
            from naruto_daily.login import authorization

            model = Model([dict(nickname='测试用户')] * 2)
            monkeypatch.setattr(authorization, 'MultimodalClient', lambda *args, **kwargs: model)
            config.update(account_name='测试用户', picker_marker=target('picker'),
                          switch_account=target('switch'), agree_button=target('agree'))
            matcher = lambda frame, path, *args: ((200, 500), 1) if path.stem == 'agree' else (None, 0)
            authorization.authorize_account(device, config, template_matcher=matcher,
                                             sleep=clock.sleep, clock=clock)
        elif module == 'recruit':
            from naruto_daily.recruit.free_button import click_free_recruit

            model = Model([dict(category='普通招募', free_count=1, button_text='免费1抽', center=[1100, 800])] * 2)
            click_free_recruit(device, target('free'), category='普通招募', guard=target('guard'),
                               images=ImageRecognition(device, lambda *args: ((200, 500), 1), sleep=clock.sleep),
                               model=model, timeout_seconds=10, poll_seconds=1, pre_click_wait=1,
                               post_click_wait=1, max_calls=3, clock=clock)
        elif module == 'secret_entry':
            from naruto_daily.secret_realm import enter

            model = Model([dict(page='main', name='忍者挑战', candidate_id=1)] * 2)
            monkeypatch.setattr(enter, 'entry_candidates', lambda frame: (frame, {1: (200, 500)}))
            enter._locate_and_tap(ImageRecognition(device, sleep=clock.sleep), model, config,
                                  'main', '忍者挑战', clock=clock)
        elif module == 'secret_realm':
            from naruto_daily.secret_realm.pre_battle import identify_battle_realm

            config.update(battle=dict(end_targets=[]))
            identify_battle_realm(ImageRecognition(device, sleep=clock.sleep),
                                  Model([dict(page='battle', realm_name='烈炎秘境')]), config, clock=clock)
        else:
            from naruto_daily.secret_realm.recognition import wait_for_tickets

            config.update(deploy_button=target('deploy'))
            wait_for_tickets(ImageRecognition(device, lambda *args: ((200, 500), 1), sleep=clock.sleep),
                             Model([dict(page='room', tickets=3)]), config, clock=clock)

    call_logs = [record.getMessage() for record in caplog.records if '调用' in record.getMessage()]
    assert call_logs
    assert any(re.search(r'调用 \d+/3 次', message) for message in call_logs) is (not scoped)
    if scoped:
        assert all('由总时限约束' in message for message in call_logs)
