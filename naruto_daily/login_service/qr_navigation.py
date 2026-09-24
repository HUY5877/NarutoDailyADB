"""Observe the QQ login UI and return its actual authorization QR pixels."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

import cv2
import numpy as np


class QRNavigationError(RuntimeError):
    """A bounded device/navigation operation failed; never contains device output."""


class LoginDevice:
    def __init__(self, adb_path: Path, serial: str, deadline: float, command_timeout: float = 20):
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", serial) or command_timeout <= 0:
            raise QRNavigationError("Invalid ADB device configuration")
        self.adb_path, self.serial = Path(adb_path), serial
        self.deadline, self.command_timeout = deadline, command_timeout

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
        if image is None or image.shape[:2] != (1080, 1920):
            raise QRNavigationError("Login requires a 1920x1080 Android screenshot")
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
        if not (0 <= x < 1920 and 0 <= y < 1080):
            raise QRNavigationError("ADB tap falls outside the login screen")
        self._run("shell", "input", "tap", str(x), str(y))


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
    if name in shared:
        # Keep one source image in assets/; wheels install it as naruto_daily.assets.
        package_root = Path(__file__).resolve().parents[1]
        path = package_root / "assets" / shared[name]
        if not path.is_file():
            path = package_root.parent / "assets" / shared[name]
    else:
        path = Path(__file__).with_name("templates") / f"{name}.png"
    data = path.read_bytes()
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)


def _match(frame: np.ndarray, name: str, roi=(0, 0, 1920, 1080), threshold=.92):
    left, top, right, bottom = roi
    region = frame[top:bottom, left:right]
    if region.size == 0:
        return None
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    needle = _template(name)
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


def crop_authorization_qr(frame: np.ndarray, xml: str) -> bytes | None:
    texts = " ".join(node.get("text", "") + node.get("content-desc", "") for node in _qq_nodes(xml))
    if any(word in texts for word in ("二维码已失效", "二维码已过期")):
        raise QRNavigationError("QQ authorization QR expired; request a new login")
    if not ("使用另一部手机QQ扫码" in texts or _match(frame, "qr_heading", (650, 100, 1300, 370))):
        return None
    # Only inspect the code area of the confirmed QQ authorization page.
    region = frame[310:820, 620:1320]
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


def choose_action(frame: np.ndarray, xml: str) -> LoginAction | None:
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
    for name, labels, roi in steps:
        action = _node_action(xml, labels, name)
        if action:
            return action
        point = _match(frame, name, roi)
        if point:
            return LoginAction(name, *point)
    point = _match(frame, "qq_android", (1350, 720, 1850, 900), .9)
    if point:
        checkbox = frame[910:942, 142:176]
        blue, green, red = cv2.split(checkbox.astype(np.int16))
        # The game's consent tick is bright green. Its gold border is not a tick.
        checked = np.count_nonzero((green > 170) & (green-red > 70) & (green-blue > 30)) > 25
        return LoginAction("qq_android", *point) if checked else LoginAction("game_agreement", 158, 924)
    if _match(frame, "start_game", (650, 780, 1270, 1020), .9):
        point = _match(frame, "game_back", (1650, 10, 1850, 170))
        if point:
            return LoginAction("game_back", *point)
    return None


def fetch_login_qr(device: LoginDevice, *, poll_seconds: float = 1) -> bytes:
    last_action, repeated = None, 0
    qr_wait_until: float | None = None
    while time.time() < device.deadline:
        if qr_wait_until is not None and time.time() >= qr_wait_until:
            raise QRNavigationError("QQ authorization QR did not load after scan login was selected")
        guard = getattr(device, "guard_authorization_surface", None)
        if guard is not None:
            guard()
        frame = device.screenshot()
        # Game pages are a SurfaceView; native QQ text is a fallback for template changes.
        png = crop_authorization_qr(frame, "")
        if png:
            return png
        action = None if qr_wait_until is not None else choose_action(frame, "")
        if action is None:
            xml = device.hierarchy()
            png = crop_authorization_qr(frame, xml)
            if png:
                return png
            action = choose_action(frame, xml)
        if qr_wait_until is not None and action is not None and action.name != "game_wait":
            # QQ can briefly show the previous page while requesting the code.
            # Never reopen the sheet or submit scan-login a second time.
            action = None
        if action:
            repeated = repeated + 1 if last_action == action.name else 1
            if repeated > 6:
                raise QRNavigationError("QQ login page did not advance after repeated observations")
            if time.time() >= device.deadline:
                break
            device.tap(action.x, action.y)
            if action.name == "scan_login":
                qr_wait_until = min(device.deadline, time.time() + 30)
            last_action = action.name
        time.sleep(min(max(0, poll_seconds), max(0, device.deadline-time.time())))
    raise QRNavigationError("QQ authorization QR was not found before the login deadline")
