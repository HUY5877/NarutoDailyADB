"""Mirror retained public Redis events to SLS without changing task execution.

Redis remains the retry source. Checkpoints contain only cursors, never log
messages or credentials. A lost SLS response can duplicate an event; readers
deduplicate by environment/taskId/id and verify the checkpoint's entryCount.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time

from .log_events import sanitize_log_message
from .models import UUID_PATTERN


LOGGER = logging.getLogger(__name__)
_CURSOR = re.compile(r"(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)")
_PAGE_SIZE = 200
_SNAPSHOT = """
if redis.call('TYPE', KEYS[1]).ok ~= 'stream' then return {} end
local info = redis.call('XINFO', 'STREAM', KEYS[1])
local firstId, lastId, added, length = '', '', 0, 0
for i = 1, #info, 2 do
  if info[i] == 'first-entry' and type(info[i+1]) == 'table' then firstId = info[i+1][1] end
  if info[i] == 'last-entry' and type(info[i+1]) == 'table' then lastId = info[i+1][1] end
  if info[i] == 'entries-added' then added = info[i+1] end
  if info[i] == 'length' then length = info[i+1] end
end
local entries = redis.call('XRANGE', KEYS[1], '(' .. ARGV[1], '+', 'COUNT', ARGV[2])
return {entries, firstId, lastId, added, length}
"""


def _cursor(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not _CURSOR.fullmatch(value):
        raise ValueError("Invalid stream cursor")
    parts = tuple(map(int, value.split("-")))
    # SLS long indexes are signed 64-bit. Real Redis timestamp/sequence values
    # fit comfortably; refuse outliers rather than changing their sort order.
    if any(part > 2**63 - 1 for part in parts):
        raise ValueError("Unsupported stream cursor")
    return parts


@dataclass(frozen=True)
class Checkpoint:
    cursor: str = "0-0"
    first_id: str = ""
    entry_count: int = 0
    truncated: bool = False
    entries_added: int = 0


class Checkpoints:
    def __init__(self, config):
        self.path = Path(config.ldplayer_state_dir) / "sls" / "checkpoints.sqlite3"
        scope = [config.app_env, config.redis_queue_key, config.sls_endpoint,
                 config.sls_project, config.sls_logstore]
        self.scope = hashlib.sha256(json.dumps(scope).encode()).hexdigest()

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=2)
        try:
            with database:
                database.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
                    scope TEXT NOT NULL, task_id TEXT NOT NULL, cursor TEXT NOT NULL,
                    first_id TEXT NOT NULL, entry_count INTEGER NOT NULL,
                    truncated INTEGER NOT NULL, entries_added INTEGER NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY(scope, task_id))""")
                yield database
        finally:
            database.close()

    def get(self, task_id: str) -> Checkpoint:
        with self._connect() as database:
            row = database.execute("""SELECT cursor, first_id, entry_count, truncated, entries_added
                FROM checkpoints WHERE scope=? AND task_id=?""", (self.scope, task_id)).fetchone()
        if row is None:
            return Checkpoint()
        _cursor(row[0])
        if row[1]:
            _cursor(row[1])
        return Checkpoint(row[0], row[1], row[2], bool(row[3]), row[4])

    def save(self, task_id: str, checkpoint: Checkpoint) -> None:
        with self._connect() as database:
            database.execute("""INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(scope,task_id) DO UPDATE SET cursor=excluded.cursor,
                first_id=excluded.first_id, entry_count=excluded.entry_count,
                truncated=excluded.truncated, entries_added=excluded.entries_added,
                updated_at=excluded.updated_at""", (self.scope, task_id, checkpoint.cursor,
                checkpoint.first_id, checkpoint.entry_count, int(checkpoint.truncated),
                checkpoint.entries_added, time.time()))

    def delete(self, task_id: str) -> None:
        with self._connect() as database:
            database.execute("DELETE FROM checkpoints WHERE scope=? AND task_id=?", (self.scope, task_id))

    def cleanup(self) -> None:
        # One day beyond the maximum Redis retention, including execution time.
        with self._connect() as database:
            database.execute("DELETE FROM checkpoints WHERE updated_at < ?", (time.time() - 8 * 86400,))


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
            # Explicit setters preserve even zero; SDK constructor defaults use
            # truthiness and otherwise substitute the current fractional time.
            item.set_time(milliseconds // 1000)
            item.set_time_nano_part(milliseconds % 1000 * 1_000_000)
            items.append(item)
        request = self._request(project=self.config.sls_project, logstore=self.config.sls_logstore,
                                topic="task-events", logitems=items, compress=True)
        self.client.put_logs(request)

    async def put(self, records: list[dict[str, str]]) -> None:
        # The SDK retries some server failures internally. A daemon thread keeps
        # that bounded network work out of the event loop AND interpreter exit;
        # cancelling this await never advances our durable cursor.
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def settle(failed: bool) -> None:
            if result.done():
                return
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
                pass  # Worker stopped; retained Redis events will replay.

        threading.Thread(target=send, name="sls-log-upload", daemon=True).start()
        await result


class SlsLogMirror:
    def __init__(self, config, queue, *, writer=None, checkpoints=None, interval: float = 5):
        self.config = config
        self.queue = queue
        self.writer = writer if writer is not None else SlsSdkWriter(config)
        self.checkpoints = checkpoints if checkpoints is not None else Checkpoints(config)
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._next_warning = 0.0
        self._next_cleanup = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sls-log-mirror")

    async def close(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _event(self, task_id: str, item_id: str, fields: list) -> dict[str, str] | None:
        ms, sequence = _cursor(item_id)
        if (not isinstance(fields, list) or len(fields) % 2 or len(fields) > 20
                or any(not isinstance(value, str) for value in fields)):
            return None
        raw = dict(zip(fields[::2], fields[1::2])).get("json")
        if not isinstance(raw, str) or len(raw) > 20000:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                return None
            stamp = value.get("time")
            if not isinstance(stamp, str) or not stamp.endswith("Z") or len(stamp) > 40:
                return None
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if value.get("level") not in {"INFO", "WARNING", "ERROR"}:
                return None
            message = value.get("message")
            if not isinstance(message, str) or not 1 <= len(message) <= 1000:
                return None
            event = {"kind": "event", "taskId": task_id, "environment": self.config.app_env,
                     "id": item_id, "streamMs": str(ms), "streamSeq": str(sequence),
                     "time": stamp, "level": value["level"],
                     "message": sanitize_log_message(message, secrets=(self.config.sls_access_key_id,
                         self.config.sls_access_key_secret, self.config.sls_sts_token or ""))}
            if "actionIndex" in value or "actionTotal" in value:
                index, total = value.get("actionIndex"), value.get("actionTotal")
                if type(index) is not int or type(total) is not int or not 0 <= index <= total <= 100000:
                    return None
                event.update(actionIndex=str(index), actionTotal=str(total))
            return event
        except (ValueError, TypeError):
            return None

    async def sync_task(self, task_id: str) -> bool:
        """Send at most one bounded page; return whether a page made progress."""
        if not UUID_PATTERN.fullmatch(task_id):
            return False
        saved = await asyncio.to_thread(self.checkpoints.get, task_id)
        key = f"{self.config.redis_queue_key}:logs:{task_id}"
        snapshot = await self.queue._call("eval", _SNAPSHOT, 1, key, saved.cursor, str(_PAGE_SIZE))
        if not snapshot:
            await asyncio.to_thread(self.checkpoints.delete, task_id)
            return False
        entries, first, last, added, length = snapshot
        if not last:
            return False
        rebuilt = (_cursor(last) < _cursor(saved.cursor) or added < saved.entries_added
                   or (not saved.truncated and saved.cursor != "0-0" and first
                       and _cursor(first) > _cursor(saved.cursor)
                       and added - saved.entry_count < length))
        if rebuilt:
            # The same key was rebuilt. Do not skip the new stream's contents.
            saved = Checkpoint()
            snapshot = await self.queue._call("eval", _SNAPSHOT, 1, key, saved.cursor, str(_PAGE_SIZE))
            if not snapshot:
                return False
            entries, first, last, added, length = snapshot
        if not entries:
            return False
        # With no previous gap, entry_count is also our position in Redis's
        # entries-added counter. Trimming already mirrored events is harmless;
        # only flag loss when unread entries outnumber the retained stream.
        truncated = saved.truncated or added - saved.entry_count > length
        events = []
        cursor = saved.cursor
        for item_id, fields in entries:
            if _cursor(item_id) <= _cursor(cursor):
                raise ValueError("Invalid stream order")
            cursor = item_id
            event = self._event(task_id, item_id, fields)
            if event is None:
                truncated = True
            else:
                events.append(event)
        first_id = saved.first_id or (events[0]["id"] if events else "")
        checkpoint = Checkpoint(cursor, first_id, saved.entry_count + len(events), truncated, added)
        if first_id:
            ms, sequence = _cursor(cursor)
            marker = {"kind": "checkpoint", "taskId": task_id, "environment": self.config.app_env,
                      "id": cursor, "streamMs": str(ms), "streamSeq": str(sequence),
                      "time": datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(
                          timespec="milliseconds").replace("+00:00", "Z"),
                      "firstId": first_id, "throughId": cursor,
                      "entryCount": str(checkpoint.entry_count), "truncated": str(truncated).lower()}
            await self.writer.put([*events, marker])
        # Never acknowledge before SLS succeeds. A failed local save safely
        # replays the same IDs and marker count on the next pass.
        await asyncio.to_thread(self.checkpoints.save, task_id, checkpoint)
        return True

    async def sync_once(self) -> None:
        cursor = 0
        prefix = f"{self.config.redis_queue_key}:logs:"
        while not self._stopped.is_set():
            cursor, keys = await self.queue._call("scan", cursor=cursor, match=prefix + "*", count=100)
            for key in keys:
                await self.sync_task(key[len(prefix):])
                if self._stopped.is_set():
                    return
            if int(cursor) == 0:
                break
            await asyncio.sleep(0)
        if time.monotonic() >= self._next_cleanup:
            await asyncio.to_thread(self.checkpoints.cleanup)
            self._next_cleanup = time.monotonic() + 3600

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.sync_once()
            except Exception:
                if time.monotonic() >= self._next_warning:
                    LOGGER.warning("SLS 日志同步暂不可用；保留 Redis 日志并稍后重试")
                    self._next_warning = time.monotonic() + 60
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
            except TimeoutError:
                pass
