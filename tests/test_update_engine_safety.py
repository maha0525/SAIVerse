from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts import update_engine


def test_dirty_worktree_is_rejected_without_stash_or_reset(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    results = [
        SimpleNamespace(stdout=" M user_file.py\n", stderr="", returncode=0),
    ]
    with patch.object(update_engine.shutil, "which", return_value="git"), patch.object(
        update_engine,
        "_run",
        side_effect=results,
    ) as run:
        with pytest.raises(update_engine.UpdateError, match="local changes"):
            update_engine.assert_git_update_ready(tmp_path)

    assert run.call_count == 1
    assert "status" in run.call_args.args[0]


def test_unverified_pid_is_never_signalled() -> None:
    with patch.object(update_engine, "_process_alive", return_value=True), patch.object(
        update_engine,
        "_identity_matches",
        return_value=False,
    ), patch("scripts.update_engine.time.sleep"):
        with pytest.raises(update_engine.UpdateError, match="refusing to signal"):
            update_engine.wait_for_owned_process_exit(4242, None, timeout=0)


def test_dependency_failure_rolls_back_and_stops_before_restart(tmp_path: Path) -> None:
    config = {
        "venv_python": "python",
        "main_pid": 10,
        "main_process_created_at": 1.0,
    }
    with patch.object(
        update_engine,
        "assert_git_update_ready",
        return_value="old-head",
    ), patch.object(update_engine, "wait_for_owned_process_exit"), patch.object(
        update_engine,
        "create_pre_update_snapshot",
        return_value="snapshot",
    ), patch.object(update_engine, "update_code"), patch.object(
        update_engine,
        "update_dependencies",
        side_effect=update_engine.UpdateError("pip failed"),
    ), patch.object(update_engine, "_rollback_code_and_dependencies") as rollback, patch.object(
        update_engine,
        "restart_application",
    ) as restart:
        with pytest.raises(update_engine.UpdateError, match="pip failed"):
            update_engine.run_update(config, tmp_path)

    rollback.assert_called_once_with(tmp_path, "python", "old-head")
    restart.assert_not_called()


def test_health_check_rejects_wrong_city_without_waiting_for_timeout() -> None:
    process = MagicMock()
    process.poll.return_value = None
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"city_name":"city_b","db_identity":"same"}'
    )
    config = {
        "listen_host": "127.0.0.1",
        "backend_port": 8000,
        "city_name": "city_a",
        "db_identity": "same",
    }
    with patch.object(update_engine, "urlopen", return_value=response):
        with pytest.raises(update_engine.UpdateError, match="wrong City"):
            update_engine.wait_for_healthy_restart(process, config, timeout=1)
