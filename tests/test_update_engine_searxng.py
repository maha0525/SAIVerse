"""SearXNG in the update (docs/intent/update_completes_with_new_code.md section 3).

What is pinned here:

- which processes count as *this checkout's* SearXNG server: python running
  ``-m searx.webapp`` with the python of this checkout's SearXNG venv (on
  ``cmdline[0]`` or ``exe()``), plus the start.bat window and the PowerShell of
  run_searxng_server.ps1 above it, and nothing else;
- stopping it fails closed like the frontend, and a checkout without SearXNG
  lists no process at all;
- it is started again the way start.bat / start.sh start it;
- the dependency step checks the pinned release and reinstalls it when the
  check fails, does nothing without SearXNG, and never fails the update.

No real process is started, signalled or killed and no real installer runs:
psutil is replaced by fakes, ``subprocess.Popen`` and ``_run`` by mocks.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil
import pytest

from scripts import update_engine

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _default_searxng_venv(monkeypatch):  # type: ignore[no-untyped-def]
    """Every case uses the default venv location inside its own checkout."""
    monkeypatch.delenv("SEARXNG_VENV_DIR", raising=False)


class FakeProcess:
    """Just enough of psutil.Process for the SearXNG search and stop."""

    def __init__(
        self,
        pid: int,
        name: str,
        *,
        cmdline: list[str] | None = None,
        exe: str = "",
        parents: list[FakeProcess] | None = None,
        deny_exe: bool = False,
        gone: bool = False,
    ) -> None:
        self.pid = pid
        self._name = name
        self._cmdline = cmdline or []
        self._exe = exe
        self._parents = parents or []
        self._deny_exe = deny_exe
        self._gone = gone
        self.terminated = False
        self.killed = False

    def name(self) -> str:
        if self._gone:
            raise psutil.NoSuchProcess(self.pid)
        return self._name

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def exe(self) -> str:
        if self._deny_exe:
            raise psutil.AccessDenied(self.pid)
        return self._exe

    def parents(self) -> list[FakeProcess]:
        return list(self._parents)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _checkout_with_searxng(root: Path) -> Path:
    venv = root / "scripts" / ".searxng-venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    (venv / "bin").mkdir()
    (venv / "bin" / "python").write_bytes(b"")
    return root


def _venv_python(project: Path) -> str:
    return str(project / "scripts" / ".searxng-venv" / "Scripts" / "python.exe")


def _pids(processes: list) -> set[int]:  # type: ignore[type-arg]
    return {process.pid for process in processes}


# --- which processes are this checkout's SearXNG ---------------------------------


def test_the_windows_launcher_and_its_child_are_found_with_their_window(tmp_path: Path) -> None:
    """The shape start.bat leaves on Windows: cmd window -> PowerShell ->
    the venv's python.exe (a launcher) -> the base interpreter, which keeps
    the venv path as cmdline[0] while its exe is the base python."""
    project = _checkout_with_searxng(tmp_path / "SAIVerse")
    venv_python = _venv_python(project)
    explorer = FakeProcess(1, "explorer.exe", cmdline=["explorer.exe"])
    window = FakeProcess(
        2,
        "cmd.exe",
        cmdline=["cmd", "/k", update_engine.SEARXNG_WINDOW_COMMAND],
        parents=[explorer],
    )
    shell = FakeProcess(
        3,
        "powershell.exe",
        cmdline=["powershell", "-ExecutionPolicy", "Bypass", "-File", "scripts\\run_searxng_server.ps1"],
        parents=[window, explorer],
    )
    launcher = FakeProcess(
        4,
        "python.exe",
        cmdline=[venv_python, "-m", "searx.webapp"],
        exe=venv_python,
        parents=[shell, window, explorer],
    )
    server = FakeProcess(
        5,
        "python.exe",
        cmdline=[venv_python, "-m", "searx.webapp"],
        exe=r"C:\Python313\python.exe",
        parents=[launcher, shell, window, explorer],
    )

    found = update_engine.find_searxng_servers(project, processes=[explorer, window, shell, launcher, server])

    assert _pids(found) == {2, 3, 4, 5}


def test_a_linux_server_is_found_by_its_venv_path(tmp_path: Path) -> None:
    """run_searxng_server.sh ``exec``s the venv python by its absolute path;
    the bash of start.sh above it is the user's and stays."""
    project = _checkout_with_searxng(tmp_path / "SAIVerse")
    venv_python = str(project / "scripts" / ".searxng-venv" / "bin" / "python")
    start_sh = FakeProcess(1, "bash", cmdline=["bash", "./start.sh"])
    server = FakeProcess(
        2, "python3", cmdline=[venv_python, "-m", "searx.webapp"], exe="/usr/bin/python3.12", parents=[start_sh]
    )
    assert _pids(update_engine.find_searxng_servers(project, processes=[start_sh, server])) == {2}


def test_another_checkouts_searxng_is_not_ours(tmp_path: Path) -> None:
    main = _checkout_with_searxng(tmp_path / "SAIVerse")
    other = _checkout_with_searxng(tmp_path / "SAIVerse-copy")
    theirs = FakeProcess(1, "python.exe", cmdline=[_venv_python(other), "-m", "searx.webapp"], exe=_venv_python(other))
    ours = FakeProcess(2, "python.exe", cmdline=[_venv_python(main), "-m", "searx.webapp"], exe=_venv_python(main))
    assert _pids(update_engine.find_searxng_servers(main, processes=[theirs, ours])) == {2}


def test_other_python_processes_are_never_searxng(tmp_path: Path) -> None:
    """The backend, the installer's own check, a python the user started from
    the SearXNG venv for something else: none run ``-m searx.webapp``, or none
    run it with this venv's python."""
    project = _checkout_with_searxng(tmp_path / "SAIVerse")
    venv_python = _venv_python(project)
    others = [
        FakeProcess(1, "python.exe", cmdline=[str(project / ".venv" / "Scripts" / "python.exe"), "main.py", "city_a"]),
        FakeProcess(2, "python.exe", cmdline=[venv_python, str(project / "scripts" / "searxng_install.py"), "check"]),
        FakeProcess(3, "python.exe", cmdline=[r"C:\Python313\python.exe", "-m", "searx.webapp"], exe=r"C:\Python313\python.exe"),
        FakeProcess(4, "node.exe", cmdline=[venv_python, "-m", "searx.webapp"]),
        FakeProcess(5, "python.exe", cmdline=[venv_python, "-c", "import searx.webapp"]),
    ]
    assert update_engine.find_searxng_servers(project, processes=others) == []


def test_only_the_searxng_window_and_script_are_taken_from_the_ancestors(tmp_path: Path) -> None:
    project = _checkout_with_searxng(tmp_path / "SAIVerse")
    users_shell = FakeProcess(10, "cmd.exe", cmdline=["cmd.exe"])
    users_powershell = FakeProcess(11, "powershell.exe", cmdline=["powershell.exe"])
    server = FakeProcess(
        1,
        "python.exe",
        cmdline=[_venv_python(project), "-m", "searx.webapp"],
        parents=[users_powershell, users_shell],
    )
    assert _pids(update_engine.find_searxng_servers(project, processes=[server])) == {1}


def test_vanished_and_uninspectable_processes_are_skipped(tmp_path: Path) -> None:
    project = _checkout_with_searxng(tmp_path / "SAIVerse")
    gone = FakeProcess(1, "python.exe", gone=True)
    hidden_exe = FakeProcess(2, "python.exe", cmdline=[_venv_python(project), "-m", "searx.webapp"], deny_exe=True)
    assert _pids(update_engine.find_searxng_servers(project, processes=[gone, hidden_exe])) == {2}


def test_without_a_searxng_venv_no_process_is_listed(tmp_path: Path) -> None:
    """A checkout without SearXNG must not even look at the machine's processes."""
    with patch.object(update_engine, "_import_psutil") as import_psutil:
        assert update_engine.find_searxng_servers(tmp_path) == []
        assert update_engine.stop_searxng_servers(tmp_path) is False
    import_psutil.assert_not_called()


def test_a_venv_named_by_searxng_venv_dir_is_the_one_matched(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    project = tmp_path / "SAIVerse"
    venv = project / "custom-venv"
    (venv / "Scripts").mkdir(parents=True)
    monkeypatch.setenv("SEARXNG_VENV_DIR", "custom-venv")  # relative to the checkout
    python = str(venv / "Scripts" / "python.exe")
    server = FakeProcess(1, "python.exe", cmdline=[python, "-m", "searx.webapp"], exe=python)
    assert _pids(update_engine.find_searxng_servers(project, processes=[server])) == {1}


# --- stopping it -------------------------------------------------------------------


def _fake_psutil(still_alive: set[int]) -> SimpleNamespace:
    def wait_procs(processes, timeout):  # type: ignore[no-untyped-def]
        alive = [p for p in processes if p.pid in still_alive]
        return [p for p in processes if p.pid not in still_alive], alive

    return SimpleNamespace(
        Error=psutil.Error,
        NoSuchProcess=psutil.NoSuchProcess,
        AccessDenied=psutil.AccessDenied,
        wait_procs=wait_procs,
    )


def test_stop_terminates_the_server_and_reports_it_was_running(tmp_path: Path) -> None:
    server = FakeProcess(1, "python.exe")
    window = FakeProcess(2, "cmd.exe")
    with patch.object(update_engine, "find_searxng_servers", side_effect=[[server, window], []]), patch.object(
        update_engine, "_import_psutil", return_value=_fake_psutil(still_alive=set())
    ):
        assert update_engine.stop_searxng_servers(tmp_path) is True
    assert server.terminated and window.terminated
    assert not server.killed


def test_stop_fails_closed_when_the_server_survives_kill(tmp_path: Path) -> None:
    stubborn = FakeProcess(1, "python.exe")
    with patch.object(update_engine, "find_searxng_servers", return_value=[stubborn]), patch.object(
        update_engine, "_import_psutil", return_value=_fake_psutil(still_alive={1})
    ):
        with pytest.raises(update_engine.UpdateError, match="SearXNG server could not be stopped"):
            update_engine.stop_searxng_servers(tmp_path)
    assert stubborn.terminated and stubborn.killed


def test_nothing_running_is_reported_as_not_running(tmp_path: Path) -> None:
    with patch.object(update_engine, "find_searxng_servers", return_value=[]):
        assert update_engine.stop_searxng_servers(tmp_path) is False


# --- starting it again ----------------------------------------------------------------


def test_the_window_command_is_the_one_start_bat_opens() -> None:
    """start_searxng reopens exactly the window the launchers open."""
    for launcher in ("start.bat", "start-dev.bat"):
        text = (REPO_ROOT / launcher).read_text(encoding="ascii")
        assert f'cmd /k "{update_engine.SEARXNG_WINDOW_COMMAND}"' in text
        assert "scripts\\.searxng-venv\\Scripts\\python.exe" in text


def test_windows_searxng_restarts_in_a_start_bat_style_window(tmp_path: Path) -> None:
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine.subprocess, "Popen", return_value=MagicMock(pid=5)
    ) as popen, patch.dict(os.environ, {"COMSPEC": "cmd.exe"}):
        update_engine.start_searxng(tmp_path)
    args, kwargs = popen.call_args
    assert list(args[0]) == ["cmd.exe", "/k", update_engine.SEARXNG_WINDOW_COMMAND]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["creationflags"] == update_engine._CREATE_NEW_CONSOLE
    assert not {"stdin", "stdout", "stderr"} & set(kwargs)


def test_posix_searxng_restarts_with_its_script_in_a_new_session(tmp_path: Path) -> None:
    with patch.object(update_engine, "_is_windows", return_value=False), patch.object(
        update_engine.subprocess, "Popen", return_value=MagicMock(pid=5)
    ) as popen:
        update_engine.start_searxng(tmp_path)
    args, kwargs = popen.call_args
    assert list(args[0]) == ["bash", str(tmp_path / "scripts" / "run_searxng_server.sh")]
    assert kwargs["start_new_session"] is True


def test_a_failed_restart_does_not_raise(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.ERROR, logger="saiverse.update"), patch.object(
        update_engine, "start_searxng", side_effect=update_engine.UpdateError("no cmd")
    ):
        assert update_engine._restart_searxng(tmp_path, True) is False  # must not raise
    assert "SearXNG server could not be started again" in caplog.text
    # Not only the log: the backend started next shows it in the banner.
    record = update_engine.searxng_restart_failure_path(tmp_path)
    assert "no cmd" in json.loads(record.read_text(encoding="utf-8"))["error"]


def test_the_installer_run_by_the_update_leaves_the_failure_to_the_update(tmp_path: Path) -> None:
    """refresh_searxng records a failed setup in DEPENDENCIES_FAILURE_RECORD; the
    installer's own record would show it in a second banner."""
    project = _checkout_for_refresh(tmp_path)
    envs: list = []

    def fake_run(command, *, cwd, label, timeout=900, check=True, encoding=None, env=None):  # type: ignore[no-untyped-def]
        envs.append(env)
        return MagicMock(returncode=1 if command[-1] == "check" else 0, stdout="", stderr="")

    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_run", side_effect=fake_run
    ), patch.object(update_engine, "find_searxng_servers", return_value=[]):
        assert update_engine.refresh_searxng(project) is None
    assert envs[0] is None  # check
    assert envs[1][update_engine.SEARXNG_SKIP_FAILURE_RECORD_ENV] == "1"  # setup


def test_a_searxng_that_was_not_running_is_not_started(tmp_path: Path) -> None:
    with patch.object(update_engine, "start_searxng") as start:
        update_engine._restart_searxng(tmp_path, False)
    start.assert_not_called()


# --- the dependency step ---------------------------------------------------------------


def _checkout_for_refresh(tmp_path: Path) -> Path:
    project = _checkout_with_searxng(tmp_path)
    (project / "scripts" / "searxng_install.py").write_text("# stand-in\n", encoding="utf-8")
    return project


def _answering_run(check_code: int, setup_code: int = 0):  # type: ignore[no-untyped-def]
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, label, timeout=900, check=True, encoding=None, env=None):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        assert check is False, "a SearXNG failure must be an answer, not an exception"
        code = check_code if command[-1] == "check" else setup_code
        return MagicMock(returncode=code, stdout="[INFO] Running: pip\n", stderr="network down" if code else "")

    return fake_run, calls


def test_a_matching_searxng_is_only_checked(tmp_path: Path) -> None:
    project = _checkout_for_refresh(tmp_path)
    fake_run, calls = _answering_run(check_code=0)
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_run", side_effect=fake_run
    ):
        assert update_engine.refresh_searxng(project) is None
    installer = str(project / "scripts" / "searxng_install.py")
    assert calls == [[_venv_python(project), installer, "check"]]


def test_a_differing_searxng_is_reinstalled(tmp_path: Path) -> None:
    project = _checkout_for_refresh(tmp_path)
    fake_run, calls = _answering_run(check_code=1)
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_run", side_effect=fake_run
    ):
        assert update_engine.refresh_searxng(project) is None
    assert [command[-1] for command in calls] == ["check", "setup"]
    assert all(command[0] == _venv_python(project) for command in calls)


def test_a_failed_reinstall_does_not_fail_the_update(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    project = _checkout_for_refresh(tmp_path)
    fake_run, _ = _answering_run(check_code=1, setup_code=1)
    with caplog.at_level(logging.WARNING, logger="saiverse.update"), patch.object(
        update_engine, "_is_windows", return_value=True
    ), patch.object(update_engine, "_run", side_effect=fake_run):
        failure = update_engine.refresh_searxng(project)  # must not raise
    assert "could not be reinstalled (exit 1)" in caplog.text
    assert "network down" in caplog.text
    # Returned, so the dependency step shows it and leaves its record unwritten.
    assert failure is not None
    assert failure.step == update_engine.DEPENDENCY_STEP_SEARXNG
    assert "could not be reinstalled (exit 1)" in failure.error
    assert "network down" in failure.error


def test_an_installer_that_cannot_start_does_not_fail_the_update(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    project = _checkout_for_refresh(tmp_path)
    with caplog.at_level(logging.WARNING, logger="saiverse.update"), patch.object(
        update_engine, "_is_windows", return_value=True
    ), patch.object(update_engine, "_run", side_effect=update_engine.UpdateError("could not run")):
        failure = update_engine.refresh_searxng(project)  # must not raise
    assert "could not be checked or reinstalled" in caplog.text
    assert failure is not None
    assert failure.step == update_engine.DEPENDENCY_STEP_SEARXNG
    assert "could not run" in failure.error


def _searxng_setup_stopped_here(project: Path) -> None:
    """What a SearXNG setup stopped right after setting the venv aside leaves:
    the record and the set-aside venv, and no venv python."""
    record = project / update_engine.SEARXNG_SETUP_IN_PROGRESS_RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{}\n", encoding="utf-8")


def test_the_record_name_is_the_one_the_installer_writes() -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import searxng_install
        import setup_in_progress
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
    assert update_engine.SEARXNG_SETUP_IN_PROGRESS_RECORD == setup_in_progress.entry_path(
        Path(), searxng_install.SET_ASIDE_ENTRY
    )


def test_a_stopped_setup_without_a_venv_python_is_finished_with_saiverse_python(tmp_path: Path) -> None:
    """The venv python is gone after the set-aside, so it is not 'not
    installed': the setup runs with SAIVerse's python, without the check."""
    project = tmp_path
    (project / "scripts").mkdir()
    (project / "scripts" / "searxng_install.py").write_text("# stand-in\n", encoding="utf-8")
    _searxng_setup_stopped_here(project)
    assert update_engine.searxng_installed(project)
    fake_run, calls = _answering_run(check_code=1)
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_run", side_effect=fake_run
    ):
        assert update_engine.refresh_searxng(project, r"C:\SAIVerse\.venv\Scripts\python.exe") is None
    installer = str(project / "scripts" / "searxng_install.py")
    assert calls == [[r"C:\SAIVerse\.venv\Scripts\python.exe", installer, "setup"]]


def test_a_stopped_setup_defaults_to_this_interpreter_even_with_a_venv_python(tmp_path: Path) -> None:
    """The venv python may be the half-made one; SAIVerse's python runs the setup."""
    project = _checkout_for_refresh(tmp_path)
    _searxng_setup_stopped_here(project)
    fake_run, calls = _answering_run(check_code=0)
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_run", side_effect=fake_run
    ), patch.object(update_engine, "find_searxng_servers", return_value=[]):
        assert update_engine.refresh_searxng(project) is None
    assert [command[0] for command in calls] == [update_engine.sys.executable]
    assert calls[0][-1] == "setup"


def test_a_failed_finish_of_a_stopped_setup_is_returned(tmp_path: Path) -> None:
    project = _checkout_for_refresh(tmp_path)
    _searxng_setup_stopped_here(project)
    fake_run, _ = _answering_run(check_code=0, setup_code=1)
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_run", side_effect=fake_run
    ), patch.object(update_engine, "find_searxng_servers", return_value=[]):
        failure = update_engine.refresh_searxng(project)
    assert failure is not None and failure.step == update_engine.DEPENDENCY_STEP_SEARXNG
    assert failure.retry


def test_without_searxng_the_dependency_step_does_nothing_for_it(tmp_path: Path) -> None:
    with patch.object(update_engine, "_run") as run:
        update_engine.refresh_searxng(tmp_path)
    run.assert_not_called()


def test_the_dependency_step_handles_searxng_after_npm(tmp_path: Path) -> None:
    """SearXNG is part of update_dependencies, i.e. of the child that runs the
    engine on disk, so a new pin arrives with the update that ships it."""
    order: list[str] = []
    (tmp_path / "frontend").mkdir()

    def fake_run(command, *, cwd, label, timeout=900, check=True, encoding=None, env=None):  # type: ignore[no-untyped-def]
        order.append(label)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(update_engine, "_run", side_effect=fake_run), patch.object(
        update_engine, "refresh_portable_node"
    ), patch.object(update_engine, "_report_addon_conflicts"), patch.object(
        update_engine.shutil, "which", return_value="npm"
    ), patch.object(update_engine, "refresh_searxng", side_effect=lambda *_: order.append("searxng")):
        update_engine.update_dependencies(tmp_path, "python")
    assert order[-2:] == ["npm install", "searxng"]


def test_the_dependency_step_returns_the_steps_that_failed(tmp_path: Path) -> None:
    """The portable Node.js and SearXNG failures do not raise; they come back
    in order, and an empty list means everything finished."""
    (tmp_path / "frontend").mkdir()
    node = update_engine.DependencyStepFailure(update_engine.DEPENDENCY_STEP_PORTABLE_NODE, "node failed")
    searxng = update_engine.DependencyStepFailure(update_engine.DEPENDENCY_STEP_SEARXNG, "searxng failed")

    def run_dependency_step(node_failure, searxng_failure):  # type: ignore[no-untyped-def]
        with patch.object(
            update_engine, "_run", return_value=MagicMock(returncode=0, stdout="", stderr="")
        ), patch.object(update_engine, "refresh_portable_node", return_value=node_failure), patch.object(
            update_engine, "_report_addon_conflicts"
        ), patch.object(update_engine.shutil, "which", return_value="npm"), patch.object(
            update_engine, "refresh_searxng", return_value=searxng_failure
        ):
            return update_engine.update_dependencies(tmp_path, "python")

    assert run_dependency_step(node, searxng) == [node, searxng]
    assert run_dependency_step(None, searxng) == [searxng]
    assert run_dependency_step(None, None) == []


# --- the launchers, for a SearXNG setup that stopped part-way ------------------------------
#
# The SearXNG venv may have no python left (stopped right after the set-aside), so
# "installed" is not only "the venv has a python": start.bat / start-dev.bat open
# the SearXNG window for the record too, and run_searxng_server.ps1 / .sh run the
# installer with SAIVerse's .venv python while the record is there.

_CMD = shutil.which("cmd")
_POWERSHELL = shutil.which("powershell")
_SEARXNG_VENV_PYTHON = Path("scripts") / ".searxng-venv" / "Scripts" / "python.exe"


def _searxng_block(launcher: str) -> str:
    text = (REPO_ROOT / launcher).read_text(encoding="ascii").replace("\r\n", "\n")
    start = text.index("REM Start SearXNG if installed")
    end = text.index("\n:searxng_done", start) + len("\n:searxng_done")
    lines = []
    for line in text[start:end].split("\n"):
        if line.startswith('start "SearXNG"'):
            assert "run_searxng_server.ps1" in line
            line = "echo STARTED_SEARXNG_WINDOW"
        elif line.startswith("timeout "):
            line = "rem " + line
        lines.append(line)
    return "\r\n".join(["@echo off", "setlocal", *lines, "exit /b 0"]) + "\r\n"


@pytest.mark.skipif(sys.platform != "win32" or _CMD is None, reason="runs the launcher's block with cmd.exe")
@pytest.mark.parametrize("launcher", ["start.bat", "start-dev.bat"])
@pytest.mark.parametrize(
    ("files", "started", "says_stopped"),
    [
        ([], False, False),
        ([_SEARXNG_VENV_PYTHON], True, False),
        ([update_engine.SEARXNG_SETUP_IN_PROGRESS_RECORD], True, True),
        ([update_engine.SEARXNG_SETUP_IN_PROGRESS_RECORD, _SEARXNG_VENV_PYTHON], True, True),
    ],
)
def test_the_launchers_open_searxng_for_a_stopped_setup(
    tmp_path: Path, launcher: str, files: list[Path], started: bool, says_stopped: bool
) -> None:
    script = tmp_path / "block.bat"
    script.write_bytes(_searxng_block(launcher).encode("ascii"))
    work = tmp_path / "checkout"
    work.mkdir()
    for name in files:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_bytes(b"")
    result = subprocess.run([_CMD, "/c", str(script)], cwd=work, capture_output=True, timeout=60, check=True)
    out = result.stdout.decode("ascii", "replace")
    assert ("STARTED_SEARXNG_WINDOW" in out) is started
    assert ("SearXNG not installed" in out) is (not started)
    assert ("setup stopped before it finished" in out) is says_stopped


@pytest.mark.skipif(sys.platform != "win32" or _POWERSHELL is None, reason="runs the script's lines with PowerShell")
@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ([], "python"),
        (["sx/Scripts/python.exe"], r"<root>\sx\Scripts\python.exe"),
        (["sx/Scripts/python.exe", ".venv/Scripts/python.exe"], r"<root>\sx\Scripts\python.exe"),
        # A stopped setup: SAIVerse's python, also over a half-made venv python.
        ([".setup_in_progress/searxng-venv.json", ".venv/Scripts/python.exe"], r"<root>\.venv\Scripts\python.exe"),
        (
            [".setup_in_progress/searxng-venv.json", "sx/Scripts/python.exe", ".venv/Scripts/python.exe"],
            r"<root>\.venv\Scripts\python.exe",
        ),
        # Without SAIVerse's python, the venv's own python is still tried.
        ([".setup_in_progress/searxng-venv.json", "sx/Scripts/python.exe"], r"<root>\sx\Scripts\python.exe"),
    ],
)
def test_the_searxng_launcher_runs_a_stopped_setup_with_saiverse_python(
    tmp_path: Path, files: list[str], expected: str
) -> None:
    lines = (REPO_ROOT / "scripts" / "run_searxng_server.ps1").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("$venvPython = "))
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("$installer = "))
    work = tmp_path / "checkout"
    work.mkdir()
    for name in files:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_bytes(b"")
    script = tmp_path / "select.ps1"
    script.write_text("\r\n".join([
        f"$WorkspaceRoot = '{work}'", f"$VENV_DIR = '{work / 'sx'}'", *lines[start:end],
        "Write-Output $bootstrapPython.Replace($WorkspaceRoot, '<root>')",
    ]) + "\r\n", encoding="utf-8")
    result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, timeout=60, check=True,
    )
    assert result.stdout.decode("utf-8", "replace").strip() == expected


def test_the_posix_searxng_launcher_names_the_same_record_and_python() -> None:
    text = (REPO_ROOT / "scripts" / "run_searxng_server.sh").read_text(encoding="utf-8")
    assert '"${WORKSPACE_ROOT}/.setup_in_progress/searxng-venv.json"' in text
    assert 'saiverse_python="${WORKSPACE_ROOT}/.venv/bin/python"' in text
    assert update_engine.SEARXNG_SETUP_IN_PROGRESS_RECORD.as_posix() == ".setup_in_progress/searxng-venv.json"
