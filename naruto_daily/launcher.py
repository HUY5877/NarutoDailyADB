"""双击启动脚本使用的 Python 控制台入口。"""

from __future__ import annotations

import traceback
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    try:
        print("请先打开模拟器并停留在第一张登录页面。按 Ctrl+C 直接停止。")
        try:
            # 导入也在保护范围内：依赖缺失时保留错误日志，中断时直接退出。
            from naruto_daily.cli import main as cli_main

            exit_code = cli_main(argv)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        except Exception:
            traceback.print_exc()
            exit_code = 1
        if exit_code == 130:
            return exit_code
        if exit_code == 0:
            print("流程已结束。")
        else:
            print(f"流程已停止，退出码：{exit_code}。请查看上方错误信息。")
        try:
            input("按回车关闭窗口。")
        except EOFError:
            pass
        return exit_code
    except KeyboardInterrupt:
        print("\n已停止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
