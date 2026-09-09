import pytest


@pytest.fixture
def agent_server():
    from agent_fakes import AgentServer
    server = AgentServer()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def configured_environment(monkeypatch):
    """测试使用自己的环境值，避免依赖本机 .env 和真实密钥。"""
    monkeypatch.setenv('MULTIMODAL_BASE_URL', 'https://api.stepfun.com/step_plan/v1')
    monkeypatch.setenv('MULTIMODAL_MODEL', 'step-3.7-flash')
    monkeypatch.setenv('LOGIN_ACCOUNT_NAME', '离落花开花败')
    monkeypatch.delenv('MULTIMODAL_API_KEY', raising=False)
