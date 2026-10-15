import base64
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from naruto_daily.battle import run_battle
from naruto_daily.common.adb_client import AdbError
from naruto_daily.multimodal.client import MultimodalClient, MultimodalRequestError
from naruto_daily.multimodal.decisions import JEV_ENDPOINT
from naruto_daily.multimodal.prompts import BATTLE_COMPLETION
from test_battle import Clock, battle_config


@pytest.fixture(autouse=True)
def offline_jev_environment(monkeypatch, configured_environment):
    monkeypatch.setenv('JEV_API_KEY', 'offline-jev-key')


class Device:
    def __init__(self, clock, page_at):
        self.clock = clock
        self.page_at = page_at
        self.frames = []
        self.taps = []

    def screenshot(self):
        now = self.clock()
        frame = f'{self.page_at(now)}:{now:g}:{len(self.frames)}'.encode()
        self.frames.append((now, frame))
        return frame

    def tap(self, x, y):
        self.taps.append((self.clock(), x, y))
        self.clock.advance(.25)


def request_image(request):
    body = json.loads(request.data)
    if 'state' in body:
        assert request.full_url == JEV_ENDPOINT
        question = body['questions']['decision']
        assert question['type'] == 'choice'
        assert set(question['criteria']) == set(BATTLE_COMPLETION.choices)
        content = body['state']
    else:
        content = body['messages'][0]['content']
    return base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1])


def response_for(image, *, decision=True):
    finished = not image.startswith(b'battle:')
    if decision:
        choice = next(label for label, result in BATTLE_COMPLETION.choices.items()
                      if result['finished'] is finished and result['page'] != 'unknown')
        return io.BytesIO(json.dumps({'answers': {'decision': {'choice': choice}}}).encode())
    result = dict(finished=finished, page='finished' if finished else 'battle', reason='offline replay')
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                                             'message': {'content': json.dumps(result)}}]}).encode())


def model_for(tmp_path, clock, request, sleep):
    config = dict(base_url='https://example.test/v1', model='offline', timeout_seconds=30, max_calls=3)
    return MultimodalClient(config, asset_root=tmp_path, open_request=request, clock=clock, sleep=sleep)


def battle(device, clock, model, timeout=180):
    return run_battle(device, dict(battle_config(), timeout_seconds=timeout),
                      template_matcher=lambda *args: (None, .2), model=model,
                      sleep=clock.advance, clock=clock, check_interval_seconds=18)


@pytest.mark.parametrize('new_page', ['result', 'room'])
def test_retry_refreshes_after_wait_and_never_attacks_finished_page(tmp_path, new_page):
    clock, requests, waits = Clock(), [], []
    device = Device(clock, lambda now: new_page if now >= 40 else 'battle')

    def request(req, *, timeout):
        requests.append(request_image(req))
        assert not device.taps
        if len(requests) == 1:
            clock.advance(timeout)
            raise URLError(TimeoutError('offline timeout'))
        return response_for(requests[-1])

    def sleep(seconds):
        assert not device.taps
        waits.append((clock(), seconds))
        clock.advance(seconds)

    result = battle(device, clock, model_for(tmp_path, clock, request, sleep))

    assert [at for at, _ in device.frames] == [0, 40]
    assert requests == [frame for _, frame in device.frames]
    assert requests[0].startswith(b'battle:') and requests[1].startswith(new_page.encode() + b':')
    assert waits == [(30, 10)]
    assert result.taps == 0 and result.end_target == 'multimodal' and not device.taps


def test_every_retry_sends_its_own_new_frame_while_attacks_stay_stopped(tmp_path):
    clock, requests, waits = Clock(), [], []
    device = Device(clock, lambda now: 'room' if now >= 33 else 'battle')
    failures = [TimeoutError('offline timeout'),
                HTTPError('https://example.test', 503, 'offline unavailable', {}, io.BytesIO()),
                URLError(TimeoutError('offline timeout'))]

    def request(req, *, timeout):
        requests.append(request_image(req))
        assert not device.taps
        if failures:
            clock.advance(1)
            raise failures.pop(0)
        return response_for(requests[-1])

    def sleep(seconds):
        assert not device.taps
        waits.append(seconds)
        clock.advance(seconds)

    result = battle(device, clock, model_for(tmp_path, clock, request, sleep))

    assert [at for at, _ in device.frames] == [0, 11, 22, 33]
    assert requests == [frame for _, frame in device.frames] and len(set(requests)) == 4
    assert waits == [10, 10, 10] and result.taps == 0 and not device.taps


def test_retry_on_new_battle_frame_resumes_only_after_success(tmp_path):
    clock, requests = Clock(), []
    device = Device(clock, lambda now: 'result' if now >= 58 else 'battle')

    def request(req, *, timeout):
        requests.append(request_image(req))
        if len(requests) == 1:
            clock.advance(timeout)
            raise TimeoutError('offline timeout')
        if len(requests) == 2:
            assert not device.taps and clock() == 40
        return response_for(requests[-1])

    result = battle(device, clock, model_for(tmp_path, clock, request, clock.advance))

    assert [at for at, _ in device.frames] == [0, 40, 58]
    assert requests == [frame for _, frame in device.frames]
    assert result.taps == len(device.taps) == 72
    assert min(at for at, _, _ in device.taps) == 40
    assert all(40 <= at < 58 for at, _, _ in device.taps)


@pytest.mark.parametrize('stop_at', ['capture_failure', 'capture_deadline', 'wait_deadline'])
def test_failed_or_expired_refresh_cannot_send_old_frame_or_resume_attacks(tmp_path, stop_at):
    clock, requests, screenshots = Clock(), [], []
    device = Device(clock, lambda now: 'room' if now >= 40 else 'battle')
    original_screenshot = device.screenshot
    timeout = 45 if stop_at == 'wait_deadline' else 60

    def screenshot():
        screenshots.append(clock())
        assert not device.taps
        if len(screenshots) == 2:
            if stop_at == 'capture_failure':
                raise AdbError('offline screenshot failed')
            clock.advance(20)
        return original_screenshot()

    def request(req, *, timeout):
        requests.append(request_image(req))
        assert len(requests) == 1, '截图失败或预算耗尽后不得发送旧截图'
        clock.advance(timeout)
        raise TimeoutError('offline timeout')

    def sleep(seconds):
        assert not device.taps
        clock.advance(15 if stop_at == 'wait_deadline' else seconds)

    device.screenshot = screenshot
    model = model_for(tmp_path, clock, request, sleep)
    with pytest.raises(MultimodalRequestError):
        battle(device, clock, model, timeout=timeout)

    assert screenshots == ([0] if stop_at == 'wait_deadline' else [0, 40])
    assert len(requests) == 1 and requests[0].startswith(b'battle:')
    assert not device.taps
