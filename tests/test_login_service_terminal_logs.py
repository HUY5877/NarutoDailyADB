"""Local stderr diagnostics are drained independently of the public protocol."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import logging
import sys
from uuid import uuid4

import pytest

from naruto_daily.login_service.config import ServiceConfig
from naruto_daily.login_service.models import parse_submission
from naruto_daily.login_service.runtime import LoginRuntime, _WorkerError


PNG = b"\x89PNG\r\n\x1a\nfixture"
LOGGER = "naruto_daily.login_service.runtime"
SUCCESS_PROTOCOL = [
    {"type": "qr", "png": base64.b64encode(PNG).decode("ascii")},
    {"type": "authorized"}, {"type": "logged_in"},
    {"type": "log", "time": "2026-09-24T12:00:00.000Z", "level": "INFO",
     "message": "公开动作节点", "actionIndex": 1, "actionTotal": 1},
    {"type": "daily_completed", "actionCount": 1},
]


class MemoryStorage:
    async def put(self, task_id, png, expires_at):
        assert png == PNG

    async def delete(self, task_id):
        pass


class Reporter:
    def __init__(self):
        self.events = []

    def emit(self, record, **event):
        self.events.append(event)

    async def close(self):
        pass


def make_runtime(tmp_path):
    reporter = Reporter()
    runtime = LoginRuntime(ServiceConfig(
        ldplayer_state_dir=tmp_path / "state", daily_timeout_seconds=10,
        oss_access_key_secret="private-oss-key", oss_sts_token="private-sts-key",
    ), MemoryStorage(), run_reporter=reporter)
    now = datetime.now(timezone.utc)
    record = parse_submission({
        "id": str(uuid4()), "accessToken": "a" * 64,
        "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(seconds=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    })
    return runtime, reporter, record


def local_messages(caplog, task_id):
    return [record.getMessage() for record in caplog.records if f"Task {task_id} |" in record.getMessage()]


def no_reader_tasks():
    return not any(task.get_name().startswith("login-stderr:") and not task.done() for task in asyncio.all_tasks())


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_large_stderr_is_drained_with_full_chinese_text_and_never_sent_to_redis(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setenv("MULTIMODAL_API_KEY", "private-model-key")
    (tmp_path / '.env').write_text('MULTIMODAL_API_KEY=private-root-key', encoding='utf-8')
    program = r'''
import json,os,sys
message=json.loads(sys.stdin.readline())
print('INFO encoding='+os.environ['PYTHONIOENCODING'], file=sys.stderr, flush=True)
for index in range(400):
    print('INFO detail-burst-%d '%index + 'x'*1200, file=sys.stderr)
print(message['ordinary'], file=sys.stderr)
print('WARNING '+message['warning'], file=sys.stderr)
print('ERROR '+message['error'], file=sys.stderr)
assert 'MULTIMODAL_API_KEY' not in os.environ
print('INFO known-secret=private-model-key private-root-key private-oss-key private-sts-key', file=sys.stderr)
print('INFO '+message['public'], file=sys.stderr)
sys.stderr.buffer.write(b'invalid-utf8: \xff\xfe\n')
print('INFO '+'L'*1600+'full-line-tail', file=sys.stderr, flush=True)
for event in message['events']:
    print(json.dumps(event),flush=True)
'''

    async def scenario():
        runtime, reporter, record = make_runtime(tmp_path)
        result = await asyncio.wait_for(runtime._run_child(record, [sys.executable, "-u", "-c", program], {
            "events": SUCCESS_PROTOCOL, "ordinary": "普通print中文排查信息", "warning": "识别重试警告",
            "error": "示例诊断错误", "public": "公开动作节点",
        }, daily=True), 8)
        assert result == PNG
        messages = local_messages(caplog, record.id)
        assert sum("detail-burst-" in item for item in messages) == 400
        assert any("encoding=utf-8" in item for item in messages)
        assert any("普通print中文排查信息" in item for item in messages)
        assert any("invalid-utf8: ��" in item for item in messages)
        assert any(len(item) > 1600 and item.endswith("full-line-tail") for item in messages)
        assert sum("公开动作节点" in item for item in messages) == 1
        assert all("private-model-key" not in item and "private-oss-key" not in item and "private-sts-key" not in item for item in messages)
        assert all("private-root-key" not in item for item in messages)
        assert any(item.levelno == logging.WARNING and "识别重试警告" in item.getMessage() for item in caplog.records)
        assert any(item.levelno == logging.ERROR and "示例诊断错误" in item.getMessage() for item in caplog.records)
        assert all("detail-burst" not in event.get("message", "") and "普通print" not in event.get("message", "")
                   for event in reporter.events)
        assert sum(event.get("message") == "公开动作节点" for event in reporter.events) == 1
        child = runtime._children[record.id]
        assert child.poll() == 0 and child.stderr.closed
        assert no_reader_tasks()
    asyncio.run(scenario())


def test_failure_traceback_and_final_unterminated_line_are_visible_and_redacted(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    program = r'''
import json,sys,traceback
json.loads(sys.stdin.readline())
try:
    raise RuntimeError('failure password=private-password')
except RuntimeError:
    traceback.print_exc(file=sys.stderr)
print('    final diagnostic without newline', file=sys.stderr, end='', flush=True)
print(json.dumps({'type':'error','code':'daily_failed'}),flush=True)
'''

    async def scenario():
        runtime, reporter, record = make_runtime(tmp_path)
        with pytest.raises(_WorkerError):
            await runtime._run_child(record, [sys.executable, "-u", "-c", program], {}, daily=True)
        messages = local_messages(caplog, record.id)
        assert any("Traceback (most recent call last):" in item for item in messages)
        assert any("RuntimeError: failure password=[REDACTED]" in item for item in messages)
        assert any(item.endswith("|     final diagnostic without newline") for item in messages)
        assert not any("private-password" in item for item in messages)
        assert all("Traceback" not in event.get("message", "") and "final diagnostic" not in event.get("message", "")
                   for event in reporter.events)
        assert runtime._children[record.id].stderr.closed
        assert no_reader_tasks()
    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["stop", "cancel"])
def test_worker_shutdown_or_cancellation_reaps_the_stderr_reader(tmp_path, caplog, finish):
    caplog.set_level(logging.INFO, logger=LOGGER)
    program = "import json,sys,time\njson.loads(sys.stdin.readline())\nprint('INFO child is running',file=sys.stderr,flush=True)\ntime.sleep(60)\n"

    async def scenario():
        runtime, _, record = make_runtime(tmp_path)
        work = asyncio.create_task(runtime._run_child(record, [sys.executable, "-u", "-c", program], {}, daily=True))
        runtime._workers.append(work)
        try:
            await eventually(lambda: any("child is running" in item for item in local_messages(caplog, record.id)))
            child = runtime._children[record.id]
            if finish == "stop":
                await asyncio.wait_for(runtime.stop(), 2)
            else:
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(work, 2)
            assert work.done() and child.poll() is not None
            assert child.stderr.closed
            assert no_reader_tasks()
        finally:
            await runtime.stop()
    asyncio.run(scenario())
