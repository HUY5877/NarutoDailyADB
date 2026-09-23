"""Run with python -m naruto_daily.login_service from the repository root."""

import argparse
from pathlib import Path

from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Python 扫码登录任务服务")
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).with_name(".env"),
        help="服务专用环境文件，默认读取本模块旁的 .env；环境变量优先",
    )
    args = parser.parse_args(argv)
    load_dotenv(args.env_file, override=False)
    import uvicorn

    from .app import create_app
    from .config import load_config

    config = load_config()
    # The mock queue lives in this process; multiple ASGI workers cannot share it.
    uvicorn.run(create_app(config=config), host=config.host, port=config.port, workers=1)


if __name__ == "__main__":
    main()
