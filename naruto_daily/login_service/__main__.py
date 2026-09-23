"""Run with python -m naruto_daily.login_service from the repository root."""

import argparse
import asyncio
import logging
from pathlib import Path
import signal
from urllib.parse import urlsplit

from dotenv import load_dotenv


async def run_worker(config) -> None:
    from .runtime import LoginRuntime

    runtime = LoginRuntime(config)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    signal_installed = False
    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, stopping.set)
            signal_installed = True
        except (NotImplementedError, RuntimeError):
            pass
        await runtime.start()
        address = urlsplit(config.redis_url)
        logging.info(
            "Redis login worker ready: host=%s port=%s db=%s queue=%s concurrency=%s storage=%s emulator=%s",
            address.hostname, address.port or 6379, address.path.lstrip("/") or "0",
            config.redis_queue_key, config.concurrency, config.storage_driver, config.emulator_driver,
        )
        await stopping.wait()
    finally:
        await runtime.stop()
        if signal_installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Python 扫码登录任务服务")
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).with_name(".env"),
        help="服务专用环境文件，默认读取本模块旁的 .env；环境变量优先",
    )
    args = parser.parse_args(argv)
    load_dotenv(args.env_file, override=False)
    from .config import load_config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config()
        if config.redis_url:
            try:
                asyncio.run(run_worker(config))
            except KeyboardInterrupt:
                logging.info("Redis login worker stopped")
            return
    except Exception as error:
        # SDK exceptions and connection URLs can contain credentials.
        logging.error("Login worker could not start or continue (%s); check Redis/OSS configuration", type(error).__name__)
        raise SystemExit(1) from None

    import uvicorn

    from .app import create_app

    logging.info("REDIS_URL is not configured; starting the legacy local HTTP demo")
    # The mock queue lives in this process; multiple ASGI workers cannot share it.
    uvicorn.run(create_app(config=config), host=config.host, port=config.port, workers=1)


if __name__ == "__main__":
    main()
