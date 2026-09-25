"""Release-channel switch in the update engine (docs/intent/early_access_release.md §3-2 / §3-3).

The switch rides ``run_update``; what is pinned here is what differs from an
update, exercised against real git repositories (a bare "origin" and a clone
standing in for a user's install) because the guarantees are git behaviour:

- joining creates a local ``early-access`` that *tracks* ``origin/early-access``
  (so later updates fast-forward it), and the restore point is labelled
  ``ea_optin_*``;
- before the first early-access release there is no ``origin/early-access``,
  and the switch refuses with a clear message before anything moves;
- the switch will not silently overwrite an ignored file the other branch
  tracks (``--no-overwrite-ignore``), and leaves the checkout as it was;
- a failure after the code moved puts the checkout back on the *recorded*
  branch at the old revision, not the old revision on the new branch;
- returning to stable fast-forwards the existing local ``main``, and is refused
  while stable is still older than this install (the world only moves forward);
- a development branch or a dirty tree is refused, with switch wording.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import update_engine

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")


def _commit_version(repo: Path, version: str) -> None:
    (repo / "VERSION").write_text(version + "\n", encoding="utf-8")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-m", f"release {version}")


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    """The publisher's repo, pushed to a bare origin, with main at 0.3.14."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "seed"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _configure(repo)
    (repo / ".gitignore").write_text("notes/\n", encoding="utf-8")
    (repo / "README.md").write_text("SAIVerse\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "README.md")
    _commit_version(repo, "0.3.14")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _publish_early_access(seed: Path, version: str = "0.4.0rc1", *, track_notes: bool = False) -> None:
    _git(seed, "switch", "-c", "early-access")
    (seed / "VERSION").write_text(version + "\n", encoding="utf-8")
    _git(seed, "add", "VERSION")
    if track_notes:
        notes = seed / "notes"
        notes.mkdir()
        (notes / "memo.txt").write_text("from the early-access release\n", encoding="utf-8")
        _git(seed, "add", "-f", "notes/memo.txt")
    _git(seed, "commit", "-m", f"release {version}")
    _git(seed, "push", "-u", "origin", "early-access")
    _git(seed, "switch", "main")


@pytest.fixture
def install(tmp_path: Path, seed: Path) -> Path:
    """A user's install: a clone of origin, on main."""
    dest = tmp_path / "install"
    subprocess.run(
        ["git", "clone", "-c", "core.autocrlf=false", str(tmp_path / "remote.git"), str(dest)],
        check=True,
        capture_output=True,
    )
    _configure(dest)
    return dest


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _branch(repo: Path) -> str:
    return _git(repo, "symbolic-ref", "--short", "HEAD")


def _run_switch(install: Path, channel: str, *, deps_side_effect=None):  # type: ignore[no-untyped-def]
    """``run_update`` in switch mode with the expensive phases faked."""
    with patch.object(
        update_engine, "create_pre_update_snapshot", return_value="snap"
    ) as snapshot, patch.object(
        update_engine, "update_dependencies", side_effect=deps_side_effect
    ) as deps, patch.object(update_engine, "write_completion_marker"):
        update_engine.run_update(None, install, switch_channel=channel)
    return snapshot, deps


# --- branch reading -----------------------------------------------------------


def test_read_checkout_branch_names_the_branch_and_its_channel(install: Path, seed: Path) -> None:
    assert update_engine.read_checkout_branch(install) == "main"
    assert update_engine.channel_for_branch("main") == update_engine.CHANNEL_STABLE
    assert update_engine.channel_for_branch("early-access") == update_engine.CHANNEL_EARLY_ACCESS
    # A development branch and a detached / unreadable HEAD keep stable behaviour.
    assert update_engine.channel_for_branch("develop") == update_engine.CHANNEL_STABLE
    assert update_engine.channel_for_branch(None) == update_engine.CHANNEL_STABLE

    _git(install, "switch", "--detach")
    assert update_engine.read_checkout_branch(install) is None


def test_read_checkout_branch_follows_a_worktree_pointer(install: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    _git(install, "worktree", "add", "-b", "early-access", str(linked))
    assert (linked / ".git").is_file()
    assert update_engine.read_checkout_branch(linked) == "early-access"


def test_read_checkout_branch_outside_a_repo_is_none(tmp_path: Path) -> None:
    assert update_engine.read_checkout_branch(tmp_path) is None


# --- joining early access --------------------------------------------------------


def test_switch_refuses_before_the_early_access_branch_is_published(install: Path) -> None:
    before = _head(install)
    with pytest.raises(update_engine.UpdateError, match="has not been published"):
        _run_switch(install, update_engine.CHANNEL_EARLY_ACCESS)
    assert _branch(install) == "main"
    assert _head(install) == before


def test_joining_early_access_creates_a_tracking_branch(install: Path, seed: Path) -> None:
    _publish_early_access(seed)

    snapshot, deps = _run_switch(install, update_engine.CHANNEL_EARLY_ACCESS)

    assert _branch(install) == "early-access"
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.4.0rc1"
    # Tracking is what makes later plain updates follow the early-access line.
    assert _git(install, "rev-parse", "--abbrev-ref", "early-access@{upstream}") == "origin/early-access"
    assert snapshot.call_args.kwargs["prefix"] == "ea_optin"
    deps.assert_called_once()


def test_switch_does_not_overwrite_an_ignored_file_the_other_branch_tracks(
    install: Path, seed: Path
) -> None:
    _publish_early_access(seed, track_notes=True)
    notes = install / "notes"
    notes.mkdir()
    (notes / "memo.txt").write_text("the user's own notes\n", encoding="utf-8")
    before = _head(install)

    with pytest.raises(update_engine.UpdateError, match="switch code to early-access"):
        _run_switch(install, update_engine.CHANNEL_EARLY_ACCESS)

    assert (notes / "memo.txt").read_text(encoding="utf-8") == "the user's own notes\n"
    assert _branch(install) == "main"
    assert _head(install) == before


def test_failure_after_the_switch_returns_to_the_recorded_branch(install: Path, seed: Path) -> None:
    _publish_early_access(seed)
    before = _head(install)
    calls: list[tuple] = []

    def deps(project_dir, python, requirements_file=None):  # type: ignore[no-untyped-def]
        calls.append((project_dir, requirements_file))
        if len(calls) == 1:
            raise update_engine.UpdateError("pip failed")

    with pytest.raises(update_engine.UpdateError, match="pip failed"):
        _run_switch(install, update_engine.CHANNEL_EARLY_ACCESS, deps_side_effect=deps)

    # Back on main at the old revision -- not the old revision on early-access.
    assert _branch(install) == "main"
    assert _head(install) == before
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.3.14"
    assert _git(install, "status", "--porcelain", "--untracked-files=no") == ""
    # The rollback reinstalled the previous revision's packages.
    assert len(calls) == 2


def test_switch_is_refused_from_a_development_branch(install: Path, seed: Path) -> None:
    _publish_early_access(seed)
    _git(install, "switch", "-c", "develop")
    with pytest.raises(update_engine.UpdateError, match="only possible from the main branch"):
        _run_switch(install, update_engine.CHANNEL_EARLY_ACCESS)
    assert _branch(install) == "develop"


def test_joining_is_refused_when_early_access_is_older_than_this_install(
    install: Path, seed: Path
) -> None:
    _publish_early_access(seed, version="0.3.13rc1")
    with pytest.raises(update_engine.UpdateError, match="older than this"):
        _run_switch(install, update_engine.CHANNEL_EARLY_ACCESS)
    assert _branch(install) == "main"


# --- returning to stable -------------------------------------------------------


def _join_directly(install: Path) -> None:
    _git(install, "fetch", "origin")
    _git(install, "switch", "-c", "early-access", "--track", "origin/early-access")


def test_return_is_refused_while_stable_is_older(install: Path, seed: Path) -> None:
    _publish_early_access(seed)
    _join_directly(install)
    with pytest.raises(update_engine.UpdateError, match="older than this"):
        _run_switch(install, update_engine.CHANNEL_STABLE)
    assert _branch(install) == "early-access"


def test_return_to_stable_fast_forwards_the_existing_main(install: Path, seed: Path) -> None:
    _publish_early_access(seed)
    _join_directly(install)
    stale_main = _git(install, "rev-parse", "main")
    _commit_version(seed, "0.4.0")
    _git(seed, "push", "origin", "main")

    snapshot, _ = _run_switch(install, update_engine.CHANNEL_STABLE)

    assert _branch(install) == "main"
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.4.0"
    assert _head(install) != stale_main
    assert _head(install) == _git(install, "rev-parse", "origin/main")
    assert _git(install, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    assert snapshot.call_args.kwargs["prefix"] == "ea_return"


def test_return_refuses_when_local_main_has_its_own_commits(install: Path, seed: Path) -> None:
    _publish_early_access(seed)
    (install / "local.txt").write_text("mine\n", encoding="utf-8")
    _git(install, "add", "local.txt")
    _git(install, "commit", "-m", "local work")
    _join_directly(install)
    _commit_version(seed, "0.4.0")
    _git(seed, "push", "origin", "main")

    with pytest.raises(update_engine.UpdateError, match="commits that origin/main does not have"):
        _run_switch(install, update_engine.CHANNEL_STABLE)
    assert _branch(install) == "early-access"


# --- wording and config ----------------------------------------------------------


def test_dirty_tree_refusal_is_worded_for_a_switch(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    results = [SimpleNamespace(stdout=" M user_file.py\0", stderr="", returncode=0)]
    with patch.object(update_engine.shutil, "which", return_value="git"), patch.object(
        update_engine, "_run", side_effect=results
    ):
        with pytest.raises(update_engine.UpdateError) as excinfo:
            update_engine.assert_git_update_ready(tmp_path, switching=True)

    message = str(excinfo.value)
    assert "channel switch was not started" in message
    assert "switch the channel again" in message
    assert "run the update again" not in message
    assert "user_file.py" in message


def test_config_switch_channel_is_validated() -> None:
    assert update_engine._config_switch_channel(None) is None
    assert update_engine._config_switch_channel({"main_pid": 1}) is None
    assert update_engine._config_switch_channel({"switch_channel": "early_access"}) == "early_access"
    with pytest.raises(update_engine.UpdateError, match="unknown release channel"):
        update_engine._config_switch_channel({"switch_channel": "nightly"})


def test_engine_preflight_for_a_switch_runs_after_the_old_process_exited(tmp_path: Path) -> None:
    """A detached switch re-checks the tree only once the old backend is gone.

    Whatever the dying process (or the user, in that window) still wrote to
    tracked files must be seen by the engine-side preflight, not just by the
    API-side one that ran before shutdown.
    """
    order: list[str] = []

    def record_wait(pid, created_at):
        order.append("wait")

    def record_preflight(project_dir, channel):
        order.append("preflight")
        raise update_engine.UpdateError("stop here: order is what this test checks")

    with patch.object(update_engine, "wait_for_owned_process_exit", side_effect=record_wait), patch.object(
        update_engine, "preflight_switch", side_effect=record_preflight
    ), patch.object(update_engine, "_ensure_portable_git_on_path"):
        with pytest.raises(update_engine.UpdateError):
            update_engine.run_update(
                {"main_pid": 1234, "venv_python": "python"},
                tmp_path,
                switch_channel="early_access",
            )

    assert order == ["wait", "preflight"]
