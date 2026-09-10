import pytest

from naruto_daily import cli
from naruto_daily import launcher


@pytest.mark.parametrize("interrupted", [False, True])
def test_ctrl_c_exits_without_waiting_for_input(monkeypatch, capsys, interrupted):
    def run(argv):
        assert argv == ["run", "secret_realm_farm", "--config", "config.local.json"]
        if interrupted:
            raise KeyboardInterrupt
        print("已停止。")
        return 130

    monkeypatch.setattr(cli, "main", run)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Ctrl+C 后不能等待确认"))

    assert launcher.main(["run", "secret_realm_farm", "--config", "config.local.json"]) == 130
    assert "已停止" in capsys.readouterr().out


@pytest.mark.parametrize("exit_code", [0, 2])
def test_completion_and_error_keep_logs_visible(monkeypatch, exit_code):
    prompts = []
    monkeypatch.setattr(cli, "main", lambda argv: exit_code)
    monkeypatch.setattr("builtins.input", prompts.append)

    assert launcher.main(["run", "all"]) == exit_code
    assert len(prompts) == 1


def test_ctrl_c_while_waiting_for_close_also_exits(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda argv: 0)

    def interrupt(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert launcher.main(["run", "all"]) == 130


def test_unexpected_error_keeps_traceback_visible(monkeypatch, capsys):
    prompts = []

    def fail(argv):
        raise RuntimeError("test failure")

    monkeypatch.setattr(cli, "main", fail)
    monkeypatch.setattr("builtins.input", prompts.append)

    assert launcher.main(["run", "all"]) == 1
    assert "RuntimeError: test failure" in capsys.readouterr().err
    assert len(prompts) == 1
