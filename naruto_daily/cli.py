from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from .adb_client import AdbClient, AdbError
from .config import ConfigError, Settings, load_settings
from .runner import ActionLimitError, ActionRunner
from .tasks import TaskError, build_all, build_task


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="naruto-daily", description="Coordinate-only LDPlayer ADB automation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="validate configuration and device")
    check.add_argument("--config", type=Path, required=True)
    run = subparsers.add_parser("run", help="run one configured task")
    run.add_argument("task", choices=["login", "sign_in", "stamina", "recruit", "abundance", "all"])
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true")
    return parser


def _preflight(settings: Settings, client_factory: Callable[..., AdbClient]) -> AdbClient:
    discovery = client_factory(settings.adb_path, serial=None)
    devices = discovery.devices()
    if settings.serial:
        if settings.serial not in devices:
            raise AdbError(f"Configured ADB device is not online: {settings.serial}")
        serial = settings.serial
    else:
        if not devices:
            raise AdbError("No online ADB device found")
        if len(devices) > 1:
            raise AdbError("Multiple online ADB devices found; configure serial explicitly")
        serial = devices[0]
    client = client_factory(settings.adb_path, serial=serial)
    actual = client.screen_size()
    expected = (settings.screen_width, settings.screen_height)
    if actual != expected:
        raise AdbError(f"Resolution mismatch: configured {expected[0]}x{expected[1]}, device {actual[0]}x{actual[1]}")
    return client


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., AdbClient] = AdbClient,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        client = _preflight(settings, client_factory)
        if args.command == "check":
            print(f"ADB device ready at {settings.screen_width}x{settings.screen_height}")
            return 0
        actions = build_all(settings) if args.task == "all" else build_task(settings, args.task)
        if args.task == "all" and not args.dry_run:
            print("Full flow starts in 5 seconds. Press Ctrl+C to cancel.")
            for _ in range(5):
                sleep(1)
        summary = ActionRunner(client, sleep=sleep, dry_run=args.dry_run).run(actions)
        mode = "dry-run" if args.dry_run else "executed"
        print(f"{mode}: {summary.action_count} actions, {summary.wait_seconds:g}s configured wait")
        return 0
    except (ConfigError, AdbError, ActionLimitError, TaskError, KeyboardInterrupt) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
