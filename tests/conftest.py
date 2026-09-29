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

    monkeypatch.setattr(environment, 'PROJECT_ROOT', tmp_path)
    (tmp_path / '.env').write_text(
        'MULTIMODAL_BASE_URL=https://api.stepfun.com/step_plan/v1\n'
        'MULTIMODAL_MODEL=step-3.7-flash\nMULTIMODAL_API_KEY=test-secret\n', encoding='utf-8')
    for name in ('MULTIMODAL_API_KEY', 'MULTIMODAL_BASE_URL', 'MULTIMODAL_MODEL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LOGIN_ACCOUNT_NAME', '离落花开花败')
