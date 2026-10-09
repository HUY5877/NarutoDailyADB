"""Run with python -m naruto_daily.login_service from the repository root."""

import argparse
import asyncio
import logging
import os
from pathlib import Path
import signal
from urllib.parse import urlsplit

from naruto_daily.common import environment


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
            "Redis login worker ready: host=%s port=%s db=%s queue=%s concurrency=%s storage=oss emulator=%s",
            address.hostname, address.port or 6379, address.path.lstrip("/") or "0",
            config.redis_queue_key, config.concurrency, config.emulator_driver,
        )
        await stopping.wait()
    finally:
        await runtime.stop()
        if signal_installed:
            loop.remove_signal_handler(signal.SIGTERM)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Python 扫码登录任务服务")
    parser.add_argument(
        "--env-file", type=Path, default=environment.PROJECT_ROOT / ".env",
        help="Cloudflare 连接凭据与机器配置文件，默认项目根目录 .env",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from .config import load_config

    try:
        environment.initialize_environment(args.env_file)
        if not os.environ.get("REDIS_URL", "").strip():
            raise environment.EnvironmentConfigError('请在 Cloudflare KV 配置非空的 REDIS_URL')
        config = load_config()
        from naruto_daily.multimodal.gateway import initialize_gateway
        initialize_gateway()
        try:
            asyncio.run(run_worker(config))
        except KeyboardInterrupt:
            logging.info("Redis login worker stopped")
    except environment.EnvironmentConfigError as error:
        logging.error('启动配置错误：%s', error)
        raise SystemExit(1) from None
    except Exception as error:
        # SDK exceptions and connection URLs can contain credentials.
        logging.error("Login worker could not start or continue (%s); check Redis/OSS configuration", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
