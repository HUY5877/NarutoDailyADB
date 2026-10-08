"""Isolated LDPlayer login and daily worker using the parent's line protocol."""

from __future__ import annotations

import base64
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable, TextIO
from uuid import UUID

from naruto_daily.common.execution import DAILY_TIMEOUT_SECONDS, DailyDeadlineExceeded

from .log_events import forward_task_logs
from .user_options import parse_user_options


_MAX_INPUT_BYTES = 16_384
_MAX_PNG_BYTES = 700_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_OUTPUT_LOCK = threading.RLock()


def _load_dependencies():
    # Keep imports lazy: validation failures must not initialize ADB/UI modules.
    from .ldplayer import LDPlayerError, LDPlayerManager
    from .qr_navigation import LoginDevice, fetch_login_qr

    return LDPlayerManager, LDPlayerError, LoginDevice, fetch_login_qr


def _load_daily_session():
    from .daily import DailySession

    return DailySession


def _send(stream: TextIO, event: dict[str, object]) -> None:
    # Task logs may originate from a background thread. All protocol events use
    # the same lock so neither a JSON line nor its flush can be interleaved.
    with _OUTPUT_LOCK:
        stream.write(json.dumps(event, separators=(",", ":")) + "\n")
        stream.flush()


def _deadline(value: object, now: float) -> float:
    if not isinstance(value, str):
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        deadline = parsed.timestamp()
    except (OverflowError, ValueError):
        raise ValueError from None
    if not now < deadline <= now + 600:
        raise ValueError
    return deadline


def _validate(
    message: object, now: float,
) -> tuple[str, float, Path, str, Path, str, float, tuple[int, ...]]:
    if not isinstance(message, dict) or message.get("type") != "start":
        raise ValueError
    task_id = message.get("id")
    try:
        parsed_id = UUID(task_id) if isinstance(task_id, str) else None
    except ValueError:
        raise ValueError from None
    if parsed_id is None or parsed_id.version != 4 or str(parsed_id) != task_id.lower():
        raise ValueError

    strings = [message.get(key) for key in ("consolePath", "template", "stateDir", "package")]
    if any(not isinstance(value, str) for value in strings):
        raise ValueError
    pool_value = message.get("poolIndices", [])
    if (not isinstance(pool_value, list)
            or any(type(index) is not int or index < 0 for index in pool_value)
            or len(set(pool_value)) != len(pool_value)):
        raise ValueError
    pool_indices = tuple(pool_value)
    if (not strings[0].strip() or not strings[2].strip() or not strings[3].strip()
            or not pool_indices and not strings[1].strip()):
        raise ValueError
    command_timeout = message.get("commandTimeout")
    if (not isinstance(command_timeout, (int, float)) or isinstance(command_timeout, bool)
            or not math.isfinite(command_timeout) or command_timeout <= 0):
        raise ValueError

    return (
        task_id.lower(),
        _deadline(message.get("expiresAt"), now),
        Path(strings[0]),
        strings[1],
        Path(strings[2]),
        strings[3],
        float(command_timeout),
        pool_indices,
    )


def _is_timeout(error: BaseException, deadline: float, clock: Callable[[], float], *, allow_message=False) -> bool:
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) or clock() >= deadline:
        return True
    # LDPlayerError deliberately wraps subprocess timeouts. Its messages are
    # fixed locally and are used only to select a safe enum, never as output.
    message = str(error).lower() if allow_message else ""
    return "timed out" in message or "timeout" in message or "deadline" in message


def _is_execution_timeout(error: BaseException, deadline: float, clock: Callable[[], float]) -> bool:
    # A socket/ADB timeout is not evidence that the whole daily budget expired.
    return isinstance(error, DailyDeadlineExceeded) or clock() >= deadline


def _daily_options(message: dict[str, object]) -> tuple[Path, float] | None:
    has_path = "dailyConfigPath" in message
    has_timeout = "dailyTimeoutSeconds" in message
    if not has_path and not has_timeout:
        return None
    if not has_path or not has_timeout:
        raise ValueError
    path, timeout = message["dailyConfigPath"], message["dailyTimeoutSeconds"]
    if not isinstance(path, str) or not path.strip() or not Path(path).is_absolute():
        raise ValueError
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or not 1 <= timeout <= 86_400):
        raise ValueError
    return Path(path), min(float(timeout), DAILY_TIMEOUT_SECONDS)


def main(
    *,
    input_stream=None,
    output_stream: TextIO | None = None,
    clock: Callable[[], float] = time.time,
    use_watchdog: bool = True,
) -> int:
    input_stream = sys.stdin.buffer if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    watchdog = threading.Timer(10, lambda: os._exit(1)) if use_watchdog else None
    if watchdog is not None:
        watchdog.daemon = True
        watchdog.start()

    try:
        raw = input_stream.readline(_MAX_INPUT_BYTES + 1)
        if not raw or len(raw) > _MAX_INPUT_BYTES:
            raise ValueError
        message = json.loads(raw)
        (
            task_id, deadline, console_path, template, state_dir, package,
            command_timeout, pool_indices,
        ) = _validate(message, clock())
        daily_options = _daily_options(message)
        user_options = parse_user_options(message.get("userOptions", {}))
        if watchdog is not None:
            watchdog.cancel()
    except Exception:
        if watchdog is not None:
            watchdog.cancel()
        try:
            _send(output_stream, {"type": "error", "code": "configuration_error"})
        except Exception:
            pass
        return 1

    phase = "configuration"
    log_context = ExitStack()
    try:
        # Capture ordinary prints across imports, navigation and daily execution;
        # structured logs retain the original protocol stream captured above.
        log_context.enter_context(redirect_stdout(sys.stderr))
        log_context.enter_context(forward_task_logs(
            lambda event: _send(output_stream, event), diagnostic_stream=sys.stderr,
        ))
        Manager, DriverError, LoginDevice, fetch_login_qr = _load_dependencies()
        if daily_options is not None:
            from naruto_daily.multimodal.gateway import initialize_gateway
            initialize_gateway()
        manager_options = dict(
            console_path=console_path,
            template=template,
            state_dir=state_dir,
            command_timeout=command_timeout,
        )
        if pool_indices:
            manager_options["pool_indices"] = pool_indices
        manager = Manager(**manager_options)

        phase = "emulator"
        _send(output_stream, {"type": "stage", "status": "starting_emulator"})
        instance = manager.allocate(task_id, deadline)
        serial = manager.start(instance, deadline)

        daily_session = None
        if daily_options is not None:
            phase = "configuration"
            # The protocol stream is captured above; legacy task imports and
            # prints must never become JSON events on the parent's stdout pipe.
            with redirect_stdout(sys.stderr):
                DailySession = _load_daily_session()
                daily_session = DailySession(
                    config_path=daily_options[0], adb_path=manager.adb_path,
                    serial=serial, command_timeout=command_timeout,
                    user_options=user_options,
                )

        phase = "game"
        _send(output_stream, {"type": "stage", "status": "opening_game"})
        manager.launch_game(instance, package, deadline)

        phase = "qr"
        _send(output_stream, {"type": "stage", "status": "preparing_qr"})
        device = LoginDevice(manager.adb_path, serial, deadline, command_timeout, game_package=package)
        png = fetch_login_qr(device)
        if (not isinstance(png, bytes) or not png.startswith(_PNG_SIGNATURE)
                or len(png) > _MAX_PNG_BYTES):
            raise ValueError
        _send(output_stream, {"type": "qr", "png": base64.b64encode(png).decode("ascii")})
        if daily_session is not None:
            phase = "login"
            with redirect_stdout(sys.stderr):
                daily_session.wait_for_authorization(deadline)
            if clock() >= deadline:
                raise TimeoutError
            phase = "login_finish"
            deadline = clock() + daily_options[1]
            _send(output_stream, {"type": "authorized", "executionDeadline": deadline})
            with redirect_stdout(sys.stderr):
                daily_session.finish_login(deadline)
            if clock() >= deadline:
                raise DailyDeadlineExceeded("日常任务已超过截止时间")
            _send(output_stream, {"type": "logged_in"})

            phase = "daily"
            with redirect_stdout(sys.stderr):
                action_count = daily_session.run_daily(deadline)
            if clock() >= deadline:
                raise DailyDeadlineExceeded("日常任务已超过截止时间")
            if type(action_count) is not int or action_count < 0:
                raise ValueError
            _send(output_stream, {"type": "daily_completed", "actionCount": action_count})
        return 0
    except Exception as error:
        # Full traceback stays in the task's local stderr stream. The browser
        # never receives traceback frames or the raw exception text.
        logging.getLogger("naruto_daily").exception("执行阶段 %s 失败", phase)
        if phase == "configuration":
            code = "configuration_error"
        elif phase == "login":
            code = "login_timeout" if _is_timeout(error, deadline, clock) else "login_failed"
        elif phase == "login_finish":
            code = "login_finish_timeout" if _is_execution_timeout(error, deadline, clock) else "login_finish_failed"
        elif phase == "daily":
            code = "daily_timeout" if _is_execution_timeout(error, deadline, clock) else "daily_failed"
        elif _is_timeout(error, deadline, clock, allow_message=phase == "qr" or isinstance(error, DriverError)):
            code = "command_timeout"
        elif phase == "emulator" and isinstance(error, DriverError):
            code = "emulator_start_failed"
        elif phase == "game" and isinstance(error, DriverError):
            code = "game_launch_failed"
        elif phase == "qr":
            code = "qr_not_found"
        else:
            code = "unexpected_error"
        try:
            _send(output_stream, {"type": "error", "code": code})
        except Exception:
            pass
        return 1
    finally:
        log_context.close()
        if watchdog is not None:
            watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
