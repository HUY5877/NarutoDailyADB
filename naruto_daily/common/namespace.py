"""Mandatory namespaces for this test-jev branch, independent of cloud defaults."""

from pathlib import Path
import re


EXPERIMENT_SUFFIX = "test-jev"
DEFAULT_REDIS_QUEUE_KEY = "naruto:login-test-jev:tasks"
DEFAULT_OSS_KEY_PREFIX = "login-test-jev"
ROUTING_COUNTER_KEY = "naruto:multimodal-test-jev:route:sequence"


def _with_suffix(value: str) -> str:
    return value if value.endswith(f"-{EXPERIMENT_SUFFIX}") else f"{value}-{EXPERIMENT_SUFFIX}"


def isolate_redis_queue_key(value: str) -> str:
    """Keep all task-derived keys under an isolated, idempotent namespace.

    Naruto keys use their subsystem segment; arbitrary custom keys use their
    first segment. Never trust REDIS_QUEUE_KEY from shared production KV.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_:{}-]{1,128}", value):
        raise ValueError("Invalid REDIS_QUEUE_KEY")
    parts = value.split(":")
    index = 1 if parts[0] == "naruto" and len(parts) > 1 else 0
    parts[index] = _with_suffix(parts[index])
    result = ":".join(parts)
    if len(result) > 128:
        raise ValueError("Invalid REDIS_QUEUE_KEY (test-jev namespace exceeds 128 characters)")
    return result


def isolate_oss_prefix(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid OSS_KEY_PREFIX")
    parts = value.strip("/").split("/")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", "/".join(parts)):
        raise ValueError("Invalid OSS_KEY_PREFIX")
    parts[0] = _with_suffix(parts[0])
    return "/".join(parts)


def isolate_state_dir(value: Path) -> Path:
    """Separate bindings, locks and run journals even for a shared absolute path."""
    value = Path(value)
    return value if value.name == EXPERIMENT_SUFFIX else value / EXPERIMENT_SUFFIX
