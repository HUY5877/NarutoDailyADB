# LDPlayer Coordinate Daily Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a testable Python CLI that drives a fixed-resolution LDPlayer instance through explicit ADB coordinate actions for four independently runnable daily flows.

**Architecture:** A subprocess-based ADB adapter is isolated behind a small interface. Validated JSON configuration supplies all coordinates and delays; a deterministic runner executes typed actions, while task builders produce sequences for sign-in, stamina, free recruitment, and one Abundance battle.

**Tech Stack:** Python 3.11+, standard library (`argparse`, `dataclasses`, `json`, `subprocess`, `time`, `logging`), pytest for tests.

**Spec:** `docs/superpowers/specs/2026-08-18-ldplayer-coordinate-daily-design.md`

## Global Constraints

- Default ADB path is `E:\雷电模拟器\leidian\LDPlayer9\adb.exe`.
- The Android resolution must match the configured width and height before any input is sent.
- No image recognition, OCR, memory access, injection, package modification, CAPTCHA handling, payment actions, or anti-detection behavior.
- `--dry-run` never sends an ADB input command.
- Abort immediately on interruption or ADB command failure.
- A task has at most 100 actions and 300 seconds of configured runtime.

---

### Task 1: ADB client and device preflight

**Files:**
- Create: `pyproject.toml`
- Create: `naruto_daily/__init__.py`
- Create: `naruto_daily/adb_client.py`
- Test: `tests/test_adb_client.py`

**Interfaces:**
- Produces: `AdbClient(adb_path: Path, serial: str | None = None)`, `devices() -> list[str]`, `screen_size() -> tuple[int, int]`, `tap(x: int, y: int) -> None`, `swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None`, and `back() -> None`.

- [ ] Write tests with an injected command runner that assert exact `adb devices`, `shell wm size`, `shell input tap`, `shell input swipe`, and keyevent commands.
- [ ] Run `python -m pytest tests/test_adb_client.py -v` and confirm failure because `naruto_daily.adb_client` does not exist.
- [ ] Implement `AdbClient`, parsing the physical size from output such as `Physical size: 1280x720` and raising `AdbError` on a nonzero exit code.
- [ ] Run `python -m pytest tests/test_adb_client.py -v` and confirm all tests pass.
- [ ] Commit with `git add pyproject.toml naruto_daily tests && git commit -m "feat: add adb device client"`.

### Task 2: Typed configuration and validation

**Files:**
- Create: `naruto_daily/config.py`
- Create: `config.example.json`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Point(x: int, y: int)`, `Settings(adb_path: Path, screen_width: int, screen_height: int, tasks: dict[str, list[dict]])`, and `load_settings(path: Path) -> Settings`.
- Consumes: no runtime interface from Task 1.

- [ ] Write tests proving valid configuration loads and invalid dimensions, negative coordinates, more than 100 task actions, waits over 300 seconds, and unsupported action names raise `ConfigError`.
- [ ] Run `python -m pytest tests/test_config.py -v` and confirm failure because the module is absent.
- [ ] Implement strict parsing for `tap`, `swipe`, `wait`, and `back` actions; reject unknown JSON keys to expose spelling mistakes.
- [ ] Add `config.example.json` with `1280x720`, the confirmed ADB path, and empty action arrays so no unsafe coordinates are guessed.
- [ ] Run `python -m pytest tests/test_config.py -v` and confirm all tests pass.
- [ ] Commit with `git add naruto_daily/config.py config.example.json tests/test_config.py && git commit -m "feat: validate coordinate configuration"`.

### Task 3: Deterministic action runner

**Files:**
- Create: `naruto_daily/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `AdbClient` from Task 1 and validated action dictionaries from Task 2.
- Produces: `ActionRunner(client: AdbClient, sleep: Callable[[float], None], dry_run: bool = False)` and `run(actions: Sequence[dict]) -> RunSummary`.

- [ ] Write tests proving action order, seconds-to-milliseconds conversion, dry-run suppression of all input, immediate abort on `AdbError`, and enforcement of the 300-second total.
- [ ] Run `python -m pytest tests/test_runner.py -v` and confirm failure because the runner is absent.
- [ ] Implement dispatch for the four allowed action types and return counts plus elapsed configured wait time in `RunSummary`.
- [ ] Run `python -m pytest tests/test_runner.py -v` and confirm all tests pass.
- [ ] Commit with `git add naruto_daily/runner.py tests/test_runner.py && git commit -m "feat: execute bounded coordinate actions"`.

### Task 4: Daily task composition and battle loop

**Files:**
- Create: `naruto_daily/tasks.py`
- Test: `tests/test_tasks.py`

**Interfaces:**
- Consumes: named task arrays from `Settings.tasks`.
- Produces: `build_task(settings: Settings, name: Literal["sign_in", "stamina", "recruit", "abundance"]) -> list[dict]` and `build_all(settings: Settings) -> list[dict]`.

- [ ] Write tests that each named task remains independently selectable, `build_all` preserves the approved order, and the abundance battle expansion never makes the result exceed 100 actions or 300 seconds.
- [ ] Run `python -m pytest tests/test_tasks.py -v` and confirm failure because task composition is absent.
- [ ] Implement composition without inventing coordinates; battle repetition must come entirely from explicit validated configuration.
- [ ] Run `python -m pytest tests/test_tasks.py -v` and confirm all tests pass.
- [ ] Commit with `git add naruto_daily/tasks.py tests/test_tasks.py && git commit -m "feat: compose daily coordinate flows"`.

### Task 5: CLI, documentation, and real-device preflight

**Files:**
- Create: `naruto_daily/cli.py`
- Create: `tests/test_cli.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `AdbClient`, `load_settings`, `ActionRunner`, `build_task`, and `build_all`.
- Produces commands `naruto-daily check --config PATH` and `naruto-daily run TASK --config PATH [--dry-run]`, where `TASK` is `sign_in`, `stamina`, `recruit`, `abundance`, or `all`.

- [ ] Write CLI tests proving `check` rejects zero or multiple devices without an explicit serial, refuses a resolution mismatch, and `run all` requires a five-second cancellable countdown unless `--dry-run` is set.
- [ ] Run `python -m pytest tests/test_cli.py -v` and confirm failure because the CLI is absent.
- [ ] Implement argparse commands, logging, preflight, resolution verification, countdown, and nonzero exit codes for configuration or ADB failures.
- [ ] Add the console entry point to `pyproject.toml` and document virtual environment setup, `check`, dry-run, single-task calibration, Ctrl+C cancellation, and the online-game account warning.
- [ ] Run `python -m pytest -v` and confirm the complete suite passes.
- [ ] With LDPlayer open, run `python -m naruto_daily.cli check --config config.local.json`; expect exactly one device and a matching Android resolution before any coordinate is calibrated.
- [ ] Commit with `git add naruto_daily/cli.py tests/test_cli.py pyproject.toml README.md && git commit -m "feat: add safe coordinate automation cli"`.
