"""Send QR-ready reminders through the official QQ group bot HTTP API."""

from __future__ import annotations

import asyncio
from http.client import HTTPException
from itertools import count
import json
import math
from time import monotonic, time
import urllib.error
import urllib.parse
import urllib.request


_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
_REQUEST_TIMEOUT_SECONDS = 5
_MAX_RESPONSE_BYTES = 64 * 1024


class QQNotifyError(RuntimeError):
    """A notification failure containing no credentials or external response text."""


def _post_json(url: str, payload: dict, *, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"QQBot {token}"
    failed = False
    body = b""
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        failed = True
    except (OSError, ValueError, HTTPException):
        failed = True

    # Raise after leaving the exception handler so raw transport errors cannot
    # remain attached to the safe error as its exception context.
    if failed:
        raise QQNotifyError("QQ notification request failed")
    if len(body) > _MAX_RESPONSE_BYTES:
        raise QQNotifyError("QQ notification response exceeded the size limit")

    result = None
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        pass
    if not isinstance(result, dict):
        raise QQNotifyError("QQ notification response was invalid")
    if result.get("code", 0) != 0:
        raise QQNotifyError("QQ notification was rejected by the API")
    return result


class QQGroupNotifier:
    """Cache bot tokens and send each requested notification once, without retries."""

    def __init__(self, app_id: str, app_secret: str, group_openid: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._message_url = (
            "https://api.bot.qq.com/v2/groups/"
            + urllib.parse.quote(group_openid, safe="")
            + "/messages"
        )
        self._token = ""
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()
        self._token_task: asyncio.Task[str] | None = None
        self._message_sequences = count(1)
        self._closed = False

    async def _access_token(self) -> str:
        if self._token_task is None or self._token_task.done():
            self._token_task = asyncio.create_task(self._fetch_access_token())
            # A cancelled caller must not cancel the still-running HTTP thread
            # or release its lock. Keep this task shared until it finishes.
            # Retrieve failures even if all its original callers have cancelled.
            self._token_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return await asyncio.shield(self._token_task)

    async def _fetch_access_token(self) -> str:
        async with self._token_lock:
            if self._closed:
                raise QQNotifyError("QQ notifier is closed")
            if self._token and monotonic() < self._refresh_at:
                return self._token

            requested_at = monotonic()
            response = await asyncio.to_thread(
                _post_json,
                _TOKEN_URL,
                {"appId": self._app_id, "clientSecret": self._app_secret},
            )
            if self._closed:
                raise QQNotifyError("QQ notifier is closed")
            token = response.get("access_token")
            expiry = response.get("expires_in")
            lifetime = 0.0
            if not isinstance(expiry, bool):
                try:
                    lifetime = float(expiry)
                except (TypeError, ValueError, OverflowError):
                    pass
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 4096
                or any(character.isspace() or not character.isprintable() for character in token)
                or not math.isfinite(lifetime)
                or lifetime <= 0
            ):
                raise QQNotifyError("QQ access token response was invalid")

            self._token = token
            # Reserve ten percent of short token lifetimes, capped at a minute.
            # Count the HTTP request duration against the token lifetime.
            self._refresh_at = requested_at + lifetime - min(60.0, lifetime * 0.1)
            return token

    async def notify_qr_ready(
        self, task_id: str, user_name: str, *, refreshed: bool = False,
        expires_at_ms: float | None = None,
    ) -> None:
        token = await self._access_token()
        if expires_at_ms is not None and time() * 1000 >= expires_at_ms:
            raise QQNotifyError('QR code expired before notification')
        printable_name = "".join(
            character if character.isprintable() else " "
            for character in (user_name or "")
        )
        name = " ".join(printable_name.split())[:80] or "未提供姓名"
        short_id = "".join(
            character for character in task_id
            if character.isalnum() or character in "-_"
        )[:8] or "未知"
        state = "已刷新" if refreshed else "已生成"
        await asyncio.to_thread(
            _post_json,
            self._message_url,
            {
                "msg_type": 0,
                "content": f"【火影忍者登录】{name} 的二维码{state}，请打开登录页面扫码。\n任务：{short_id}",
                "msg_seq": next(self._message_sequences),
            },
            token=token,
        )

    async def close(self) -> None:
        # A cancelled notification may still have a shielded HTTP request.
        # Closing must not wait for its lock or retain a late token response.
        self._closed = True
        self._token = ""
        self._refresh_at = 0.0
