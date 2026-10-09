"""The command-line entrypoint must require Redis and never start a web demo."""

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto_daily.login_service import __main__ as entrypoint
from naruto_daily.login_service import config as service_config


@pytest.fixture(autouse=True)
def isolated_entrypoint(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)

    def unexpected_web_start(*args, **kwargs):
        pytest.fail("The worker entrypoint must never initialize a local web service")

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=unexpected_web_start))
    monkeypatch.setitem(
        sys.modules,
        "naruto_daily.login_service.app",
        SimpleNamespace(create_app=unexpected_web_start),
    )


@pytest.mark.parametrize("redis_url", [None, "", " \t\n"])
def test_missing_redis_fails_before_loading_config_or_starting_worker(
    monkeypatch, caplog, redis_url
):
    if redis_url is not None:
        monkeypatch.setenv("REDIS_URL", redis_url)

    def unexpected_initialization(*args, **kwargs):
        pytest.fail("Missing REDIS_URL must fail before initializing the worker")

    monkeypatch.setattr(service_config, "load_config", unexpected_initialization)
    monkeypatch.setattr(entrypoint, "run_worker", unexpected_initialization)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as error:
        entrypoint.main([])

    assert error.value.code == 1
    assert "REDIS_URL" in caplog.text


@pytest.mark.parametrize("source", ["environment", "cloud"])
def test_configured_redis_runs_worker(monkeypatch, source):
    redis_url = "redis://localhost:6379/0"
    config = SimpleNamespace(redis_url=redis_url)
    calls = []
    env_file = Path("entrypoint-test.env")

    def fake_initialize(path=None):
        assert path is None or path == env_file
        if source == "cloud":
            monkeypatch.setenv("REDIS_URL", redis_url)

    async def fake_run_worker(actual_config):
        calls.append(actual_config)

    if source == "environment":
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setattr(entrypoint.environment, "initialize_environment", fake_initialize)
    monkeypatch.setattr(service_config, "load_config", lambda: config)
    monkeypatch.setattr(entrypoint, "run_worker", fake_run_worker)

    entrypoint.main(["--env-file", str(env_file)])

    assert calls == [config]


def test_default_entrypoint_loads_bootstrap_from_project_root(monkeypatch, tmp_path):
    from naruto_daily.common import cloudflare_kv

    (tmp_path / '.env').write_text('CLOUDFLARE_API_TOKEN=root-token\n', encoding='utf-8-sig')
    elsewhere = tmp_path / 'other-directory'
    elsewhere.mkdir()
    (elsewhere / '.env').write_text('CLOUDFLARE_API_TOKEN=wrong-token', encoding='utf-8')
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(entrypoint.environment, '_initialized', False)
    # The initializer mutates only this test's environment mapping.
    monkeypatch.setattr(os, 'environ', dict(os.environ))

    def fetch(settings):
        assert settings['CLOUDFLARE_API_TOKEN'] == 'root-token'
        return {'REDIS_URL': 'redis://cloud.invalid/3',
                'YUWAN_API_KEY': 'cloud-key', 'YUWAN_MODEL': 'cloud-model'}

    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', fetch)
    monkeypatch.setattr(service_config, 'load_config', lambda: SimpleNamespace(redis_url=os.environ['REDIS_URL']))
    calls = []

    async def run(config):
        calls.append(config.redis_url)

    monkeypatch.setattr(entrypoint, 'run_worker', run)
    entrypoint.main([])
    assert calls == ['redis://cloud.invalid/3']


def test_keyboard_interrupt_stops_worker_normally(monkeypatch, caplog):
    config = SimpleNamespace(redis_url="redis://localhost:6379/0")
    monkeypatch.setenv("REDIS_URL", config.redis_url)
    monkeypatch.setattr(service_config, "load_config", lambda: config)

    async def interrupted_worker(actual_config):
        raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint, "run_worker", interrupted_worker)

    with caplog.at_level(logging.INFO):
        entrypoint.main([])

    assert "stopped" in caplog.text


def test_worker_failure_exits_without_logging_credentials(monkeypatch, caplog):
    redis_url = "redis://worker:entrypoint-test-secret@localhost:6379/0"
    config = SimpleNamespace(redis_url=redis_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setattr(service_config, "load_config", lambda: config)

    async def failing_worker(actual_config):
        raise RuntimeError(f"Could not connect to {actual_config.redis_url}")

    monkeypatch.setattr(entrypoint, "run_worker", failing_worker)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as error:
        entrypoint.main([])

    assert error.value.code == 1
    assert "RuntimeError" in caplog.text
    assert "entrypoint-test-secret" not in caplog.text
    assert redis_url not in caplog.text


def test_missing_channels_fails_at_startup_before_worker_runs(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('YUWAN_API_KEY')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://test.invalid/0')
    monkeypatch.setattr(service_config, 'load_config', lambda: SimpleNamespace(redis_url='redis://test.invalid/0'))

    async def forbidden_worker(config):
        pytest.fail('No worker or provider request should run without configured channels')

    monkeypatch.setattr(entrypoint, 'run_worker', forbidden_worker)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as error:
        entrypoint.main([])
    assert error.value.code == 1
    assert 'YUWAN_API_KEY' in caplog.text
