"""Capture diagnostics never enter public events, even from older children."""

import asyncio
from io import StringIO
import json
import logging
from types import SimpleNamespace

import pytest

from naruto_daily.login_service import ldplayer_worker
from naruto_daily.login_service.capture_apks import ApkDownloadError
from naruto_daily.login_service.log_events import ProtocolLogHandler, forward_task_logs
from naruto_daily.login_service.models import parse_queue_task
from naruto_daily.login_service.qq_capture import CaptureError
from naruto_daily.login_service.runtime import LoginRuntime
from naruto_daily.login_service.run_logs import SAFE_RUN_ERRORS, STAGE_MESSAGES, iso_time
from test_login_service_run_logs import (
    body, claim, config, drain, fake_client, keys, make_reporter, published,
)
from naruto_daily.login_service.queue import RedisTaskQueue


INTERNAL_MESSAGES = [
    '当前雷电实例缺少抓号器，正在下载并安装',
    '抓号器和上号器已准备完成',
    '辅助应用自动补装失败（下载上号器），继续尝试原 QQ 扫码流程',
    '暂时无法检测 QQ 辅助应用，将继续原扫码登录流程',
    'QQ 抓号轮次清理暂未完成；本轮仍受原截止时间限制',
    'QQ 授权回调地址无效',
    'QQ 自动化接口调用失败，请检查应用版本和当前轮次',
    'QQ 授权交接失败',
    'QQ capture identity temporarily unavailable',
    'capture marker drain unavailable',
    'Capture package check returned no complete package list.',
    'LDPlayer helper application installation did not report success.',
    '具体错误：ApkDownloadError: Invalid APK HTTPS URL；执行阶段 configuration 失败',
    'provider content://com.huy.authcapture.automation unavailable',
    'callback_url=auth://callback?token=fixture',
    '扫号器准备失败',
    'call_back_url=auth://callback?token=fixture',
    '回调地址无效',
    '本轮授权已清理',
    '授权轮次已失效',
    'QQ_APK_DOWNLOADS invalid',
    'HUYCapture.apk download failed',
    'HUYLogin provider unavailable',
    'QQ automation provider unavailable',
    'Missing helpers after install',
    'APK SHA256 verification failed',
    'APK download or cache operation failed',
    'Invalid APK HTTPS URL',
    '注入授权失败',
    'QQ\n自动化接口调用失败',
    '游戏恢复到开始游戏页，返回重新选择本次登录通道',
    '识别到之前的 QQ 扫码授权页，返回游戏重新选择本次登录通道',
    '已点击本次指定登录通道：android_qq',
    '已点击本次指定登录通道：ios_qq',
]


@pytest.mark.parametrize('name', [
    'naruto_daily.login_service.qq_capture',
    'naruto_daily.login_service.qq_capture.provider',
    'naruto_daily.login_service.capture_apks',
])
@pytest.mark.parametrize('level', [logging.INFO, logging.WARNING, logging.ERROR])
def test_internal_capture_modules_keep_plain_messages_only_in_diagnostics(name, level):
    events, diagnostic = [], StringIO()
    with forward_task_logs(events.append, diagnostic_stream=diagnostic):
        logging.getLogger(name).log(level, '内部操作已结束')
    assert events == []
    assert '内部操作已结束' in diagnostic.getvalue()


@pytest.mark.parametrize('message', INTERNAL_MESSAGES)
def test_protocol_drops_capture_text_from_other_loggers_before_sanitizing(message):
    events, diagnostic = [], StringIO()
    with forward_task_logs(events.append, diagnostic_stream=diagnostic):
        logging.getLogger('naruto_daily').warning(message)
    assert events == []
    assert diagnostic.getvalue()


def test_capture_marker_beyond_public_truncation_is_still_private():
    events, diagnostic = [], StringIO()
    with forward_task_logs(events.append, diagnostic_stream=diagnostic):
        logging.getLogger('naruto_daily').error('上下文' * 1000 + ' QQ 抓号轮次无效')
    assert events == []
    assert 'QQ 抓号轮次无效' in diagnostic.getvalue()


@pytest.mark.parametrize('internal_error', [CaptureError('内部操作失败'), ApkDownloadError('内部下载失败')])
@pytest.mark.parametrize('link', ['__cause__', '__context__'])
def test_wrapped_capture_exceptions_remain_private_even_past_summary_depth(internal_error, link):
    error = internal_error
    for _ in range(12):
        outer = RuntimeError('操作失败' * 400)
        setattr(outer, link, error)
        error = outer
    internal_error.__context__ = error  # An accidental cycle must also terminate.
    events = []
    record = logging.LogRecord('naruto_daily', logging.ERROR, __file__, 1,
                               '执行阶段 login 失败', (), (type(error), error, None))
    ProtocolLogHandler(events.append).handle(record)
    assert events == []


@pytest.mark.parametrize('module_name', ['naruto_daily.login_service.ldplayer_worker', '__main__'])
def test_capture_timeout_without_message_is_detected_from_operation_traceback(tmp_path, monkeypatch, module_name):
    events = []
    # The production child runs with -m, so traceback globals name this module
    # __main__ even though the operation still comes from ldplayer_worker.py.
    monkeypatch.setitem(ldplayer_worker.__dict__, '__name__', module_name)
    try:
        ldplayer_worker._capture_apps_ready(object(), object(), 1, tmp_path, lambda: 2)
    except TimeoutError as error:
        record = logging.LogRecord('naruto_daily', logging.ERROR, __file__, 1,
                                   '执行阶段 emulator 失败', (), (type(error), error, error.__traceback__))
    ProtocolLogHandler(events.append).handle(record)
    assert events == []


@pytest.mark.parametrize('success', [True, False])
def test_provisioning_under_main_keeps_backend_diagnostics_without_public_logs(tmp_path, monkeypatch, success):
    from naruto_daily.login_service import capture_apks
    from test_login_service_apk_provisioning import DOWNLOADS, PACKAGES

    missing = set(PACKAGES)

    class Manager:
        def capture_apps_installed(self, *args):
            return False

        def missing_capture_apps(self, *args):
            return missing

        def install_missing_app(self, instance, apk, package, deadline):
            missing.remove(package)

        def verify_capture_instance(self, *args):
            pass

    monkeypatch.setitem(ldplayer_worker.__dict__, '__name__', '__main__')
    monkeypatch.setenv('QQ_APK_DOWNLOADS', DOWNLOADS if success else 'invalid')
    monkeypatch.setattr(capture_apks, 'download_apk', lambda *args: tmp_path / 'fixture.apk')
    events, diagnostic = [], StringIO()
    with forward_task_logs(events.append, diagnostic_stream=diagnostic):
        assert ldplayer_worker._capture_apps_ready(Manager(), 'owned', 100, tmp_path, lambda: 0) is success
    assert events == []
    assert ('抓号器和上号器已准备完成' if success else '辅助应用自动补装失败') in diagnostic.getvalue()


@pytest.mark.parametrize('message', INTERNAL_MESSAGES)
@pytest.mark.parametrize('level', ['INFO', 'WARNING', 'ERROR'])
def test_parent_rejects_old_child_capture_logs_even_with_action_metadata(message, level):
    emitted = []
    runtime = SimpleNamespace(_run_reporter=SimpleNamespace(emit=lambda *args, **event: emitted.append(event)))
    record = parse_queue_task(body(), 'login')
    LoginRuntime._accept_log(runtime, record, {
        'type': 'log', 'time': iso_time(), 'message': message, 'level': level,
        'actionIndex': 1, 'actionTotal': 2,
    })
    assert emitted == []
    assert record.secret_realm_completed_at is None


def test_parent_private_marker_cannot_mutate_public_milestones():
    emitted = []
    runtime = SimpleNamespace(_run_reporter=SimpleNamespace(emit=lambda *args, **event: emitted.append(event)))
    record = parse_queue_task(body(), 'login')
    record.daily_started_at, record.daily_status = 1.0, 'running_daily'
    stamp = iso_time()
    LoginRuntime._accept_log(runtime, record, {
        'type': 'log', 'time': stamp, 'level': 'INFO', 'message': '内部操作完成',
        'backend_only': True, 'secretRealmCompletedAt': stamp, 'executionCheckpoint': True,
    })
    assert record.secret_realm_completed_at is None
    assert emitted == []


@pytest.mark.parametrize('message', [
    '二维码已生成，请扫码登录', '扫码授权已完成，正在进入游戏',
    'QQ 扫码授权已完成', '登录成功，开始执行日常任务',
    '本次识别暂未成功', '游戏截图 capture 失败',
    '模型 provider 暂不可用', 'callback 处理失败',
])
def test_public_login_and_unrelated_diagnostics_remain_visible(message):
    emitted = []
    record = parse_queue_task(body(), 'login')
    runtime = SimpleNamespace(_run_reporter=SimpleNamespace(emit=lambda *args, **event: emitted.append(event)))
    with forward_task_logs(lambda event: LoginRuntime._accept_log(runtime, record, event)):
        logging.getLogger('naruto_daily').warning(message, extra={'action_index': 1, 'action_total': 2})
    assert len(emitted) == 1
    assert emitted[0]['message'] == message
    assert emitted[0]['action_index'] == 1 and emitted[0]['action_total'] == 2


def test_capture_logs_cannot_reach_sls_publisher_or_redis_summary_but_public_stages_survive():
    async def scenario():
        client, cfg = fake_client(), config()
        queue = RedisTaskQueue(cfg, client=client)
        record = await claim(client, queue)
        reporter = make_reporter(cfg, queue)
        reporter.start()
        runtime = SimpleNamespace(_run_reporter=reporter)
        try:
            for message in INTERNAL_MESSAGES:
                LoginRuntime._accept_log(runtime, record, {
                    'type': 'log', 'time': iso_time(), 'level': 'WARNING', 'message': message,
                })
            assert published(reporter, record.id) == []
            reporter.emit(record, status='waiting_scan')
            reporter.emit(record, status='finishing_login')
            LoginRuntime._accept_log(runtime, record, {
                'type': 'log', 'time': iso_time(), 'level': 'WARNING', 'message': '本次识别暂未成功',
            })
            reporter.emit(record, status='failed', error_code='login_failed')
            await drain(reporter)
            assert [event['message'] for _, event in published(reporter, record.id)] == [
                STAGE_MESSAGES['waiting_scan'], STAGE_MESSAGES['finishing_login'],
                '本次识别暂未成功', SAFE_RUN_ERRORS['login_failed'],
            ]
            run_key, _, _ = keys(cfg, record)
            summary = json.loads(await client.get(run_key))
            assert summary['status'] == 'failed'
            assert summary['errorCode'] == 'login_failed'
            assert not any(message in json.dumps(summary, ensure_ascii=False) for message in INTERNAL_MESSAGES)
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_capture_worker_failure_preserves_generic_protocol_error(monkeypatch, tmp_path, capsys):
    from test_login_service_ldplayer_worker import daily_dependencies, invoke, request

    class Capture:
        def __init__(self, *args, **kwargs):
            raise CaptureError('内部操作失败')

    monkeypatch.setattr(ldplayer_worker, '_capture_apps_ready', lambda *args: True)
    monkeypatch.setattr(ldplayer_worker, '_load_capture_session', lambda: Capture)
    monkeypatch.setattr(ldplayer_worker, '_load_daily_session', lambda: lambda **kwargs: object())
    code, events = invoke(monkeypatch, request(
        dailyConfigPath=str(tmp_path / 'daily.json'), dailyTimeoutSeconds=600,
    ), daily_dependencies([]))
    assert code == 1
    assert events[-1] == {'type': 'error', 'code': 'configuration_error'}
    assert not any('内部操作失败' in event.get('message', '') for event in events)
    assert '内部操作失败' in capsys.readouterr().err
