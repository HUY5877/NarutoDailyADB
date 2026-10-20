"""Bounded in-memory task logger; upload directly to SLS, never via Redis."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import logging
import re
import secrets
import threading
import time

from .log_events import sanitize_log_message
from .models import UUID_PATTERN

LOGGER = logging.getLogger(__name__)


class SlsSdkWriter:
    def __init__(self, config):
        from aliyun.log import LogClient, LogItem, PutLogsRequest
        self.client = LogClient(config.sls_endpoint, config.sls_access_key_id,
                                config.sls_access_key_secret, securityToken=config.sls_sts_token)
        self.client.timeout = 5
        self.config = config
        self._item = LogItem
        self._request = PutLogsRequest

    def _put(self, records: list[dict[str, str]]) -> None:
        items = []
        for record in records:
            milliseconds = int(record["streamMs"])
            item = self._item(contents=list(record.items()))
            item.set_time(milliseconds // 1000)
            item.set_time_nano_part(milliseconds % 1000 * 1_000_000)
            items.append(item)
        request = self._request(project=self.config.sls_project, logstore=self.config.sls_logstore,
                                topic="task-events", logitems=items, compress=True)
        self.client.put_logs(request)

    async def put(self, records: list[dict[str, str]]) -> None:
        # SDK retries run outside both the game event loop and interpreter exit.
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def settle(failed: bool) -> None:
            if not result.done():
                if failed:
                    result.set_exception(RuntimeError("SLS upload failed"))
                else:
                    result.set_result(None)

        def send() -> None:
            failed = False
            try:
                self._put(records)
            except Exception:
                failed = True
            try:
                loop.call_soon_threadsafe(settle, failed)
            except RuntimeError:
                pass

        threading.Thread(target=send, name="sls-log-upload", daemon=True).start()
        await result


@dataclass
class _TaskLogs:
    first_id: str
    uploaded: int = 0
    pending: int = 0
    truncated: bool = False
    finished: bool = False
    marker: dict[str, str] | None = None


class SlsLogPublisher:
    """emit() does no network/disk work; one background task batches and retries.

    Pending logs, including the active upload, are bounded by count and bytes.
    Overflow drops older queued logs while preserving each task's accepted tail.
    A failed batch is retried unchanged: readers deduplicate its stable IDs.
    There is intentionally no durable spool or Redis fallback for new logs.
    """
    def __init__(self, config, *, writer=None, interval: float = 1,
                 retry_interval: float = 5, buffer_limit: int = 4096,
                 byte_limit: int = 16 * 1024 * 1024, batch_size: int = 200):
        self.config = config
        self.writer = writer if writer is not None else SlsSdkWriter(config)
        self.interval = max(0.001, interval)
        self.retry_interval = max(0.001, retry_interval)
        self.buffer_limit = max(2, buffer_limit)
        self.byte_limit = max(1024, byte_limit)
        self.batch_size = max(1, min(batch_size, self.buffer_limit // 2))
        self._pending: deque[tuple[dict[str, str], int]] = deque()
        self._tasks: dict[str, _TaskLogs] = {}
        self._corrections: dict[str, dict[str, str]] = {}
        self._uploading: set[str] = set()
        self._inherited: set[str] = set()
        self._count = self._bytes = 0
        self._last_ms = 0
        self._sequence = secrets.randbits(52)
        self._lock = threading.Lock()
        self._wake = asyncio.Event()
        self._loop = None
        self._task: asyncio.Task | None = None
        self._closing = self._closed = False
        self._next_warning = 0.0

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._loop = asyncio.get_running_loop()
            self._task = self._loop.create_task(self._upload_loop(), name="sls-task-logs")

    def _notify(self) -> None:
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    def observe_cursor(self, cursor: str | None, *, task_id: str | None = None) -> None:
        """Continue after the previous owner's watermark, even after clock skew."""
        if not isinstance(cursor, str) or not re.fullmatch(r"[0-9]{1,16}-[0-9]{1,16}", cursor):
            return
        ms, sequence = map(int, cursor.split("-"))
        if ms >= 2**53 or sequence >= 2**53:
            return
        with self._lock:
            self._last_ms = max(self._last_ms, ms)
            self._sequence = max(self._sequence, sequence)
            if (cursor != '0-0' and task_id is not None and UUID_PATTERN.fullmatch(task_id)
                    and task_id not in self._tasks and len(self._inherited) < self.config.max_tasks):
                self._inherited.add(task_id)

    def _event(self, task_id: str, value: dict) -> dict[str, str] | None:
        if not isinstance(task_id, str) or not UUID_PATTERN.fullmatch(task_id) or not isinstance(value, dict):
            return None
        stamp, message, level = value.get("time"), value.get("message"), value.get("level")
        if (not isinstance(stamp, str) or not stamp.endswith("Z") or len(stamp) > 40
                or not isinstance(message, str) or not message
                or not isinstance(level, str) or level not in {"INFO", "WARNING", "ERROR"}):
            return None
        try:
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        fields = {"kind": "event", "taskId": task_id, "environment": self.config.app_env,
                  "time": stamp, "level": level,
                  "message": sanitize_log_message(message, secrets=(self.config.sls_access_key_id,
                      self.config.sls_access_key_secret, self.config.sls_sts_token or ""))}
        if "actionIndex" in value or "actionTotal" in value:
            index, total = value.get("actionIndex"), value.get("actionTotal")
            if type(index) is not int or type(total) is not int or not 0 <= index <= total <= 100000:
                return None
            fields.update(actionIndex=str(index), actionTotal=str(total))
        return fields

    def emit(self, task_id: str, event: dict) -> str | None:
        fields = self._event(task_id, event)
        if fields is None:
            return None
        with self._lock:
            if self._closing or self._closed:
                return None
            state = self._tasks.get(task_id)
            if state is None and len(self._tasks) >= self.config.max_tasks:
                return None
            self._last_ms = max(self._last_ms, int(time.time() * 1000))
            self._sequence += 1
            if self._sequence >= 2**53:
                self._last_ms += 1
                self._sequence = 0
            item_id = f"{self._last_ms}-{self._sequence}"
            fields.update(id=item_id, streamMs=str(self._last_ms), streamSeq=str(self._sequence),
                          firstId=state.first_id if state is not None else item_id)
            size = sum(len(k.encode()) + len(v.encode()) for k, v in fields.items()) + 256
            if state is None:
                state = self._tasks[task_id] = _TaskLogs(item_id, truncated=task_id in self._inherited)
                self._inherited.discard(task_id)
            if not self._reserve(task_id, size):
                self._mark_gap(task_id)
                self._notify()
                return None
            self._tasks[task_id] = state
            state.pending += 1
            state.finished = False
            self._pending.append((fields, size))
            self._count += 1
            self._bytes += size
        self._notify()
        return item_id

    def finish(self, task_id: str) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is not None:
                state.finished = True
                if not state.pending and task_id not in self._corrections and task_id not in self._uploading:
                    self._tasks.pop(task_id, None)

    def _reserve(self, task_id: str, size: int) -> bool:
        if self._count < self.buffer_limit and self._bytes + size <= self.byte_limit:
            return True
        # Plan evictions first: never discard an accepted tail if the replacement
        # cannot fit, and never discard another task's final queued log.
        latest = {event["taskId"]: i for i, (event, _) in enumerate(self._pending)}
        count, byte_count = self._count, self._bytes
        removed = set()
        for i, (event, event_size) in enumerate(self._pending):
            if count < self.buffer_limit and byte_count + size <= self.byte_limit:
                break
            if event["taskId"] != task_id and latest[event["taskId"]] == i:
                continue
            removed.add(i)
            count -= 1
            byte_count -= event_size
        if count >= self.buffer_limit or byte_count + size > self.byte_limit:
            return False
        kept = deque()
        for i, (event, event_size) in enumerate(self._pending):
            if i in removed:
                self._mark_gap(event["taskId"])
                self._release(event, event_size)
            else:
                kept.append((event, event_size))
        self._pending = kept
        return True

    def _mark_gap(self, task_id: str) -> None:
        state = self._tasks[task_id]
        state.truncated = True
        if state.marker is not None and state.marker["truncated"] != "true":
            self._corrections[task_id] = dict(state.marker, truncated="true")

    def _release(self, event: dict, size: int) -> None:
        self._count -= 1
        self._bytes -= size
        state = self._tasks[event["taskId"]]
        state.pending -= 1
        if (state.finished and not state.pending and event["taskId"] not in self._corrections
                and event["taskId"] not in self._uploading):
            self._tasks.pop(event["taskId"], None)

    def _take_batch(self):
        with self._lock:
            batch = [self._pending.popleft() for _ in range(min(self.batch_size, len(self._pending)))]
            grouped: dict[str, list[dict[str, str]]] = {}
            for event, _ in batch:
                grouped.setdefault(event["taskId"], []).append(event)
            markers = []
            for task_id in list(self._corrections):
                if task_id not in grouped:
                    markers.append(self._corrections.pop(task_id))
            for task_id, events in grouped.items():
                state, last = self._tasks[task_id], events[-1]
                self._corrections.pop(task_id, None)
                markers.append({"kind": "checkpoint", "taskId": task_id, "environment": self.config.app_env,
                                "id": last["id"], "streamMs": last["streamMs"], "streamSeq": last["streamSeq"],
                                "time": last["time"], "firstId": state.first_id, "throughId": last["id"],
                                "entryCount": str(state.uploaded + len(events)), "truncated": str(state.truncated).lower()})
            self._uploading = set(grouped) | {marker["taskId"] for marker in markers}
            return batch, [event for event, _ in batch] + markers

    async def _upload_loop(self) -> None:
        batch, records = [], []
        while True:
            if not records:
                if not self._pending and not self._corrections:
                    if self._closing:
                        return
                    self._wake.clear()
                    if not self._pending and not self._corrections:
                        await self._wake.wait()
                if not self._closing:
                    await asyncio.sleep(self.interval)
                batch, records = self._take_batch()
                if not records:
                    continue
            try:
                await self.writer.put(records)
            except Exception:
                if time.monotonic() >= self._next_warning:
                    LOGGER.warning("SLS 日志上报暂不可用，将重试内存中的日志")
                    self._next_warning = time.monotonic() + 60
                await asyncio.sleep(self.retry_interval)
                continue
            with self._lock:
                for marker in records:
                    if marker["kind"] == "checkpoint":
                        state = self._tasks[marker["taskId"]]
                        state.marker = marker
                        if state.truncated and marker["truncated"] != "true":
                            self._mark_gap(marker["taskId"])
                        elif marker["truncated"] == "true":
                            self._corrections.pop(marker["taskId"], None)
                for event, size in batch:
                    self._tasks[event["taskId"]].uploaded += 1
                    self._release(event, size)
                self._uploading.clear()
                for task_id, state in list(self._tasks.items()):
                    if state.finished and not state.pending and task_id not in self._corrections:
                        self._tasks.pop(task_id, None)
            batch, records = [], []

    async def close(self) -> None:
        """Stop accepting logs and wait for all buffered batches to succeed.

        Cancellation of the caller is deferred until the upload finishes. There
        is no shutdown deadline: a temporary SLS outage must not discard logs.
        """
        if self._closed:
            return
        self.start()
        with self._lock:
            self._closing = True
        self._notify()
        LOGGER.info("正在等待 SLS 内存日志上报完成")
        cancelled = False
        while not self._task.done():
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                cancelled = True
        # Surface an unexpected uploader failure; never report a successful
        # flush or clear its data unless the upload loop finished normally.
        self._task.result()
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._tasks.clear()
            self._corrections.clear()
            self._uploading.clear()
            self._inherited.clear()
            self._count = self._bytes = 0
        LOGGER.info("SLS 内存日志已全部上报")
        if cancelled:
            raise asyncio.CancelledError
