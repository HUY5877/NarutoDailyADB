"""Observe the selected QQ/WeChat channel and return its actual QR pixels."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from .login_types import DEFAULT_LOGIN_TYPE, parse_login_type


class QRNavigationError(RuntimeError):
    """A bounded device/navigation operation failed; never contains device output."""


class LoginDevice:
    def __init__(self, adb_path: Path, serial: str, deadline: float, command_timeout: float = 20,
                 *, game_package: str = "com.tencent.KiHan", login_type: str = DEFAULT_LOGIN_TYPE):
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", serial) or command_timeout <= 0:
            raise QRNavigationError("Invalid ADB device configuration")
        self.adb_path, self.serial = Path(adb_path), serial
        self.deadline, self.command_timeout = deadline, command_timeout
        self.game_package = game_package
        self.login_type = parse_login_type(login_type)

    def _run(self, *arguments: str) -> bytes:
        remaining = self.deadline - time.time()
        if remaining <= 0:
            raise QRNavigationError("Login task deadline expired")
        try:
            result = subprocess.run(
                [str(self.adb_path), "-s", self.serial, *arguments],
                capture_output=True, check=True, shell=False,
                timeout=min(remaining, self.command_timeout),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except (OSError, subprocess.SubprocessError):
            raise QRNavigationError("ADB operation failed or timed out") from None
        return result.stdout

    def screenshot(self) -> np.ndarray:
        image = cv2.imdecode(np.frombuffer(self._run("exec-out", "screencap", "-p"), np.uint8), 1)
        if image is None or image.shape[:2] not in ((1080, 1920), (1920, 1080)):
            raise QRNavigationError("Login requires a 1920x1080 or 1080x1920 Android screenshot")
        return image

    def guard_authorization_surface(self) -> None:
        activity = self._run("shell", "dumpsys", "activity", "activities").decode("utf-8", "replace")
        for line in activity.splitlines():
            if "mResumedActivity:" not in line and "topResumedActivity:" not in line:
                continue
            if "com.tencent.mobileqq/" in line and any(
                name in line for name in ("SplashActivity", "MainActivity", "QQSettingSettingActivity")
            ):
                raise QRNavigationError("QQ main page opened instead of game authorization; login stopped")

    def hierarchy(self) -> str:
        # Static remote command, private to this task's device; no credentials are logged.
        try:
            return self._run("shell", "uiautomator dump /sdcard/nd-login-ui.xml >/dev/null 2>&1 && cat /sdcard/nd-login-ui.xml").decode("utf-8", "replace")
        except QRNavigationError:
            # SurfaceView pages need image templates and may not expose accessibility.
            return ""

    def tap(self, x: int, y: int) -> None:
        # The WeChat Cancel control also lies within these bounds in portrait.
        if not (0 <= x < 1920 and 0 <= y < 1080):
            raise QRNavigationError("ADB tap falls outside the login screen")
        self._run("shell", "input", "tap", str(x), str(y))

    def back(self) -> None:
        self._run("shell", "input", "keyevent", "4")


@dataclass(frozen=True)
class LoginAction:
    name: str
    x: int
    y: int


@lru_cache(maxsize=16)
def _template(name: str) -> np.ndarray:
    shared = {
        "qq_android": "qq_android_button.png",
        "switch_account": "qq_account_switch_account.png",
        "start_game": "start_game_button.png",
    }
    filename = shared.get(name, f"login_{name}.png")
    # All source images live in assets/; wheels install them as naruto_daily.assets.
    package_root = Path(__file__).resolve().parents[1]
    path = package_root / "assets" / filename
    if not path.is_file():
        path = package_root.parent / "assets" / filename
    data = path.read_bytes()
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)


def _match(frame: np.ndarray, name: str, roi=(0, 0, 1920, 1080), threshold=.92,
           *, binary_threshold: int | None = None):
    left, top, right, bottom = roi
    region = frame[top:bottom, left:right]
    if region.size == 0:
        return None
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    needle = _template(name)
    if binary_threshold is not None:
        gray = cv2.threshold(gray, binary_threshold, 255, cv2.THRESH_BINARY)[1]
        needle = cv2.threshold(needle, binary_threshold, 255, cv2.THRESH_BINARY)[1]
    if any(a < b for a, b in zip(gray.shape, needle.shape)):
        return None
    _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(gray, needle, cv2.TM_CCOEFF_NORMED))
    if score < threshold:
        return None
    h, w = needle.shape
    return left + point[0] + w // 2, top + point[1] + h // 2


def _nodes(xml: str):
    try:
        return list(ET.fromstring(xml).iter("node"))
    except ET.ParseError:
        return []


def _qq_nodes(xml: str):
    return [node for node in _nodes(xml) if node.get("package") == "com.tencent.mobileqq"]


def _node_action(xml: str, labels: tuple[str, ...], name: str):
    for node in _qq_nodes(xml):
        if node.get("enabled", "true") == "false":
            continue
        if not any(node.get(field, "").strip() in labels for field in ("text", "content-desc")):
            continue
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if bounds:
            x1, y1, x2, y2 = map(int, bounds.groups())
            if 0 <= x1 < x2 <= 1920 and 0 <= y1 < y2 <= 1080:
                return LoginAction(name, (x1+x2)//2, (y1+y2)//2)
    return None


def crop_authorization_qr(frame: np.ndarray, xml: str,
                          *, game_package: str = "com.tencent.KiHan", login_type: str = DEFAULT_LOGIN_TYPE) -> bytes | None:
    login_type = parse_login_type(login_type)
    texts = " ".join(node.get("text", "") + node.get("content-desc", "") for node in _qq_nodes(xml))
    game_texts = " ".join(node.get("text", "") + node.get("content-desc", "")
                          for node in _nodes(xml) if node.get("package") == game_package)
    authorization_texts = [re.sub(r"\s+", "", value) for value in (texts, game_texts)]
    if any(word in text for text in authorization_texts
           for word in ("二维码失效", "二维码已失效", "二维码已过期")):
        raise QRNavigationError("Authorization QR expired; request a new login")

    wechat_page = login_type.endswith('_wechat') and _wechat_qr_page(frame, xml, game_package)
    if login_type.endswith('_wechat') and not wechat_page:
        return None
    if not login_type.endswith('_wechat') and frame.shape[:2] != (1080, 1920):
        return None

    native_page = not wechat_page and ("使用另一部手机QQ扫码" in texts or _match(frame, "qr_heading", (650, 100, 1300, 370)))
    web_page = False
    if not native_page and not wechat_page:
        # Without QQ installed, the game opens its own authorization web page.
        # Require both its title and scanning instructions before accepting a QR.
        web_page = (
            any("QQ授权登录" in text and "使用QQ手机版扫码授权登录" in text for text in authorization_texts)
        ) or (
            _match(frame, "web_qr_heading", (480, 0, 1440, 220))
            and _match(frame, "web_qr_instruction", (480, 450, 1440, 820))
        )
    if not (native_page or web_page or wechat_page):
        return None
    if any(word in text for text in authorization_texts for word in ("已失效", "已过期")):
        raise QRNavigationError("QQ authorization QR expired; request a new login")
    if web_page and _match(frame, "web_qr_expired", (480, 180, 1440, 900), binary_threshold=220):
        # The game SurfaceView does not expose this expiry message in UI XML.
        # Its white text sits over a dimmed QR which the detector can still find.
        raise QRNavigationError("QQ authorization QR expired; request a new login")
    # The two confirmed authorization layouts place the QR at different heights.
    # Detect its actual bounds instead of cutting off a code at a fixed y offset.
    region = frame
    found, points = cv2.QRCodeDetector().detect(region)
    if not found or points is None:
        return None
    points = points.reshape(-1, 2)
    low, high = points.min(axis=0), points.max(axis=0)
    width, height = high-low
    if min(width, height) < 160 or max(width, height) / min(width, height) > 1.15:
        return None
    x1, y1 = np.floor(low).astype(int)
    x2, y2 = np.ceil(high).astype(int) + 1
    crop = region[max(0,y1):y2, max(0,x1):x2]
    # Preserve the original pixels and add a quiet zone for phone scanning.
    padding = max(24, round(max(width, height) * .1))
    crop = cv2.copyMakeBorder(crop, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=(255,255,255))
    success, encoded = cv2.imencode(".png", crop)
    return encoded.tobytes() if success else None


def _wechat_qr_page(frame: np.ndarray, xml: str = '', game_package: str = 'com.tencent.KiHan') -> bool:
    if frame.shape[:2] != (1920, 1080):
        return False
    nodes = [node for node in _nodes(xml) if node.get('package') == game_package]
    return bool(any(node.get('resource-id') == f'{game_package}:id/qrcode_iv' for node in nodes)
                and any(node.get('resource-id') == f'{game_package}:id/qrcode_status_tv' for node in nodes)
                or _match(frame, 'wechat_qr_instruction', (250, 1100, 830, 1360)))


def choose_action(frame: np.ndarray, xml: str, login_type: str = DEFAULT_LOGIN_TYPE) -> LoginAction | None:
    login_type = parse_login_type(login_type)
    if frame.shape[:2] != (1080, 1920):
        return None
    nodes = _nodes(xml)
    if any(node.get("package") == "android" and node.get("resource-id") == "android:id/alertTitle"
           and node.get("text") == "火影忍者没有响应" for node in nodes):
        for node in nodes:
            if node.get("package") == "android" and node.get("resource-id") == "android:id/aerr_wait":
                bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
                if bounds:
                    x1, y1, x2, y2 = map(int, bounds.groups())
                    if 0 <= x1 < x2 <= 1920 and 0 <= y1 < y2 <= 1080:
                        return LoginAction("game_wait", (x1+x2)//2, (y1+y2)//2)
    # Frontmost sheets before background links; never click an existing account's Agree.
    steps = (
        ("scan_login", ("扫码登录",), (540, 840, 1375, 1080)),
        ("add_account", ("添加账号",), (1630, 670, 1920, 900)),
        ("other_login", ("其他登录方式",), (990, 910, 1240, 1080)),
        ("switch_account", ("切换账号",), (800, 875, 1160, 980)),
    )
    for name, labels, roi in (steps if login_type.endswith('_qq') else ()):
        action = _node_action(xml, labels, name)
        if action:
            return action
        point = _match(frame, name, roi)
        if point:
            return LoginAction(name, *point)
    name, roi = {
        'android_qq': ('qq_android', (1350, 720, 1850, 900)),
        'ios_qq': ('qq_ios', (950, 720, 1400, 900)),
        'android_wechat': ('wechat_android', (550, 720, 1000, 900)),
        'ios_wechat': ('wechat_ios', (150, 720, 600, 900)),
    }[login_type]
    point = _match(frame, name, roi, .9)
    if point:
        checkbox = frame[910:942, 142:176]
        blue, green, red = cv2.split(checkbox.astype(np.int16))
        # The game's consent tick is bright green. Its gold border is not a tick.
        checked = np.count_nonzero((green > 170) & (green-red > 70) & (green-blue > 30)) > 25
        return LoginAction(name, *point) if checked else LoginAction("game_agreement", 158, 924)
    if _match(frame, "start_game", (650, 780, 1270, 1020), .9):
        point = _match(frame, "game_back", (1650, 10, 1850, 170))
        if point:
            return LoginAction("game_back", *point)
    return None


def find_startup_ad_close(frame: np.ndarray) -> LoginAction | None:
    """Find the neutral gray/white ad X, independent of the advertised game."""
    if frame.shape[:2] != (1080, 1920) or authorization_page(frame):
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    template = _template('startup_ad_close')
    best_score, best_point = .94, None
    # Only the inside of the button is sampled: no artwork, logo or rounded
    # corners whose background changes with the advertisement. Search the full
    # Android frame because different ad layouts move and resize the control.
    for scale in (.7, .8, .9, 1., 1.1, 1.2, 1.3, 1.4, 1.5):
        needle = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(gray, needle, cv2.TM_CCOEFF_NORMED))
        if score < best_score:
            continue
        h, w = needle.shape
        x, y = point
        patch = frame[y:y+h, x:x+w].astype(np.int16)
        # Correlation alone ignores color and brightness. Reject the game's
        # orange X and light authorization controls even if their shapes match.
        if (np.percentile(patch.max(axis=2) - patch.min(axis=2), 95) > 25
                or not 45 <= np.median(gray[y:y+h, x:x+w]) <= 155):
            continue
        best_score, best_point = score, (x + w // 2, y + h // 2)
    return LoginAction('startup_ad_close', *best_point) if best_point else None


def fetch_login_qr(device: LoginDevice, *, poll_seconds: float = 1,
                   is_authorized: Callable[[], bool] | None = None,
                   allow_startup_ads: bool = True) -> bytes | None:
    last_action, repeated = None, 0
    qr_wait_until: float | None = None
    channel_attempts = 0
    ad_observed, ad_clicks = False, 0
    login_type = parse_login_type(getattr(device, 'login_type', DEFAULT_LOGIN_TYPE))
    while time.time() < device.deadline:
        if is_authorized is not None and is_authorized():
            return None
        guard = getattr(device, "guard_authorization_surface", None)
        if guard is not None:
            guard()
        frame = device.screenshot()
        # Game pages are a SurfaceView; scoped UI text is a fallback for template changes.
        game_package = getattr(device, "game_package", "com.tencent.KiHan")
        png = crop_authorization_qr(frame, "", game_package=game_package, login_type=login_type)
        if png:
            return png
        action = None if qr_wait_until is not None else choose_action(frame, "", login_type)
        if action is None:
            xml = device.hierarchy()
            png = crop_authorization_qr(frame, xml, game_package=game_package, login_type=login_type)
            if png:
                return png
            action = choose_action(frame, xml, login_type)
        ad_action = (find_startup_ad_close(frame)
                     if allow_startup_ads and qr_wait_until is None
                     and (action is None or action.name != 'game_wait') else None)
        if ad_action:
            if ad_clicks >= 3:
                raise QRNavigationError('启动广告关闭后仍然出现，已尝试 3 次；停止点击，请检查模拟器广告页面')
            if not ad_observed:
                logging.getLogger(__name__).info('识别到启动广告关闭按钮；下一轮重新截图确认后关闭')
                ad_observed = True
            else:
                if is_authorized is not None and is_authorized():
                    return None
                if time.time() >= device.deadline:
                    break
                device.tap(ad_action.x, ad_action.y)
                ad_clicks += 1
                ad_observed = False
                logging.getLogger(__name__).info(
                    '启动广告关闭按钮复查通过，点击坐标：(%s, %s)，第 %s/3 次；重新截图继续准备二维码',
                    ad_action.x, ad_action.y, ad_clicks,
                )
            time.sleep(min(max(0, poll_seconds), max(0, device.deadline-time.time())))
            continue
        if ad_observed:
            logging.getLogger(__name__).info('启动广告关闭按钮复查未匹配，取消点击，按当前页面继续准备二维码')
        ad_observed = False
        if qr_wait_until is not None and time.time() >= qr_wait_until:
            if (login_type.endswith('_wechat') and action is not None
                    and action.name in ('wechat_android', 'wechat_ios') and channel_attempts < 3):
                # The SDK can reject a quick refresh and return to the selector.
                # Retry only the positively identified original channel, after
                # the bounded wait; never tap a loading or unknown portrait page.
                qr_wait_until = None
                logging.getLogger(__name__).warning('微信二维码请求暂未完成，重新获取（%s/3）', channel_attempts + 1)
            else:
                raise QRNavigationError("Authorization QR did not load after QR login was selected")
        if qr_wait_until is not None and action is not None and action.name != "game_wait":
            # QQ can briefly show the previous page while requesting the code.
            # Never reopen the sheet or submit scan-login a second time.
            action = None
        if action:
            repeated = repeated + 1 if last_action == action.name else 1
            if repeated > 6:
                raise QRNavigationError("Login page did not advance after repeated observations")
            if time.time() >= device.deadline:
                break
            device.tap(action.x, action.y)
            if action.name != 'game_wait':
                allow_startup_ads = False
            if action.name in ('wechat_android', 'wechat_ios'):
                channel_attempts += 1
            if action.name in ("scan_login", "wechat_android", "wechat_ios"):
                qr_wait_until = min(device.deadline, time.time() + 30)
            last_action = action.name
        time.sleep(min(max(0, poll_seconds), max(0, device.deadline-time.time())))
    raise QRNavigationError("Authorization QR was not found before the login deadline")


def authorization_page(frame: np.ndarray) -> bool:
    """Headings remain visible on both valid and expired QQ authorization codes."""
    return bool(_match(frame, 'web_qr_heading', (480, 0, 1440, 220))
                or _match(frame, 'qr_heading', (650, 100, 1300, 370)) or _wechat_qr_page(frame))


def pre_login_image(png: bytes) -> bool:
    frame = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if frame is None or frame.shape[:2] not in ((1080, 1920), (1920, 1080)):
        return False
    # A portrait QR page may show an expiry message in place of its instruction.
    # It must not trigger the game's landscape home-screen model recognition.
    if frame.shape[:2] == (1920, 1080):
        return True
    if authorization_page(frame):
        return True
    action = choose_action(frame, '')
    return action is not None and action.name != 'game_back'


def refresh_login_qr(device: LoginDevice, *, is_authorized: Callable[[], bool],
                     poll_seconds: float = 0.5) -> bytes | None:
    # Check positive authorization markers before every navigation decision.
    # Cancellation is sent once and only on a recognized authorization page; never tap
    # an expired code, a remembered account's Agree button, or the game's Back.
    returned = False
    login_type = parse_login_type(getattr(device, 'login_type', DEFAULT_LOGIN_TYPE))
    while time.time() < device.deadline:
        if is_authorized():
            return None
        frame = device.screenshot()
        on_qr_page = authorization_page(frame)
        if not on_qr_page and login_type.endswith('_wechat') and frame.shape[:2] == (1920, 1080):
            on_qr_page = _wechat_qr_page(frame, device.hierarchy(), getattr(device, 'game_package', 'com.tencent.KiHan'))
        if on_qr_page:
            if not returned:
                if is_authorized():
                    return None
                if login_type.endswith('_wechat') and frame.shape[:2] == (1920, 1080):
                    # Android Back closes this SDK Activity without completing
                    # cancellation, leaving subsequent channel clicks ignored.
                    point = _match(frame, 'wechat_qr_cancel', (0, 36, 180, 111))
                    if not point:
                        raise QRNavigationError('WeChat QR cancellation control was not found')
                    device.tap(*point)
                    # The selector appears before the SDK delivers its cancel
                    # callback. Give it time to release the previous login request.
                    time.sleep(min(3, max(0, device.deadline - time.time())))
                else:
                    device.back()
                returned = True
        else:
            action = choose_action(frame, '', login_type)
            if action and action.name != 'game_back':
                return fetch_login_qr(device, poll_seconds=poll_seconds, is_authorized=is_authorized,
                                      allow_startup_ads=False)
        time.sleep(min(poll_seconds, max(0, device.deadline - time.time())))
    raise QRNavigationError('Login task deadline expired during QR refresh')
