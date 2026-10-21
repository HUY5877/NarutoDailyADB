"""真实丰饶/秘境流程复用持续触摸通道；不连接 ADB 或模型服务。"""
import base64
from copy import deepcopy
import json
from pathlib import Path

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
        self.model_checks, self.realm_checks, self.paused_batches = [], [], None
        payload = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
        task = 'abundance' if kind == 'abundance' else 'secret_realm'
        self.action = deepcopy(next(action for action in payload['tasks'][task]
                                    if action['type'] == f'handle_{task}'))
        self.action.update(load_wait_seconds=0, pre_click_wait_seconds=0,
                           post_click_wait_seconds=0, poll_seconds=.1)
        self.action['multimodal'] = {'timeout_seconds': 30, 'max_calls': 6}

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
        png = f'{self.state}:{len(self.frames)}'.encode()
        self.frames[png] = self.state
        return png

    def tap(self, x, y):
        point = (x, y)
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
        if len(self.model_checks) > 1:
            assert len(self.processes) == 1 and self.processes[0].poll() is None
            assert b'STOP\n' not in self.processes[0].lines, '暂停检查时应保留同一个连接'
        if len(self.model_checks) == 2:
            if self.failure == 'observation':
                raise RuntimeError('结算观察失败')
            self.paused_batches = len(self.batches)
            return {'finished': False, 'page': 'unknown', 'reason': '暂未确认，暂停输入'}
        if len(self.model_checks) == 3:
            assert len(self.batches) == self.paused_batches, '观察期间不能继续发送触摸'
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
    module = abundance if device.kind == 'abundance' else secret
    monkeypatch.setattr(module, 'MultimodalClient', lambda *a, **kw: device)
    battle_name = 'run_battle_without_first_skill' if device.kind == 'abundance' else 'run_battle'
    real_battle = getattr(module, battle_name)

    def enter_battle(*args, **kwargs):
        return real_battle(*args, **dict(kwargs, clock=device.clock))

    monkeypatch.setattr(module, battle_name, enter_battle)
    run = module.handle_abundance if device.kind == 'abundance' else module.handle_secret_realm
    with execution_scope(200, clock=device.clock, sleep=device.clock.advance):
        return run(device, device.action, template_matcher=device.matcher,
                   asset_root=ROOT, sleep=device.clock.advance)


@pytest.mark.parametrize('kind,period,count', [('abundance', 30, 3), ('secret', 18, 4)])
def test_real_pve_handlers_keep_one_touch_connection_across_result_check_pauses(monkeypatch, kind, period, count):
    device = Device(kind)
    original = deepcopy(device.action)

    summary = execute(monkeypatch, device)

    assert device.model_checks == pytest.approx([0, period, period + 1, 2 * period + 1])
    assert len(device.processes) == 1
    assert device.processes[0].lines[-1] == b'STOP\n'
    assert device.processes[0].poll() == 0 and not device.processes[0].killed
    expected = [(button['x'], button['y']) for button in device.action['battle']['buttons']
                if kind == 'secret' or button['name'] != '一技能']
    assert len(expected) == count and len(device.batches) == 2 * period
    for _, packet in device.batches:
        down, up = [events(base64.b64decode(part)) for part in packet.split()]
        assert list(zip([v for t, c, v in down if (t, c) == (3, 53)],
                        [v for t, c, v in down if (t, c) == (3, 54)])) == expected
        assert [v for t, c, v in up if (t, c) == (3, 57)] == [-1] * count
    assert device.taps == [CHALLENGE if kind == 'abundance' else DEPLOY]
    if kind == 'abundance':
        assert summary.taps == len(device.batches) * count
    else:
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


@pytest.mark.parametrize('kind', ['abundance', 'secret'])
@pytest.mark.parametrize('failure', ['packet', 'observation'])
def test_pve_input_or_observation_failure_closes_channel_without_individual_tap_fallback(monkeypatch, kind, failure):
    device = Device(kind, failure=failure)

    with pytest.raises(AdbError if failure == 'packet' else RuntimeError,
                       match='高速战斗点击通道异常' if failure == 'packet' else '结算观察失败'):
        execute(monkeypatch, device)

    assert len(device.processes) == 1 and device.processes[0].lines[-1] == b'STOP\n'
    assert device.processes[0].poll() == 0
    assert len(device.batches) == (1 if failure == 'packet' else 30 if kind == 'abundance' else 18)
    assert device.taps == [CHALLENGE if kind == 'abundance' else DEPLOY]
