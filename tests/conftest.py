import pytest


@pytest.fixture
def reward_swipe_vision(monkeypatch):
    """Legacy navigation state tests use symbolic frames; real CV has replay tests."""
    from naruto_daily.common import navigation
    monkeypatch.setattr(navigation, 'reward_card_swipe_starts', lambda png: [(1450, 550)])
    monkeypatch.setattr(navigation, 'reward_card_displacement', lambda before, after: -520.0)


@pytest.fixture
def agent_server():
    from agent_fakes import AgentServer
    server = AgentServer()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def configured_environment(monkeypatch, tmp_path):
    """每个测试使用独立项目根目录，绝不读取本机 .env 或真实密钥。"""
    from naruto_daily.common import environment
    from naruto_daily.multimodal import gateway
    from threading import Lock

    class Counter:
        def __init__(self):
            self.value = 0
            self.keys = []
            self.lock = Lock()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def incr(self, key):
            with self.lock:
                self.keys.append(key)
                self.value += 1
                return self.value

    counter = Counter()

    class AsyncCounter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def incr(self, key):
            return counter.incr(key)

    monkeypatch.setattr(gateway, '_gateway', None)
    monkeypatch.setattr(gateway, '_redis_client', lambda _: counter)
    monkeypatch.setattr(gateway, '_async_redis_client', lambda _: AsyncCounter())

    monkeypatch.setattr(environment, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(environment, '_initialized', True)
    (tmp_path / '.env').write_text('', encoding='utf-8')
    for name in (*environment.CLOUD_ENV_NAMES, environment.INHERITED_CONFIG_MARKER,
                 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_KV_NAMESPACE_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('YUWAN_MODEL', 'step-3.7-flash')
    monkeypatch.setenv('YUWAN_API_KEY', 'test-secret')
    monkeypatch.setenv('REDIS_URL', 'redis://test.invalid/0')
    monkeypatch.setenv('LOGIN_ACCOUNT_NAME', '离落花开花败')
    return counter
