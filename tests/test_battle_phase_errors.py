from threading import Barrier, Lock
from contextlib import nullcontext

import pytest

from naruto_daily.battle import run_battle_without_first_skill
from naruto_daily.common.adb_client import AdbError
from naruto_daily.common.execution import DailyDeadlineExceeded, PhaseDeadlineExceeded, execution_phase_scope
from test_battle import Clock, Client, battle_config


@pytest.mark.parametrize('kind', ['ordinary_over_phase', 'daily_over_ordinary'])
@pytest.mark.parametrize('abundance_phase', [False, True])
def test_all_battle_inputs_finish_and_error_priority_only_changes_in_abundance(kind, abundance_phase):
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
    expected = errors[1] if abundance_phase else errors[0]
    scope = execution_phase_scope(180, clock=clock, sleep=clock.advance) if abundance_phase else nullcontext()
    with pytest.raises(type(expected)) as caught:
        with scope:
            run_battle_without_first_skill(
                client, battle_config(), clock=clock, sleep=clock.advance,
                template_matcher=lambda *args: (None, .1),
            )

    assert caught.value is expected
    assert sorted(settled) == [2, 3, 4]
