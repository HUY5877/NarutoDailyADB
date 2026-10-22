"""真实丰饶流程使用普通并发点击，秘境复用高速通道；不连接外部服务。"""
import base64
from copy import deepcopy
import json
from pathlib import Path
from threading import Barrier, Lock, current_thread

import pytest

from naruto_daily.abundance import handler as abundance
from naruto_daily.common import fast_duel_tap as fast
from naruto_daily.common.adb_client import AdbClient, AdbError, CommandResult
from naruto_daily.common.execution import execution_scope
from naruto_daily.multimodal.prompt_jev import BATTLE_COMPLETION
from naruto_daily.multimodal.prompts import SECRET_BATTLE_REALM, SECRET_REALM_TICKETS
from naruto_daily.secret_realm import handler as secret
from test_battle import Clock
from test_fast_duel_tap import CAPS, FakeProcess, events


ROOT = Path(__file__).resolve().parents[1]
DEPLOY, CHALLENGE = (1787, 920), (800, 950)
PAUSE, EXIT, CONFIRM, RESULT_RETURN = (1860, 50), (780, 680), (790, 690), (209, 1010)


class Device(AdbClient):
    def __init__(self, kind, *, after_deploy='battle', realm='烈炎秘境',
                 settle_after_name=False, failure=None):
        super().__init__(Path('unused-adb.exe'), serial='fixture-device')
        self.kind, self.clock = kind, Clock()
        self.state = 'challenge' if kind == 'abundance' else 'room'
        self.after_deploy, self.realm = after_deploy, realm
        self.settle_after_name = settle_after_name
        self.failure = failure
        self.frames, self.taps, self.batches, self.processes = {}, [], [], []
        self.ordinary_batches, self.ordinary_threads, self.waits = [], [], []
        self.active_taps, self.input_lock = set(), Lock()
        self.ordinary_barrier = Barrier(3, action=self._finish_ordinary_batch, timeout=3)
        self.model_checks, self.realm_checks, self.paused_batches = [], [], None
        payload = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
        task = 'abundance' if kind == 'abundance' else 'secret_realm'
        self.action = deepcopy(next(action for action in payload['tasks'][task]
                                    if action['type'] == f'handle_{task}'))
        self.action.update(load_wait_seconds=0, pre_click_wait_seconds=0,
                           post_click_wait_seconds=0, poll_seconds=.1)
        self.action['multimodal'] = {'timeout_seconds': 30, 'max_calls': 6}

    def _finish_ordinary_batch(self):
        self.ordinary_batches.append((self.clock(), tuple(self.taps[-3:])))
        self.clock.advance(.125)  # 短于0.2秒，能发现残留的丰饶强制限速。

    def sleep(self, seconds):
        self.waits.append((self.clock(), seconds))
        self.clock.advance(seconds)

    def _command(self, *arguments, **kwargs):
        if arguments[:3] == ('shell', 'getevent', '-lp'):
            return CommandResult(0, CAPS, '')
        if arguments[:2] == ('shell', 'od'):
            return CommandResult(0, '2 1', '')
        if arguments == ('shell', 'wm', 'size'):
            return CommandResult(0, 'Physical size: 1920x1080', '')
        assert len(arguments) == 2 and arguments[1] == 'test -w /dev/input/event2'
        return CommandResult(0, '', '')

    def screenshot(self):
        assert not self.active_taps, '截图前必须等待本批全部普通点击结束'
        png = f'{self.state}:{len(self.frames)}'.encode()
        self.frames[png] = self.state
        return png

    def tap(self, x, y):
        point = (x, y)
        if self.kind == 'abundance' and point in {
            (button['x'], button['y']) for button in self.action['battle']['buttons']
        }:
            assert self.state == 'battle' and self.model_checks
            assert point != (1500, 960), '丰饶不能点击一技能'
            thread = current_thread().name
            with self.input_lock:
                self.taps.append(point)
                self.ordinary_threads.append(thread)
                self.active_taps.add(thread)
            try:
                if self.failure == 'tap':
                    raise AdbError('普通战斗点击失败')
                self.ordinary_barrier.wait()
            finally:
                with self.input_lock:
                    self.active_taps.remove(thread)
            return
        self.taps.append(point)
        transitions = {
            CHALLENGE: ('challenge', 'battle'), DEPLOY: ('room', self.after_deploy),
            PAUSE: ('battle', 'pause'), EXIT: ('pause', 'confirm'),
            CONFIRM: ('confirm', 'room'), RESULT_RETURN: ('result', 'room'),
        }
        assert point in transitions, f'战斗输入必须走高速通道，不能退回普通点击：{point}'
        expected, next_state = transitions[point]
        assert self.state == expected
        assert self.frames[next(reversed(self.frames))] == expected
        self.state = next_state

    def matcher(self, png, path, roi, threshold):
        targets = {
            'challenge': {'abundance_challenge': CHALLENGE},
            'completed': {'abundance_completed': (960, 800)},
            'room': {'secret_deploy': DEPLOY},
            'battle': {'secret_battle_pause': PAUSE},
            'pause': {'secret_exit_battle': EXIT},
            'confirm': {'secret_exit_question': (960, 480), 'secret_exit_confirm': CONFIRM},
            'result': {'secret_result_marker': (537, 837), 'secret_result_return': RESULT_RETURN},
        }
        point = targets.get(self.frames[png], {}).get(path.stem)
        return point, 1. if point is not None else .1

    def complete_json(self, png, prompt, **kwargs):
        if prompt == SECRET_REALM_TICKETS:
            assert self.frames[png] == 'room'
            return {'page': 'room', 'tickets': int(not self.taps)}
        if prompt == SECRET_BATTLE_REALM:
            assert not self.processes, '秘境入场检查完成前不能打开触摸连接'
            self.realm_checks.append(png)
            if self.settle_after_name:
                self.state = 'result'
            return {'page': 'battle', 'realm_name': self.realm}
        assert prompt == BATTLE_COMPLETION
        self.model_checks.append(self.clock())
        inputs = self.ordinary_batches if self.kind == 'abundance' else self.batches
        if self.kind == 'abundance':
            assert not self.processes and not self.batches
        elif len(self.model_checks) > 1:
            assert len(self.processes) == 1 and self.processes[0].poll() is None
            assert b'STOP\n' not in self.processes[0].lines, '暂停检查时应保留同一个连接'
        if len(self.model_checks) == 2:
            if self.failure == 'observation':
                raise RuntimeError('结算观察失败')
            self.paused_batches = len(inputs)
            return {'finished': False, 'page': 'unknown', 'reason': '暂未确认，暂停输入'}
        if len(self.model_checks) == 3:
            assert len(inputs) == self.paused_batches, '观察期间不能继续发送战斗输入'
        finished = len(self.model_checks) == 4
        if finished:
            self.state = 'completed' if self.kind == 'abundance' else 'room'
        return {'finished': finished, 'page': 'finished' if finished else 'battle', 'reason': '回放'}


def execute(monkeypatch, device):
    real_channel = fast.DuelTapChannel

    def channel(*args, **kwargs):
        process = FakeProcess(reply=b'ERROR\n' if device.failure == 'packet' else b'OK\n')
        write = process.write

        def send(line):
            if line != b'STOP\n':
                device.batches.append((device.clock(), line))
                device.clock.advance(1)  # 模拟一批输入与确认耗时，避免真实等待。
            return write(line)

        process.write = send

        def launch(*args, **kwargs):
            assert device.state == 'battle' and len(device.model_checks) >= 1
            device.processes.append(process)
            return process

        return real_channel(*args, **kwargs, popen=launch, clock=device.clock)

    monkeypatch.setattr(fast, 'DuelTapChannel', channel)
    if device.kind == 'abundance':
        def forbid_fast_taps(*args, **kwargs):
            pytest.fail('丰饶必须保持普通三线程点击，不能打开高速通道')
        monkeypatch.setattr(device, 'battle_taps', forbid_fast_taps)
        monkeypatch.setattr(device, 'duel_taps', forbid_fast_taps)
    module = abundance if device.kind == 'abundance' else secret
    monkeypatch.setattr(module, 'MultimodalClient', lambda *a, **kw: device)
    battle_name = 'run_battle_without_first_skill' if device.kind == 'abundance' else 'run_battle'
    real_battle = getattr(module, battle_name)

    def enter_battle(*args, **kwargs):
        return real_battle(*args, **dict(kwargs, clock=device.clock))

    monkeypatch.setattr(module, battle_name, enter_battle)
    run = module.handle_abundance if device.kind == 'abundance' else module.handle_secret_realm
    with execution_scope(200, clock=device.clock, sleep=device.sleep):
        return run(device, device.action, template_matcher=device.matcher,
                   asset_root=ROOT, sleep=device.sleep)


def test_real_abundance_uses_three_ordinary_threads_without_rate_floor_or_fast_connection(monkeypatch):
    device = Device('abundance')
    original = deepcopy(device.action)

    summary = execute(monkeypatch, device)

    expected = {(button['x'], button['y']) for button in device.action['battle']['buttons']
                if button['name'] != '一技能'}
    assert len(expected) == 3
    assert device.model_checks == pytest.approx([0, 30, 31, 61])
    assert len(device.ordinary_batches) == 480
    assert all(set(points) == expected for _, points in device.ordinary_batches)
    assert len(set(device.ordinary_threads)) == 3
    assert all(name.startswith('battle-tap') for name in device.ordinary_threads)
    assert [at for at, _ in device.ordinary_batches] == pytest.approx(
        [index * .125 for index in range(240)]
        + [31 + index * .125 for index in range(240)])
    assert [(at, seconds) for at, seconds in device.waits if seconds > 0] == [(30, 1)]
    assert device.taps[0] == CHALLENGE
    assert set(device.taps[1:]) == expected
    assert summary.taps == len(device.taps) - 1 == 1440
    assert device.processes == device.batches == []
    assert not device.active_taps
    assert device.action == original


def test_real_secret_handler_keeps_one_touch_connection_across_result_check_pauses(monkeypatch):
    device = Device('secret')
    original = deepcopy(device.action)

    summary = execute(monkeypatch, device)

    assert device.model_checks == pytest.approx([0, 18, 19, 37])
    assert len(device.processes) == 1
    assert device.processes[0].lines[-1] == b'STOP\n'
    assert device.processes[0].poll() == 0 and not device.processes[0].killed
    expected = [(button['x'], button['y']) for button in device.action['battle']['buttons']]
    assert len(expected) == 4 and len(device.batches) == 36
    for _, packet in device.batches:
        down, up = [events(base64.b64decode(part)) for part in packet.split()]
        assert list(zip([v for t, c, v in down if (t, c) == (3, 53)],
                        [v for t, c, v in down if (t, c) == (3, 54)])) == expected
        assert [v for t, c, v in up if (t, c) == (3, 57)] == [-1] * 4
    assert device.taps == [DEPLOY]
    assert summary == 1
    assert device.action == original


@pytest.mark.parametrize('scenario', ['exit', 'entry-result', 'settle-during-name'])
def test_real_secret_entry_or_local_result_never_opens_touch_transport(monkeypatch, scenario):
    device = Device('secret', after_deploy='result' if scenario == 'entry-result' else 'battle',
                    realm='落岩秘境' if scenario == 'exit' else '烈炎秘境',
                    settle_after_name=scenario == 'settle-during-name')

    completed = execute(monkeypatch, device)

    assert completed == (0 if scenario == 'exit' else 1)
    assert device.processes == device.batches == device.model_checks == []
    assert device.taps == ([DEPLOY, PAUSE, EXIT, CONFIRM] if scenario == 'exit'
                           else [DEPLOY, RESULT_RETURN])


@pytest.mark.parametrize('failure', ['packet', 'observation'])
def test_secret_input_or_observation_failure_closes_channel_without_individual_tap_fallback(monkeypatch, failure):
    device = Device('secret', failure=failure)

    with pytest.raises(AdbError if failure == 'packet' else RuntimeError,
                       match='高速战斗点击通道异常' if failure == 'packet' else '结算观察失败'):
        execute(monkeypatch, device)

    assert len(device.processes) == 1 and device.processes[0].lines[-1] == b'STOP\n'
    assert device.processes[0].poll() == 0
    assert len(device.batches) == (1 if failure == 'packet' else 18)
    assert device.taps == [DEPLOY]


@pytest.mark.parametrize('failure', ['tap', 'observation'])
def test_abundance_ordinary_input_or_observation_failure_stops_without_fast_fallback(monkeypatch, failure):
    device = Device('abundance', failure=failure)

    with pytest.raises(AdbError if failure == 'tap' else RuntimeError,
                       match='普通战斗点击失败' if failure == 'tap' else '结算观察失败'):
        execute(monkeypatch, device)

    expected = {(button['x'], button['y']) for button in device.action['battle']['buttons']
                if button['name'] != '一技能'}
    assert device.taps[0] == CHALLENGE
    assert set(device.taps[1:]) <= expected
    if failure == 'tap':
        assert 1 <= len(device.taps[1:]) <= 3
        assert device.ordinary_batches == []
        assert device.model_checks == [0]
    else:
        assert len(device.ordinary_batches) == 240
        assert len(device.taps[1:]) == 720
        assert device.model_checks == [0, 30]
    assert device.processes == device.batches == []
    assert not device.active_taps
