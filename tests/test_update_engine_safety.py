from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts import update_engine


def _make_portable_git(project_dir: Path) -> Path:
    portable_cmd = project_dir / ".git-portable" / "cmd"
    portable_cmd.mkdir(parents=True)
    (portable_cmd / "git.exe").write_text("")
    return portable_cmd


def test_portable_git_is_prepended_to_path_when_present(tmp_path: Path) -> None:
    portable_cmd = _make_portable_git(tmp_path)
    with patch.dict(update_engine.os.environ, {"PATH": "/usr/bin"}, clear=False):
        update_engine._ensure_portable_git_on_path(tmp_path)
        entries = update_engine.os.environ["PATH"].split(update_engine.os.pathsep)
        assert entries[0] == str(portable_cmd)


def test_portable_git_absent_is_noop(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()  # a checkout but no portable git
    with patch.dict(update_engine.os.environ, {"PATH": "/usr/bin"}, clear=False):
        update_engine._ensure_portable_git_on_path(tmp_path)
        assert update_engine.os.environ["PATH"] == "/usr/bin"


def test_portable_git_prepend_is_idempotent(tmp_path: Path) -> None:
    portable_cmd = _make_portable_git(tmp_path)
    seeded = str(portable_cmd) + update_engine.os.pathsep + "/usr/bin"
    with patch.dict(update_engine.os.environ, {"PATH": seeded}, clear=False):
        update_engine._ensure_portable_git_on_path(tmp_path)
        assert update_engine.os.environ["PATH"] == seeded


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


def _rollback_with_reset(project: Path, *, lock_after_reset: bool):  # type: ignore[no-untyped-def]
    """Run the rollback with ``git reset`` faked to leave (or not leave) a lock
    behind, and return the ``update_dependencies`` mock."""

    def fake_run(command, *, cwd, label, timeout=900, check=True):  # type: ignore[no-untyped-def]
        assert command[:3] == ["git", "reset", "--hard"]
        lock = project / update_engine.REQUIREMENTS_LOCK
        if lock_after_reset:
            lock.write_text("pytest==9.0.0\n", encoding="utf-8")
        elif lock.exists():
            lock.unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(update_engine, "_run", side_effect=fake_run), patch.object(
        update_engine, "update_dependencies"
    ) as deps:
        update_engine._rollback_code_and_dependencies(project, "python", "old-head")
    return deps


def test_rollback_to_a_lock_revision_reinstalls_from_the_lock(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level("WARNING", logger="saiverse.update"):
        deps = _rollback_with_reset(tmp_path, lock_after_reset=True)

    deps.assert_called_once_with(tmp_path, "python", None)
    assert not any("predates" in record.getMessage() for record in caplog.records)


def test_rollback_to_a_pre_lock_revision_reinstalls_from_its_requirements_txt(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Updating from v0.3.3 (no lock) to the first lock release and failing
    mid-way: after ``git reset`` the old tree has only requirements.txt, so the
    repair must install from that and say why."""
    (tmp_path / update_engine.REQUIREMENTS_LOCK).write_text("new-lock\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest>=9.0.0\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="saiverse.update"):
        deps = _rollback_with_reset(tmp_path, lock_after_reset=False)

    deps.assert_called_once_with(tmp_path, "python", update_engine.LEGACY_REQUIREMENTS)
    assert update_engine.LEGACY_REQUIREMENTS == "requirements.txt"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("old-head predates requirements.lock" in m and "requirements.txt" in m for m in warnings)


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
