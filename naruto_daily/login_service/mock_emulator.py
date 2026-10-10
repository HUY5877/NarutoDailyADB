"""One isolated Python process per mock login. This never authenticates an account."""

from __future__ import annotations

import base64
from io import BytesIO
import json
import os
import re
import sys
import threading
import time

import qrcode
from PIL import Image


def _send(event: dict[str, object]) -> None:
    print(json.dumps(event, separators=(",", ":")), flush=True)


def main() -> int:
    # A child launched without a parent task cannot linger indefinitely.
    watchdog = threading.Timer(10, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()
    try:
        message = json.loads(sys.stdin.buffer.readline(16_384))
        if not isinstance(message, dict) or message.get("type") != "start":
            return 1
        task_id, nonce, delay = message.get("id"), message.get("nonce"), message.get("stepDelayMs")
        if (not isinstance(task_id, str)
                or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", task_id, re.I)
                or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce, re.I)
                or not isinstance(delay, int) or isinstance(delay, bool) or not 0 <= delay <= 10_000
                or not isinstance(message.get("expiresAt"), str)):
            return 1
        watchdog.cancel()
        _send({"type": "stage", "status": "starting_emulator", "pid": os.getpid()})
        time.sleep(delay / 1000)
        _send({"type": "stage", "status": "opening_game"})
        time.sleep(delay / 1000)
        # Real adapter seam: Android friends -> switch account -> QR login.
        _send({"type": "stage", "status": "preparing_qr"})
        time.sleep(delay / 1000)
        payload = {
            "kind": "naruto-login-demo", "simulation": True, "taskId": task_id,
            "nonce": nonce, "expiresAt": message["expiresAt"],
            "message": "Mock QR only. This code cannot authenticate a game account.",
        }
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=3)
        qr.add_data(json.dumps(payload, separators=(",", ":")))
        qr.make(fit=True)
        bitmap = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        bitmap = bitmap.resize((360, 360), Image.Resampling.NEAREST)
        output = BytesIO()
        bitmap.save(output, format="PNG")
        _send({"type": "qr", "png": base64.b64encode(output.getvalue()).decode("ascii")})
        return 0
    except Exception:
        return 1
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
