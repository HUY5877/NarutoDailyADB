import pytest


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
    (tmp_path / '.env').write_text(
        'MULTIMODAL_BASE_URL=https://api.stepfun.com/step_plan/v1\n'
        'MULTIMODAL_MODEL=step-3.7-flash\nMULTIMODAL_API_KEY=test-secret\n'
        'REDIS_URL=redis://test.invalid/0\n', encoding='utf-8')
    for name in (*environment.MULTIMODAL_ENV_NAMES, 'REDIS_URL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LOGIN_ACCOUNT_NAME', '离落花开花败')
    return counter
