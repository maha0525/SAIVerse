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
        SimpleNamespace(stdout=" M user_file.py\0", stderr="", returncode=0),
    ]
    with patch.object(update_engine.shutil, "which", return_value="git"), patch.object(
        update_engine,
        "_run",
        side_effect=results,
    ) as run:
        with pytest.raises(update_engine.UpdateError, match="local changes") as excinfo:
            update_engine.assert_git_update_ready(tmp_path)

    assert run.call_count == 1
    command = run.call_args.args[0]
    assert "status" in command
    # Untracked files (.DS_Store, a dropped-in diagnostics script) must not
    # block the update; only modified tracked files do.
    assert "--untracked-files=no" in command
    assert "--untracked-files=all" not in command
    # NUL-separated output decoded as UTF-8, so non-ASCII names stay readable
    # whatever the console code page is (see _parse_porcelain_z).
    assert "-z" in command
    assert run.call_args.kwargs.get("encoding") == "utf-8"
    # The refusal names the offending file so a non-developer can act on it,
    # and offers both exits: a copy-pasteable discard command built from the
    # listed files, and committing to keep the changes. It must still refuse —
    # the updater itself never stashes or resets (asserted by raising above).
    message = str(excinfo.value)
    assert "user_file.py" in message
    assert "git checkout -- user_file.py" in message
    assert "commit" in message


def test_dirty_worktree_message_keeps_unicode_names_and_renames_readable(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    # Staged rename (R in X) and work-tree rename (R in Y) both carry the
    # original path in an extra NUL field.
    status = " M 設定メモ.md\0R  new name.txt\0old.txt\0 R moved.txt\0was.txt\0"
    results = [SimpleNamespace(stdout=status, stderr="", returncode=0)]
    with patch.object(update_engine.shutil, "which", return_value="git"), patch.object(
        update_engine,
        "_run",
        side_effect=results,
    ):
        with pytest.raises(update_engine.UpdateError) as excinfo:
            update_engine.assert_git_update_ready(tmp_path)

    message = str(excinfo.value)
    assert "  設定メモ.md\n" in message
    assert "  old.txt -> new name.txt" in message
    assert "  was.txt -> moved.txt" in message
    assert "\\3" not in message  # no octal escapes leak into the list
    # The original path must not surface as a record of its own.
    assert "\n  was.txt\n" not in message and "\n  old.txt\n" not in message
    # A rename entry ("old -> new") is not a path, so the per-file discard
    # command cannot express the full set; the catch-all is offered instead of
    # a command that would discard only part and get refused again.
    assert "git checkout -- ." in message
    assert "git checkout -- 設定メモ.md" not in message


def test_update_code_refuses_to_overwrite_ignored_files(tmp_path: Path) -> None:
    """A fast-forward must not clobber an ignored file at a newly tracked path.

    ``git pull`` cannot be told this (``--no-overwrite-ignore`` is a merge
    option), so the update is fetch + merge with the flag.
    """
    with patch.object(update_engine, "_run") as run:
        update_engine.update_code(tmp_path)

    commands = [call.args[0] for call in run.call_args_list]
    assert commands[0][:2] == ["git", "fetch"]
    merge = commands[1]
    assert merge[:2] == ["git", "merge"]
    assert "--ff-only" in merge
    assert "--no-overwrite-ignore" in merge
    assert not any(cmd[:2] == ["git", "pull"] for cmd in commands)


def test_clean_tracked_tree_returns_head_revision(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    results = [
        SimpleNamespace(stdout="", stderr="", returncode=0),
        SimpleNamespace(stdout="abc123\n", stderr="", returncode=0),
    ]
    with patch.object(update_engine.shutil, "which", return_value="git"), patch.object(
        update_engine,
        "_run",
        side_effect=results,
    ) as run:
        assert update_engine.assert_git_update_ready(tmp_path) == "abc123"

    assert run.call_count == 2
    assert run.call_args_list[1].args[0] == ["git", "rev-parse", "HEAD"]


def test_dirty_worktree_message_lists_at_most_twenty_paths(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    status = "".join(f" M file_{i:02d}.py\0" for i in range(25))
    results = [SimpleNamespace(stdout=status, stderr="", returncode=0)]
    with patch.object(update_engine.shutil, "which", return_value="git"), patch.object(
        update_engine,
        "_run",
        side_effect=results,
    ):
        with pytest.raises(update_engine.UpdateError) as excinfo:
            update_engine.assert_git_update_ready(tmp_path)

    message = str(excinfo.value)
    assert update_engine.LOCAL_CHANGES_LIST_LIMIT == 20
    assert "file_00.py" in message
    assert "file_19.py" in message
    assert "file_20.py" not in message
    assert "... and 5 more" in message
    # With the list truncated, a per-file discard command would miss the hidden
    # files and the update would refuse again; offer the catch-all instead.
    assert "git checkout -- ." in message
    assert "git checkout -- file_00.py" not in message


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
