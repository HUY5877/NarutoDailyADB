from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from naruto_daily.common.adb_client import AdbClient, AdbError
from .config import ConfigError, Settings, load_settings
from .runner import ActionRunner
from .tasks import TaskError, build_all, build_task
from naruto_daily.common.visual import VisualError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="naruto-daily", description="雷电模拟器固定坐标 ADB 自动化")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="检查配置和设备")
    check.add_argument("--config", type=Path, required=True)
    run = subparsers.add_parser("run", help="执行一个已配置任务")
    run.add_argument("task", choices=["login", "organization_pray", "sign_in", "stamina", "recruit", "abundance", "secret_realm", "secret_realm_farm", "all"])
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true")
    return parser


def _preflight(settings: Settings, client_factory: Callable[..., AdbClient]) -> AdbClient:
    discovery = client_factory(settings.adb_path, serial=None)
    devices = discovery.devices()
    if settings.serial:
        if settings.serial not in devices:
            raise AdbError(f"配置的 ADB 设备不在线：{settings.serial}")
        serial = settings.serial
    else:
        if not devices:
            raise AdbError("没有发现在线的 ADB 设备")
        if len(devices) > 1:
            raise AdbError("发现多个在线 ADB 设备，请明确配置 serial")
        serial = devices[0]
    client = client_factory(settings.adb_path, serial=serial)
    actual = client.screen_size()
    expected = (settings.screen_width, settings.screen_height)
    if actual != expected:
        raise AdbError(f"分辨率不一致：配置为 {expected[0]}x{expected[1]}，设备为 {actual[0]}x{actual[1]}")
    return client


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., AdbClient] = AdbClient,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        client = _preflight(settings, client_factory)
        if args.command == "check":
            print(f"ADB 设备已就绪，分辨率 {settings.screen_width}x{settings.screen_height}")
            return 0
        actions = build_all(settings) if args.task == "all" else build_task(settings, args.task)
        if args.task == "all" and not args.dry_run:
            print("完整流程将在 5 秒后开始，按 Ctrl+C 可取消。")
            for _ in range(5):
                sleep(1)
        summary = ActionRunner(
            client,
            sleep=sleep,
            dry_run=args.dry_run,
            asset_root=args.config.parent,
        ).run(actions)
        mode = "模拟运行" if args.dry_run else "执行完成"
        print(f"{mode}：{summary.action_count} 个动作")
        return 0
    except (ConfigError, AdbError, TaskError, VisualError, KeyboardInterrupt) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
