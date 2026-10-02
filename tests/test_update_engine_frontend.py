"""The update engine and the frontend server (docs/issues/archive/ui_update_fails_while_frontend_runs.md).

On Windows a running ``next start`` / ``next dev`` holds a native module in
``frontend/node_modules`` open, so ``npm ci`` beside it fails after deleting
every other package. What is pinned here:

- which processes count as *this checkout's* frontend: node processes whose
  working directory or command-line paths lie inside ``<project>/frontend`` on
  path boundaries (another worktree's frontend is not ours), plus the
  start.bat window that runs them, and nothing else;
- which mode the frontend ran in, so it can be started again the same way;
- the start-time check now looks for every declared package, not just the
  ``node_modules`` folder, even when the completion marker matches;
- the detached update stops the frontend before any change, builds it when it
  was a production server, and starts it again only after the backend is
  healthy;
- whatever fails after the old backend exited, the previous version's backend
  and frontend are started again (docs/issues/updater_failure_leaves_backend_stopped.md);
- a manual update refuses, before changing anything, while the frontend runs.

No real process is started, signalled or killed: psutil is replaced by fakes
and ``subprocess.Popen`` by a mock.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil
import pytest

from scripts import update_engine

# --- fakes --------------------------------------------------------------------


class FakeProcess:
    """Just enough of psutil.Process for the frontend search and stop."""

    def __init__(
        self,
        pid: int,
        name: str,
        *,
        cmdline: list[str] | None = None,
        cwd: str | None = None,
        parents: list[FakeProcess] | None = None,
        deny_cwd: bool = False,
        deny_cmdline: bool = False,
        gone: bool = False,
    ) -> None:
        self.pid = pid
        self._name = name
        self._cmdline = cmdline or []
        self._cwd = cwd
        self._parents = parents or []
        self._deny_cwd = deny_cwd
        self._deny_cmdline = deny_cmdline
        self._gone = gone
        self.terminated = False
        self.killed = False

    def name(self) -> str:
        if self._gone:
            raise psutil.NoSuchProcess(self.pid)
        return self._name

    def cmdline(self) -> list[str]:
        if self._deny_cmdline:
            raise psutil.AccessDenied(self.pid)
        return list(self._cmdline)

    def cwd(self) -> str | None:
        if self._deny_cwd:
            raise psutil.AccessDenied(self.pid)
        return self._cwd

    def parents(self) -> list[FakeProcess]:
        return list(self._parents)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _checkout(root: Path) -> Path:
    (root / "frontend").mkdir(parents=True)
    return root


def _next_bin(frontend: Path) -> str:
    # The shape seen on a real machine: ``.bin\\..\\next\\dist\\bin\\next``.
    return str(frontend / "node_modules" / ".bin") + os.sep + os.path.join("..", "next", "dist", "bin", "next")


def _pids(servers: update_engine.FrontendServers) -> set[int]:
    return {process.pid for process in servers.processes}


# --- which processes are this checkout's frontend -------------------------------


def test_node_in_this_frontend_is_found_and_another_worktrees_is_not(tmp_path: Path) -> None:
    main = _checkout(tmp_path / "SAIVerse")
    worktree = _checkout(main / ".worktrees" / "x")
    ours = FakeProcess(1, "node.exe", cmdline=["node", _next_bin(main / "frontend"), "start"], cwd=str(main / "frontend"))
    theirs = FakeProcess(
        2, "node.exe", cmdline=["node", _next_bin(worktree / "frontend"), "dev"], cwd=str(worktree / "frontend")
    )

    found_main = update_engine.find_frontend_servers(main, processes=[ours, theirs])
    found_worktree = update_engine.find_frontend_servers(worktree, processes=[ours, theirs])

    assert _pids(found_main) == {1}
    assert found_main.mode == update_engine.FRONTEND_MODE_START
    assert _pids(found_worktree) == {2}
    assert found_worktree.mode == update_engine.FRONTEND_MODE_DEV


def test_a_sibling_directory_sharing_the_prefix_is_not_the_frontend(tmp_path: Path) -> None:
    project = _checkout(tmp_path / "SAIVerse")
    sibling = project / "frontend-old"
    sibling.mkdir()
    node = FakeProcess(1, "node", cmdline=["node", str(sibling / "server.js")], cwd=str(sibling))
    assert update_engine.find_frontend_servers(project, processes=[node]).processes == []


def test_command_line_path_is_enough_when_the_working_directory_is_hidden(tmp_path: Path) -> None:
    project = _checkout(tmp_path / "SAIVerse")
    node = FakeProcess(1, "node.exe", cmdline=["node", _next_bin(project / "frontend"), "start"], deny_cwd=True)
    found = update_engine.find_frontend_servers(project, processes=[node])
    assert _pids(found) == {1}
    assert found.mode == update_engine.FRONTEND_MODE_START


def test_npm_process_is_found_by_its_working_directory(tmp_path: Path) -> None:
    project = _checkout(tmp_path / "SAIVerse")
    npm = FakeProcess(
        1,
        "node.exe",
        cmdline=[r"C:\Program Files\nodejs\\node.exe", r"C:\Users\u\AppData\Roaming\npm\node_modules\npm\bin\npm-cli.js", "start"],
        cwd=str(project / "frontend" / "src"),
    )
    found = update_engine.find_frontend_servers(project, processes=[npm])
    assert _pids(found) == {1}
    assert found.mode == update_engine.FRONTEND_MODE_START


def test_non_node_processes_are_never_the_frontend(tmp_path: Path) -> None:
    """A shell the user cd'd into frontend, or a python there, is theirs."""
    project = _checkout(tmp_path / "SAIVerse")
    frontend = str(project / "frontend")
    others = [
        FakeProcess(1, "cmd.exe", cmdline=["cmd.exe"], cwd=frontend),
        FakeProcess(2, "powershell.exe", cmdline=["powershell.exe"], cwd=frontend),
        FakeProcess(3, "python.exe", cmdline=["python", str(project / "frontend" / "x.py")], cwd=frontend),
        FakeProcess(4, "bash", cmdline=["bash"], cwd=frontend),
    ]
    assert update_engine.find_frontend_servers(project, processes=others).processes == []


def test_vanished_and_uninspectable_processes_are_skipped(tmp_path: Path) -> None:
    project = _checkout(tmp_path / "SAIVerse")
    gone = FakeProcess(1, "node.exe", gone=True)
    sealed = FakeProcess(2, "node.exe", deny_cwd=True, deny_cmdline=True)
    ours = FakeProcess(3, "node.exe", cmdline=["node", _next_bin(project / "frontend"), "start"])
    assert _pids(update_engine.find_frontend_servers(project, processes=[gone, sealed, ours])) == {3}


def test_the_start_bat_window_is_included_only_with_its_signature(tmp_path: Path) -> None:
    project = _checkout(tmp_path / "SAIVerse")
    window = FakeProcess(
        10, "cmd.exe", cmdline=["cmd", "/k", "title SAIVerse Frontend && cd frontend && npm start"]
    )
    users_shell = FakeProcess(11, "cmd.exe", cmdline=["cmd.exe"])
    explorer = FakeProcess(12, "explorer.exe", cmdline=["explorer.exe"])
    npm = FakeProcess(
        1,
        "node.exe",
        cmdline=["node", "npm-cli.js", "start"],
        cwd=str(project / "frontend"),
        parents=[window, explorer],
    )
    manual = FakeProcess(
        2,
        "node.exe",
        cmdline=["node", _next_bin(project / "frontend"), "start"],
        parents=[users_shell, explorer],
    )

    found = update_engine.find_frontend_servers(project, processes=[npm, manual])

    assert _pids(found) == {1, 2, 10}
    assert found.mode == update_engine.FRONTEND_MODE_START


def test_no_frontend_directory_means_nothing_runs(tmp_path: Path) -> None:
    node = FakeProcess(1, "node.exe", cwd=str(tmp_path / "frontend"))
    assert update_engine.find_frontend_servers(tmp_path, processes=[node]) == update_engine.FrontendServers([], None)


# --- which mode it ran in ----------------------------------------------------------


@pytest.mark.parametrize(
    ("cmdline", "mode"),
    [
        (["node", r"C:\p\frontend\node_modules\.bin\\..\next\dist\bin\next", "start"], "start"),
        (["node", "/p/frontend/node_modules/next/dist/bin/next", "dev", "-H", "0.0.0.0"], "dev"),
        ([r"C:\Program Files\nodejs\\node.exe", r"C:\npm\bin\npm-cli.js", "start"], "start"),
        (["node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "run", "dev"], "dev"),
        (["npm start"], "start"),  # a Linux process title shows as one argument
        (["cmd", "/k", "title SAIVerse Frontend && cd frontend && npm start"], "start"),
        (["cmd", "/k", "title SAIVerse Frontend (Dev) && cd frontend && npm run dev"], "dev"),
        (["node", "/p/frontend/node_modules/next/dist/bin/next", "build"], None),
        (["node", "/p/frontend/scripts/sync-addon-panels.mjs"], None),
        ([], None),
    ],
)
def test_mode_is_read_from_the_command_line(cmdline: list[str], mode: str | None) -> None:
    assert update_engine._frontend_mode_from_cmdline(cmdline) == mode


def test_unknown_or_conflicting_modes_are_reported_as_unknown(tmp_path: Path) -> None:
    project = _checkout(tmp_path / "SAIVerse")
    frontend = str(project / "frontend")
    unknown = FakeProcess(1, "node", cmdline=["node", "tsc", "--watch"], cwd=frontend)
    assert update_engine.find_frontend_servers(project, processes=[unknown]).mode is None

    start = FakeProcess(2, "node", cmdline=["node", _next_bin(project / "frontend"), "start"], cwd=frontend)
    dev = FakeProcess(3, "node", cmdline=["node", _next_bin(project / "frontend"), "dev"], cwd=frontend)
    found = update_engine.find_frontend_servers(project, processes=[start, dev])
    assert _pids(found) == {2, 3}
    assert found.mode is None


# --- stopping it ---------------------------------------------------------------------


def _fake_psutil(still_alive: set[int]) -> SimpleNamespace:
    """psutil with wait_procs answered from ``still_alive`` (pids that ignore signals)."""

    def wait_procs(processes, timeout):  # type: ignore[no-untyped-def]
        alive = [p for p in processes if p.pid in still_alive]
        return [p for p in processes if p.pid not in still_alive], alive

    return SimpleNamespace(
        Error=psutil.Error,
        NoSuchProcess=psutil.NoSuchProcess,
        AccessDenied=psutil.AccessDenied,
        wait_procs=wait_procs,
    )


def test_stop_terminates_then_kills_what_is_left(tmp_path: Path) -> None:
    project = _checkout(tmp_path)
    server = FakeProcess(1, "node.exe")
    window = FakeProcess(2, "cmd.exe")
    found = update_engine.FrontendServers([server, window], update_engine.FRONTEND_MODE_START)
    fake = _fake_psutil(still_alive=set())
    with patch.object(update_engine, "find_frontend_servers", side_effect=[found, update_engine.FrontendServers([], None)]), patch.object(
        update_engine, "_import_psutil", return_value=fake
    ):
        stopped = update_engine.stop_frontend_servers(project)

    assert stopped.mode == update_engine.FRONTEND_MODE_START
    assert server.terminated and window.terminated
    assert not server.killed


def test_stop_fails_closed_when_a_process_survives_kill(tmp_path: Path) -> None:
    project = _checkout(tmp_path)
    stubborn = FakeProcess(1, "node.exe")
    found = update_engine.FrontendServers([stubborn], update_engine.FRONTEND_MODE_START)
    with patch.object(update_engine, "find_frontend_servers", return_value=found), patch.object(
        update_engine, "_import_psutil", return_value=_fake_psutil(still_alive={1})
    ):
        with pytest.raises(update_engine.UpdateError, match="could not be stopped"):
            update_engine.stop_frontend_servers(project)
    assert stubborn.terminated and stubborn.killed


def test_stop_catches_a_process_that_appeared_while_stopping(tmp_path: Path) -> None:
    project = _checkout(tmp_path)
    first = FakeProcess(1, "node.exe")
    late = FakeProcess(2, "node.exe")
    with patch.object(
        update_engine,
        "find_frontend_servers",
        side_effect=[
            update_engine.FrontendServers([first], update_engine.FRONTEND_MODE_DEV),
            update_engine.FrontendServers([late], None),
        ],
    ), patch.object(update_engine, "_import_psutil", return_value=_fake_psutil(still_alive=set())):
        stopped = update_engine.stop_frontend_servers(project)
    assert late.terminated
    assert stopped.mode == update_engine.FRONTEND_MODE_DEV


# --- starting it again -------------------------------------------------------------


def _popen_kwargs(popen: MagicMock) -> tuple[list[str], dict]:
    args, kwargs = popen.call_args
    return list(args[0]), kwargs


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("start", "title SAIVerse Frontend && npm start"),
        ("dev", "title SAIVerse Frontend (Dev) && npm run dev"),
    ],
)
def test_windows_frontend_restarts_in_a_visible_start_bat_style_window(
    tmp_path: Path, mode: str, expected: str
) -> None:
    project = _checkout(tmp_path)
    portable = str(tmp_path / ".node" / "npm.cmd")
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "_find_npm", return_value=portable
    ), patch.object(update_engine.subprocess, "Popen", return_value=MagicMock(pid=5)) as popen, patch.dict(
        os.environ, {"COMSPEC": "cmd.exe"}
    ):
        update_engine.start_frontend(project, mode)

    command, kwargs = _popen_kwargs(popen)
    assert command == ["cmd.exe", "/k", expected]
    assert kwargs["creationflags"] == update_engine._CREATE_NEW_CONSOLE
    assert kwargs["cwd"] == str(project / "frontend")
    assert kwargs["close_fds"] is True
    # The window's own console is the output: no handle is redirected or inherited.
    assert not {"stdin", "stdout", "stderr"} & set(kwargs)
    # A portable npm is not on PATH by itself; the window must find it by name.
    assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(tmp_path / ".node")


def test_posix_frontend_restarts_in_a_new_session(tmp_path: Path) -> None:
    project = _checkout(tmp_path)
    with patch.object(update_engine, "_is_windows", return_value=False), patch.object(
        update_engine, "_find_npm", return_value="/usr/bin/npm"
    ), patch.object(update_engine.subprocess, "Popen", return_value=MagicMock(pid=5)) as popen:
        update_engine.start_frontend(project, "dev")

    command, kwargs = _popen_kwargs(popen)
    assert command == ["/usr/bin/npm", "run", "dev"]
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


def test_windows_backend_restarts_in_its_own_visible_console(tmp_path: Path) -> None:
    config = {"project_dir": str(tmp_path), "venv_python": "python.exe", "main_args": ["city_a"]}
    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine.subprocess, "Popen", return_value=MagicMock(pid=5)
    ) as popen:
        update_engine.restart_application(config)

    command, kwargs = _popen_kwargs(popen)
    # python directly, not wrapped in cmd: poll() / terminate() must reach it.
    assert command == ["python.exe", str(tmp_path.resolve() / "main.py"), "city_a"]
    assert kwargs["creationflags"] == update_engine._CREATE_NEW_CONSOLE
    assert kwargs["close_fds"] is True
    assert not {"stdin", "stdout", "stderr"} & set(kwargs)


def test_build_runs_npm_run_build_in_frontend(tmp_path: Path) -> None:
    project = _checkout(tmp_path)
    with patch.object(update_engine, "_find_npm", return_value="npm"), patch.object(update_engine, "_run") as run:
        update_engine.build_frontend(project)
    args, kwargs = run.call_args
    assert args[0] == ["npm", "run", "build"]
    assert kwargs["cwd"] == project / "frontend"


# --- the start-time check: every declared package, not the folder -------------------


def _project_with_frontend(tmp_path: Path, *, installed: list[str]) -> Path:
    (tmp_path / "VERSION").write_text("0.3.19\n", encoding="utf-8")
    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    (frontend / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"next": "^16", "react": "^19", "@types/node": "^25"},
                "devDependencies": {"typescript": "^5"},
            }
        ),
        encoding="utf-8",
    )
    for name in installed:
        package = frontend / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text("{}\n", encoding="utf-8")
    return tmp_path


_ALL = ["next", "react", "@types/node", "typescript"]


def test_missing_packages_are_named_including_scoped(tmp_path: Path) -> None:
    # What the failed npm ci left: one held file and nothing else.
    project = _project_with_frontend(tmp_path, installed=["react"])
    held = project / "frontend" / "node_modules" / "@next" / "swc-win32-x64-msvc"
    held.mkdir(parents=True)
    (held / "next-swc.win32-x64-msvc.node").write_bytes(b"")
    assert update_engine.missing_frontend_packages(project) == ["next", "@types/node"]


def test_missing_dev_dependencies_are_not_counted(tmp_path: Path) -> None:
    # npm ci skips devDependencies under NODE_ENV=production; counting them
    # would send every start into the finishing pass forever.
    project = _project_with_frontend(tmp_path, installed=["next", "react", "@types/node"])
    assert update_engine.missing_frontend_packages(project) == []
    assert update_engine.frontend_packages_installed(project) is True


def test_a_matching_marker_does_not_hide_missing_packages(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    project = _project_with_frontend(tmp_path, installed=["react"])
    update_engine.write_completion_marker(project)
    with caplog.at_level(logging.WARNING, logger="saiverse.update"):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "next" in message and "@types/node" in message


def test_a_matching_marker_with_every_package_is_ready(tmp_path: Path) -> None:
    project = _project_with_frontend(tmp_path, installed=_ALL)
    update_engine.write_completion_marker(project)
    assert update_engine.frontend_packages_installed(project) is True
    assert update_engine.check_update_complete(project) == update_engine.CHECK_READY


def test_the_log_names_at_most_five_missing_packages(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    project = _project_with_frontend(tmp_path, installed=[])
    (project / "frontend" / "package.json").write_text(
        json.dumps({"dependencies": {f"pkg{i}": "1" for i in range(8)}}), encoding="utf-8"
    )
    update_engine.write_completion_marker(project)
    with caplog.at_level(logging.WARNING, logger="saiverse.update"):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "8 frontend package(s)" in message
    assert "pkg4" in message and "pkg5" not in message


@pytest.mark.parametrize("content", [None, "{not json", "[1, 2]", '{"dependencies": ["next"]}'])
def test_unreadable_package_json_cannot_be_judged(tmp_path: Path, content: str | None) -> None:
    project = _project_with_frontend(tmp_path, installed=[])
    manifest = project / "frontend" / "package.json"
    if content is None:
        manifest.unlink()
    else:
        manifest.write_text(content, encoding="utf-8")
    assert update_engine.missing_frontend_packages(project) is None
    assert update_engine.frontend_packages_installed(project) is None
    # With a matching marker the marker is trusted, as before.
    update_engine.write_completion_marker(project)
    assert update_engine.check_update_complete(project) == update_engine.CHECK_READY


def test_markerless_start_with_unreadable_package_json_is_inconclusive(tmp_path: Path) -> None:
    project = _project_with_frontend(tmp_path, installed=[])
    (project / "frontend" / "package.json").unlink()
    with patch.object(
        update_engine, "missing_dependencies", return_value=update_engine.DependencyReport([], [], False)
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_INCONCLUSIVE
    assert not update_engine.marker_path(project).exists()


# --- the detached update -------------------------------------------------------------

_CONFIG = {"venv_python": "python", "main_pid": 10, "main_process_created_at": 1.0}


class _Recorder:
    """Patches every phase of run_update and records the order they ran in."""

    def __init__(
        self,
        frontend_mode: str | None,
        *,
        fail: str | None = None,
        switch: bool = False,
        fail_every_time: bool = False,
    ) -> None:
        # ``fail`` fails the named phase the first time only (a rebuild in the
        # recovery then succeeds), or every time with ``fail_every_time``.
        self.fail_every_time = fail_every_time
        self.order: list[str] = []
        self.calls: dict[str, list[tuple[tuple, dict]]] = {}
        self.frontend_mode = frontend_mode
        self.fail = fail
        self.switch = switch

    def step(self, name: str, result=None):  # type: ignore[no-untyped-def]
        def run(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.order.append(name)
            self.calls.setdefault(name, []).append((args, kwargs))
            if self.fail == name and (self.fail_every_time or self.order.count(name) == 1):
                raise update_engine.UpdateError(f"{name} failed")
            return result

        return run

    def stop_frontend(self, project_dir):  # type: ignore[no-untyped-def]
        self.order.append("stop frontend")
        if self.fail == "stop frontend":
            raise update_engine.UpdateError("stop frontend failed")
        processes = [FakeProcess(1, "node.exe")] if self.frontend_mode else []
        return update_engine.FrontendServers(processes, self.frontend_mode)

    def restart(self, config):  # type: ignore[no-untyped-def]
        # The first restart is the new version, a later one the previous version.
        restarts = sum(1 for entry in self.order if entry.startswith("restart backend"))
        self.order.append("restart backend" if restarts == 0 else "restart previous backend")
        return MagicMock(pid=100 + restarts)

    def health(self, process, config):  # type: ignore[no-untyped-def]
        self.order.append("health")
        if self.fail == "health" and self.order.count("health") == 1:
            raise update_engine.UpdateError("health failed")
        return {"city_name": "city_a", "version": "0.3.19"}

    def start_frontend(self, project_dir, mode):  # type: ignore[no-untyped-def]
        self.order.append(f"start frontend {mode}")
        return MagicMock(pid=200)

    def run(self, project: Path) -> None:
        plan = update_engine.SwitchPlan("early_access", "early-access", "main", "old-head")
        with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
            update_engine, "wait_for_owned_process_exit", side_effect=self.step("wait")
        ), patch.object(
            update_engine, "assert_git_update_ready", side_effect=self.step("preflight", "old-head")
        ), patch.object(
            update_engine, "preflight_switch", side_effect=self.step("preflight", plan)
        ), patch.object(update_engine, "stop_frontend_servers", side_effect=self.stop_frontend), patch.object(
            update_engine, "create_pre_update_snapshot", side_effect=self.step("snapshot", "snap")
        ), patch.object(update_engine, "update_code", side_effect=self.step("code")), patch.object(
            update_engine, "prepare_switch_branch", side_effect=self.step("prepare branch")
        ), patch.object(update_engine, "switch_code", side_effect=self.step("code")), patch.object(
            update_engine, "_checkout_untouched", return_value=True
        ), patch.object(
            update_engine, "update_dependencies", side_effect=self.step("dependencies (npm ci)")
        ), patch.object(update_engine, "build_frontend", side_effect=self.step("build")), patch.object(
            update_engine, "_rollback_code_and_dependencies", side_effect=self.step("rollback")
        ), patch.object(update_engine, "restart_application", side_effect=self.restart), patch.object(
            update_engine, "wait_for_healthy_restart", side_effect=self.health
        ), patch.object(update_engine, "_terminate_spawned", side_effect=self.step("terminate new backend")), patch.object(
            update_engine, "start_frontend", side_effect=self.start_frontend
        ), patch.object(update_engine, "write_completion_marker", side_effect=self.step("marker")):
            update_engine.run_update(
                _CONFIG, project, switch_channel="early_access" if self.switch else None
            )


def test_production_frontend_is_stopped_first_built_and_started_after_the_backend(tmp_path: Path) -> None:
    recorder = _Recorder(update_engine.FRONTEND_MODE_START)
    recorder.run(tmp_path)
    assert recorder.order == [
        "wait",
        "preflight",
        "stop frontend",
        "snapshot",
        "code",
        "dependencies (npm ci)",
        "build",
        "restart backend",
        "health",
        "marker",
        "start frontend start",
    ]


def test_dev_frontend_is_not_built_and_comes_back_in_dev_mode(tmp_path: Path) -> None:
    recorder = _Recorder(update_engine.FRONTEND_MODE_DEV)
    recorder.run(tmp_path)
    assert "build" not in recorder.order
    assert recorder.order[-1] == "start frontend dev"
    assert recorder.order.index("stop frontend") < recorder.order.index("dependencies (npm ci)")


def test_no_running_frontend_is_neither_built_nor_started(tmp_path: Path) -> None:
    recorder = _Recorder(None)
    recorder.run(tmp_path)
    assert "build" not in recorder.order
    assert not any(entry.startswith("start frontend") for entry in recorder.order)


def _failing_run(tmp_path: Path, fail: str, frontend_mode: str | None = "start", *, switch: bool = False) -> list[str]:
    recorder = _Recorder(frontend_mode, fail=fail, switch=switch)
    with pytest.raises(update_engine.UpdateError, match=re.escape(f"{fail} failed")):
        recorder.run(tmp_path)
    return recorder.order


def test_a_frontend_that_cannot_be_rebuilt_is_not_started(tmp_path: Path) -> None:
    recorder = _Recorder("start", fail="build", fail_every_time=True)
    with pytest.raises(update_engine.UpdateError, match="build failed"):
        recorder.run(tmp_path)
    # The backend still comes back; a `next start` over a broken build would not.
    assert recorder.order[-4:] == ["rollback", "restart backend", "health", "build"]


@pytest.mark.parametrize("fail", ["snapshot", "code"])
def test_failure_before_the_code_moved_restarts_the_previous_version_unbuilt(tmp_path: Path, fail: str) -> None:
    order = _failing_run(tmp_path, fail)
    assert "rollback" not in order
    assert "build" not in order  # the build on disk is still the previous one
    assert order[order.index(fail) + 1:] == ["restart backend", "health", "start frontend start"]
    assert "marker" not in order


def test_failure_before_the_frontend_was_stopped_does_not_start_a_second_one(tmp_path: Path) -> None:
    order = _failing_run(tmp_path, "preflight")
    assert "stop frontend" not in order
    assert order[-2:] == ["restart backend", "health"]


def test_failure_to_stop_the_frontend_restarts_only_the_backend(tmp_path: Path) -> None:
    order = _failing_run(tmp_path, "stop frontend")
    assert order[-2:] == ["restart backend", "health"]
    assert "snapshot" not in order


@pytest.mark.parametrize("fail", ["dependencies (npm ci)", "build"])
def test_failure_after_the_code_moved_rolls_back_rebuilds_and_restarts(tmp_path: Path, fail: str) -> None:
    order = _failing_run(tmp_path, fail)
    tail = order[order.index(fail) + 1:]
    assert tail == ["rollback", "restart backend", "health", "build", "start frontend start"]


def test_failed_health_of_the_new_version_restores_the_previous_one(tmp_path: Path) -> None:
    order = _failing_run(tmp_path, "health")
    tail = order[order.index("health") + 1:]
    assert tail == [
        "terminate new backend",
        "rollback",
        "restart previous backend",
        "health",
        "build",
        "start frontend start",
    ]
    assert "marker" not in order


def test_dev_frontend_is_restored_without_a_build(tmp_path: Path) -> None:
    order = _failing_run(tmp_path, "dependencies (npm ci)", frontend_mode="dev")
    assert "build" not in order
    assert order[-1] == "start frontend dev"


def test_a_failed_rollback_starts_nothing(tmp_path: Path) -> None:
    """Code in an unknown state must not be started over the world."""
    recorder = _Recorder("start", fail="dependencies (npm ci)")
    original_step = recorder.step

    def step(name, result=None):  # type: ignore[no-untyped-def]
        if name == "rollback":
            def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
                recorder.order.append("rollback")
                raise update_engine.UpdateError("git reset failed")

            return broken
        return original_step(name, result)

    recorder.step = step  # type: ignore[method-assign]
    with pytest.raises(update_engine.UpdateError, match="npm ci"):
        recorder.run(tmp_path)
    assert recorder.order[-1] == "rollback"


def test_a_switch_that_fails_after_moving_is_rolled_back_to_its_branch_and_restarted(tmp_path: Path) -> None:
    order = _failing_run(tmp_path, "dependencies (npm ci)", switch=True)
    assert order[:5] == ["wait", "preflight", "stop frontend", "snapshot", "prepare branch"]
    assert order[-5:] == ["rollback", "restart backend", "health", "build", "start frontend start"]


def test_rollback_of_a_switch_returns_to_the_recorded_branch(tmp_path: Path) -> None:
    recorder = _Recorder("start", fail="dependencies (npm ci)", switch=True)
    with pytest.raises(update_engine.UpdateError):
        recorder.run(tmp_path)
    assert recorder.calls["rollback"] == [((tmp_path, "python", "old-head"), {"branch": "main"})]


# --- the manual entrance ---------------------------------------------------------------


def test_manual_update_refuses_while_the_frontend_runs_without_changing_anything(tmp_path: Path) -> None:
    running = update_engine.FrontendServers([FakeProcess(1, "node.exe")], update_engine.FRONTEND_MODE_START)
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "find_frontend_servers", return_value=running
    ), patch.object(update_engine, "assert_git_update_ready") as ready, patch.object(
        update_engine, "preflight_switch"
    ) as preflight, patch.object(update_engine, "create_pre_update_snapshot") as snapshot, patch.object(
        update_engine, "update_code"
    ) as code, patch.object(update_engine, "update_dependencies") as deps, patch.object(
        update_engine, "stop_frontend_servers"
    ) as stop:
        with pytest.raises(update_engine.UpdateError) as excinfo:
            update_engine.run_update(None, tmp_path)

    message = str(excinfo.value)
    assert "Frontend" in message
    assert "閉じて" in message
    for phase in (ready, preflight, snapshot, code, deps, stop):
        phase.assert_not_called()


def test_manual_update_proceeds_when_psutil_cannot_check(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """update.bat is how psutil gets installed; it must not be blocked by its absence."""
    with patch.object(
        update_engine,
        "find_frontend_servers",
        side_effect=update_engine.FrontendCheckUnavailable("psutil is unavailable"),
    ), caplog.at_level(logging.WARNING, logger="saiverse.update"):
        update_engine.refuse_while_frontend_runs(tmp_path)  # must not raise
    assert any("frontend is running" in record.getMessage() for record in caplog.records)
