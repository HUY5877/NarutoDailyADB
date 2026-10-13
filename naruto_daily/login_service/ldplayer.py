"""Safe lifecycle management for service-owned LDPlayer instances."""

from __future__ import annotations

import csv
from contextlib import contextmanager
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from uuid import UUID, uuid4


_MANAGED_NAME = re.compile(
    r"nd-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
_SERIAL = re.compile(r"[A-Za-z0-9._:-]+")
_SIZE = re.compile(r"(\d+)x(\d+)")


class LDPlayerError(RuntimeError):
    """A safe, actionable LDPlayer failure suitable for task status output."""


class LDPlayerPoolFull(LDPlayerError):
    """All explicitly configured LDPlayer pool instances are reserved."""


@dataclass
class LDPlayerInstance:
    name: str
    index: int
    serial: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class _ListedInstance:
    index: int
    name: str
    running: bool


class LDPlayerManager:
    def __init__(
        self,
        console_path: Path,
        template: str,
        state_dir: Path,
        command_timeout: float = 20,
        pool_indices: tuple[int, ...] = (),
    ) -> None:
        if (not isinstance(pool_indices, tuple)
                or any(type(index) is not int or index < 0 for index in pool_indices)
                or len(set(pool_indices)) != len(pool_indices)):
            raise LDPlayerError("LDPlayer pool indices must be unique non-negative integers.")
        if (not template or not isinstance(template, str)) and not pool_indices:
            raise LDPlayerError("An LDPlayer template name or index is required.")
        if command_timeout <= 0:
            raise LDPlayerError("LDPlayer command timeout must be positive.")
        self.console_path = Path(console_path)
        self.adb_path = self.console_path.with_name("adb.exe")
        self.template = template
        self.pool_indices = pool_indices
        self.state_dir = Path(state_dir)
        self.command_timeout = float(command_timeout)
        self._state_path = (self.state_dir / "bindings.json").resolve()

    @staticmethod
    def _task(task_id: str) -> tuple[str, str]:
        try:
            canonical = str(UUID(task_id))
        except (AttributeError, TypeError, ValueError):
            raise LDPlayerError("Invalid login task id; create a new login request.") from None
        return canonical, f"nd-{canonical}"

    @staticmethod
    def _decode(output: bytes) -> str:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return output.decode(encoding)
            except UnicodeDecodeError:
                pass
        raise LDPlayerError("LDPlayer returned unreadable instance information.")

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise LDPlayerError("The LDPlayer operation deadline expired; retry the login request.")
        return remaining

    def _run(self, arguments: list[Path | str], deadline: float, action: str) -> bytes:
        return self._run_result(arguments, deadline, action).stdout

    def _run_result(
        self,
        arguments: list[Path | str],
        deadline: float,
        action: str,
        *,
        full_deadline: bool = False,
        accepted_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        remaining = self._remaining(deadline)
        timeout = remaining if full_deadline else min(self.command_timeout, remaining)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            result = subprocess.run(
                arguments,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired:
            raise LDPlayerError(f"LDPlayer {action} timed out; retry or close the login task.") from None
        except OSError:
            raise LDPlayerError(f"Could not run LDPlayer {action}; check the configured installation.") from None
        if result.returncode not in accepted_returncodes:
            raise LDPlayerError(f"LDPlayer {action} failed; check the emulator and retry.")
        return result

    @contextmanager
    def _exclusive(self, deadline: float):
        """Serialize threads and processes through separately opened file handles."""
        self._remaining(deadline)
        handle = None
        locked = False
        try:
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                lock_path = self.state_dir / "bindings.lock"
                try:
                    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    pass
                else:
                    try:
                        os.write(descriptor, b"\0")
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                while True:
                    handle = lock_path.open("r+b")
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() >= 1:
                        break
                    handle.close()
                    handle = None
                    time.sleep(min(0.05, self._remaining(deadline)))
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError:
                        time.sleep(min(0.05, self._remaining(deadline)))
            except LDPlayerError:
                raise
            except OSError:
                raise LDPlayerError("Could not lock LDPlayer binding state; check the state directory.") from None
            yield
        finally:
            if handle is not None:
                if locked:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()

    def _list(self, deadline: float) -> list[_ListedInstance]:
        output = self._run([self.console_path, "list2"], deadline, "instance listing")
        rows: list[_ListedInstance] = []
        try:
            for fields in csv.reader(io.StringIO(self._decode(output))):
                if not fields or all(not field.strip() for field in fields):
                    continue
                if len(fields) < 7:
                    raise ValueError
                index = int(fields[0].strip())
                name = fields[1].strip()
                state = fields[4].strip()
                if not name or state not in {"0", "1", "2"}:
                    raise ValueError
                rows.append(_ListedInstance(index, name, state != "0"))
        except (ValueError, csv.Error):
            raise LDPlayerError("LDPlayer returned invalid instance information; update or restart LDPlayer.") from None
        return rows

    @staticmethod
    def _find(rows: list[_ListedInstance], name: str) -> _ListedInstance | None:
        return next((row for row in rows if row.name == name), None)

    def _template(self, rows: list[_ListedInstance]) -> _ListedInstance | None:
        if self.template.isdecimal():
            index = int(self.template)
            return next((row for row in rows if row.index == index), None)
        return self._find(rows, self.template)

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self._state_path.exists():
            return {}
        try:
            document = json.loads(self._state_path.read_text("utf-8"))
            bindings = document["bindings"]
            if not isinstance(bindings, dict):
                raise ValueError
            for task_id, binding in bindings.items():
                expected_task, expected_name = self._task(task_id)
                if (expected_task != task_id or not isinstance(binding, dict)
                        or binding.get("index") is not None and type(binding.get("index")) is not int
                        or binding.get("serial") is not None and not isinstance(binding.get("serial"), str)):
                    raise ValueError
                mode = binding.get("mode", "clone")
                if mode == "clone":
                    if binding.get("name") != expected_name or "state" in binding:
                        raise ValueError
                elif mode == "pool":
                    if (not isinstance(binding.get("name"), str) or not binding["name"]
                            or type(binding.get("index")) is not int
                            or binding.get("state") not in {
                                "reserved", "qr_ready", "released", "retryable",
                            }
                            or binding.get("releasedFrom") not in {None, "reserved", "qr_ready"}
                            or (binding.get("state") == "retryable"
                                and binding.get("releasedFrom") != "reserved")):
                        raise ValueError
                else:
                    raise ValueError
            return bindings
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError, LDPlayerError):
            raise LDPlayerError("LDPlayer binding state is invalid; restore the service state backup.") from None

    def _save_state(self, bindings: dict[str, dict[str, Any]]) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.state_dir / f".bindings-{uuid4().hex}.tmp"
            payload = json.dumps({"bindings": bindings}, sort_keys=True, separators=(",", ":"))
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            raise LDPlayerError("Could not save LDPlayer binding state; check the state directory.") from None

    @staticmethod
    def _check_managed(row: _ListedInstance) -> None:
        if row.index == 0:
            raise LDPlayerError("Refusing to operate on LDPlayer index 0; check the managed instance binding.")
        if _MANAGED_NAME.fullmatch(row.name) is None:
            raise LDPlayerError("Refusing to operate on an unmanaged LDPlayer instance.")

    def allocate(self, task_id: str, deadline: float) -> LDPlayerInstance:
        task_id, expected_name = self._task(task_id)
        self._remaining(deadline)
        with self._exclusive(deadline):
            bindings = self._load_state()
            if self.pool_indices:
                return self._allocate_pool(task_id, bindings, deadline)
            binding = bindings.get(task_id)
            if binding is not None and binding.get("mode", "clone") != "clone":
                raise LDPlayerError("Login task is bound to an LDPlayer pool; use the configured pool manager.")
            if binding is not None and binding["name"] != expected_name:
                raise LDPlayerError("LDPlayer binding does not match the login task; restore service state.")
            if binding is None:
                binding = {"index": None, "name": expected_name, "serial": None}
                bindings[task_id] = binding
                # Persist intent before copy so a process timeout cannot orphan a late instance.
                self._save_state(bindings)

            rows = self._list(deadline)
            existing = self._find(rows, expected_name)
            if existing is not None:
                self._check_managed(existing)
                binding["index"] = existing.index
                self._save_state(bindings)
                return LDPlayerInstance(existing.name, existing.index, binding.get("serial"))

            template = self._template(rows)
            if template is None:
                raise LDPlayerError("Configured LDPlayer template was not found; check the template setting.")
            if template.index == 0:
                raise LDPlayerError(
                    "Refusing to use LDPlayer index 0 as a template; configure a dedicated stopped template."
                )
            if template.running:
                raise LDPlayerError("Configured LDPlayer template is running; close it manually before retrying.")

            copy_result = self._run_result(
                [self.console_path, "copy", "--name", expected_name, "--from", self.template],
                deadline,
                "instance copy",
                full_deadline=True,
                accepted_returncodes=(0, 1),
            )
            if copy_result.returncode == 1:
                created = self._find(self._list(deadline), expected_name)
                if created is None:
                    raise LDPlayerError(
                        "LDPlayer instance copy did not create the requested instance; retry the login request."
                    )
                self._check_managed(created)
                binding["index"] = created.index
                binding["serial"] = None
                self._save_state(bindings)
                return LDPlayerInstance(created.name, created.index)
            while True:
                created = self._find(self._list(deadline), expected_name)
                if created is not None:
                    self._check_managed(created)
                    binding["index"] = created.index
                    binding["serial"] = None
                    self._save_state(bindings)
                    return LDPlayerInstance(created.name, created.index)
                time.sleep(min(0.2, self._remaining(deadline)))

    @staticmethod
    def _pool_occupancy(bindings: dict[str, dict[str, Any]]) -> dict[int, str]:
        occupied: dict[int, str] = {}
        for task_id, binding in bindings.items():
            if (binding.get("mode") != "pool"
                    or binding.get("state") in {"released", "retryable"}):
                continue
            index = binding["index"]
            if index in occupied:
                raise LDPlayerError("LDPlayer pool state assigns one instance to multiple active tasks.")
            occupied[index] = task_id
        return occupied

    def _allocate_pool(
        self,
        task_id: str,
        bindings: dict[str, dict[str, Any]],
        deadline: float,
    ) -> LDPlayerInstance:
        binding = bindings.get(task_id)
        occupied = self._pool_occupancy(bindings)
        rows = self._list(deadline)
        rows_by_index = {row.index: row for row in rows}
        if binding is not None and binding.get("state") != "retryable":
            if binding.get("mode") != "pool":
                raise LDPlayerError("Login task is bound to a cloned LDPlayer instance.")
            if binding["state"] == "released":
                raise LDPlayerError("This login task released its pool instance and cannot reserve it again.")
            if binding["index"] not in self.pool_indices:
                raise LDPlayerError("Login task binding is outside the configured LDPlayer pool.")
            row = rows_by_index.get(binding["index"])
            if row is None or row.name != binding["name"]:
                raise LDPlayerError("Bound LDPlayer pool instance is unavailable or was renamed.")
            return LDPlayerInstance(row.name, row.index, binding.get("serial"), task_id)

        free = [index for index in self.pool_indices if index not in occupied]
        if not free:
            raise LDPlayerPoolFull("All LDPlayer login instances are busy; retry later.")
        row = next((rows_by_index[index] for index in free if index in rows_by_index), None)
        if row is None:
            raise LDPlayerError("No free configured LDPlayer pool instance was found; check the pool indices.")
        bindings[task_id] = {
            "name": row.name,
            "index": row.index,
            "serial": None,
            "mode": "pool",
            "state": "reserved",
        }
        self._save_state(bindings)
        return LDPlayerInstance(row.name, row.index, None, task_id)

    def has_capacity(self) -> bool:
        if not self.pool_indices:
            return True
        deadline = time.time() + self.command_timeout
        with self._exclusive(deadline):
            bindings = self._load_state()
            occupied = self._pool_occupancy(bindings)
            free = [index for index in self.pool_indices if index not in occupied]
            if not free:
                return False
            available = {row.index for row in self._list(deadline)}
            return any(index in available for index in free)

    def pool_status(self) -> list[dict[str, object]]:
        if not self.pool_indices:
            return []
        deadline = time.time() + self.command_timeout
        with self._exclusive(deadline):
            bindings = self._load_state()
            occupied = self._pool_occupancy(bindings)
            rows_by_index = {row.index: row for row in self._list(deadline)}
            status: list[dict[str, object]] = []
            for index in self.pool_indices:
                row = rows_by_index.get(index)
                if row is None:
                    raise LDPlayerError("Configured LDPlayer pool instance is unavailable.")
                task_id = occupied.get(index)
                if task_id is None:
                    status.append({"name": row.name, "index": index, "taskId": None, "state": "idle"})
                    continue
                binding = bindings[task_id]
                if binding["name"] != row.name:
                    raise LDPlayerError("Bound LDPlayer pool instance was renamed.")
                status.append({
                    "name": row.name,
                    "index": index,
                    "taskId": task_id,
                    "state": binding["state"],
                })
            return status

    def mark_ready(self, task_id: str) -> None:
        task_id, _ = self._task(task_id)
        deadline = time.time() + self.command_timeout
        with self._exclusive(deadline):
            bindings = self._load_state()
            binding = bindings.get(task_id)
            if binding is None or binding.get("mode") != "pool":
                raise LDPlayerError("Login task has no active LDPlayer pool binding.")
            if binding["state"] not in {"reserved", "qr_ready"}:
                raise LDPlayerError("This login task does not hold an active LDPlayer pool instance.")
            binding["state"] = "qr_ready"
            self._save_state(bindings)

    def prepare_retry(self, task_id: str) -> None:
        task_id, _ = self._task(task_id)
        deadline = time.time() + self.command_timeout
        with self._exclusive(deadline):
            bindings = self._load_state()
            binding = bindings.get(task_id)
            if binding is None:
                return
            if binding.get("mode") != "pool":
                raise LDPlayerError("Login task has no LDPlayer pool binding to retry.")
            if binding["state"] == "retryable":
                return
            if binding["state"] != "released":
                raise LDPlayerError("Close the LDPlayer pool instance before preparing a retry.")
            if binding.get("releasedFrom") != "reserved":
                raise LDPlayerError("A QR-ready login task cannot be prepared for retry.")
            binding["state"] = "retryable"
            self._save_state(bindings)

    def _tracked(
        self, instance: LDPlayerInstance, deadline: float,
    ) -> tuple[str, dict[str, Any], _ListedInstance, str]:
        if not isinstance(instance, LDPlayerInstance):
            raise LDPlayerError("Refusing to operate on an unmanaged LDPlayer instance.")
        bindings = self._load_state()
        binding = bindings.get(instance.task_id) if instance.task_id is not None else None
        if binding is not None and binding.get("mode") == "pool":
            task_id = instance.task_id
            if (binding.get("state") not in {"reserved", "qr_ready"}
                    or binding.get("name") != instance.name
                    or binding.get("index") != instance.index):
                raise LDPlayerError("LDPlayer pool instance owner no longer holds this slot.")
            if instance.index not in self.pool_indices:
                raise LDPlayerError("LDPlayer pool binding is outside the configured pool.")
            row = next((candidate for candidate in self._list(deadline)
                        if candidate.index == instance.index), None)
            if row is None or row.name != binding["name"]:
                raise LDPlayerError("Bound LDPlayer pool instance is unavailable or was renamed.")
            mode = "pool"
        else:
            if instance.task_id is not None:
                raise LDPlayerError("LDPlayer pool instance owner no longer holds this slot.")
            if _MANAGED_NAME.fullmatch(instance.name) is None:
                raise LDPlayerError("Refusing to operate on an unmanaged LDPlayer instance.")
            task_id = instance.name.removeprefix("nd-")
            binding = bindings.get(task_id)
            if (binding is None or binding.get("mode", "clone") != "clone"
                    or binding.get("name") != instance.name):
                raise LDPlayerError("Managed LDPlayer binding was not found; allocate the task again.")
            row = self._find(self._list(deadline), instance.name)
            if row is None:
                raise LDPlayerError("Managed LDPlayer instance was not found; allocate the task again.")
            self._check_managed(row)
            mode = "clone"
        instance.index = row.index
        binding["index"] = row.index
        return task_id, bindings, row, mode

    def start(self, instance: LDPlayerInstance, deadline: float) -> str:
        with self._exclusive(deadline):
            task_id, bindings, current, mode = self._tracked(instance, deadline)
            if not current.running:
                self._run(
                    [self.console_path, "launch", "--name", instance.name],
                    deadline,
                    "instance launch",
                )
                while True:
                    rows = self._list(deadline)
                    current = (next((row for row in rows if row.index == instance.index), None)
                               if mode == "pool" else self._find(rows, instance.name))
                    if current is not None:
                        if mode == "pool":
                            if current.name != instance.name:
                                raise LDPlayerError("Bound LDPlayer pool instance was renamed during startup.")
                        else:
                            self._check_managed(current)
                        instance.index = current.index
                        bindings[task_id]["index"] = current.index
                        if current.running:
                            break
                    time.sleep(min(0.2, self._remaining(deadline)))

            while True:
                try:
                    output = self._run(
                        [self.console_path, "adb", "--index", str(instance.index),
                         "--command", "get-serialno"],
                        deadline,
                        "ADB serial lookup",
                    )
                    lines = [line.strip() for line in self._decode(output).splitlines() if line.strip()]
                    serial = lines[-1] if lines else ""
                    if serial and serial != "unknown" and _SERIAL.fullmatch(serial) is not None:
                        break
                except LDPlayerError:
                    pass
                try:
                    time.sleep(min(0.2, self._remaining(deadline)))
                except LDPlayerError:
                    raise LDPlayerError(
                        "LDPlayer did not provide a usable ADB serial before the deadline; restart and retry."
                    ) from None

            last_problem = "boot"
            while True:
                try:
                    booted = self._decode(self._run(
                        [self.adb_path, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                        deadline,
                        "ADB readiness check",
                    )).strip()
                    if booted == "1":
                        size_output = self._decode(self._run(
                            [self.adb_path, "-s", serial, "shell", "wm", "size"],
                            deadline,
                            "display check",
                        ))
                        sizes = _SIZE.findall(size_output)
                        if sizes and tuple(map(int, sizes[-1])) == (1920, 1080):
                            break
                        last_problem = "resolution"
                except LDPlayerError:
                    # Preserve a confirmed bad display size if the final probe
                    # itself reaches the deadline on the next iteration.
                    if last_problem != "resolution":
                        last_problem = "boot"
                try:
                    time.sleep(min(0.2, self._remaining(deadline)))
                except LDPlayerError:
                    if last_problem == "resolution":
                        raise LDPlayerError(
                            "Managed LDPlayer must use 1920x1080 resolution; update the template and retry."
                        ) from None
                    raise LDPlayerError(
                        "Managed LDPlayer did not finish booting before the deadline; retry the login request."
                    ) from None

            instance.serial = serial
            bindings[task_id]["serial"] = serial
            self._save_state(bindings)
            return serial

    def launch_game(self, instance: LDPlayerInstance, package: str, deadline: float) -> None:
        if not isinstance(package, str) or _PACKAGE.fullmatch(package) is None:
            raise LDPlayerError("Invalid Android package name; check the game package setting.")
        with self._exclusive(deadline):
            task_id, bindings, current, _ = self._tracked(instance, deadline)
            if not current.running:
                raise LDPlayerError("Managed LDPlayer is not running; start it before launching the game.")
            serial = bindings[task_id].get("serial")
            if (not isinstance(serial, str) or serial == "unknown"
                    or _SERIAL.fullmatch(serial) is None):
                raise LDPlayerError("Start the managed LDPlayer before launching the game.")
            if instance.serial is not None and instance.serial != serial:
                raise LDPlayerError("LDPlayer ADB serial no longer matches the task binding; start it again.")
            self._run(
                [self.adb_path, "-s", serial, "shell", "am", "force-stop", package],
                deadline,
                "game reset",
            )
            self._run(
                [self.adb_path, "-s", serial, "shell", "am", "force-stop", "com.tencent.mobileqq"],
                deadline,
                "QQ reset",
            )
            self._run(
                [self.console_path, "runapp", "--name", instance.name, "--packagename", package],
                deadline,
                "game launch",
            )

    def close(self, task_id: str) -> None:
        task_id, expected_name = self._task(task_id)
        deadline = time.time() + self.command_timeout
        with self._exclusive(deadline):
            bindings = self._load_state()
            binding = bindings.get(task_id)
            if binding is None:
                return
            if binding.get("mode") == "pool":
                if binding["state"] in {"released", "retryable"}:
                    return
                released_from = binding["state"]
                if binding["index"] not in self.pool_indices:
                    raise LDPlayerError("Login task binding is outside the configured LDPlayer pool.")
                current = next((row for row in self._list(deadline)
                                if row.index == binding["index"]), None)
                if current is None or current.name != binding["name"]:
                    raise LDPlayerError("Bound LDPlayer pool instance is unavailable or was renamed.")
                if current.running:
                    self._run(
                        [self.console_path, "quit", "--name", current.name],
                        deadline,
                        "instance close",
                    )
                    while True:
                        current = next((row for row in self._list(deadline)
                                        if row.index == binding["index"]), None)
                        if current is None or not current.running:
                            break
                        time.sleep(min(0.2, self._remaining(deadline)))
                binding["state"] = "released"
                binding["releasedFrom"] = released_from
                self._save_state(bindings)
                return
            if binding.get("name") != expected_name:
                raise LDPlayerError("LDPlayer binding does not match the login task; restore service state.")
            current = self._find(self._list(deadline), expected_name)
            if current is None:
                return
            self._check_managed(current)
            binding["index"] = current.index
            if current.running:
                self._run(
                    [self.console_path, "quit", "--name", expected_name],
                    deadline,
                    "instance close",
                )
                while True:
                    current = self._find(self._list(deadline), expected_name)
                    if current is None or not current.running:
                        break
                    time.sleep(min(0.2, self._remaining(deadline)))
            self._save_state(bindings)
