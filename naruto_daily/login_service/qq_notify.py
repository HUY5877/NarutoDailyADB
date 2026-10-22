"""Send login reminders and task results through the official QQ group bot API."""

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

from .run_logs import SAFE_RUN_ERRORS
from .user_options import MainTask


_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
_REQUEST_TIMEOUT_SECONDS = 5
_MAX_RESPONSE_BYTES = 64 * 1024


class QQNotifyError(RuntimeError):
    """A notification failure containing no credentials or external response text."""


def _notification_name(user_name: str) -> str:
    printable_name = "".join(
        character if character.isprintable() else " "
        for character in (user_name or "")
    )
    return " ".join(printable_name.split())[:80] or "未提供姓名"


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
        name = _notification_name(user_name)
        content = (
            f"{name}，二维码已经刷新啦！快打开登录页面扫新的码吧～"
            if refreshed else f"{name}，你的二维码准备好啦！快打开登录页面扫码吧～"
        )
        await self._send_message(
            content,
            token=token,
        )

    async def notify_task_finished(
        self, task_id: str, user_name: str, *, status: str,
        error_code: str | None = None, secret_realm_completed: bool = False,
        main_task: MainTask = 'daily',
    ) -> None:
        if status not in {"completed", "failed", "expired"}:
            return

        name = _notification_name(user_name)
        if status == "completed":
            result = ("决斗场周胜任务结束啦！辛苦啦～" if main_task == 'arena_weekly'
                      else "日常任务全部搞定啦！辛苦啦～")
        elif status == "expired":
            result = "这次任务超时啦，重新申请后再试一次吧～"
        elif error_code == "daily_timeout" and secret_realm_completed:
            result = (
                "日常全流程已经跑通啦，秘境探险也已完成至少一次！"
                "只是本次运行时间已用完，扫荡券还没用完～"
            )
        else:
            safe_code = error_code if error_code in SAFE_RUN_ERRORS else "unexpected_error"
            result = "这次任务超时啦。" if safe_code.endswith("_timeout") else "这次任务没能完成。"
            result += SAFE_RUN_ERRORS[safe_code]

        token = await self._access_token()
        await self._send_message(
            f"{name}，{result}",
            token=token,
        )

    async def _send_message(self, content: str, *, token: str) -> None:
        await asyncio.to_thread(
            _post_json,
            self._message_url,
            {
                "msg_type": 0,
                "content": content,
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
