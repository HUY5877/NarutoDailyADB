"""Validate resource names supplied by the selected cloud configuration."""

import re


DEFAULT_REDIS_QUEUE_KEY = 'naruto:login:tasks'
DEFAULT_OSS_KEY_PREFIX = 'login'


def validate_redis_key(value: str, name: str = 'REDIS_QUEUE_KEY') -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_:{}-]{1,128}', value):
        raise ValueError(f'Invalid {name}')
    return value


def validate_oss_prefix(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Invalid OSS_KEY_PREFIX')
    value = value.strip('/')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*', value):
        raise ValueError('Invalid OSS_KEY_PREFIX')
    return value
