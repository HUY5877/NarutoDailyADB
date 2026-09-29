"""Configuration for the Python login worker service."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

from naruto_daily.common.execution import DAILY_TIMEOUT_SECONDS


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
    ldplayer_console_path: Path | None = None
    ldplayer_template: str = ""
    ldplayer_pool_indices: tuple[int, ...] = ()
    ldplayer_state_dir: Path = PROJECT_ROOT / ".data/login-service/ldplayer"
    ldplayer_package: str = "com.tencent.KiHan"
    ldplayer_command_timeout_seconds: int = 20
    daily_config_path: Path = PROJECT_ROOT / "config.example.json"
    daily_timeout_seconds: int = DAILY_TIMEOUT_SECONDS
    run_log_retention_seconds: int = 86_400
    run_log_max_entries: int = 2_000
    sweep_interval_ms: int = 250
    storage_sweep_interval_ms: int = 10_000
    oss_region: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = field(default="", repr=False)
    oss_sts_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Preserve shorter configured/test limits without letting old 7200s
        # settings extend the 25-minute execution ceiling.
        object.__setattr__(self, "daily_timeout_seconds", min(self.daily_timeout_seconds, DAILY_TIMEOUT_SECONDS))


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
    console_value = env.get("LDPLAYER_CONSOLE_PATH", "").strip()
    ldplayer_console_path = Path(console_value) if console_value else None
    if ldplayer_console_path is not None and not ldplayer_console_path.is_absolute():
        ldplayer_console_path = PROJECT_ROOT / ldplayer_console_path
    if ldplayer_console_path is not None:
        ldplayer_console_path = ldplayer_console_path.resolve()
    ldplayer_template = env.get("LDPLAYER_TEMPLATE", "").strip()
    pool_value = env.get("LDPLAYER_POOL_INDICES", "").strip()
    if pool_value:
        pool_parts = [part.strip() for part in pool_value.split(",")]
        if any(not re.fullmatch(r"\d+", part) for part in pool_parts):
            raise ValueError("Invalid LDPLAYER_POOL_INDICES")
        ldplayer_pool_indices = tuple(int(part) for part in pool_parts)
        if len(set(ldplayer_pool_indices)) != len(ldplayer_pool_indices):
            raise ValueError("Invalid LDPLAYER_POOL_INDICES")
    else:
        ldplayer_pool_indices = ()
    state_dir = Path(env.get(
        "LDPLAYER_STATE_DIR", str(ServiceConfig.ldplayer_state_dir),
    ))
    if not state_dir.is_absolute():
        state_dir = PROJECT_ROOT / state_dir
    state_dir = state_dir.resolve()
    ldplayer_package = env.get("LDPLAYER_PACKAGE", "com.tencent.KiHan").strip()
    daily_value = env.get("DAILY_CONFIG_PATH", str(ServiceConfig.daily_config_path)).strip()
    if not daily_value:
        raise ValueError("DAILY_CONFIG_PATH must name the daily task configuration")
    daily_config_path = Path(daily_value)
    if not daily_config_path.is_absolute():
        daily_config_path = PROJECT_ROOT / daily_config_path
    daily_config_path = daily_config_path.resolve()
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", ldplayer_package):
        raise ValueError("Invalid LDPLAYER_PACKAGE")
    if emulator_driver == "ldplayer":
        if ldplayer_console_path is None or not ldplayer_console_path.is_file():
            raise ValueError("LDPLAYER_CONSOLE_PATH must name an existing file")
        if (not ldplayer_pool_indices
                and (not ldplayer_template or any(ord(character) < 32 for character in ldplayer_template))):
            raise ValueError("LDPLAYER_TEMPLATE must name a clean stopped template")
        if ldplayer_pool_indices and redis_url is None:
            raise ValueError("LDPLAYER_POOL_INDICES requires REDIS_URL worker mode")
        if not daily_config_path.is_file():
            raise ValueError("DAILY_CONFIG_PATH must name an existing JSON configuration")
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
        ldplayer_console_path=ldplayer_console_path,
        ldplayer_template=ldplayer_template,
        ldplayer_pool_indices=ldplayer_pool_indices,
        ldplayer_state_dir=state_dir,
        daily_config_path=daily_config_path,
        daily_timeout_seconds=_integer(env, "DAILY_TIMEOUT_SECONDS", DAILY_TIMEOUT_SECONDS, 1, 86_400),
        run_log_retention_seconds=_integer(env, "RUN_LOG_RETENTION_SECONDS", 86_400, 60, 604_800),
        run_log_max_entries=_integer(env, "RUN_LOG_MAX_ENTRIES", 2_000, 100, 10_000),
        ldplayer_package=ldplayer_package,
        ldplayer_command_timeout_seconds=_integer(
            env, "LDPLAYER_COMMAND_TIMEOUT_SECONDS", 20, 1, 300,
        ),
        **oss,
    )
