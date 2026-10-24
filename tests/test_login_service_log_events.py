from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import StringIO
import json
import logging
import sys
import time

import pytest

from naruto_daily.login_service import ldplayer_worker
from naruto_daily.login_service.log_events import (
    ProtocolLogHandler,
    forward_task_logs,
    sanitize_log_message,
)


@pytest.mark.parametrize("message,secrets", [
    ("redis://account:private-password@example.invalid:6379/0", ["account", "private-password"]),
    ("https://account@example.invalid/path", ["account"]),
    ("Bearer abc.xyz-123_=", ["abc.xyz-123_="]),
    ('{"api_key": "private key with spaces", "status": "ok"}', ["private key with spaces"]),
    ("api-key='private value' more text", ["private value"]),
    ("accessToken=abc123&signature=signed-value", ["abc123", "signed-value"]),
    ("Authorization: Basic encoded-private-value", ["encoded-private-value"]),
    ("Cookie: session=private-value; refresh=other-value", ["private-value", "other-value"]),
    ("password=private-value, token=other-value", ["private-value", "other-value"]),
    ("accessKeyId=public-account-id accessKeySecret=private-value", ["public-account-id", "private-value"]),
    ("YUWAN_API_KEY=private-model-value REDIS_PASSWORD=private-redis-value", ["private-model-value", "private-redis-value"]),
    ("API key: private-value", ["private-value"]),
    ("密码=private-value", ["private-value"]),
])
def test_credentials_are_redacted_from_user_logs(message, secrets):
    sanitized = sanitize_log_message(message)
    assert "[REDACTED]" in sanitized
    for secret in secrets:
        assert secret not in sanitized


def test_known_environment_secret_is_redacted_without_label_before_truncation(monkeypatch):
    secret = "private-model-key-value"
    monkeypatch.setenv("YUWAN_API_KEY", secret)
    events = []
    handler = ProtocolLogHandler(events.append)
    record = logging.LogRecord("naruto_daily", logging.INFO, __file__, 1, "x" * 990 + secret + "tail", (), None)
    handler.handle(record)
    message = events[0]["message"]
    assert len(message) == 1_000
    assert "private-" not in message
    assert message.endswith("…")


def test_model_secret_loaded_after_handler_creation_is_still_redacted(monkeypatch):
    monkeypatch.delenv("YUWAN_API_KEY", raising=False)
    events = []
    handler = ProtocolLogHandler(events.append)
    monkeypatch.setenv("YUWAN_API_KEY", "private-key-from-daily-dotenv")
    handler.handle(logging.LogRecord(
        "naruto_daily", logging.INFO, __file__, 1,
        "model response repeated %s", ("private-key-from-daily-dotenv",), None,
    ))
    assert events[0]["message"] == "model response repeated [REDACTED]"


@pytest.mark.parametrize('variable', [
    'YUWAN_API_KEY', 'DEEPSEEK_API_KEY',
])
def test_cloud_model_key_is_redacted_from_protocol_and_diagnostic_logs(tmp_path, monkeypatch, variable):
    monkeypatch.setenv(variable, 'private-cloud-key')
    events, terminal = [], StringIO()
    with forward_task_logs(events.append, diagnostic_stream=terminal):
        logging.getLogger('naruto_daily').info('response echoed private-cloud-key')
    assert events[0]['message'] == 'response echoed [REDACTED]'
    assert terminal.getvalue() == 'INFO response echoed [REDACTED]\n'


def test_log_event_keeps_only_safe_fields_and_normalizes_level_and_time():
    events = []
    handler = ProtocolLogHandler(events.append)
    try:
        raise RuntimeError("private exception detail")
    except RuntimeError:
        record = logging.LogRecord(
            "naruto_daily.runner", logging.CRITICAL, "/private/source.py", 99,
            "执行%s\n第二行\x00", ("失败",), sys.exc_info(),
            sinfo="private stack trace",
        )
    record.created = 1_800_000_000.0
    record.action_index = 2
    record.action_total = 10
    record.api_key = "private extra value"
    handler.handle(record)
    assert events == [{
        "type": "log", "time": "2027-01-15T08:00:00.000Z", "level": "ERROR",
        "message": "具体错误：RuntimeError: private exception detail；执行失败 第二行",
        "actionIndex": 2, "actionTotal": 10,
    }]


def test_execution_checkpoint_marks_original_log_without_changing_event_count_or_diagnostics():
    events, terminal = [], StringIO()
    with forward_task_logs(events.append, diagnostic_stream=terminal):
        logging.getLogger("naruto_daily").info(
            "战斗检查，尚未结束", extra={"execution_checkpoint": True},
        )

    assert len(events) == 1
    assert events[0]["type"] == "log"
    assert events[0]["level"] == "INFO"
    assert events[0]["message"] == "战斗检查，尚未结束"
    assert events[0]["executionCheckpoint"] is True
    assert "execution_checkpoint" not in events[0]
    assert "actionIndex" not in events[0]
    assert terminal.getvalue() == "INFO 战斗检查，尚未结束\n"


@pytest.mark.parametrize("marker", [None, False, 1, "true", [], {}])
def test_execution_checkpoint_requires_an_explicit_true_marker(marker):
    events = []
    record = logging.LogRecord("naruto_daily", logging.INFO, __file__, 1, "战斗检查", (), None)
    record.execution_checkpoint = marker

    ProtocolLogHandler(events.append).handle(record)

    assert len(events) == 1 and events[0]["type"] == "log"
    assert "executionCheckpoint" not in events[0]


def test_unmarked_battle_diagnostics_do_not_create_execution_checkpoints():
    events = []
    with forward_task_logs(events.append):
        logging.getLogger("naruto_daily").info("[战斗检查 第1轮] 已停止全部连点")

    assert len(events) == 1 and events[0]["type"] == "log"
    assert "executionCheckpoint" not in events[0]


def test_backend_only_record_keeps_its_original_protocol_exclusion_with_checkpoint_marker():
    events = []
    record = logging.LogRecord("naruto_daily", logging.INFO, __file__, 1, "内部诊断", (), None)
    record.backend_only = True
    record.execution_checkpoint = True

    ProtocolLogHandler(events.append).handle(record)

    assert events == []


@pytest.mark.parametrize('marker, level, expected', [
    (True, logging.INFO, True), (False, logging.INFO, False),
    (1, logging.INFO, False), ('true', logging.INFO, False),
    (True, logging.ERROR, False),
])
def test_secret_realm_milestone_uses_the_public_log_timestamp(marker, level, expected):
    events = []
    handler = ProtocolLogHandler(events.append)
    record = logging.LogRecord('naruto_daily', level, __file__, 1, '秘境成功结算', (), None)
    record.secret_realm_completed = marker
    handler.handle(record)
    assert ('secretRealmCompletedAt' in events[0]) is expected
    if expected:
        assert events[0]['secretRealmCompletedAt'] == events[0]['time']
        assert 'actionIndex' not in events[0]


def test_local_diagnostics_keep_tracebacks_and_web_logs_receive_sanitized_causes():
    events, terminal = [], StringIO()
    secret = "local-diagnostic-test-secret"
    long_message = "识别进度" * 400
    with forward_task_logs(events.append, diagnostic_stream=terminal, secrets=[secret]):
        logger = logging.getLogger("naruto_daily")
        logger.info(long_message)
        try:
            try:
                raise ValueError("模型网络异常 " + secret)
            except ValueError as error:
                raise RuntimeError("一乐外卖多次识别失败") from error
        except RuntimeError:
            logger.exception("执行阶段 daily 失败")
    output = terminal.getvalue()
    assert long_message in output
    assert "Traceback (most recent call last)" in output
    assert "ValueError: 模型网络异常 [REDACTED]" in output
    assert "RuntimeError: 一乐外卖多次识别失败" in output
    assert secret not in output
    assert len(events[0]["message"]) == 1000
    assert events[-1]["message"] == (
        "具体错误：RuntimeError: 一乐外卖多次识别失败；原因：ValueError: 模型网络异常 [REDACTED]"
        "；执行阶段 daily 失败"
    )
    assert "Traceback" not in json.dumps(events)
    assert secret not in json.dumps(events, ensure_ascii=False)
    assert __file__ not in json.dumps(events, ensure_ascii=False)


@pytest.mark.parametrize("index,total", [(True, 5), (1, True), (-1, 5), (6, 5), ("1", 5), (1, None)])
def test_malformed_progress_is_omitted(index, total):
    events = []
    record = logging.LogRecord("naruto_daily", logging.INFO, __file__, 1, "message", (), None)
    record.action_index = index
    record.action_total = total
    ProtocolLogHandler(events.append).handle(record)
    assert "actionIndex" not in events[0]
    assert "actionTotal" not in events[0]


def test_logging_sink_and_format_errors_do_not_interrupt_execution(capsys):
    def broken_sink(event):
        raise BrokenPipeError("private output details")

    with forward_task_logs(broken_sink):
        logging.getLogger("naruto_daily").info("safe task message")
        logging.getLogger("naruto_daily").info("mismatched %d", "private malformed value")
    assert capsys.readouterr().err == ""


def test_namespace_capture_is_isolated_and_restores_existing_logger_state():
    events = []
    logger = logging.getLogger("naruto_daily")
    child = logging.getLogger("naruto_daily.existing_test_child")
    outside = logging.getLogger("unrelated_test_logger")
    # Preserve pytest's/global application logging state even if an assertion fails.
    originals = [(item, item.level, item.disabled, item.propagate, item.handlers[:])
                 for item in (logger, child, outside)]
    original_stream = StringIO()
    existing_handler = logging.StreamHandler(original_stream)
    try:
        logger.setLevel(logging.ERROR)
        child.setLevel(logging.CRITICAL)
        child.disabled = True
        child.propagate = False
        child.handlers = [existing_handler]
        outside.handlers = [existing_handler]
        outside.setLevel(logging.INFO)
        outside.propagate = False
        with pytest.raises(RuntimeError, match="task failed"):
            with forward_task_logs(events.append):
                logger.debug("hidden debug")
                logger.info("parent message")
                child.warning("child message")
                outside.info("unrelated message")
                raise RuntimeError("task failed")
        assert [(event["level"], event["message"]) for event in events] == [
            ("INFO", "parent message"), ("WARNING", "child message"),
        ]
        assert original_stream.getvalue() == "unrelated message\n"
        assert logger.level == logging.ERROR
        assert child.level == logging.CRITICAL
        assert child.disabled is True
        assert child.propagate is False
        assert child.handlers == [existing_handler]
        with forward_task_logs(events.append):
            logger.info("second task")
        assert [event["message"] for event in events].count("second task") == 1
    finally:
        for item, level, disabled, propagate, handlers in originals:
            item.setLevel(level)
            item.disabled = disabled
            item.propagate = propagate
            item.handlers = handlers


def test_action_runner_reports_progress_without_changing_actions():
    from naruto_daily.runner import ActionRunner

    events = []
    with forward_task_logs(events.append):
        summary = ActionRunner(object(), dry_run=True).run([
            {"type": "tap", "x": 100, "y": 200},
            {"type": "wait", "seconds": 1},
        ])
    assert summary.action_count == 2
    assert [(event["actionIndex"], event["actionTotal"])
            for event in events if "actionIndex" in event] == [(0, 2), (1, 2), (2, 2), (2, 2)]


def test_worker_forwards_logs_throughout_lifecycle_and_keeps_prints_off_protocol(monkeypatch, tmp_path, capsys):
    now = 1_800_000_000.0
    logger = logging.getLogger("naruto_daily.worker_test")
    output = StringIO()
    png = b"\x89PNG\r\n\x1a\nfixture"

    class DriverError(RuntimeError):
        pass

    class Manager:
        adb_path = tmp_path / "adb.exe"

        def capture_apps_installed(self, instance, deadline):
            return False

        def __init__(self, **kwargs):
            logger.info("initialize manager")

        def allocate(self, task_id, deadline):
            print("legacy allocate print")
            logger.info("allocate emulator")
            return "instance"

        def start(self, instance, deadline):
            return "emulator-5554"

        def launch_game(self, instance, package, deadline):
            logger.info("launch game")

    class Device:
        def __init__(self, *args, game_package, login_type):
            assert game_package == "test.game"

    def fetch(device):
        logger.info("fetch qr")
        return png

    def load_dependencies():
        print("legacy dependency print")
        logger.info("import dependencies")
        return Manager, DriverError, Device, fetch

    class DailySession:
        def __init__(self, **kwargs):
            pass

        def wait_for_authorization(self, deadline):
            assert json.loads(output.getvalue().splitlines()[-1])["type"] == "qr"
            logger.info("wait for scan")

        def finish_login(self, deadline):
            assert json.loads(output.getvalue().splitlines()[-1])["type"] == "authorized"
            logger.info("finish login")

        def run_daily(self, deadline):
            from naruto_daily.runner import ActionRunner

            assert json.loads(output.getvalue().splitlines()[-1])["type"] == "logged_in"
            ActionRunner(object(), dry_run=True).run([{"type": "back"}])
            return 1

    monkeypatch.setattr(ldplayer_worker, "_load_dependencies", load_dependencies)
    monkeypatch.setattr(ldplayer_worker, "_load_daily_session", lambda: DailySession)
    message = {
        "type": "start", "id": "11111111-1111-4111-8111-111111111111",
        "expiresAt": datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
        "consolePath": str(tmp_path / "console.exe"), "template": "test-template",
        "stateDir": str(tmp_path), "package": "test.game", "commandTimeout": 20,
        "dailyConfigPath": str(tmp_path / "daily.json"), "dailyTimeoutSeconds": 600,
    }
    result = ldplayer_worker.main(
        input_stream=StringIO(json.dumps(message) + "\n"), output_stream=output,
        clock=lambda: now, use_watchdog=False,
    )
    assert result == 0
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["type"] for event in events if event["type"] != "log"] == [
        "stage", "stage", "stage", "qr", "authorized", "logged_in", "daily_completed",
    ]
    assert [event["message"] for event in events if event["type"] == "log"][:7] == [
        "import dependencies", "多模态网关已初始化：active_channels=yuwan",
        "initialize manager", "allocate emulator", "launch game",
        "fetch qr", "wait for scan",
    ]
    assert any(event.get("actionIndex") == 1 and event.get("actionTotal") == 1 for event in events)
    terminal = capsys.readouterr().err
    assert "legacy dependency print" in terminal and "legacy allocate print" in terminal
    assert "INFO import dependencies" in terminal
    assert "INFO fetch qr" in terminal and "INFO finish login" in terminal
    assert "INFO 动作 1/1：返回" in terminal


def test_log_and_stage_events_share_an_atomic_output_writer():
    class SlowStream:
        def __init__(self):
            self.fragments = []

        def write(self, value):
            midpoint = len(value) // 2
            self.fragments.append(value[:midpoint])
            time.sleep(0.001)
            self.fragments.append(value[midpoint:])

        def flush(self):
            pass

    stream = SlowStream()
    handler = ProtocolLogHandler(lambda event: ldplayer_worker._send(stream, event))

    def send_event(index):
        if index % 2:
            handler.handle(logging.LogRecord("naruto_daily", logging.INFO, __file__, 1,
                                             "log %d", (index,), None))
        else:
            ldplayer_worker._send(stream, {"type": "stage", "status": "starting_emulator"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(send_event, range(40)))
    events = [json.loads(line) for line in "".join(stream.fragments).splitlines()]
    assert len(events) == 40
    assert sum(event["type"] == "log" for event in events) == 20


def test_exception_summary_redacts_credentials_before_bounding_long_context_and_cause(monkeypatch):
    secret = "private-exception-api-key"
    monkeypatch.setenv("YUWAN_API_KEY", secret)
    events = []
    try:
        try:
            raise ValueError("模型请求被拒绝：" + secret + " token=other-private-token " + "响应" * 1000)
        except ValueError as error:
            raise RuntimeError("装备材料识别失败 " + "上下文" * 1000) from error
    except RuntimeError:
        record = logging.LogRecord(
            "naruto_daily", logging.ERROR, "/private/source.py", 1,
            "任务诊断" * 1000 + secret, (), sys.exc_info(), sinfo="private stack trace",
        )
    ProtocolLogHandler(events.append).handle(record)
    message = events[0]["message"]
    assert len(message) <= 1000
    assert message.startswith("具体错误：RuntimeError: 装备材料识别失败")
    assert "原因：ValueError: 模型请求被拒绝：[REDACTED] token=[REDACTED]" in message
    assert secret not in message and "private-exception" not in message
    assert "other-private-token" not in message
    assert "source.py" not in message and "private stack trace" not in message


@pytest.mark.parametrize("error,expected", [
    (TimeoutError(), "TimeoutError"),
    (RuntimeError("token=private-token"), "RuntimeError: token=[REDACTED]"),
])
def test_empty_or_credential_bearing_exception_still_provides_a_safe_summary(error, expected):
    events = []
    ProtocolLogHandler(events.append).handle(logging.LogRecord(
        "naruto_daily", logging.ERROR, __file__, 1, "执行阶段 daily 失败", (),
        (type(error), error, None),
    ))
    assert events[0]["message"] == f"具体错误：{expected}；执行阶段 daily 失败"


def test_unprintable_exception_and_cyclic_causes_do_not_break_logging():
    class UnprintableError(RuntimeError):
        def __str__(self):
            raise ValueError("private formatting failure")

    error = UnprintableError()
    cause = TimeoutError("模型请求超时")
    error.__cause__ = cause
    cause.__cause__ = error
    events = []
    ProtocolLogHandler(events.append).handle(logging.LogRecord(
        "naruto_daily", logging.ERROR, __file__, 1, "执行阶段 daily 失败", (),
        (type(error), error, None),
    ))
    assert events[0]["message"] == (
        "具体错误：UnprintableError: 异常信息无法读取；原因：TimeoutError: 模型请求超时；执行阶段 daily 失败"
    )


def test_suppressed_exception_context_is_not_exposed():
    events = []
    try:
        try:
            raise ValueError("suppressed internal context")
        except ValueError:
            raise RuntimeError("截图失败") from None
    except RuntimeError:
        record = logging.LogRecord("naruto_daily", logging.ERROR, __file__, 1, "任务失败", (), sys.exc_info())
    ProtocolLogHandler(events.append).handle(record)
    assert events[0]["message"] == "具体错误：RuntimeError: 截图失败；任务失败"
