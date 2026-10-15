"""Four channels retain their identity across parsing, QR navigation and refresh."""

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import qrcode

from naruto_daily.login_service import qr_navigation as navigation
from naruto_daily.login_service.login_types import LOGIN_TYPES
from naruto_daily.login_service.models import TaskValidationError, parse_queue_task, parse_submission
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.config import ServiceConfig
from test_login_service_user_options import queue_body
from test_login_service_ldplayer_worker import daily_dependencies, invoke, request

ASSETS = Path(__file__).parents[1] / 'assets'
BUTTONS = {'android_qq': ('qq_android_button.png', 1423), 'ios_qq': ('login_qq_ios.png', 1038),
           'android_wechat': ('login_wechat_android.png', 638), 'ios_wechat': ('login_wechat_ios.png', 238)}


def login_frame():
    frame = np.full((1080, 1920, 3), 255, np.uint8)
    for filename, x in BUTTONS.values():
        template = cv2.imdecode(np.frombuffer((ASSETS / filename).read_bytes(), np.uint8), 1)
        h, w = template.shape[:2]
        frame[780:780+h, x:x+w] = template
    frame[910:942, 142:176] = (100, 235, 45)
    return frame


def wechat_frame():
    frame = np.full((1920, 1080, 3), 255, np.uint8)
    template = cv2.imdecode(np.frombuffer((ASSETS / 'login_wechat_qr_instruction.png').read_bytes(), np.uint8), 1)
    h, w = template.shape[:2]
    frame[1210:1210+h, 324:324+w] = template
    template = cv2.imdecode(np.frombuffer((ASSETS / 'login_wechat_qr_cancel.png').read_bytes(), np.uint8), 1)
    h, w = template.shape[:2]
    frame[57:57+h, 44:44+w] = template
    pixels = np.array(qrcode.make('public-wechat-channel-test').convert('RGB'))
    h, w = pixels.shape[:2]
    frame[400:400+h, 350:350+w] = pixels
    return frame


@pytest.mark.parametrize('channel', LOGIN_TYPES)
@pytest.mark.parametrize('parser', [lambda body: parse_queue_task(body, 'login-test-jev'),
                                     lambda body: parse_submission(body | {'accessToken': 'ab' * 32})])
def test_channels_parse_and_legacy_defaults(channel, parser):
    assert parser(queue_body() | {'loginType': channel}).login_type == channel
    assert parser(queue_body()).login_type == 'android_qq'


@pytest.mark.parametrize('invalid', [None, '', 'qq', 'ANDROID_QQ', 1, False, [], {}])
def test_invalid_channels_rejected_before_device_operations(invalid):
    with pytest.raises(TaskValidationError, match='Invalid login type'):
        parse_queue_task(queue_body() | {'loginType': invalid}, 'login-test-jev')


@pytest.mark.parametrize('channel,button', [('android_qq', 'qq_android'), ('ios_qq', 'qq_ios'),
                                           ('android_wechat', 'wechat_android'), ('ios_wechat', 'wechat_ios')])
def test_all_buttons_present_selects_only_requested_channel_and_preserves_consent(channel, button):
    frame = login_frame()
    assert navigation.choose_action(frame, '', channel).name == button
    frame[910:942, 142:176] = (45, 65, 90)
    assert navigation.choose_action(frame, '', channel).name == 'game_agreement'


def test_portrait_wechat_qr_recognition_preserves_pixels_and_provider():
    frame = wechat_frame()
    for channel in ('android_wechat', 'ios_wechat'):
        png = navigation.crop_authorization_qr(frame, '', login_type=channel)
        image = cv2.imdecode(np.frombuffer(png, np.uint8), 1)
        assert cv2.QRCodeDetector().detectAndDecode(image)[0] == 'public-wechat-channel-test'
        assert navigation.authorization_page(frame)
        assert navigation.pre_login_image(cv2.imencode('.png', frame)[1].tobytes())
    assert navigation.crop_authorization_qr(frame, '', login_type='android_qq') is None
    assert navigation.choose_action(frame, '', 'ios_wechat') is None
    assert navigation.crop_authorization_qr(np.full((1920, 1080, 3), 255, np.uint8), '', login_type='ios_wechat') is None


@pytest.mark.parametrize('channel', ['android_wechat', 'ios_wechat'])
def test_delayed_wechat_qr_clicks_channel_once(monkeypatch, channel):
    frames = iter([login_frame(), login_frame(), login_frame(), wechat_frame()])
    clicks = []
    device = SimpleNamespace(deadline=time.time()+10, login_type=channel, screenshot=lambda: next(frames),
                             hierarchy=lambda: '', tap=lambda x, y: clicks.append((x, y)))
    assert navigation.fetch_login_qr(device, poll_seconds=0).startswith(b'\x89PNG')
    assert len(clicks) == 1


@pytest.mark.parametrize('channel', ['android_wechat', 'ios_wechat'])
def test_rejected_wechat_request_retries_original_channel_after_wait(monkeypatch, channel):
    clock = [100]
    clicks = []
    frames = [login_frame(), login_frame(), wechat_frame()]
    def screenshot():
        if len(clicks) == 1:
            clock[0] = 131
        return frames.pop(0)
    monkeypatch.setattr(navigation.time, 'time', lambda: clock[0])
    device = SimpleNamespace(deadline=200, login_type=channel, screenshot=screenshot,
                             hierarchy=lambda: '', tap=lambda x, y: clicks.append((x, y)))
    assert navigation.fetch_login_qr(device, poll_seconds=0).startswith(b'\x89PNG')
    assert len(clicks) == 2 and clicks[0] == clicks[1]


def test_wechat_retry_is_bounded_and_never_taps_an_unknown_portrait_page(monkeypatch):
    for unknown in (False, True):
        clock, clicks = [100], []
        def screenshot():
            clock[0] += 31
            return np.full((1920, 1080, 3), 255, np.uint8) if unknown and clicks else login_frame()
        monkeypatch.setattr(navigation.time, 'time', lambda: clock[0])
        device = SimpleNamespace(deadline=1000, login_type='ios_wechat', screenshot=screenshot,
                                 hierarchy=lambda: '', tap=lambda x, y: clicks.append((x, y)))
        with pytest.raises(navigation.QRNavigationError):
            navigation.fetch_login_qr(device, poll_seconds=0)
        assert len(clicks) == (1 if unknown else 3)


@pytest.mark.parametrize('channel', LOGIN_TYPES)
def test_refresh_selects_same_channel_after_returning_from_qr(monkeypatch, channel):
    frame = [wechat_frame() if channel.endswith('_wechat') else np.full((1080, 1920, 3), 255, np.uint8)]
    actions = []
    device = SimpleNamespace(deadline=time.time()+10, login_type=channel, screenshot=lambda: frame[0],
        back=lambda: (actions.append('back'), frame.__setitem__(0, login_frame())),
        tap=lambda x, y: (actions.append('cancel'), frame.__setitem__(0, login_frame())))
    monkeypatch.setattr(navigation, 'authorization_page', lambda value: not np.array_equal(value, login_frame()))
    monkeypatch.setattr(navigation.time, 'sleep', lambda seconds: actions.append('cancel_settle') if seconds == 3 else None)
    def fetch(bound, **kwargs):
        assert bound.login_type == channel
        actions.append(navigation.choose_action(bound.screenshot(), '', bound.login_type).name)
        return b'new qr'
    monkeypatch.setattr(navigation, 'fetch_login_qr', fetch)
    assert navigation.refresh_login_qr(device, is_authorized=lambda: False) == b'new qr'
    expected = ['cancel', 'cancel_settle'] if channel.endswith('_wechat') else ['back']
    assert actions == expected + [{'android_qq': 'qq_android', 'ios_qq': 'qq_ios',
                                  'android_wechat': 'wechat_android', 'ios_wechat': 'wechat_ios'}[channel]]


@pytest.mark.parametrize('channel', LOGIN_TYPES)
def test_worker_passes_channel_to_bound_device(monkeypatch, channel):
    from naruto_daily.login_service import ldplayer_worker
    dependencies = list(daily_dependencies([]))
    original = dependencies[2]
    seen = []
    class Device(original):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs['login_type'])
            super().__init__(*args, **kwargs)
    dependencies[2] = Device
    code, _ = invoke(monkeypatch, request(loginType=channel), tuple(dependencies))
    assert code == 0
    assert seen == [channel]


@pytest.mark.parametrize('channel', LOGIN_TYPES)
def test_claim_requeue_and_child_dispatch_preserve_channel(monkeypatch, channel, tmp_path):
    from naruto_daily.login_service.runtime import LoginRuntime
    record = parse_queue_task(queue_body() | {'loginType': channel}, 'login-test-jev')
    runtime = LoginRuntime(ServiceConfig(ldplayer_console_path=tmp_path/'ldconsole.exe'), object())
    async def child(bound, command, message, **kwargs):
        assert message['loginType'] == channel
        return b'png'
    monkeypatch.setattr(runtime, '_run_child', child)
    assert asyncio.run(runtime._run_ldplayer(record)) == b'png'
    fakeredis = pytest.importorskip('fakeredis.aioredis')
    pytest.importorskip('lupa')
    async def scenario():
        client = fakeredis.FakeRedis(decode_responses=True)
        try:
            config = ServiceConfig(redis_url='redis://unused.invalid', redis_queue_key='test:channels')
            body = queue_body() | {'loginType': channel}
            session = {'id': body['id'], 'status': 'queued', 'createdAt': body['createdAt'], 'expiresAt': body['expiresAt'],
                       'simulation': True, 'tokenHash': 'ab' * 32, 'loginType': channel}
            await client.set(f"{config.redis_queue_key}:sessions:{body['id']}", json.dumps(session), px=60_000)
            await client.lpush(config.redis_queue_key, json.dumps(body))
            queue = RedisTaskQueue(config, client=client)
            claimed = await queue.pop()
            assert claimed.login_type == channel
            assert await queue.requeue(claimed)
            assert (await queue.pop()).login_type == channel
        finally:
            await client.aclose()
    asyncio.run(scenario())
