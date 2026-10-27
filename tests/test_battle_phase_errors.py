from threading import Barrier, Lock

import pytest

from naruto_daily.battle import run_battle_without_first_skill
from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.execution import DailyDeadlineExceeded, PhaseDeadlineExceeded
from test_battle import Clock, Client, battle_config


@pytest.mark.parametrize('kind', ['ordinary_over_phase', 'daily_over_ordinary'])
def test_all_battle_inputs_finish_and_real_errors_win_over_phase_timeout(kind):
    clock = Clock()
    client = Client(clock)
    barrier, lock = Barrier(3, timeout=3), Lock()
    settled = []
    errors = ([PhaseDeadlineExceeded('phase expired'), AdbError('device offline')]
              if kind == 'ordinary_over_phase'
              else [AdbError('device offline'), DailyDeadlineExceeded('daily expired')])

    def tap(x, y):
        try:
            barrier.wait()
            if x in (2, 3):
                raise errors[x - 2]
        finally:
            with lock:
                settled.append(x)

    client.tap = tap
    with pytest.raises(type(errors[1])) as caught:
        run_battle_without_first_skill(
            client, battle_config(), clock=clock, sleep=clock.advance,
            template_matcher=lambda *args: (None, .1),
        )

    assert caught.value is errors[1]
    assert sorted(settled) == [2, 3, 4]
