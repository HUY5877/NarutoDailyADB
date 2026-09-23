"""Explicit local administration for configured LDPlayer pool bindings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID


def _task_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        raise argparse.ArgumentTypeError("task id must be a UUIDv4") from None
    if parsed.version != 4:
        raise argparse.ArgumentTypeError("task id must be a UUIDv4")
    return str(parsed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LDPlayer 固定实例池管理")
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).with_name(".env"),
        help="服务专用环境文件，默认读取本模块旁的 .env；环境变量优先",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出固定池实例及任务绑定")
    release = commands.add_parser("release", help="关闭并释放一个任务绑定")
    release.add_argument("task_id", type=_task_id, metavar="UUID")
    return parser


def _safe_status(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    status = {
        "name": value["name"],
        "index": value["index"],
        "taskId": value["taskId"],
        "state": value["state"],
    }
    if (not isinstance(status["name"], str)
            or type(status["index"]) is not int or status["index"] < 0
            or status["taskId"] is not None and not isinstance(status["taskId"], str)
            or not isinstance(status["state"], str)):
        raise ValueError
    return status


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    try:
        from .config import load_config

        config = load_config()
        pool_indices = config.ldplayer_pool_indices
        if config.emulator_driver != "ldplayer" or not pool_indices:
            print("LDPlayer pool mode is not configured.", file=sys.stderr)
            return 1

        from .ldplayer import LDPlayerManager

        manager = LDPlayerManager(
            console_path=config.ldplayer_console_path,
            template=config.ldplayer_template,
            state_dir=config.ldplayer_state_dir,
            command_timeout=config.ldplayer_command_timeout_seconds,
            pool_indices=pool_indices,
        )
        if args.command == "list":
            statuses = [_safe_status(value) for value in manager.pool_status()]
            statuses.sort(key=lambda value: value["index"])
            print(json.dumps(statuses, ensure_ascii=False, separators=(",", ":")))
        else:
            manager.close(args.task_id)
            print(json.dumps({"released": args.task_id}, separators=(",", ":")))
        return 0
    except Exception as error:
        # Configuration, process, and SDK errors can contain credentials or
        # private paths. Keep administrator output deliberately fixed.
        print(f"LDPlayer pool command failed ({type(error).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
