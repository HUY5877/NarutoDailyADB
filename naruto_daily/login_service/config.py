"""Configuration for the Python login worker service."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICE_TOKEN = "local-development-token"


@dataclass(frozen=True)
class ServiceConfig:
    redis_url: str | None = field(default=None, repr=False)
    redis_queue_key: str = "naruto:login:tasks"
    host: str = "127.0.0.1"
    port: int = 4320
    service_token: str = DEFAULT_SERVICE_TOKEN
    storage_driver: str = "local"
    local_storage_dir: Path = PROJECT_ROOT / ".data/login-service/objects"
    key_prefix: str = "login"
    concurrency: int = 2
    max_queue_size: int = 100
    max_tasks: int = 500
    retention_ms: int = 60_000
    step_delay_ms: int = 600
    emulator_driver: str = "mock"
    sweep_interval_ms: int = 250
    storage_sweep_interval_ms: int = 10_000
    oss_region: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_sts_token: str | None = None


def _integer(env: Mapping[str, str], name: str, fallback: int, minimum: int, maximum: int) -> int:
    raw = env.get(name, str(fallback))
    if not re.fullmatch(r"[+-]?\d+", raw):
        raise ValueError(f"Invalid {name}")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def load_config(env: Mapping[str, str] | None = None) -> ServiceConfig:
    env = os.environ if env is None else env
    redis_url = env.get("REDIS_URL", "").strip() or None
    if redis_url:
        try:
            address = urlsplit(redis_url)
            if (address.scheme not in {"redis", "rediss"} or not address.hostname
                    or address.query or address.fragment
                    or not re.fullmatch(r"/\d*", address.path or "/")
                    or (address.port is not None and not 1 <= address.port <= 65535)):
                raise ValueError
        except ValueError:
            raise ValueError("REDIS_URL must be a valid redis:// or rediss:// URL") from None
    redis_queue_key = env.get("REDIS_QUEUE_KEY", "naruto:login:tasks")
    if not re.fullmatch(r"[a-zA-Z0-9_:{}-]{1,128}", redis_queue_key):
        raise ValueError("Invalid REDIS_QUEUE_KEY")
    storage_driver = env.get("STORAGE_DRIVER", "local")
    if storage_driver not in {"local", "oss"}:
        raise ValueError("STORAGE_DRIVER must be local or oss")
    emulator_driver = env.get("EMULATOR_DRIVER", "mock")
    if emulator_driver not in {"mock", "ldplayer"}:
        raise ValueError("EMULATOR_DRIVER must be mock or ldplayer")
    key_prefix = env.get("OSS_KEY_PREFIX", "login").strip("/")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", key_prefix):
        raise ValueError("Invalid OSS_KEY_PREFIX")
    service_token = env.get("SERVICE_TOKEN", DEFAULT_SERVICE_TOKEN)
    if len(service_token) < 16:
        raise ValueError("SERVICE_TOKEN must contain at least 16 characters")
    host = env.get("HOST", "127.0.0.1")
    if host.lower() not in {"127.0.0.1", "::1", "localhost"} and service_token == DEFAULT_SERVICE_TOKEN:
        raise ValueError("Set a unique SERVICE_TOKEN before binding HOST outside loopback")
    oss = {
        "oss_region": env.get("OSS_REGION", ""),
        "oss_bucket": env.get("OSS_BUCKET", ""),
        "oss_access_key_id": env.get("OSS_ACCESS_KEY_ID", ""),
        "oss_access_key_secret": env.get("OSS_ACCESS_KEY_SECRET", ""),
        "oss_sts_token": env.get("OSS_STS_TOKEN") or None,
    }
    if storage_driver == "oss" and any(not oss[name] for name in (
        "oss_region", "oss_bucket", "oss_access_key_id", "oss_access_key_secret",
    )):
        raise ValueError("OSS_REGION, OSS_BUCKET, OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET are required for OSS")
    local_storage_dir = Path(env.get("LOCAL_STORAGE_DIR", str(ServiceConfig.local_storage_dir)))
    if not local_storage_dir.is_absolute():
        local_storage_dir = PROJECT_ROOT / local_storage_dir
    return ServiceConfig(
        redis_url=redis_url,
        redis_queue_key=redis_queue_key,
        host=host,
        port=_integer(env, "PORT", 4320, 0, 65535),
        service_token=service_token,
        storage_driver=storage_driver,
        local_storage_dir=local_storage_dir.resolve(),
        key_prefix=key_prefix,
        concurrency=_integer(env, "WORKER_CONCURRENCY", 2, 1, 16),
        max_queue_size=_integer(env, "MAX_QUEUE_SIZE", 100, 1, 10_000),
        max_tasks=_integer(env, "MAX_TASKS", 500, 1, 100_000),
        retention_ms=_integer(env, "TASK_RETENTION_MS", 60_000, 0, 3_600_000),
        step_delay_ms=_integer(env, "MOCK_STEP_DELAY_MS", 600, 0, 10_000),
        emulator_driver=emulator_driver,
        **oss,
    )
