"""Real-process regressions for LDPlayer commands that spawn an ADB server."""

import os
from pathlib import Path
import sys
import time

import pytest

from naruto_daily.login_service.ldplayer import LDPlayerError, LDPlayerManager


@pytest.mark.skipif(os.name != "nt", reason="Windows subprocess.run has a second, unbounded pipe drain")
@pytest.mark.parametrize("parent_times_out", [False, True])
def test_command_does_not_wait_for_descendant_inherited_output(tmp_path, parent_times_out):
    stop = tmp_path / "stop"
    ready = tmp_path / "ready"
    finished = tmp_path / "finished"
    descendant = tmp_path / "descendant.py"
    descendant.write_text(
        "from pathlib import Path\n"
        "import sys, time\n"
        "stop, ready, finished = map(Path, sys.argv[1:])\n"
        "ready.touch()\n"
        "deadline = time.monotonic() + 4\n"
        "while not stop.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "finished.touch()\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "subprocess.Popen([sys.executable, sys.argv[1], *sys.argv[2:5]],\n"
        "                 stdout=sys.stdout, stderr=sys.stderr, close_fds=False)\n"
        "deadline = time.monotonic() + 2\n"
        "while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.buffer.write(b'emulator-5554\\r\\n'); sys.stdout.flush()\n"
        "sys.stderr.buffer.write(b'ADB status\\r\\n'); sys.stderr.flush()\n"
        "if sys.argv[5] == 'wait': time.sleep(8)\n",
        encoding="utf-8",
    )
    manager = LDPlayerManager(Path(sys.executable), "unused", tmp_path / "state", command_timeout=0.8)
    arguments = [sys.executable, str(parent), str(descendant), str(stop), str(ready), str(finished),
                 "wait" if parent_times_out else "exit"]
    # Deliberately keep the descendant alive much longer than the command's
    # timeout, as an ADB server would. It exits itself or on our private stop file.
    started = time.monotonic()
    try:
        if parent_times_out:
            with pytest.raises(LDPlayerError, match="timed out"):
                manager._run_result(arguments, time.time() + 6, "ADB serial lookup")
        else:
            result = manager._run_result(arguments, time.time() + 6, "ADB serial lookup")
            assert result.returncode == 0
            assert result.stdout == b"emulator-5554\r\n"
            assert result.stderr == b"ADB status\r\n"
        assert ready.exists(), "The descendant must have inherited the command's output handles"
        assert time.monotonic() - started < 2.5, "Command waited for unrelated descendant output handles"
        assert not finished.exists(), "The command must return while its descendant remains alive"
    finally:
        stop.touch()
        cleanup_deadline = time.monotonic() + 5
        while ready.exists() and not finished.exists() and time.monotonic() < cleanup_deadline:
            time.sleep(0.02)
