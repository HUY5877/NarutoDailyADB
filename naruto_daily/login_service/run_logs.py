"""Bounded, best-effort publication of task progress to shared Redis streams."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import time

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
}

# Retry is safe even when Redis accepted the write but its response was lost.
# A new owner may take over only after the login queue has assigned it the task.
# Once the QR session expires, the existing run still authorizes its own worker.
_PUBLISH = """
local runKind = redis.call('TYPE', KEYS[1]).ok
local logKind = redis.call('TYPE', KEYS[2]).ok
local sessionKind = redis.call('TYPE', KEYS[3]).ok
if (runKind ~= 'none' and runKind ~= 'string')
    or (logKind ~= 'none' and logKind ~= 'stream') then return 0 end
local old = nil
if runKind == 'string' then
    local ok, value = pcall(cjson.decode, redis.call('GET', KEYS[1]))
    if not ok or type(value) ~= 'table' then return 0 end
    old = value
end
local session = nil
if sessionKind == 'string' then
    local ok, value = pcall(cjson.decode, redis.call('GET', KEYS[3]))
    if ok and type(value) == 'table' then session = value end
end
local ok, update = pcall(cjson.decode, ARGV[3])
if not ok or type(update) ~= 'table' then return 0 end
local ownsSession = session and session.workerId == ARGV[1]
-- Requeue can clear the owner before a buffered shutdown event is delivered.
-- Only the last recorded owner may finish that attempt, until a new claim.
local requeuedStop = session and session.status == 'queued'
    and (session.workerId == nil or session.workerId == cjson.null)
    and old and old.workerId == ARGV[1] and update.status == 'interrupted'
if session and not ownsSession and not requeuedStop then return 0 end
if (not old or old.workerId ~= ARGV[1])
    and not ownsSession then return 0 end
if old and old.workerId == ARGV[1] and tonumber(old.sequence or 0) >= tonumber(ARGV[2]) then return 1 end
-- A deleted/expired/requeued session cannot authorize late progress to reopen
-- a stopped run. A fresh owned claim can legitimately begin another attempt.
local activeClaim = ownsSession and (session.status == 'starting_emulator'
    or session.status == 'opening_game' or session.status == 'preparing_qr'
    or session.status == 'qr_ready')
if old and old.workerId == ARGV[1] and not activeClaim
    and (old.status == 'completed' or old.status == 'failed'
        or old.status == 'interrupted' or old.status == 'expired')
    and update.status ~= 'completed' and update.status ~= 'failed'
    and update.status ~= 'interrupted' and update.status ~= 'expired' then return 0 end
redis.call('XADD', KEYS[2], 'MAXLEN', ARGV[6], '*', 'json', ARGV[4])
redis.call('SET', KEYS[1], ARGV[3], 'EX', ARGV[7])
redis.call('EXPIRE', KEYS[2], ARGV[5])
return 1
"""


def iso_time(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat(
        timespec="milliseconds",
    ).replace("+00:00", "Z")


@dataclass
class _Update:
    snapshot: dict
    event: dict


class RedisRunReporter:
    """Queue in memory; a slow/unavailable Redis must never block the game loop.

    Keep the newest 256 updates per task and at most the configured task capacity.
    Redis retains the newest 2,000 entries, by default for seven days. Run
    summaries always last at least seven days, even with a shorter log setting,
    so the website's recent-history view retains scan and completion times.
    Active runs also reserve their full daily execution budget.
    """

    def __init__(self, config, queue, *, retention_seconds: int = 604_800,
                 stream_limit: int = 2_000, buffer_limit: int = 256) -> None:
        self.config = config
        self.queue = queue
        self.retention_seconds = retention_seconds
        self.stream_limit = stream_limit
        self.buffer_limit = buffer_limit
        self._states: dict[str, dict] = {}
        self._pending: dict[str, deque[_Update]] = {}
        self._finished: set[str] = set()
        self._dropped: dict[str, int] = {}
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
        if previous is None and len(self._states) >= self.config.max_tasks:
            return
        now = iso_time()
        state = dict(previous or {
            "taskId": record.id, "status": status or "starting_emulator",
            "startedAt": None, "finishedAt": None,
        })
        if status is not None:
            if status not in STAGE_MESSAGES:
                return
            if previous and previous["status"] == status and message is None and error_code == previous.get("errorCode"):
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
        if error_code is not None:
            state["errorCode"] = error_code
        if action_index is not None and action_total is not None:
            state.update(actionIndex=action_index, actionTotal=action_total)
        self._sequence += 1
        state.update(updatedAt=now, workerId=self.queue.owner, sequence=self._sequence)
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
        self._states[record.id] = state
        pending = self._pending.setdefault(record.id, deque())
        if len(pending) >= self.buffer_limit:
            pending.popleft()
            self._dropped[record.id] = self._dropped.get(record.id, 0) + 1
        pending.append(_Update(state, event))
        self._wake.set()

    def finish(self, task_id: str) -> None:
        """Release local buffers once all accepted updates have been delivered."""
        self._finished.add(task_id)
        self._discard_finished(task_id)

    async def flush(self, task_id: str, *, timeout: float = 2) -> None:
        """Publish an attempt's final update before relinquishing its owner."""
        async def drain() -> None:
            while self._pending.get(task_id):
                self._wake.set()
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(drain(), timeout)
        except TimeoutError:
            logger.warning("Task %s progress could not be sent before ownership handoff", task_id)

    def _discard_finished(self, task_id: str) -> None:
        if task_id in self._finished and not self._pending.get(task_id):
            self._finished.discard(task_id)
            self._states.pop(task_id, None)
            self._pending.pop(task_id, None)
            self._dropped.pop(task_id, None)

    async def _write(self, task_id: str, update: _Update) -> None:
        prefix = self.config.redis_queue_key
        ttl = self.retention_seconds
        run_ttl = max(self.retention_seconds, 604_800)
        if update.snapshot["status"] not in TERMINAL:
            ttl += self.config.daily_timeout_seconds
            run_ttl += self.config.daily_timeout_seconds
        event = update.event
        dropped = self._dropped.get(task_id, 0)
        if dropped:
            # Attach an explicit gap notice without changing the final state.
            event = dict(event, message=f"部分执行日志因网络中断未保留（{dropped} 条）；" + event["message"])
            event["message"] = event["message"][:1000]
        accepted = await self.queue._call(
            "eval", _PUBLISH, 3, f"{prefix}:runs:{task_id}", f"{prefix}:logs:{task_id}",
            f"{prefix}:sessions:{task_id}", self.queue.owner, str(update.snapshot["sequence"]),
            json.dumps(update.snapshot, ensure_ascii=False), json.dumps(event, ensure_ascii=False),
            str(ttl), str(self.stream_limit), str(run_ttl),
        )
        if accepted:
            self._dropped[task_id] = max(0, self._dropped.get(task_id, 0) - dropped)

    async def _publish_loop(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
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
        self._finished.clear()
