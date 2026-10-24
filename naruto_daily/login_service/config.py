"""Configuration for the Python login worker service."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

from naruto_daily.common.execution import DAILY_TIMEOUT_SECONDS
from naruto_daily.common.environment import EnvironmentConfigError, require_environment, resolve_app_env
from naruto_daily.common.namespace import (
    DEFAULT_OSS_KEY_PREFIX, DEFAULT_REDIS_QUEUE_KEY, validate_oss_prefix, validate_redis_key,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICE_TOKEN = "local-development-token"


@dataclass(frozen=True)
class ServiceConfig:
    redis_url: str | None = field(default=None, repr=False)
    redis_queue_key: str = DEFAULT_REDIS_QUEUE_KEY
    host: str = "127.0.0.1"
    port: int = 4320
    service_token: str = DEFAULT_SERVICE_TOKEN
    key_prefix: str = DEFAULT_OSS_KEY_PREFIX
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
    run_log_retention_seconds: int = 604_800
    run_log_max_entries: int = 2_000
    sweep_interval_ms: int = 250
    storage_sweep_interval_ms: int = 10_000
    oss_region: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = field(default="", repr=False)
    oss_sts_token: str | None = field(default=None, repr=False)
    qq_bot_app_id: str = ""
    qq_bot_app_secret: str = field(default="", repr=False)
    qq_bot_group_openid: str = ""
    database_url: str | None = field(default=None, repr=False)
    qq_capture_api_base: str | None = None
    app_env: str = "prod"
    sls_enabled: bool = False
    sls_endpoint: str = ""
    sls_project: str = ""
    sls_logstore: str = ""
    sls_access_key_id: str = field(default="", repr=False)
    sls_access_key_secret: str = field(default="", repr=False)
    sls_sts_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Preserve shorter configured/test limits without letting old 7200s
        # settings extend the 50-minute execution ceiling.
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
    if env is None:
        from naruto_daily.common.environment import initialize_environment
        initialize_environment()
        env = os.environ
    app_env = resolve_app_env(env)
    sls_flag = env.get("SLS_ENABLED", "false").strip().lower()
    if sls_flag not in {"true", "false"}:
        raise EnvironmentConfigError("SLS_ENABLED 必须为 true 或 false")
    sls = {"sls_enabled": sls_flag == "true"}
    if sls["sls_enabled"]:
        endpoint = require_environment(env, "SLS_ENDPOINT")
        address = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint)
        if (address.scheme != "https" or address.username or address.password
                or not re.fullmatch(r"[a-z0-9-]+\.log\.aliyuncs\.com", address.netloc)
                or address.path not in {"", "/"} or address.query or address.fragment):
            raise EnvironmentConfigError("SLS_ENDPOINT 必须为阿里云 SLS HTTPS 地域地址")
        sls["sls_endpoint"] = "https://" + address.netloc
        for name in ("SLS_PROJECT", "SLS_LOGSTORE"):
            value = require_environment(env, name)
            if not re.fullmatch(r"[a-z][a-z0-9_-]{1,61}[a-z0-9]", value):
                raise EnvironmentConfigError(f"{name} 格式无效")
            sls[name.lower()] = value
        for name in ("SLS_ACCESS_KEY_ID", "SLS_ACCESS_KEY_SECRET"):
            value = require_environment(env, name)
            if any(character.isspace() or ord(character) < 32 for character in value):
                raise EnvironmentConfigError(f"{name} 格式无效")
            sls[name.lower()] = value
        token = env.get("SLS_STS_TOKEN", "").strip()
        if any(character.isspace() or ord(character) < 32 for character in token):
            raise EnvironmentConfigError("SLS_STS_TOKEN 格式无效")
        sls["sls_sts_token"] = token or None
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
    redis_queue_key = validate_redis_key(require_environment(env, "REDIS_QUEUE_KEY"))
    if sls["sls_enabled"] and not redis_url:
        raise EnvironmentConfigError("启用 SLS 日志同步需要 REDIS_URL")
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
    state_dir = Path(require_environment(env, "LDPLAYER_STATE_DIR"))
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
    key_prefix = validate_oss_prefix(require_environment(env, "OSS_KEY_PREFIX"))
    service_token = env.get("SERVICE_TOKEN", DEFAULT_SERVICE_TOKEN)
    if len(service_token) < 16:
        raise ValueError("SERVICE_TOKEN must contain at least 16 characters")
    host = env.get("HOST", "127.0.0.1")
    if host.lower() not in {"127.0.0.1", "::1", "localhost"} and service_token == DEFAULT_SERVICE_TOKEN:
        raise ValueError("Set a unique SERVICE_TOKEN before binding HOST outside loopback")
    oss = {
        "oss_region": env.get("OSS_REGION", "").strip(),
        "oss_bucket": env.get("OSS_BUCKET", "").strip(),
        "oss_access_key_id": env.get("OSS_ACCESS_KEY_ID", "").strip(),
        "oss_access_key_secret": env.get("OSS_ACCESS_KEY_SECRET", "").strip(),
        "oss_sts_token": env.get("OSS_STS_TOKEN") or None,
    }
    if any(not oss[name] for name in (
        "oss_region", "oss_bucket", "oss_access_key_id", "oss_access_key_secret",
    )):
        raise ValueError("OSS_REGION, OSS_BUCKET, OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET are required for OSS")
    qq = {
        "qq_bot_app_id": env.get("QQ_BOT_APP_ID", "").strip(),
        "qq_bot_app_secret": env.get("QQ_BOT_APP_SECRET", "").strip(),
        "qq_bot_group_openid": env.get("QQ_BOT_GROUP_OPENID", "").strip(),
    }
    if any(qq.values()) and not all(qq.values()):
        raise EnvironmentConfigError("QQ_BOT_APP_ID, QQ_BOT_APP_SECRET and QQ_BOT_GROUP_OPENID must be configured together")
    if qq["qq_bot_app_id"] and not re.fullmatch(r"[0-9]{1,32}", qq["qq_bot_app_id"]):
        raise EnvironmentConfigError("Invalid QQ_BOT_APP_ID")
    if qq["qq_bot_group_openid"] and not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", qq["qq_bot_group_openid"]):
        raise EnvironmentConfigError("Invalid QQ_BOT_GROUP_OPENID")
    if any(ord(char) < 32 for char in qq["qq_bot_app_secret"]):
        raise EnvironmentConfigError("Invalid QQ_BOT_APP_SECRET")
    capture_base = env.get("QQ_CAPTURE_API_BASE", "")
    if capture_base:
        try:
            address = urlsplit(capture_base)
            if (address.scheme != 'https' or not address.hostname or address.username or address.password
                    or address.path not in ('', '/') or address.query or address.fragment
                    or any(character.isspace() or ord(character) < 32 for character in capture_base)
                    or address.port not in (None, 443)):
                raise ValueError
        except ValueError:
            raise EnvironmentConfigError('QQ_CAPTURE_API_BASE 必须为不含路径和凭据的 HTTPS 网站根地址') from None
        capture_base = capture_base.rstrip('/')
    database_url = env.get("DATABASE_URL", "").strip() or None
    if qq["qq_bot_app_id"] and not database_url:
        raise EnvironmentConfigError("请配置 DATABASE_URL，以查询 QQ 通知中的网站用户名")
    if database_url:
        try:
            database_address = urlsplit(database_url)
            if (database_address.scheme not in {"postgres", "postgresql"}
                    or not database_address.hostname or any(ord(char) < 32 for char in database_url)):
                raise ValueError
            database_address.port
        except ValueError:
            raise EnvironmentConfigError("Invalid DATABASE_URL") from None
    return ServiceConfig(
        app_env=app_env,
        redis_url=redis_url,
        redis_queue_key=redis_queue_key,
        host=host,
        port=_integer(env, "PORT", 4320, 0, 65535),
        service_token=service_token,
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
        run_log_retention_seconds=_integer(env, "RUN_LOG_RETENTION_SECONDS", 604_800, 60, 604_800),
        run_log_max_entries=_integer(env, "RUN_LOG_MAX_ENTRIES", 2_000, 100, 10_000),
        ldplayer_package=ldplayer_package,
        ldplayer_command_timeout_seconds=_integer(
            env, "LDPLAYER_COMMAND_TIMEOUT_SECONDS", 20, 1, 300,
        ),
        **oss, **qq, **sls, database_url=database_url, qq_capture_api_base=capture_base or None,
    )
