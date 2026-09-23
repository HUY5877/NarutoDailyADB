"""Isolated LDPlayer QR-login worker using the parent's line protocol."""

from __future__ import annotations

import base64
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable, TextIO
from uuid import UUID


_MAX_INPUT_BYTES = 16_384
_MAX_PNG_BYTES = 700_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _load_dependencies():
    # Keep imports lazy: validation failures must not initialize ADB/UI modules.
    from .ldplayer import LDPlayerError, LDPlayerManager
    from .qr_navigation import LoginDevice, fetch_login_qr

    return LDPlayerManager, LDPlayerError, LoginDevice, fetch_login_qr


def _send(stream: TextIO, event: dict[str, object]) -> None:
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


def _is_timeout(error: BaseException, deadline: float, clock: Callable[[], float]) -> bool:
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) or clock() >= deadline:
        return True
    # LDPlayerError deliberately wraps subprocess timeouts. Its messages are
    # fixed locally and are used only to select a safe enum, never as output.
    message = str(error).lower()
    return "timed out" in message or "timeout" in message or "deadline" in message


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
    try:
        Manager, DriverError, LoginDevice, fetch_login_qr = _load_dependencies()
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

        phase = "game"
        _send(output_stream, {"type": "stage", "status": "opening_game"})
        manager.launch_game(instance, package, deadline)

        phase = "qr"
        _send(output_stream, {"type": "stage", "status": "preparing_qr"})
        device = LoginDevice(manager.adb_path, serial, deadline, command_timeout)
        png = fetch_login_qr(device)
        if (not isinstance(png, bytes) or not png.startswith(_PNG_SIGNATURE)
                or len(png) > _MAX_PNG_BYTES):
            raise ValueError
        _send(output_stream, {"type": "qr", "png": base64.b64encode(png).decode("ascii")})
        return 0
    except Exception as error:
        if _is_timeout(error, deadline, clock):
            code = "command_timeout"
        elif phase == "configuration":
            code = "configuration_error"
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
        if watchdog is not None:
            watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
