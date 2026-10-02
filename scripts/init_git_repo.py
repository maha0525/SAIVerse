"""Put a ZIP-extracted SAIVerse folder under Git so it can update itself.

Called by setup.bat / setup.sh once Git is available. Both platforms share this
one implementation.

The contract: when this script reports success, the revision Git records for
the folder holds exactly the files that are in the folder. The updater refuses
to start when tracked files differ from the recorded revision, so a record that
does not match the files leaves the user unable to update.

The earlier setup step ran ``git reset origin/main`` unconditionally. That only
honours the contract when the ZIP is the newest release: a ZIP downloaded before
a newer release came out got its record moved to the newest revision while its
files stayed old, and every file changed between the two versions then looked
like a local edit (docs/issues/setup_from_older_zip_blocks_update.md).

So the record is set to the release the folder actually contains -- the tag
named after the VERSION file -- and the newer release stays ahead of it as an
ordinary update. The result is checked rather than assumed: whatever the cause,
a folder whose files do not match the record is reported to the user here,
while they are looking at the setup window.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_REMOTE_URL = "https://github.com/maha0525/SAIVerse.git"
RELEASE_BRANCH = "main"
LATEST_ZIP_URL = "https://github.com/maha0525/SAIVerse/releases/latest/download/SAIVerse.zip"

EXIT_OK = 0
EXIT_GIT_FAILED = 1
EXIT_FILES_DO_NOT_MATCH = 2


class GitInitError(RuntimeError):
    """A git command this script cannot continue without has failed."""


def _git(project_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=project_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitInitError(f"git {' '.join(args)} failed (exit {result.returncode}): {detail}")
    return result


def _git_showing_progress(project_dir: Path, *args: str) -> None:
    """Run a long git command with its own output on the setup window.

    The download can take minutes on a slow line; with the output captured the
    window would sit silent and look frozen.
    """
    sys.stdout.flush()
    result = subprocess.run(["git", *args], cwd=project_dir, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise GitInitError(f"git {' '.join(args)} failed (exit {result.returncode}); see the messages above")


def read_version(project_dir: Path) -> str | None:
    """The version the folder says it is, or None when it does not say."""
    try:
        text = (project_dir / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def resolve_release_tag(project_dir: Path, version: str | None) -> str | None:
    """The tag of the release this folder was made from, if Git knows it.

    Only a tag that is part of the release branch's history counts: the folder
    is put on that branch, and a tag from another line of development (an
    early-access build) could not be fast-forwarded to the branch's newest
    revision afterwards.
    """
    if not version:
        return None
    tag = f"v{version}"
    if _git(project_dir, "rev-parse", "--quiet", "--verify", f"refs/tags/{tag}^{{commit}}", check=False).returncode != 0:
        return None
    upstream = f"origin/{RELEASE_BRANCH}"
    if _git(project_dir, "merge-base", "--is-ancestor", tag, upstream, check=False).returncode != 0:
        return None
    return tag


def count_mismatched_files(project_dir: Path) -> int:
    """How many tracked files differ from the recorded revision.

    The same question the updater asks before it starts (tracked files only;
    untracked files do not block an update).
    """
    status = _git(project_dir, "status", "--porcelain", "--untracked-files=no").stdout
    return len([line for line in status.splitlines() if line.strip()])


def commits_behind(project_dir: Path) -> int:
    result = _git(project_dir, "rev-list", "--count", f"HEAD..origin/{RELEASE_BRANCH}", check=False)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def has_recorded_revision(project_dir: Path) -> bool:
    """Whether Git already records a revision for this folder.

    A ``.git`` folder alone does not say so: a setup run that was interrupted
    after ``git init`` (no network for the fetch, window closed) leaves one
    behind with nothing recorded, and treating that as "already initialized"
    would leave the folder without updates for good.
    """
    if not (project_dir / ".git").exists():
        return False
    return _git(project_dir, "rev-parse", "--quiet", "--verify", "HEAD^{commit}", check=False).returncode == 0


def _differ_phrase(count: int) -> str:
    return "1 file differs" if count == 1 else f"{count} files differ"


def report_existing_repository(project_dir: Path) -> int:
    """A folder Git already records is left exactly as it is.

    It may be a developer's checkout with work in progress, so nothing is moved
    here. It may also be a folder an earlier setup left with a record that does
    not match its files; that user is told what they are looking at, because
    nothing else will tell them why updates are refused.
    """
    print("[OK] Git repository already exists")
    mismatched = count_mismatched_files(project_dir)
    if not mismatched:
        return EXIT_OK
    print(f"[INFO] Git reports local changes in this folder ({_differ_phrase(mismatched)} from the recorded version).")
    print("  If you edited those files yourself, this is expected.")
    print("  If you did not, update.bat / update.sh and the Update button will refuse to update.")
    print("  A new folder made from the newest ZIP can update again (run setup there):")
    print(f"    {LATEST_ZIP_URL}")
    return EXIT_FILES_DO_NOT_MATCH


def init_repository(project_dir: Path, remote_url: str) -> int:
    if has_recorded_revision(project_dir):
        return report_existing_repository(project_dir)

    print("")
    print("[SETUP] Initializing repository for automatic updates...")
    upstream = f"origin/{RELEASE_BRANCH}"
    # Every step below can be repeated, so an interrupted run is finished by
    # running setup again.
    _git(project_dir, "init")
    if _git(project_dir, "remote", "get-url", "origin", check=False).returncode != 0:
        _git(project_dir, "remote", "add", "origin", remote_url)
    print("[SETUP] Downloading the update history (this can take a few minutes)...")
    _git_showing_progress(project_dir, "fetch", "origin")
    # Nothing is recorded yet, so there is no branch to rename; point the
    # not-yet-existing current branch at the release branch's name instead.
    _git(project_dir, "symbolic-ref", "HEAD", f"refs/heads/{RELEASE_BRANCH}")
    # Which branch updates come from. Written before the record itself, so that
    # the reset below is the last step: a folder with a recorded revision is
    # always a finished one, whenever the window was closed.
    _git(project_dir, "config", f"branch.{RELEASE_BRANCH}.remote", "origin")
    _git(project_dir, "config", f"branch.{RELEASE_BRANCH}.merge", f"refs/heads/{RELEASE_BRANCH}")

    version = read_version(project_dir)
    tag = resolve_release_tag(project_dir, version)
    # A mixed reset: it moves Git's record and leaves every file in the folder
    # as it is. Nothing the user has is overwritten here.
    _git(project_dir, "reset", "--quiet", tag or upstream)

    mismatched = count_mismatched_files(project_dir)
    if mismatched:
        print("")
        print(f"[WARN] The files in this folder do not match the version Git recorded for it ({_differ_phrase(mismatched)}).")
        if tag:
            print(f"  Git recorded this folder as {tag}, the release named in its VERSION file.")
        else:
            shown = version or "(no VERSION file)"
            print(f"  This folder says it is version {shown}, which is not a release Git knows,")
            print("  so the newest release was recorded instead.")
        print("  SAIVerse will run, but update.bat / update.sh and the Update button will refuse to update,")
        print("  because the differing files look like local edits.")
        print("  To get a folder that can update, download the newest ZIP and run setup there:")
        print(f"    {LATEST_ZIP_URL}")
        return EXIT_FILES_DO_NOT_MATCH

    label = tag or upstream
    print(f"[OK] Git repository initialized ({label})")
    behind = commits_behind(project_dir)
    if behind:
        print("[INFO] A newer version of SAIVerse is available.")
        print("  After setup finishes, run update.bat (Windows) or ./update.sh (macOS / Linux) to get it.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="The SAIVerse folder (default: the folder this script belongs to).",
    )
    parser.add_argument(
        "--remote-url",
        default=DEFAULT_REMOTE_URL,
        help="The repository to track (tests pass a local one).",
    )
    args = parser.parse_args(argv)
    try:
        return init_repository(args.project_dir.resolve(), args.remote_url)
    except GitInitError as exc:
        print(f"[WARN] Could not initialize the Git repository: {exc}")
        print("  SAIVerse will run, but automatic updates will not be available.")
        print("  You can run setup again later to retry.")
        return EXIT_GIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
