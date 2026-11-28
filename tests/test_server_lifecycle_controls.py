"""Contract tests for clean detached backend restart and full-stack shutdown.

Covers:
- ``POST /api/admin/restart`` and ``POST /api/admin/shutdown`` prechecks,
  confirmation guard, single-launch lock, and detached worker handoff.
- ``scripts/update_engine.py`` lifecycle operations (``restart_backend`` and
  ``shutdown_all``), verified child/window cleanup, and companion server
  shutdown resilience.
- ``api/routes/system.py`` invoking the registered canonical
  ``app_state.shutdown_handler`` before ``os._exit(0)``.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.routes import admin as admin_routes
from api.routes import system as system_routes
from saiverse import app_state
from scripts import update_engine


def _make_admin_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path]:
    project = tmp_path / "project"
    updater = project / "scripts" / "update_engine.py"
    updater.parent.mkdir(parents=True)
    updater.write_text("", encoding="utf-8")

    monkeypatch.setattr(app_state, "project_dir", str(project))
    monkeypatch.setattr(system_routes, "_updater_launched", False)

    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/admin")
    return TestClient(app), project


def test_admin_restart_uses_detached_lifecycle_worker_and_enforces_lock(
    tmp_path: Path, monkeypatch
) -> None:
    client, project = _make_admin_client(tmp_path, monkeypatch)

    # Without psutil probe succeeding, 409 before spawning or claiming lock
    res_no_psutil = client.post("/api/admin/restart")
    assert res_no_psutil.status_code == 409
    assert not (project / ".update_config.json").exists()

    launched: list[dict] = []

    def fake_launch(project_path, updater_script, venv_python, extra_config=None):
        launched.append(
            {
                "project_path": project_path,
                "updater_script": updater_script,
                "venv_python": venv_python,
                "extra_config": extra_config,
            }
        )

    with patch.object(
        system_routes.subprocess, "run", return_value=Mock(returncode=0)
    ), patch.object(admin_routes, "_launch_detached_updater", side_effect=fake_launch):
        res = client.post("/api/admin/restart")
        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["status"] == "restarting"
        assert body["operation"] == update_engine.OPERATION_RESTART_BACKEND
        assert len(launched) == 1
        assert launched[0]["extra_config"] == {"operation": update_engine.OPERATION_RESTART_BACKEND}

        # Second concurrent request must be refused with 409
        res_dup = client.post("/api/admin/restart")
        assert res_dup.status_code == 409
        assert len(launched) == 1


def test_admin_shutdown_requires_confirm_and_launches_shutdown_all(
    tmp_path: Path, monkeypatch
) -> None:
    client, project = _make_admin_client(tmp_path, monkeypatch)

    # Missing explicit confirmation -> 400
    assert client.post("/api/admin/shutdown", json={"confirm": False}).status_code == 400
    assert not (project / ".update_config.json").exists()

    # Without psutil -> 409
    assert client.post("/api/admin/shutdown", json={"confirm": True}).status_code == 409

    launched: list[dict] = []

    def fake_launch(_p, _u, _v, extra_config=None):
        launched.append(extra_config or {})

    with patch.object(
        system_routes.subprocess, "run", return_value=Mock(returncode=0)
    ), patch.object(admin_routes, "_launch_detached_updater", side_effect=fake_launch):
        res = client.post("/api/admin/shutdown", json={"confirm": True})
        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["status"] == "shutting_down"
        assert body["operation"] == update_engine.OPERATION_SHUTDOWN_ALL
        assert launched == [{"operation": update_engine.OPERATION_SHUTDOWN_ALL}]


def test_detached_restart_backend_waits_cleans_and_restarts(tmp_path: Path) -> None:
    order: list[str] = []
    config = {
        "operation": update_engine.OPERATION_RESTART_BACKEND,
        "main_pid": 999999,
        "main_process_created_at": 1234.5,
        "child_processes": [],
        "backend_windows": [],
    }

    fake_proc = MagicMock(pid=4242)
    with patch.object(
        update_engine,
        "wait_for_owned_process_exit",
        side_effect=lambda *_a, **_k: order.append("wait_exit"),
    ), patch.object(
        update_engine,
        "_cleanup_after_backend_exit",
        side_effect=lambda _c: order.append("cleanup"),
    ), patch.object(
        update_engine,
        "restart_application",
        side_effect=lambda _c: (order.append("restart"), fake_proc)[1],
    ), patch.object(
        update_engine,
        "wait_for_healthy_restart",
        side_effect=lambda _p, _c: (
            order.append("healthy"),
            {"city_name": "city_a", "version": "0.3.35"},
        )[1],
    ), patch.object(update_engine, "stop_frontend_servers") as stop_fe, patch.object(
        update_engine, "stop_searxng_servers"
    ) as stop_sx:
        update_engine._run_detached_lifecycle_operation(config, tmp_path)

    assert order == ["wait_exit", "cleanup", "restart", "healthy"]
    stop_fe.assert_not_called()
    stop_sx.assert_not_called()


def test_detached_shutdown_all_stops_frontend_and_searxng_without_restarting(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    config = {
        "operation": update_engine.OPERATION_SHUTDOWN_ALL,
        "main_pid": 999999,
        "main_process_created_at": 1234.5,
        "child_processes": [],
        "backend_windows": [],
    }

    with patch.object(
        update_engine,
        "wait_for_owned_process_exit",
        side_effect=lambda *_a, **_k: order.append("wait_exit"),
    ), patch.object(
        update_engine,
        "_cleanup_after_backend_exit",
        side_effect=lambda _c: order.append("cleanup"),
    ), patch.object(
        update_engine,
        "stop_frontend_servers",
        side_effect=lambda _d: order.append("stop_frontend"),
    ), patch.object(
        update_engine,
        "stop_searxng_servers",
        side_effect=lambda _d: order.append("stop_searxng"),
    ), patch.object(update_engine, "restart_application") as restart_mock:
        update_engine._run_detached_lifecycle_operation(config, tmp_path)

    assert order == ["wait_exit", "cleanup", "stop_frontend", "stop_searxng"]
    restart_mock.assert_not_called()


def test_detached_shutdown_all_still_stops_searxng_if_frontend_stop_fails(
    tmp_path: Path,
) -> None:
    config = {
        "operation": update_engine.OPERATION_SHUTDOWN_ALL,
        "main_pid": 999999,
        "main_process_created_at": 1234.5,
    }
    searxng_called: list[bool] = []

    with patch.object(update_engine, "wait_for_owned_process_exit"), patch.object(
        update_engine, "_cleanup_after_backend_exit"
    ), patch.object(
        update_engine,
        "stop_frontend_servers",
        side_effect=update_engine.UpdateError("frontend stuck"),
    ), patch.object(
        update_engine,
        "stop_searxng_servers",
        side_effect=lambda _d: searxng_called.append(True),
    ):
        with pytest.raises(update_engine.UpdateError, match="frontend stuck"):
            update_engine._run_detached_lifecycle_operation(config, tmp_path)

    assert searxng_called == [True]


def test_stop_verified_recorded_processes_skips_unverified_and_recycled_pids() -> None:
    fake_proc = MagicMock(pid=200)
    fake_proc.is_running.return_value = True
    fake_psutil = MagicMock()
    fake_psutil.Error = Exception
    fake_psutil.Process.return_value = fake_proc

    records = [
        {"pid": 100, "process_created_at": None},  # unverified -> skip
        {"pid": 101, "process_created_at": 10.0},  # mismatched -> skip
        {"pid": 200, "process_created_at": 20.0},  # verified -> terminate
    ]

    with patch.object(update_engine, "_import_psutil", return_value=fake_psutil), patch.object(
        update_engine,
        "_identity_matches",
        side_effect=lambda pid, ts: pid == 200 and ts == 20.0,
    ), patch.object(update_engine, "_terminate_processes", return_value=[]) as terminate:
        update_engine._stop_verified_recorded_processes(records, "test proc")

    terminate.assert_called_once_with([fake_proc], fake_psutil, 10.0)


def test_force_exit_runs_registered_canonical_shutdown_handler(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    updater = project / "scripts" / "update_engine.py"
    updater.parent.mkdir(parents=True)
    updater.write_text("", encoding="utf-8")

    called: list[str] = []
    monkeypatch.setattr(app_state, "shutdown_handler", lambda: called.append("canonical"))
    monkeypatch.setattr(app_state, "child_processes", [])

    scheduled_fn = []

    class _FakeTimer:
        def __init__(self, _delay, fn):
            self.daemon = False
            scheduled_fn.append(fn)

        def start(self):
            pass

    with patch.object(system_routes.subprocess, "Popen"), patch.object(
        system_routes.threading, "Timer", _FakeTimer
    ), patch.object(system_routes.os, "_exit", side_effect=lambda code: called.append(f"exit:{code}")), patch.object(
        system_routes.logging, "shutdown"
    ):
        system_routes._launch_detached_updater(
            project,
            updater,
            "python",
            extra_config={"operation": update_engine.OPERATION_SHUTDOWN_ALL},
        )
        assert len(scheduled_fn) == 1
        scheduled_fn[0]()

    assert called == ["canonical", "exit:0"]
    written = json.loads((project / ".update_config.json").read_text(encoding="utf-8"))
    assert written["operation"] == update_engine.OPERATION_SHUTDOWN_ALL
    assert "backend_windows" in written


def test_update_engine_main_dispatches_lifecycle_operation_and_cleans_config(
    tmp_path: Path,
) -> None:
    repo_root = Path(update_engine.__file__).resolve().parent.parent
    cfg = tmp_path / ".update_config.json"
    cfg.write_text(
        json.dumps(
            {
                "project_dir": str(repo_root),
                "operation": update_engine.OPERATION_SHUTDOWN_ALL,
                "main_pid": 1,
            }
        ),
        encoding="utf-8",
    )

    with patch.object(update_engine, "setup_logging"), patch.object(
        update_engine, "_run_detached_lifecycle_operation"
    ) as lifecycle:
        rc = update_engine.main(["--config", str(cfg)])

    assert rc == 0
    lifecycle.assert_called_once()
    assert not cfg.exists()


def test_detached_restart_on_linux_start_sh_recovers_searxng_and_frontend(
    tmp_path: Path,
) -> None:
    """Codex PR #529 finding: on Linux start.sh/start-dev.sh, backend exit triggers
    wait -n -> cleanup() killing SearXNG and Frontend. Detached restart must settle
    and restart SearXNG (before backend) and Frontend (after backend is healthy)."""
    order: list[str] = []
    config = {
        "operation": update_engine.OPERATION_RESTART_BACKEND,
        "main_pid": 999999,
        "main_process_created_at": 1234.5,
        "pre_searxng_running": True,
        "pre_frontend_mode": update_engine.FRONTEND_MODE_START,
        "posix_launcher_present": True,
    }
    fake_proc = MagicMock(pid=4242)

    with patch.object(update_engine, "_is_windows", return_value=False), patch.object(
        update_engine,
        "wait_for_owned_process_exit",
        side_effect=lambda *_a, **_k: order.append("wait_exit"),
    ), patch.object(
        update_engine,
        "_cleanup_after_backend_exit",
        side_effect=lambda _c: order.append("cleanup"),
    ), patch.object(
        update_engine,
        "stop_searxng_servers",
        side_effect=lambda _d: (order.append("stop_searxng"), True)[1],
    ), patch.object(
        update_engine,
        "stop_frontend_servers",
        side_effect=lambda _d: (
            order.append("stop_frontend"),
            update_engine.FrontendServers([], update_engine.FRONTEND_MODE_START),
        )[1],
    ), patch.object(update_engine, "find_searxng_servers", return_value=[]), patch.object(
        update_engine,
        "find_frontend_servers",
        return_value=update_engine.FrontendServers([], None),
    ), patch.object(
        update_engine,
        "_restart_searxng",
        side_effect=lambda _d, was: (order.append(f"restart_searxng:{was}"), True)[1],
    ), patch.object(
        update_engine,
        "restart_application",
        side_effect=lambda _c: (order.append("restart_backend"), fake_proc)[1],
    ), patch.object(
        update_engine,
        "wait_for_healthy_restart",
        side_effect=lambda _p, _c: (
            order.append("healthy"),
            {"city_name": "city_a", "version": "0.3.35"},
        )[1],
    ), patch.object(
        update_engine,
        "_restart_frontend",
        side_effect=lambda _d, mode: order.append(f"restart_frontend:{mode}"),
    ):
        update_engine._run_detached_lifecycle_operation(config, tmp_path)

    assert order == [
        "wait_exit",
        "cleanup",
        "stop_searxng",
        "stop_frontend",
        "restart_searxng:True",
        "restart_backend",
        "healthy",
        "restart_frontend:start",
    ]


def test_detached_restart_on_windows_leaves_running_companions_untouched(
    tmp_path: Path,
) -> None:
    """When SearXNG and Frontend remain alive (Windows separate windows),
    restarting the backend must not stop or restart them."""
    config = {
        "operation": update_engine.OPERATION_RESTART_BACKEND,
        "main_pid": 999999,
        "main_process_created_at": 1234.5,
        "pre_searxng_running": True,
        "pre_frontend_mode": update_engine.FRONTEND_MODE_START,
        "posix_launcher_present": False,
    }
    fake_proc = MagicMock(pid=4242)

    with patch.object(update_engine, "_is_windows", return_value=True), patch.object(
        update_engine, "wait_for_owned_process_exit"
    ), patch.object(update_engine, "_cleanup_after_backend_exit"), patch.object(
        update_engine, "stop_searxng_servers"
    ) as stop_sx, patch.object(
        update_engine, "stop_frontend_servers"
    ) as stop_fe, patch.object(
        update_engine, "find_searxng_servers", return_value=[MagicMock(pid=88)]
    ), patch.object(
        update_engine,
        "find_frontend_servers",
        return_value=update_engine.FrontendServers([MagicMock(pid=30)], "start"),
    ), patch.object(update_engine, "_restart_searxng") as restart_sx, patch.object(
        update_engine, "restart_application", return_value=fake_proc
    ), patch.object(
        update_engine,
        "wait_for_healthy_restart",
        return_value={"city_name": "city_a", "version": "0.3.35"},
    ), patch.object(update_engine, "_restart_frontend") as restart_fe:
        update_engine._run_detached_lifecycle_operation(config, tmp_path)

    stop_sx.assert_not_called()
    stop_fe.assert_not_called()
    restart_sx.assert_not_called()
    restart_fe.assert_not_called()


def test_detached_backup_operation_recovers_stopped_companions_on_linux_start_sh(
    tmp_path: Path,
) -> None:
    """Offline backup operations also stop the backend; when started from
    Linux start.sh, SearXNG and Frontend must be recovered around the restart."""
    order: list[str] = []
    config = {
        "operation": update_engine.OPERATION_SNAPSHOT_SAVE,
        "snapshot_name": "manual_snap",
        "main_pid": 999999,
        "main_process_created_at": 1234.5,
        "pre_searxng_running": True,
        "pre_frontend_mode": update_engine.FRONTEND_MODE_DEV,
        "posix_launcher_present": True,
    }
    fake_proc = MagicMock(pid=5555)

    with patch.object(update_engine, "_is_windows", return_value=False), patch.object(
        update_engine, "wait_for_owned_process_exit"
    ), patch.object(update_engine, "_cleanup_after_backend_exit"), patch.object(
        update_engine,
        "stop_searxng_servers",
        side_effect=lambda _d: (order.append("stop_searxng"), True)[1],
    ), patch.object(
        update_engine,
        "stop_frontend_servers",
        side_effect=lambda _d: (
            order.append("stop_frontend"),
            update_engine.FrontendServers([], update_engine.FRONTEND_MODE_DEV),
        )[1],
    ), patch.object(
        update_engine,
        "create_pre_update_snapshot",
        side_effect=lambda *_a, **_k: (order.append("snapshot"), "manual_snap")[1],
    ), patch.object(update_engine, "find_searxng_servers", return_value=[]), patch.object(
        update_engine,
        "find_frontend_servers",
        return_value=update_engine.FrontendServers([], None),
    ), patch.object(
        update_engine,
        "_restart_searxng",
        side_effect=lambda _d, _w: (order.append("restart_searxng"), True)[1],
    ), patch.object(
        update_engine,
        "restart_application",
        side_effect=lambda _c: (order.append("restart_backend"), fake_proc)[1],
    ), patch.object(
        update_engine,
        "wait_for_healthy_restart",
        return_value={"city_name": "city_a", "version": "0.3.35"},
    ), patch.object(
        update_engine,
        "_restart_frontend",
        side_effect=lambda _d, mode: order.append(f"restart_frontend:{mode}"),
    ):
        update_engine._run_detached_backup_operation(config, tmp_path, "python")

    assert order == [
        "stop_searxng",
        "stop_frontend",
        "snapshot",
        "restart_searxng",
        "restart_backend",
        "restart_frontend:dev",
    ]


def test_has_posix_launcher_ancestor_matches_only_this_checkout(tmp_path: Path) -> None:
    project = tmp_path / "saiverse"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    (project / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (other / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def make_psutil(parent_cmdline: list[str], parent_cwd: str):
        parent = MagicMock()
        parent.cmdline.return_value = parent_cmdline
        parent.cwd.return_value = parent_cwd
        proc = MagicMock()
        proc.parents.return_value = [parent]
        ps = MagicMock()
        ps.Error = Exception
        ps.Process.return_value = proc
        return ps

    with patch.object(update_engine, "_is_windows", return_value=False):
        with patch.object(
            update_engine,
            "_import_psutil",
            return_value=make_psutil(["bash", "./start.sh"], str(project)),
        ):
            assert update_engine._has_posix_launcher_ancestor(123, project) is True

        with patch.object(
            update_engine,
            "_import_psutil",
            return_value=make_psutil(["bash", "./start.sh"], str(other)),
        ):
            assert update_engine._has_posix_launcher_ancestor(123, project) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Terminal exit-code-0 check")
def test_windows_terminate_processes_exits_cmd_window_with_code_zero() -> None:
    """Stopping a ``cmd /k`` console window on Windows must exit with code 0
    (not psutil's default 15) so Windows Terminal closes the tab cleanly."""
    import subprocess
    import time

    import psutil

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    proc = subprocess.Popen(
        ["cmd.exe", "/k", "title saiverse_test_exit_zero"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=si,
    )
    try:
        time.sleep(0.25)
        ps_proc = psutil.Process(proc.pid)
        survivors = update_engine._terminate_processes([ps_proc], psutil, timeout=5.0)
        assert survivors == []
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
