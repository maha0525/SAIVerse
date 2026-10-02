"""scripts/init_git_repo.py: a ZIP-extracted folder is put under Git so that the
revision Git records holds exactly the files in the folder.

These tests run real git against a throwaway "published" repository, and build
the installed folder the way a user gets it: a ZIP made by ``git archive`` (the
command the release workflow uses), extracted somewhere else.

Background: docs/issues/setup_from_older_zip_blocks_update.md
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import init_git_repo  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    # Keep the developer's own git configuration out of the result.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **_GIT_ENV},
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch):
    for key, value in _GIT_ENV.items():
        monkeypatch.setenv(key, value)


def _write(path: Path, text: str, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)


def _commit_release(remote: Path, version: str, body: str) -> None:
    _write(remote / "VERSION", f"{version}\n")
    _write(remote / "app.py", f"print({body!r})\n")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-m", f"release {version}")
    _git(remote, "tag", f"v{version}")


@pytest.fixture
def published(tmp_path: Path) -> Path:
    """A published repository: releases 1.0.0 and 1.1.0 on main, both tagged."""
    remote = tmp_path / "published"
    remote.mkdir()
    _git(remote, "init")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    # The real repository normalizes line endings the same way: Windows scripts
    # are CRLF in the ZIP and LF in Git's record, everything else is LF.
    _write(remote / ".gitattributes", "* text=auto eol=lf\n*.bat text eol=crlf\n")
    _write(remote / "setup.bat", "@echo off\necho setup\n")
    _commit_release(remote, "1.0.0", "first")
    _commit_release(remote, "1.1.0", "second")
    return remote


def _install_from_zip(published: Path, ref: str, target: Path) -> Path:
    """Extract the ZIP of ``ref`` the way a user would."""
    archive = target.parent / f"{target.name}.zip"
    _git(published, "archive", "--format=zip", "-o", str(archive), ref)
    target.mkdir()
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(target)
    return target


def _tracked_changes(folder: Path) -> str:
    return _git(folder, "status", "--porcelain", "--untracked-files=no")


def test_older_zip_is_recorded_as_its_own_release_and_can_update(published, tmp_path, capsys):
    folder = _install_from_zip(published, "v1.0.0", tmp_path / "installed")

    code = init_git_repo.init_repository(folder, str(published))

    assert code == init_git_repo.EXIT_OK
    assert _git(folder, "rev-parse", "HEAD") == _git(published, "rev-parse", "v1.0.0^{commit}")
    assert _tracked_changes(folder) == ""
    assert _git(folder, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(folder, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    assert "A newer version of SAIVerse is available" in capsys.readouterr().out

    # What the updater does next: fast-forward to the newest release.
    _git(folder, "merge", "--ff-only", "origin/main")
    assert (folder / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    assert _tracked_changes(folder) == ""


def test_newest_zip_is_recorded_as_the_newest_release(published, tmp_path, capsys):
    folder = _install_from_zip(published, "v1.1.0", tmp_path / "installed")

    code = init_git_repo.init_repository(folder, str(published))

    assert code == init_git_repo.EXIT_OK
    assert _git(folder, "rev-parse", "HEAD") == _git(published, "rev-parse", "main")
    assert _tracked_changes(folder) == ""
    assert "A newer version" not in capsys.readouterr().out


def test_folder_of_an_unknown_version_is_reported_not_passed_off_as_fine(published, tmp_path, capsys):
    # A ZIP of something that is not a release (a development branch).
    folder = _install_from_zip(published, "v1.0.0", tmp_path / "installed")
    _write(folder / "VERSION", "9.9.9\n")

    code = init_git_repo.init_repository(folder, str(published))

    out = capsys.readouterr().out
    assert code == init_git_repo.EXIT_FILES_DO_NOT_MATCH
    assert "do not match the version Git recorded" in out
    assert "9.9.9" in out
    assert init_git_repo.LATEST_ZIP_URL in out
    assert "[OK] Git repository initialized" not in out
    # Nothing in the folder was overwritten.
    assert (folder / "VERSION").read_text(encoding="utf-8").strip() == "9.9.9"
    assert (folder / "app.py").read_text(encoding="utf-8") == "print('first')\n"


def test_release_folder_with_changed_files_is_reported(published, tmp_path, capsys):
    folder = _install_from_zip(published, "v1.0.0", tmp_path / "installed")
    _write(folder / "app.py", "print('edited by the user')\n")

    code = init_git_repo.init_repository(folder, str(published))

    out = capsys.readouterr().out
    assert code == init_git_repo.EXIT_FILES_DO_NOT_MATCH
    assert "1 files differ" in out
    assert "v1.0.0" in out
    assert (folder / "app.py").read_text(encoding="utf-8") == "print('edited by the user')\n"


def test_tag_outside_the_release_branch_is_not_used(published, tmp_path):
    # An early-access build: tagged, but not part of main's history.
    _git(published, "checkout", "-b", "early-access", "v1.0.0")
    _commit_release(published, "2.0.0rc1", "early")
    _git(published, "checkout", "main")
    folder = _install_from_zip(published, "v2.0.0rc1", tmp_path / "installed")

    code = init_git_repo.init_repository(folder, str(published))

    # Recording that tag would put main on a revision it can never fast-forward
    # from, so it is not used; the mismatch that follows is reported instead.
    assert _git(folder, "rev-parse", "HEAD") == _git(published, "rev-parse", "main")
    assert code == init_git_repo.EXIT_FILES_DO_NOT_MATCH


def test_interrupted_setup_is_finished_by_running_again(published, tmp_path, capsys):
    folder = _install_from_zip(published, "v1.0.0", tmp_path / "installed")
    missing = tmp_path / "not-there-yet"

    # First run: the download fails (no network).
    assert init_git_repo.main(["--project-dir", str(folder), "--remote-url", str(missing)]) == init_git_repo.EXIT_GIT_FAILED
    assert (folder / ".git").exists()
    assert "Could not initialize the Git repository" in capsys.readouterr().out

    # The network is back; the same remote now answers.
    shutil.copytree(published, missing)
    assert init_git_repo.main(["--project-dir", str(folder), "--remote-url", str(missing)]) == init_git_repo.EXIT_OK
    assert _git(folder, "rev-parse", "HEAD") == _git(published, "rev-parse", "v1.0.0^{commit}")
    assert _tracked_changes(folder) == ""


def test_folder_that_already_has_a_recorded_revision_is_left_alone(published, tmp_path, capsys):
    folder = _install_from_zip(published, "v1.0.0", tmp_path / "installed")
    assert init_git_repo.init_repository(folder, str(published)) == init_git_repo.EXIT_OK
    head = _git(folder, "rev-parse", "HEAD")
    capsys.readouterr()

    assert init_git_repo.init_repository(folder, str(published)) == init_git_repo.EXIT_OK

    assert "[OK] Git repository already exists" in capsys.readouterr().out
    assert _git(folder, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("script", ["setup.bat", "setup.sh"])
def test_setup_scripts_delegate_to_the_shared_implementation(script):
    """Setup parity: both platforms go through init_git_repo.py, and neither
    keeps its own copy of the reset that recorded the newest release
    unconditionally."""
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    assert "init_git_repo.py" in text
    assert "git reset origin/main" not in text
