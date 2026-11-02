"""Nonblocking SLS events and coalesced Redis task summaries."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import time
from typing import Callable

from .models import TaskRecord
from .queue import SAFE_DRIVER_ERRORS, SAFE_REJECTION_ERRORS


logger = logging.getLogger(__name__)
TERMINAL = frozenset({"completed", "failed", "interrupted", "expired"})
STAGE_MESSAGES = {
    "starting_emulator": "正在启动模拟器",
    "opening_game": "正在打开游戏",
    "preparing_qr": "正在准备登录二维码",
    "waiting_scan": "二维码已生成，请扫码登录",
    "finishing_login": "扫码授权已完成，正在进入游戏",
    "running_daily": "登录成功，开始执行日常任务",
    "completed": "任务执行完成",
    "failed": "任务执行失败",
    "interrupted": "服务已停止，任务执行中断",
    "expired": "登录申请已过期",
}
SAFE_RUN_ERRORS = {
    **SAFE_REJECTION_ERRORS,
    "configuration_error": "执行服务器配置有误，无法开始任务，请联系管理员。",
    "emulator_start_failed": "模拟器启动失败，请重新申请；若仍失败，请联系管理员。",
    "game_launch_failed": "游戏启动失败，请重新申请。",
    "qr_not_found": "未能获取登录二维码，请重新申请。",
    "command_timeout": "启动模拟器或获取二维码超时，请重新申请。",
    "unexpected_error": "执行任务时发生异常，请重新申请；若仍失败，请联系管理员。",
    "login_timeout": "等待扫码登录超时，请重新申请并及时扫码。",
    "login_failed": "未能确认游戏登录成功，请重新申请。",
    "login_finish_timeout": SAFE_DRIVER_ERRORS["login_finish_timeout"],
    "login_finish_failed": SAFE_DRIVER_ERRORS["login_finish_failed"],
    "daily_timeout": SAFE_DRIVER_ERRORS["daily_timeout"],
    "daily_failed": "日常任务执行失败，本次任务已停止。",
    "protocol_error": "执行进程返回的数据异常，任务已停止，请联系管理员。",
    "instance_cleanup_failed": "任务结束后未能关闭模拟器，请联系管理员检查执行服务器。",
    "worker_stopped": "执行服务器已停止，任务执行中断。",
    "admin_interrupted": "管理员已中断任务，请重新申请。",
}

# Retry is safe even when Redis accepted the write but its response was lost.
# A new owner may take over only after the login queue has assigned it the task.
# The login business deadline never expires an already authorized execution.
_PUBLISH = """
if redis.call('TYPE', KEYS[1]).ok ~= 'string' then return 0 end
local ok, old = pcall(cjson.decode, redis.call('GET', KEYS[1]))
local valid, update = pcall(cjson.decode, ARGV[3])
if not ok or not valid or type(old) ~= 'table' or type(update) ~= 'table' then return 0 end
if old.workerId ~= ARGV[1] then return 0 end
if old.taskId and old.taskId ~= update.taskId then return 0 end
if old.attemptId and old.attemptId ~= update.attemptId then return 0 end
if old.status == 'interrupted' then
    if update.status == 'interrupted' then return 1 end
    return -1
end
if tonumber(old.sequence or 0) >= tonumber(ARGV[2]) then return 1 end
local terminal = {completed=true, failed=true, interrupted=true, expired=true}
if terminal[old.status] and old.status ~= update.status then return 0 end
if update.scanCapturedAt and not update.authorizedAt and update.status == 'opening_game' then
    update.status = 'waiting_scan'
    old.scanCaptured = true
    old.refreshRequestId = nil
    redis.call('DEL', KEYS[2])
end
local rank = {queued=0, starting_emulator=1, opening_game=2, preparing_qr=3,
    waiting_scan=4, qr_ready=4, finishing_login=5, running_daily=6}
-- Success requires synchronous cleanup confirmation. Failure remains visible
-- during the diagnostic hold; late progress cannot reopen any terminal outcome.
local statusAllowed = not terminal[old.status] or old.status == update.status
if update.status == 'completed' and not terminal[old.status] then statusAllowed = false end
if statusAllowed and not terminal[old.status] and not terminal[update.status]
    and (rank[old.status] or 0) > (rank[update.status] or 0) then statusAllowed = false end
local fields = {'username', 'startedAt', 'authorizedAt', 'scanCapturedAt',
    'secretRealmCompletedAt', 'actionIndex', 'actionTotal',
    'logSource', 'logCursor', 'updatedAt', 'sequence'}
for _, key in ipairs(fields) do
    if update[key] ~= nil and update[key] ~= cjson.null then old[key] = update[key] end
end
-- A delayed buffered execution snapshot cannot shorten a newer arena budget.
if (old.executionDeadline == nil or old.executionDeadline == cjson.null)
    and update.executionDeadline ~= nil then old.executionDeadline = update.executionDeadline end
if old.startedAt == nil then old.startedAt = cjson.null end
if statusAllowed then
    old.status = update.status
    if update.errorCode ~= nil then old.errorCode = update.errorCode end
    if old.finishedAt == nil or old.finishedAt == cjson.null then
        if update.finishedAt ~= nil then old.finishedAt = update.finishedAt end
    end
end
local retention = math.max(tonumber(ARGV[4]), 604800)
redis.call('SET', KEYS[1], cjson.encode(old), 'EX', retention)
if not terminal[old.status] and type(old.userId) == 'string' then
    local activeKey = ARGV[5] .. ':users:' .. old.userId .. ':active'
    if redis.call('TYPE', activeKey).ok == 'string'
        and redis.call('GET', activeKey) == (old.id or old.taskId) then
        redis.call('EXPIRE', activeKey, retention)
    end
end
return 1
"""


def iso_time(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat(
        timespec="milliseconds",
    ).replace("+00:00", "Z")


@dataclass
class _Update:
    snapshot: dict


class RedisRunReporter:
    """Queue in memory; a slow/unavailable Redis must never block the game loop.

    Keep only the newest summary per task, at most the configured task capacity.
    Events enter the SLS publisher immediately, independently of Redis. Run
    summaries always last at least seven days, even with a shorter legacy setting,
    so the website's recent-history view retains scan and completion times.
    Active runs also reserve their full daily execution budget.
    """

    def __init__(self, config, queue, *, retention_seconds: int = 604_800,
                 stream_limit: int = 2_000, buffer_limit: int = 256, log_publisher=None,
                 on_interrupted: Callable[[str, str | None], None] | None = None) -> None:
        self.config = config
        self.queue = queue
        self.log_publisher = log_publisher
        self.retention_seconds = retention_seconds
        self.on_interrupted = on_interrupted
        self._interrupted_attempts: dict[str, str | None] = {}
        # Legacy keyword arguments remain accepted for existing callers.
        self._states: dict[str, dict] = {}
        self._pending: dict[str, deque[_Update]] = {}
        self._finished: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._sequence = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._publish_loop())

    def emit(self, record: TaskRecord, *, status: str | None = None,
             message: str | None = None, level: str = "INFO", timestamp: str | None = None,
             action_index: int | None = None, action_total: int | None = None,
             error_code: str | None = None) -> None:
        if self._closed:
            return
        from .log_events import sanitize_log_message

        previous = self._states.get(record.id)
        if previous and previous.get("attemptId") != record.attempt_id:
            previous = None
        if previous is None and len(self._states) >= self.config.max_tasks:
            return
        now = iso_time()
        state = dict(previous or {
            "taskId": record.id, "status": status or "starting_emulator",
            "startedAt": None, "finishedAt": None,
        })
        username_changed = bool(record.username) and state.get("username") != record.username
        scan_captured_changed = record.scan_captured_at is not None and 'scanCapturedAt' not in state
        if record.username:
            state["username"] = record.username
        if status is not None:
            if status not in STAGE_MESSAGES:
                return
            if (previous and previous["status"] == status and message is None
                    and error_code == previous.get("errorCode") and not username_changed
                    and not scan_captured_changed):
                return
            state["status"] = status
            state["finishedAt"] = now if status in TERMINAL else None
            if status == "running_daily" and (not previous or previous["status"] != status):
                state.pop("actionIndex", None)
                state.pop("actionTotal", None)
            state.pop("errorCode", None)
        if record.daily_started_at is not None:
            state["startedAt"] = iso_time(record.daily_started_at)
        if record.secret_realm_completed_at is not None:
            state.setdefault('secretRealmCompletedAt', record.secret_realm_completed_at)
        if record.authorized_at is not None:
            state["authorizedAt"] = iso_time(record.authorized_at)
        if record.execution_deadline is not None:
            state["executionDeadline"] = record.execution_deadline
        if record.scan_captured_at is not None:
            state.setdefault('scanCapturedAt', iso_time(record.scan_captured_at))
        if error_code is not None:
            state["errorCode"] = error_code
        if action_index is not None and action_total is not None:
            state.update(actionIndex=action_index, actionTotal=action_total)
        self._sequence += 1
        state.update(updatedAt=now, workerId=self.queue.owner, sequence=self._sequence)
        if record.attempt_id is not None:
            state["attemptId"] = record.attempt_id
        if level not in {"INFO", "WARNING", "ERROR"}:
            level = "INFO"
        default_message = (
            SAFE_RUN_ERRORS.get(error_code, STAGE_MESSAGES[state["status"]])
            if state["status"] in {"failed", "interrupted"} else STAGE_MESSAGES[state["status"]]
        )
        event = {
            "time": timestamp or now, "level": level,
            "message": sanitize_log_message(message or default_message),
        }
        if action_index is not None and action_total is not None:
            event.update(actionIndex=action_index, actionTotal=action_total)
        state["logSource"] = "sls" if self.log_publisher is not None else "disabled"
        if self.log_publisher is not None:
            if previous is None:
                self.log_publisher.emit(record.id, {
                    "time": record.created_at, "level": "INFO",
                    "message": "登录申请已提交，等待执行服务器领取任务。",
                })
            cursor = self.log_publisher.emit(record.id, event)
            if cursor is not None:
                state["logCursor"] = cursor
        self._states[record.id] = state
        pending = self._pending.setdefault(record.id, deque())
        pending.clear()
        pending.append(_Update(state))
        self._wake.set()

    def refresh(self, record: TaskRecord) -> None:
        """Refresh an active execution checkpoint without adding a public log."""
        previous = self._states.get(record.id)
        if (self._closed or record._admin_interrupted or record.id in self._finished
                or previous is None or previous.get("attemptId") != record.attempt_id
                or previous["status"] in TERMINAL):
            return
        self._sequence += 1
        state = dict(previous, updatedAt=iso_time(), sequence=self._sequence)
        self._states[record.id] = state
        pending = self._pending.setdefault(record.id, deque())
        pending.clear()
        pending.append(_Update(state))
        self._wake.set()

    def finish(self, task_id: str) -> None:
        """Release local buffers once all accepted updates have been delivered."""
        if self.log_publisher is not None:
            self.log_publisher.finish(task_id)
        self._finished.add(task_id)
        self._discard_finished(task_id)

    async def flush(self, task_id: str, *, timeout: float = 2) -> bool:
        """Publish an attempt's final update before relinquishing its owner."""
        async def drain() -> None:
            while self._pending.get(task_id):
                self._wake.set()
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(drain(), timeout)
            return True
        except TimeoutError:
            logger.warning("Task %s progress could not be sent before ownership handoff", task_id)
            return False

    def _discard_finished(self, task_id: str) -> None:
        if task_id in self._finished and not self._pending.get(task_id):
            self._finished.discard(task_id)
            self._states.pop(task_id, None)
            self._pending.pop(task_id, None)
            self._interrupted_attempts.pop(task_id, None)

    async def _write(self, task_id: str, update: _Update) -> None:
        prefix = self.config.redis_queue_key
        run_ttl = max(self.retention_seconds, 604_800)
        if update.snapshot["status"] not in TERMINAL:
            run_ttl += self.config.daily_timeout_seconds
        result = await self.queue._call(
            "eval", _PUBLISH, 2, f"{prefix}:runs:{task_id}",
            f"{prefix}:refresh:{task_id}", self.queue.owner, str(update.snapshot["sequence"]),
            json.dumps(update.snapshot, ensure_ascii=False), str(run_ttl), prefix,
        )
        if result == -1:
            attempt_id = update.snapshot.get("attemptId")
            if (task_id not in self._interrupted_attempts
                    or self._interrupted_attempts[task_id] != attempt_id):
                if self.on_interrupted is not None:
                    self.on_interrupted(task_id, attempt_id)
                self._interrupted_attempts[task_id] = attempt_id
            # Drop only this execution's buffered progress, never a newer claim.
            pending = self._pending.get(task_id)
            if pending and pending[-1].snapshot.get("attemptId") == attempt_id:
                pending.clear()

    async def _publish_loop(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            # Logs arriving on separate event-loop turns still share one state
            # write. SLS enqueue is immediate; only Redis summaries wait here.
            await asyncio.sleep(0.25)
            failed = False
            for task_id, pending in list(self._pending.items()):
                if not pending:
                    self._discard_finished(task_id)
                    continue
                update = pending[0]
                try:
                    await self._write(task_id, update)
                except Exception:
                    failed = True
                    break
                # emit() may trim the deque while the network call is pending.
                if pending and pending[0] is update:
                    pending.popleft()
                self._discard_finished(task_id)
            if failed:
                await asyncio.sleep(1)
            if any(self._pending.values()):
                self._wake.set()

    async def close(self, *, timeout: float = 2) -> None:
        async def drain() -> None:
            while any(self._pending.values()):
                self._wake.set()
                await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(drain(), timeout)
        except TimeoutError:
            logger.warning("Some task progress could not be sent before worker shutdown")
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._states.clear()
        self._pending.clear()
        self._interrupted_attempts.clear()
        self._finished.clear()
