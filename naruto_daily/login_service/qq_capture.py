"""Bounded QQ capture capabilities scoped to one website task and device."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg


class CaptureError(RuntimeError):
    """Safe error that never includes database, provider or authorization data."""


class CaptureConfigurationError(CaptureError):
    """Both apps exist but their task-scoped automation is unavailable."""


@dataclass(frozen=True)
class CaptureRound:
    capture_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class CapturedAuthorization:
    callback_url: str = field(repr=False)
    captured_at_epoch_ms: int


class CaptureStore:
    def __init__(self, database_url: str, *, user_id: str, task_id: str, attempt_id: str,
                 login_type: str, deadline: float):
        try:
            self.user_id, self.task_id = str(UUID(user_id)), str(UUID(task_id))
            if not re.fullmatch('[0-9a-f]{32}', attempt_id) or login_type not in ('android_qq', 'ios_qq'):
                raise ValueError
            self.expires_at = datetime.fromtimestamp(deadline, timezone.utc)
            if not database_url:
                raise ValueError
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise CaptureConfigurationError('QQ 抓号任务配置无效') from None
        self._database_url = database_url
        self.attempt_id, self.login_type = attempt_id, login_type

    def _query(self, query: str, params: tuple, *, returning=True):
        try:
            with psycopg.connect(self._database_url, connect_timeout=5,
                                 options='-c statement_timeout=5000 -c lock_timeout=5000') as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
                    return cursor.fetchone() if returning else None
        except Exception:
            raise CaptureError('无法读取或更新本次 QQ 抓号授权') from None

    def begin(self, *, previous: CaptureRound | None = None) -> CaptureRound:
        current = CaptureRound(str(uuid4()), secrets.token_hex(32))
        token_hash = sha256(current.token.encode('utf-8')).hexdigest()
        if previous is None:
            row = self._query('''
                INSERT INTO naruto_login.qq_capture_requests
                    (task_id, owner_id, attempt_id, capture_id, capture_token_hash, login_type, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, attempt_id) DO NOTHING
                RETURNING capture_id
            ''', (self.task_id, self.user_id, self.attempt_id, current.capture_id, token_hash,
                  self.login_type, self.expires_at))
        else:
            # This takes the same row lock as the website upload transaction.
            row = self._query('''
                UPDATE naruto_login.qq_capture_requests SET capture_id = %s, capture_token_hash = %s,
                    status = 'pending', callback_url = NULL, captured_at = NULL,
                    authorization_expires_at = NULL, authorization_hash = NULL, updated_at = now()
                WHERE task_id = %s AND owner_id = %s AND attempt_id = %s AND capture_id = %s
                    AND status IN ('pending', 'completed') AND expires_at > clock_timestamp()
                RETURNING capture_id
            ''', (current.capture_id, token_hash, self.task_id, self.user_id, self.attempt_id, previous.capture_id))
        if row is None:
            raise CaptureError('QQ 抓号轮次已失效，请重新申请任务')
        return current

    def consume(self, current: CaptureRound) -> CapturedAuthorization | None:
        row = self._query('''
            UPDATE naruto_login.qq_capture_requests SET status = 'consumed', updated_at = now()
            WHERE task_id = %s AND owner_id = %s AND attempt_id = %s AND capture_id = %s
                AND status = 'completed' AND expires_at > clock_timestamp() AND authorization_expires_at > clock_timestamp()
            RETURNING callback_url, captured_at
        ''', (self.task_id, self.user_id, self.attempt_id, current.capture_id))
        if row is None:
            return None
        if (not isinstance(row[0], str) or not row[0] or len(row[0].encode('utf-8')) > 65_536
                or not isinstance(row[1], datetime) or row[1].tzinfo is None):
            raise CaptureError('QQ 抓号授权内容无效')
        return CapturedAuthorization(row[0], int(row[1].timestamp() * 1000))

    def cancel(self, current: CaptureRound) -> None:
        self._query('''
            UPDATE naruto_login.qq_capture_requests
            SET status = CASE WHEN status = 'consumed' THEN status ELSE 'cancelled' END,
                callback_url = NULL, updated_at = now()
            WHERE task_id = %s AND owner_id = %s AND attempt_id = %s AND capture_id = %s
        ''', (self.task_id, self.user_id, self.attempt_id, current.capture_id), returning=False)

    def cancel_attempt(self) -> None:
        """Parent fallback after OS termination, scoped to its trusted execution."""
        # Inserting a tombstone also fences a first begin whose transaction is
        # still committing when the OS kills its client. A plain UPDATE could
        # miss that invisible insert and leave a newly committed pending round.
        self._query('''
            INSERT INTO naruto_login.qq_capture_requests AS request
                (task_id, owner_id, attempt_id, capture_id, capture_token_hash, login_type, expires_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'cancelled')
            ON CONFLICT (task_id, attempt_id) DO UPDATE
            SET status = CASE WHEN request.status = 'consumed' THEN request.status ELSE 'cancelled' END,
                callback_url = NULL, updated_at = now()
            WHERE request.owner_id = EXCLUDED.owner_id
        ''', (self.task_id, self.user_id, self.attempt_id, str(uuid4()), '0' * 64,
              self.login_type, self.expires_at), returning=False)


def login_callback_url(callback_url: str, captured_at_epoch_ms: int) -> str:
    """Preserve the received credential and its original lifetime exactly."""
    try:
        address = urlsplit(callback_url)
        if not address.scheme or not address.netloc or len(callback_url.encode('utf-8')) > 65_536:
            raise ValueError
        if address.query and address.fragment:
            raise ValueError
        payload = address.fragment or address.query
        if any(name == 'auth_time' for name, _ in parse_qsl(payload, keep_blank_values=True)):
            return callback_url
        payload += ('&' if payload else '') + 'auth_time=' + str(captured_at_epoch_ms)
        return urlunsplit((address.scheme, address.netloc, address.path,
                          '' if address.fragment else payload, payload if address.fragment else ''))
    except (ValueError, TypeError):
        raise CaptureError('QQ 授权回调地址无效') from None


class AutomationProvider:
    def __init__(self, device, authority: str):
        if authority not in ('com.huy.authcapture.automation', 'com.tencent.tim.huyautomation'):
            raise CaptureConfigurationError('未知 QQ 自动化接口')
        self.device, self.uri = device, 'content://' + authority

    def call(self, method: str, argument: dict | str | None = None) -> dict:
        if method not in ('capabilities', 'begin', 'refresh', 'status', 'cancel', 'inject', 'confirm', 'clear'):
            raise CaptureError('未知 QQ 自动化操作')
        args = ['shell', 'content', 'call', '--uri', self.uri, '--method', method]
        if argument is not None:
            if isinstance(argument, dict):
                argument = base64.urlsafe_b64encode(json.dumps(argument, separators=(',', ':')).encode()).decode().rstrip('=')
            if not isinstance(argument, str) or not re.fullmatch('[A-Za-z0-9_-]+', argument):
                raise CaptureError('QQ 自动化参数无效')
            args += ['--arg', argument]
        try:
            raw = self.device._run(*args)
            if len(raw) > 32_768:
                raise ValueError
            match = re.search(rb'\bresult=([A-Za-z0-9_-]+)(?:[,}\]])', raw)
            if not match:
                raise ValueError
            encoded = match[1]
            result = json.loads(base64.urlsafe_b64decode(encoded + b'=' * (-len(encoded) % 4)))
            if (not isinstance(result, dict) or result.get('error')
                    or result.get('state') == 'error' or result.get('errorCode')):
                raise ValueError
            return result
        except Exception:
            raise CaptureError('QQ 自动化接口调用失败，请检查应用版本和当前轮次') from None

    def qr(self, capture_id: str) -> bytes:
        try:
            capture_id = str(UUID(capture_id))
            png = self.device._run('exec-out', 'content', 'read', '--uri', self.uri + '/qr/' + capture_id)
            if not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) > 700_000:
                raise ValueError
            return png
        except Exception:
            raise CaptureError('无法读取本轮 QQ 抓号二维码') from None


class QQCaptureSession:
    """A single serial owns capture, refresh and URL submission sequentially."""

    def __init__(self, device, *, user_id: str, task_id: str, attempt_id: str,
                 database_url: str, api_base: str, clock=time.time, sleep=time.sleep):
        try:
            address = urlsplit(api_base)
            if (not api_base or address.scheme != 'https' or not address.hostname
                    or address.username or address.password or address.path not in ('', '/')
                    or address.query or address.fragment or any(char.isspace() for char in api_base)
                    or address.port not in (None, 443)):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CaptureConfigurationError('请配置 QQ_CAPTURE_API_BASE 为 HTTPS 网站根地址') from None
        self.store = CaptureStore(database_url, user_id=user_id, task_id=task_id,
            attempt_id=attempt_id, login_type=device.login_type, deadline=device.deadline)
        self.device, self.api_base = device, api_base.rstrip('/')
        self._clock, self._sleep = clock, sleep
        self.current: CaptureRound | None = None
        self.capture = AutomationProvider(device, 'com.huy.authcapture.automation')
        self.login = AutomationProvider(device, 'com.tencent.tim.huyautomation')
        try:
            for provider, kind in ((self.capture, 'capture'), (self.login, 'login')):
                capabilities = provider.call('capabilities')
                if capabilities.get('protocol') != 1 or capabilities.get('kind') != kind:
                    raise ValueError
        except Exception:
            raise CaptureConfigurationError('QQ 抓号器和上号器需要支持自动化协议的新版本') from None

    def _remaining(self):
        remaining = self.device.deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError('等待 QQ 抓号授权超时')
        return remaining

    def _pause(self):
        self._sleep(min(.5, self._remaining()))

    def _capture_status(self) -> dict:
        state = self.capture.call('status', self.current.capture_id)
        if (state.get('captureId') != self.current.capture_id
                or state.get('state') not in ('preparing', 'qr_ready', 'uploading', 'uploaded')):
            raise CaptureError('QQ 抓号器已离开本次授权轮次')
        return state

    def prepare(self, *, refresh=False) -> bytes:
        self._remaining()
        self.current = self.store.begin(previous=self.current) if refresh else self.store.begin()
        context = {
            'userId': self.store.user_id, 'taskId': self.store.task_id, 'attemptId': self.store.attempt_id,
            'captureId': self.current.capture_id, 'captureToken': self.current.token,
            'apiBase': self.api_base, 'expiresAtEpochMs': int(self.device.deadline * 1000),
            'loginType': self.device.login_type,
        }
        self.capture.call('refresh' if refresh else 'begin', context)
        while True:
            self._remaining()
            state = self._capture_status()
            if (state['state'] in ('qr_ready', 'uploading', 'uploaded')
                    and type(state.get('qrRevision')) is int and state['qrRevision'] > 0):
                return self.capture.qr(self.current.capture_id)
            self._pause()

    def wait(self, *, refresh_requests=None, publish) -> CapturedAuthorization:
        while True:
            self._remaining()
            # A refresh already accepted by this worker invalidates the old
            # capability before consuming any completed upload for that round.
            request_id = refresh_requests.poll() if refresh_requests is not None else None
            if request_id is not None:
                publish({'type': 'refreshing', 'requestId': request_id})
                png = self.prepare(refresh=True)
                publish({'type': 'qr', 'requestId': request_id, 'png': base64.b64encode(png).decode('ascii')})
            authorization = self.store.consume(self.current)
            if authorization is not None:
                return authorization
            self._capture_status()
            self._pause()

    def prepare_login(self):
        self._remaining()
        self.device._run('shell', 'am', 'force-stop', 'com.tencent.tim')

    def _login_activity(self) -> bool:
        raw = self.device._run('shell', 'dumpsys', 'activity', 'activities').decode('utf-8', 'replace')
        resumed = [line for line in raw.splitlines()
                   if 'mResumedActivity:' in line or 'topResumedActivity:' in line]
        return any('com.tencent.tim/com.tencent.open.agent.AgentActivity' in line for line in resumed)

    def _open_login_channel(self):
        from .qr_navigation import (QRNavigationError, cancel_wechat_qr, choose_action,
                                    find_startup_ad_close, qq_authorization_page, wechat_qr_page)
        expected = {'android_qq': 'qq_android', 'ios_qq': 'qq_ios'}[self.device.login_type]
        selected_at = None
        cancelled_unselected = False
        wechat_cancelled_at = None
        qq_cancelled_at = None
        qq_cancelled_layers = set()
        last_action, repeated, ad_observed, ad_clicks = None, 0, False, 0
        while True:
            self._remaining()
            if self._login_activity():
                if selected_at is not None:
                    return
                if cancelled_unselected:
                    raise CaptureError('QQ 上号器再次提前打开，无法确认本次游戏通道')
                # The game can restore its last QQ channel on startup. Cancel
                # that request before selecting this task's Android/iOS channel.
                logging.getLogger(__name__).info('QQ 上号器提前打开，返回游戏重新选择本次登录通道')
                self._remaining()
                self.device.back()
                cancelled_unselected = True
                cancel_deadline = self._clock() + 10
                while self._login_activity():
                    self._remaining()
                    if self._clock() >= cancel_deadline:
                        raise CaptureError('取消提前打开的 QQ 授权页后未返回游戏')
                    self._pause()
                continue
            guard = getattr(self.device, 'guard_authorization_surface', None)
            if guard is not None:
                guard()
            frame = self.device.screenshot()
            if wechat_qr_page(self.device, frame):
                if wechat_cancelled_at is None:
                    wechat_cancelled_at = self._clock()
                    try:
                        cancel_wechat_qr(self.device, frame, clock=self._clock, sleep=self._sleep)
                    except QRNavigationError as error:
                        raise CaptureError(str(error)) from None
                    # A delayed restored WeChat request can cover the game
                    # after the QQ tap. Select QQ again only after cancelling it.
                    selected_at = None
                elif self._clock() - wechat_cancelled_at >= 10:
                    raise CaptureError('取消微信授权页后未返回游戏，已停止自动上号')
                self._pause()
                continue
            if selected_at is not None:
                restored_game = choose_action(frame, '', self.device.login_type)
                if restored_game is not None and restored_game.name == 'game_back':
                    self._remaining()
                    if self._login_activity():
                        continue
                    self.device.tap(restored_game.x, restored_game.y)
                    selected_at = None
                    logging.getLogger(__name__).info('游戏恢复到开始游戏页，返回重新选择本次登录通道')
                    self._pause()
                    continue
                if self._clock() - selected_at >= 30:
                    raise CaptureError('选择 QQ 通道后上号器未打开')
                self._pause()
                continue
            if qq_authorization_page(self.device, frame):
                if qq_cancelled_at is None:
                    self._remaining()
                    logging.getLogger(__name__).info('识别到之前的 QQ 扫码授权页，返回游戏重新选择本次登录通道')
                    self.device.back()
                    qq_cancelled_at = self._clock()
                elif self._clock() - qq_cancelled_at >= 10:
                    raise CaptureError('取消 QQ 扫码授权页后未返回游戏，已停止自动上号')
                self._pause()
                continue
            action = choose_action(frame, '', self.device.login_type)
            if action is None:
                action = choose_action(frame, self.device.hierarchy(), self.device.login_type)
            if qq_cancelled_at is not None and (action is None or action.name in (
                    'scan_login', 'add_account', 'other_login', 'switch_account')):
                if self._clock() - qq_cancelled_at >= 10:
                    raise CaptureError('取消 QQ 授权后仍未返回游戏渠道选择页')
                if action is not None and action.name not in qq_cancelled_layers:
                    self._remaining()
                    self.device.back()
                    qq_cancelled_layers.add(action.name)
                self._pause()
                continue
            ad = find_startup_ad_close(frame) if action is None or action.name != 'game_wait' else None
            if ad:
                if ad_clicks >= 3:
                    raise CaptureError('启动广告关闭后仍重复出现')
                if ad_observed:
                    self._remaining()
                    self.device.tap(ad.x, ad.y)
                    ad_clicks += 1
                ad_observed = not ad_observed
                self._pause()
                continue
            ad_observed = False
            if action is not None:
                if action.name not in (expected, 'game_agreement', 'game_back', 'game_wait'):
                    raise CaptureError('游戏未进入本次 QQ 通道，已停止自动上号')
                if action.name == expected and self._login_activity():
                    # The authorization page may reopen after the screenshot.
                    # Do not count a tap on that page as selecting a channel.
                    continue
                repeated = repeated + 1 if action.name == last_action else 1
                if repeated > 6:
                    raise CaptureError('QQ 游戏通道在重复操作后未前进')
                self._remaining()
                self.device.tap(action.x, action.y)
                last_action = action.name
                if action.name == expected:
                    selected_at = self._clock()
                    logging.getLogger(__name__).info('已点击本次指定登录通道：%s', self.device.login_type)
            self._pause()

    def inject(self, authorization: CapturedAuthorization):
        self._open_login_channel()
        callback = login_callback_url(authorization.callback_url, authorization.captured_at_epoch_ms)
        state = self.login.call('inject', {'taskId': self.store.task_id,
            'captureId': self.current.capture_id, 'callbackUrl': callback})
        while True:
            self._remaining()
            if state.get('captureId') != self.current.capture_id or state.get('state') not in ('waiting', 'ready'):
                raise CaptureError('QQ 上号器未确认本轮授权地址')
            if state['state'] == 'ready':
                if not self._login_activity():
                    raise CaptureError('QQ 上号器已离开本轮游戏授权页面')
                result = self.login.call('confirm', self.current.capture_id)
                if result.get('captureId') != self.current.capture_id or result.get('state') != 'submitted':
                    raise CaptureError('QQ 上号器未提交本轮授权')
                return
            self._pause()
            state = self.login.call('status', self.current.capture_id)

    def close(self):
        if self.current is None:
            return
        # Cleanup operations are individually fenced. Failure of one must not
        # suppress the database revocation or target another execution's row.
        for operation in (lambda: self.store.cancel(self.current),
                          lambda: self.capture.call('cancel', self.current.capture_id),
                          lambda: self.login.call('cancel', self.current.capture_id)):
            try:
                operation()
            except Exception:
                logging.getLogger(__name__).warning('本轮 QQ 抓号状态清理暂不可用，将按原有效期失效')
