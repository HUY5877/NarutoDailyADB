from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .adb_client import AdbClient


class ActionLimitError(ValueError):
    """Raised when an action sequence exceeds hard safety limits."""


@dataclass(frozen=True)
class RunSummary:
    action_count: int
    wait_seconds: float


class ActionRunner:
    def __init__(self, client: AdbClient, sleep: Callable[[float], None] = time.sleep, dry_run: bool = False):
        self.client = client
        self.sleep = sleep
        self.dry_run = dry_run

    def run(self, actions: Sequence[dict]) -> RunSummary:
        if len(actions) > 100:
            raise ActionLimitError("Action sequence exceeds 100 actions")
        wait_seconds = sum(float(action.get("seconds", 0)) for action in actions if action.get("type") == "wait")
        if wait_seconds > 300:
            raise ActionLimitError("Action sequence exceeds 300 seconds")
        for action in actions:
            if self.dry_run:
                continue
            action_type = action["type"]
            if action_type == "tap":
                self.client.tap(action["x"], action["y"])
            elif action_type == "swipe":
                self.client.swipe(action["x1"], action["y1"], action["x2"], action["y2"], action["duration_ms"])
            elif action_type == "back":
                self.client.back()
            elif action_type == "wait":
                self.sleep(float(action["seconds"]))
            else:
                raise ValueError(f"Unsupported action type: {action_type!r}")
        return RunSummary(len(actions), wait_seconds)
