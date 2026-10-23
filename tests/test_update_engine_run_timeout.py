"""A phase past its timeout is stopped together with what it started
(scripts/update_engine.py ``_run`` / ``_stop_process_tree``)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil
import pytest

from scripts import update_engine

# Starts a grandchild that keeps writing to a file, prints nothing, and waits.
_PARENT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
open(sys.argv[3], "w").write(str(child.pid))
time.sleep(600)
"""
_GRANDCHILD = """
import sys, time
while True:
    with open(sys.argv[1], "a") as f:
        f.write("x")
    time.sleep(0.2)
"""


def test_a_timed_out_phase_stops_the_processes_it_started(tmp_path: Path) -> None:
    written = tmp_path / "written.txt"
    pid_file = tmp_path / "grandchild.pid"
    command = [sys.executable, "-c", _PARENT, _GRANDCHILD, str(written), str(pid_file)]
    with pytest.raises(update_engine.UpdateError, match="timed out after 5 s and was stopped"):
        update_engine._run(command, cwd=tmp_path, label="slow phase", timeout=5)
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    assert not psutil.pid_exists(grandchild) or psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE
    size = written.stat().st_size
    time.sleep(1)
    assert written.stat().st_size == size  # nothing writes any more


def test_a_phase_within_its_timeout_returns_its_output(tmp_path: Path) -> None:
    result = update_engine._run(
        [sys.executable, "-c", "print('hello')"], cwd=tmp_path, label="quick", timeout=60, encoding="utf-8"
    )
    assert result.returncode == 0 and result.stdout.strip() == "hello"


def test_a_failing_phase_still_raises_with_its_output(tmp_path: Path) -> None:
    with pytest.raises(update_engine.UpdateError, match="failed with exit 3: boom"):
        update_engine._run(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            cwd=tmp_path, label="broken", timeout=60,
        )


def test_a_command_that_cannot_start_is_an_update_error(tmp_path: Path) -> None:
    with pytest.raises(update_engine.UpdateError, match="could not run"):
        update_engine._run([str(tmp_path / "no-such-program")], cwd=tmp_path, label="missing", timeout=60)
