"""Canonical fail-closed SAIVerse update engine.

The API updater and the platform wrappers all delegate here.  This module is
deliberately self-contained because the working tree can change while it is
running.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.error import URLError
from urllib.request import Request, urlopen

try:  # ``packaging`` ships with pip, so every SAIVerse venv has it.
    from packaging.requirements import Requirement as _Requirement
except Exception:  # pragma: no cover - exercised by the degraded-parser test
    _Requirement = None  # type: ignore[assignment]

LOGGER = logging.getLogger("saiverse.update")

# Written next to the other self-update state files (``.update_config.json``,
# ``self_update.log``) and gitignored with them. It records what the checkout
# was *fully* updated to -- code, Python packages and frontend packages
# together -- so a launcher can tell a finished update from one that died
# half-way. See docs/issues/v0229_update_bat_truncates_after_git_pull.md.
UPDATE_COMPLETE_MARKER = ".update_complete"

# The pinned, verified package set. Every install path (setup.bat / setup.sh,
# this engine, addon installs as constraints) reads this file, never
# requirements.txt, so all users get the same combination. It is universal:
# lines carry environment markers (``pywin32==311 ; sys_platform == 'win32'``)
# and ``scan_requirements`` evaluates them for the running interpreter.
# See docs/intent/dependency_management.md.
REQUIREMENTS_LOCK = "requirements.lock"

# Exit codes of ``--check-complete``. The launchers branch on these, so they are
# part of the contract with start.bat / start.sh.
CHECK_READY = 0
CHECK_NEEDS_FINISH = 10
CHECK_INCONCLUSIVE = 11


class UpdateError(RuntimeError):
    """A phase failed and no later mutating phase may run."""


def setup_logging(project_dir: Path, *, to_file: bool = True) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        handlers.insert(0, logging.FileHandler(project_dir / "self_update.log", encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def read_version(project_dir: Path) -> str | None:
    """The VERSION file contents, or None when it cannot be read."""
    try:
        text = (project_dir / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def marker_path(project_dir: Path) -> Path:
    return project_dir / UPDATE_COMPLETE_MARKER


def _file_digest(path: Path) -> str | None:
    """sha256 of a file, or None when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def completion_fingerprint(project_dir: Path) -> dict[str, Any] | None:
    """What a completed update installs, as comparable values.

    The VERSION alone is not enough: a pull can change the dependency lists
    while VERSION stays put (every commit between two releases does), and the
    marker would then claim a finished update over packages that were never
    installed. Hashing the lists closes that. ``requirements.lock`` is what pip
    actually installs (see ``update_dependencies``); ``requirements.txt`` is
    the intent it was generated from, and a change there without a regenerated
    lock is still a reason to run the finishing pass rather than hide the gap.
    The commit SHA deliberately is *not* part of this -- on a development
    checkout every pull would then demand a pip run that changes nothing.

    Returns None when VERSION is unreadable, i.e. when nothing can be recorded.
    """
    version = read_version(project_dir)
    if version is None:
        return None
    return {
        "version": version,
        "requirements_sha256": _file_digest(project_dir / "requirements.txt"),
        "requirements_lock_sha256": _file_digest(project_dir / REQUIREMENTS_LOCK),
        "package_lock_sha256": _file_digest(project_dir / "frontend" / "package-lock.json"),
    }


def read_completion_marker(project_dir: Path) -> dict[str, Any] | None:
    """The recorded completion state, or None when there is no marker at all.

    A marker left by an older build (a bare VERSION string) or one whose
    contents are damaged reads back as an empty dict: the file exists, so this
    is not a pre-marker install, but nothing in it can match the current
    fingerprint -- so one finishing pass runs and rewrites it in this format.
    That pass is idempotent, so the cost of the format change is one extra
    ``--manual`` run, once.
    """
    try:
        text = marker_path(project_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        recorded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return recorded if isinstance(recorded, dict) else {}


def write_completion_marker(project_dir: Path) -> None:
    """Record what this checkout is fully updated to.

    Called only after every mutating phase has succeeded. Its counterpart,
    ``invalidate_completion_marker``, removes the marker as the mutating phases
    begin, so an update that dies part-way leaves no marker at all and the next
    start re-derives the state. Written through a temporary file and ``os.replace`` so a
    crash mid-write cannot leave a half-written marker that later reads as a
    mismatch of unknown origin.

    A write failure is not fatal and must not be reported as a failed update:
    the update itself already succeeded. The only consequence is that the next
    start re-derives the state and may run one unnecessary finishing pass.
    """
    fingerprint = completion_fingerprint(project_dir)
    if fingerprint is None:
        LOGGER.warning("VERSION is unreadable; update completion was not recorded")
        return
    target = marker_path(project_dir)
    # Same prefix as the marker so .gitignore's `.update_complete*` covers it:
    # a leftover temp file must never make the working tree dirty, because the
    # updater refuses to run on a dirty tree.
    temporary = target.with_name(f"{target.name}.tmp{os.getpid()}")
    try:
        payload = json.dumps(fingerprint, indent=2, sort_keys=True) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        LOGGER.warning(
            "Could not write %s. The update itself succeeded; the next start will most "
            "likely run an unnecessary finishing pass.",
            target,
            exc_info=True,
        )
        try:
            temporary.unlink()
        except OSError:
            pass


def invalidate_completion_marker(project_dir: Path) -> None:
    """Drop the marker before anything but the code itself is mutated.

    From this point until the final ``write_completion_marker`` the checkout is
    in an intermediate state, and a marker left over from the *previous* update
    would still describe a finished one. That matters when a phase fails and the
    rollback fails too: the code can end up back at the old revision -- matching
    the old marker's fingerprint exactly -- while the packages are half
    installed, and the next start would read READY and skip every check.

    Removing it first makes "died half-way => no marker" structural rather than
    a property of which phases happen to fail, so the next start always
    re-derives the state from what is actually installed.

    A removal failure is not fatal and must not abort an update that can still
    succeed; the next start then falls back to the fingerprint comparison.
    """
    target = marker_path(project_dir)
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError:
        LOGGER.warning(
            "Could not remove %s before updating packages. If this update fails, the next "
            "start may trust that stale marker.",
            target,
            exc_info=True,
        )


def _canonical_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


class RequiredDist(NamedTuple):
    """One distribution this environment must have, with its version bound."""

    name: str
    # packaging SpecifierSet, or None when the naive fallback parser produced
    # this entry and no version bound could be extracted.
    specifier: Any | None


class RequirementScan(NamedTuple):
    required: list[RequiredDist]
    # Lines that could not be turned into a requirement. They are *not* silently
    # dropped: an unchecked line means the answer "nothing is missing" is
    # incomplete, so the caller must not record a completed update from it.
    unparsed: list[str]
    # True when ``packaging`` was unavailable and names were parsed by hand.
    degraded: bool


_INCLUDE_LINE = re.compile(r"(-r|--requirement)[\s=]+(.+)$")

# pip options that only change where packages are fetched from or how they are
# built. They add no distribution of their own, so skipping them leaves the
# answer "nothing is missing" complete.
#
# Everything outside this set is reported as unparsed instead. ``-e`` /
# ``--editable`` and ``-c`` / ``--constraint`` are the ones that matter: an
# editable line installs a distribution that this scan would then never check,
# and a constraint file changes which versions count as satisfying. An unknown
# option is treated the same way, because whether it adds a distribution cannot
# be decided here -- and a wrong "nothing is missing" would be recorded as a
# completed update.
_IGNORABLE_PIP_OPTIONS = frozenset(
    {
        "--index-url",
        "-i",  # --index-url の短縮形 (2026-09-01 裁定: 長形と同義なので同格に扱う)
        "--extra-index-url",
        "--trusted-host",
        "--find-links",
        "-f",  # --find-links の短縮形
        "--no-binary",
        "--only-binary",
        "--prefer-binary",
        "--pre",
        "--hash",
        "--require-hashes",
    }
)


def _pip_option_name(line: str) -> str:
    """The option itself, without its value (``--index-url=x`` -> ``--index-url``)."""
    return re.split(r"[\s=]", line, maxsplit=1)[0]


def _naive_requirement_name(line: str) -> str | None:
    head = line.split(";", 1)[0].strip()
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", head)
    return match.group(0) if match else None


def scan_requirements(requirements: Path, *, depth: int = 1) -> RequirementScan | None:
    """Parse a requirements file into what this interpreter must actually have.

    Environment markers are evaluated here rather than skipped, so a line like
    ``pywin32; sys_platform == "win32"`` is checked on Windows and ignored
    elsewhere, and version bounds are carried through so an outdated pin is
    caught as well as an absent package. ``-r`` includes are followed one level,
    option lines known to add no distribution are skipped, and any other option
    line is reported as unparsed rather than ignored.

    Returns None when the file itself cannot be read.
    """
    try:
        raw_lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    required: list[RequiredDist] = []
    unparsed: list[str] = []
    degraded = _Requirement is None
    for raw in raw_lines:
        # pip treats `#` as a comment only at the start of a line or after
        # whitespace, so a URL fragment stays intact.
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            include = _INCLUDE_LINE.match(line)
            if include is None:
                if _pip_option_name(line) not in _IGNORABLE_PIP_OPTIONS:
                    # Not known to be harmless: the scan cannot claim to be
                    # complete, so this counts as a line that was not checked.
                    unparsed.append(line)
                continue
            nested = requirements.parent / include.group(2).strip().strip("\"'")
            nested_scan = scan_requirements(nested, depth=depth - 1) if depth > 0 else None
            if nested_scan is None:
                unparsed.append(line)
                continue
            required.extend(nested_scan.required)
            unparsed.extend(nested_scan.unparsed)
            degraded = degraded or nested_scan.degraded
            continue
        if _Requirement is None:
            if ";" in line:
                # A marker cannot be evaluated without packaging. Treating the
                # line as required would report Windows-only pins (pywin32,
                # colorama) as missing on macOS / Linux and send every start
                # through a finishing pass that can never satisfy them.
                unparsed.append(line)
                continue
            name = _naive_requirement_name(line)
            if name is None:
                unparsed.append(line)
            else:
                required.append(RequiredDist(name, None))
            continue
        try:
            requirement = _Requirement(line)
            needed_here = requirement.marker is None or requirement.marker.evaluate()
        except Exception:
            unparsed.append(line)
            continue
        if not needed_here:
            continue  # not required on this platform / interpreter
        required.append(RequiredDist(requirement.name, requirement.specifier))
    return RequirementScan(required, unparsed, degraded)


class DependencyReport(NamedTuple):
    # Absent, or present at a version the requirements file rules out.
    missing: list[str]
    # Requirement lines that could not be checked at all.
    unchecked: list[str]
    degraded: bool


def _installed_versions() -> dict[str, str | None] | None:
    try:
        from importlib import metadata

        installed: dict[str, str | None] = {}
        for dist in metadata.distributions():
            try:
                name = dist.metadata["Name"]
            except Exception:  # a broken dist-info must not hide the others
                continue
            if not name:
                continue
            try:
                version = dist.version
            except Exception:
                version = None
            installed[_canonical_package_name(name)] = version
    except Exception:
        LOGGER.warning("Could not inspect installed packages", exc_info=True)
        return None
    return installed or None


def missing_dependencies(project_dir: Path) -> DependencyReport | None:
    """Requirements this interpreter does not satisfy.

    Judged against ``requirements.lock`` -- the exact set an update installs --
    so a package present at a version other than the pinned one counts as
    missing, the same way an absent one does.

    Returns None when the answer cannot be determined at all (unreadable lock
    file, unreadable package metadata) so callers can tell "nothing missing"
    apart from "could not look".
    """
    scan = scan_requirements(project_dir / REQUIREMENTS_LOCK)
    if scan is None:
        return None
    installed = _installed_versions()
    if installed is None:
        return None
    missing: list[str] = []
    unchecked = list(scan.unparsed)
    for dist in scan.required:
        key = _canonical_package_name(dist.name)
        if key not in installed:  # absent from the environment entirely
            missing.append(dist.name)
            continue
        version = installed[key]
        if dist.specifier is None:
            continue  # name is present and there is no bound to check
        if version is None:
            # The dist-info exists but carries no version (damaged metadata):
            # the pin cannot be confirmed, so say "unchecked" instead of
            # letting a broken install pass as satisfied.
            unchecked.append(f"{dist.name} (installed version unreadable, required {dist.specifier})")
            continue
        try:
            satisfied = dist.specifier.contains(version, prereleases=True)
        except Exception:
            unchecked.append(f"{dist.name}{dist.specifier}")
            continue
        if not satisfied:
            missing.append(f"{dist.name} (installed {version}, required {dist.specifier})")
    return DependencyReport(missing, unchecked, scan.degraded)


def frontend_packages_installed(project_dir: Path) -> bool | None:
    """Whether ``npm ci`` has populated the frontend.

    Existence only. Verifying the tree against package-lock.json is npm's job
    and would cost more than the question is worth here; the failure this
    guards against is the interrupted update that never ran npm at all.

    Returns None when there is no frontend directory to judge.
    """
    frontend = project_dir / "frontend"
    if not frontend.is_dir():
        return None
    return (frontend / "node_modules").is_dir()


def check_update_complete(project_dir: Path) -> int:
    """Decide whether this checkout is safe to start.

    Returns one of CHECK_READY / CHECK_NEEDS_FINISH / CHECK_INCONCLUSIVE. When
    the checkout is verified complete the marker is (re)written, so the cheap
    comparison answers every later start.
    """
    fingerprint = completion_fingerprint(project_dir)
    if fingerprint is None:
        LOGGER.warning("VERSION is unreadable; cannot verify that the update finished")
        return CHECK_INCONCLUSIVE

    recorded = read_completion_marker(project_dir)
    if recorded is not None:
        if recorded == fingerprint:
            return CHECK_READY
        if recorded.get("version") == fingerprint["version"]:
            LOGGER.warning(
                "Update was interrupted: the code is still %s, but its dependency lists "
                "changed after the last completed update",
                fingerprint["version"],
            )
        else:
            LOGGER.warning(
                "Update was interrupted: this checkout was last completed at %s but the "
                "code is now %s",
                recorded.get("version") or "an unrecorded state",
                fingerprint["version"],
            )
        return CHECK_NEEDS_FINISH

    # No marker. Either this install predates the marker (v0.2.x, whose broken
    # update.bat stopped right after `git pull`) or someone removed it. Decide
    # on the things that actually break a start: packages the new code needs.
    frontend_ready = frontend_packages_installed(project_dir)
    if frontend_ready is False:
        LOGGER.warning("Update was interrupted: frontend/node_modules is not installed")
        return CHECK_NEEDS_FINISH

    report = missing_dependencies(project_dir)
    if report is None:
        LOGGER.warning("Could not verify installed packages; starting without recording a version")
        return CHECK_READY
    if report.missing:
        LOGGER.warning(
            "Update was interrupted: %d required package(s) are not installed as required (%s)",
            len(report.missing),
            ", ".join(report.missing[:5]),
        )
        return CHECK_NEEDS_FINISH
    if report.unchecked or report.degraded:
        # Nothing is known to be missing, but the check was not complete. Saying
        # "ready" here would write a marker that claims more than was verified.
        if report.unchecked:
            LOGGER.warning(
                "Could not check %d requirement line(s) (%s); starting without recording a version",
                len(report.unchecked),
                ", ".join(report.unchecked[:5]),
            )
        else:
            LOGGER.warning(
                "Requirements were parsed without the packaging library, so version bounds "
                "were not checked; starting without recording a version"
            )
        return CHECK_INCONCLUSIVE
    if frontend_ready is None:
        LOGGER.warning("No frontend directory to verify; starting without recording a version")
        return CHECK_INCONCLUSIVE
    write_completion_marker(project_dir)
    return CHECK_READY


def _run(
    command: list[str],
    *,
    cwd: Path,
    label: str,
    timeout: int = 900,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one phase. A non-zero exit raises ``UpdateError`` unless ``check`` is
    False, for commands whose non-zero exit is an answer rather than a failure
    (``pip check`` exits 1 when it finds conflicts)."""
    LOGGER.info("Phase: %s", label)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"{label} could not run: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise UpdateError(f"{label} failed with exit {result.returncode}: {detail}")
    return result


def _process_create_time(pid: int) -> float | None:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _process_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        if sys.platform == "win32":
            return _process_create_time(pid) is not None
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _identity_matches(pid: int, expected_created_at: float | None) -> bool:
    if expected_created_at is None:
        return False
    actual = _process_create_time(pid)
    return actual is not None and abs(actual - expected_created_at) <= 0.01


def wait_for_owned_process_exit(
    pid: int,
    expected_created_at: float | None,
    *,
    timeout: float = 30.0,
) -> None:
    """Wait for the recorded process; only terminate that verified identity."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.25)
    if not _identity_matches(pid, expected_created_at):
        raise UpdateError(
            f"PID {pid} did not exit and its identity cannot be verified; refusing to signal it"
        )
    LOGGER.warning("Verified main process PID %d did not exit; terminating it", pid)
    try:
        import psutil

        process = psutil.Process(pid)
        process.terminate()
        process.wait(timeout=10)
    except Exception as exc:
        raise UpdateError(f"Verified main process PID {pid} could not be stopped: {exc}") from exc


def _ensure_portable_git_on_path(project_dir: Path) -> None:
    """Make a setup-installed PortableGit visible to this (separate) session.

    ``setup.bat`` installs PortableGit into ``.git-portable/`` and prepends
    ``.git-portable\\cmd`` to PATH within its own session. ``update.bat`` runs in
    a *later* session that does not inherit that PATH, so a user who has only
    PortableGit (no system / winget Git) would otherwise fail the git readiness
    check below. Prepend the portable ``cmd`` dir so ``shutil.which('git')`` and
    the ``git`` subprocess calls resolve. No-op when the binary is absent
    (non-Windows setups never create it) or the dir is already on PATH.
    See docs/issues/git_required_for_zip_install.md.
    """
    portable_cmd = project_dir / ".git-portable" / "cmd"
    if not (portable_cmd / "git.exe").exists():
        return
    portable_str = str(portable_cmd)
    current = os.environ.get("PATH", "")
    if portable_str in current.split(os.pathsep):
        return
    os.environ["PATH"] = portable_str + os.pathsep + current
    LOGGER.info("Using setup-installed PortableGit at %s", portable_cmd)


def assert_git_update_ready(project_dir: Path) -> str:
    if not (project_dir / ".git").is_dir() or shutil.which("git") is None:
        raise UpdateError(
            "Automatic update requires a Git checkout. The former ZIP overlay path is "
            "disabled because it cannot safely remove retired files without deleting "
            "unknown user files."
        )
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_dir,
        label="verify clean working tree",
        timeout=60,
    ).stdout
    if status.strip():
        raise UpdateError(
            "Working tree has local changes. Update was not started; commit or otherwise "
            "resolve them explicitly. The updater never stashes or resets user work."
        )
    return _run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        label="record current revision",
        timeout=60,
    ).stdout.strip()


def _saiverse_home() -> Path:
    """SAIVerse ホームディレクトリ。``scripts/snapshot.py`` の ``saiverse_home()``
    と同じ規則。

    update_engine は動作中に作業ツリーが入れ替わりうるので他モジュールを import
    しない方針（モジュール冒頭の注記）。そのため規則を二重に持つが、snapshot.py
    側を変えたらここも必ず揃えること。
    """
    env = os.environ.get("SAIVERSE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".saiverse"


def _remove_partial_snapshot_archive(tmp_archive: Path) -> None:
    """中断されたスナップショットの書きかけファイルを消す。

    掃除の失敗で本来のエラーを隠さないよう、例外は握り潰してログに残すだけに
    する。
    """
    try:
        tmp_archive.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        LOGGER.warning("Could not remove partial snapshot archive %s: %s", tmp_archive, exc)
        return
    LOGGER.warning("Removed the partial snapshot archive left behind: %s", tmp_archive)


# pre-update スナップショットに許す時間。他フェーズ（pip / npm の既定 900 秒、
# git pull の 300 秒）より長いのは、この処理だけが世界の大きさに比例して伸びる
# ため。2026-09-02、実測 24GB の世界が 900 秒に収まらず start.bat が起動不能に
# なった。llama_cache を除外して対象は 8.9GB に落ちたが、世界は今後も育つので
# 余裕を取る。
#
# ⚠ これは暫定値であって解ではない。固定値である限り、世界が育てばいつか再び
# 追い越される。恒久策（進捗を見て「生きている限り待つ」形へ）は
# docs/issues/snapshot_timeout_is_fixed_while_world_grows.md。
SNAPSHOT_TIMEOUT_SECONDS = 3600


def create_pre_update_snapshot(project_dir: Path, python: str) -> str:
    name = datetime.now(timezone.utc).strftime("auto_before_update_%Y%m%d_%H%M%S_%f")
    # snapshot.py は書き上がった ZIP を .zip.tmp から os.replace で publish する。
    # ここでタイムアウトすると _run が子プロセスを kill するので snapshot.py 側の
    # except 節は走らず、書きかけの .zip.tmp が数十 GB のまま残る。子を殺した
    # 側であるこちらが後始末する。パスの規則は snapshot.py と同じ。
    tmp_archive = _saiverse_home() / "snapshots" / f"{name}.zip.tmp"
    try:
        _run(
            [
                python,
                str(project_dir / "scripts" / "snapshot.py"),
                "save",
                name,
                "--note",
                "Automatic restore point before code update",
            ],
            cwd=project_dir,
            label="create and validate pre-update world snapshot",
            timeout=SNAPSHOT_TIMEOUT_SECONDS,
        )
    except Exception:
        _remove_partial_snapshot_archive(tmp_archive)
        raise
    return name


def update_code(project_dir: Path) -> None:
    _run(
        ["git", "pull", "--ff-only"],
        cwd=project_dir,
        label="fast-forward code update",
        timeout=300,
    )


_PIP_CHECK_CLEAN = "No broken requirements found."


def _report_addon_conflicts(project_dir: Path, python: str) -> None:
    """Make it visible, in this update's log, when the lock broke a package the
    lock does not own.

    The venv is shared with addons (voice-tts brings numba, torch, ...). ``pip
    install -r requirements.lock`` moves the core's packages and exits 0 even
    when an addon's package now has an unsatisfiable requirement (2026-09-02:
    numpy moved past what numba allowed, and voice-tts broke a day later with
    nothing in the update log). Addon consistency is the addon's responsibility
    (docs/intent/dependency_management.md §3-3), so this neither fails the
    update nor rolls back -- it only logs each conflict ``pip check`` reports.
    """
    try:
        result = _run(
            [python, "-m", "pip", "check"],
            cwd=project_dir,
            label="check installed packages for conflicts",
            timeout=300,
            check=False,
        )
    except UpdateError as exc:
        LOGGER.warning("[deps] pip check could not run; addon conflicts were not checked: %s", exc)
        return
    if result.returncode == 0:
        return
    conflicts = [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip() and line.strip() != _PIP_CHECK_CLEAN
    ]
    if not conflicts:
        conflicts = [(result.stderr or "").strip()[-2000:] or f"exit {result.returncode} with no output"]
    for line in conflicts:
        LOGGER.warning("[deps] pip check: %s", line)
    LOGGER.warning(
        "[deps] 上の %d 件は requirements.lock の外にあるパッケージ (アドオンか手で入れたもの) との衝突です。"
        "本体の更新自体は完了しています。該当のアドオンは入れ直す (アドオンを入れ直す) 必要があるかもしれません。",
        len(conflicts),
    )


def update_dependencies(project_dir: Path, python: str) -> None:
    _run(
        [python, "-m", "pip", "install", "-r", REQUIREMENTS_LOCK],
        cwd=project_dir,
        label="install Python dependencies",
    )
    _report_addon_conflicts(project_dir, python)
    frontend = project_dir / "frontend"
    if not frontend.is_dir():
        raise UpdateError("frontend directory is missing after code update")
    npm = shutil.which("npm")
    portable_npm = project_dir / ".node" / ("npm.cmd" if sys.platform == "win32" else "npm")
    if npm is None and portable_npm.is_file():
        npm = str(portable_npm)
    if npm is None:
        raise UpdateError("npm is required to update the frontend")
    npm_command = "ci" if (frontend / "package-lock.json").is_file() else "install"
    _run(
        [npm, npm_command],
        cwd=frontend,
        label=f"npm {npm_command}",
    )


def _rollback_code_and_dependencies(
    project_dir: Path,
    python: str,
    old_revision: str,
) -> None:
    """Best-effort repair used only after the initial clean-tree invariant."""
    LOGGER.error("Rolling code back to %s", old_revision)
    _run(
        ["git", "reset", "--hard", old_revision],
        cwd=project_dir,
        label="rollback code revision",
        timeout=120,
    )
    try:
        update_dependencies(project_dir, python)
    except UpdateError:
        LOGGER.exception("Dependency repair for the previous revision also failed")


def restart_application(config: dict[str, Any]) -> subprocess.Popen[Any]:
    project_dir = Path(config["project_dir"]).resolve()
    python = str(config["venv_python"])
    main_args = config.get("main_args")
    if not isinstance(main_args, list) or not all(isinstance(item, str) for item in main_args):
        raise UpdateError("Update config is missing the exact main arguments")
    command = [python, str(project_dir / "main.py"), *main_args]
    LOGGER.info("Restarting the same City process with %d preserved arguments", len(main_args))
    kwargs: dict[str, Any] = {
        "cwd": str(project_dir),
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise UpdateError(f"Could not restart SAIVerse: {exc}") from exc


def _health_url(config: dict[str, Any]) -> str:
    host = str(config.get("listen_host") or "127.0.0.1")
    if host in {"0.0.0.0", "::", "localhost"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(config['backend_port'])}/api/system/version"


def wait_for_healthy_restart(
    process: subprocess.Popen[Any],
    config: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    url = _health_url(config)
    deadline = time.monotonic() + timeout
    last_error = "health endpoint not reached"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise UpdateError(f"Restarted process exited with code {process.returncode}")
        headers = {"User-Agent": "SAIVerse-Updater"}
        owner_token = os.getenv("SAIVERSE_OWNER_TOKEN")
        if owner_token:
            headers["Authorization"] = f"Bearer {owner_token}"
        try:
            with urlopen(Request(url, headers=headers), timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("city_name") != config.get("city_name"):
                raise UpdateError(
                    "Restarted backend reports the wrong City: "
                    f"{payload.get('city_name')!r}"
                )
            if payload.get("db_identity") != config.get("db_identity"):
                raise UpdateError("Restarted backend reports a different database identity")
            return payload
        except UpdateError:
            raise
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(1)
    raise UpdateError(f"Restart health check timed out: {last_error}")


def _terminate_spawned(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:
        LOGGER.exception("Could not terminate failed restarted process PID %s", process.pid)


def run_update(config: dict[str, Any] | None, project_dir: Path) -> None:
    python = str(config.get("venv_python", sys.executable)) if config else sys.executable
    _ensure_portable_git_on_path(project_dir)
    old_revision = assert_git_update_ready(project_dir)

    if config:
        wait_for_owned_process_exit(
            int(config["main_pid"]),
            config.get("main_process_created_at"),
        )
    snapshot_name = create_pre_update_snapshot(project_dir, python)
    LOGGER.info("Pre-update restore point: %s", snapshot_name)

    code_changed = False
    try:
        update_code(project_dir)
        code_changed = True
        # The code moved, so whatever the marker recorded no longer holds. Drop
        # it here -- before the first phase that can leave packages half
        # installed -- so no failure path can end with a stale marker claiming
        # this checkout is finished. Both the manual and the detached run reach
        # the final write below through this same block.
        invalidate_completion_marker(project_dir)
        update_dependencies(project_dir, python)
    except UpdateError:
        if code_changed:
            _rollback_code_and_dependencies(project_dir, python, old_revision)
        raise

    if not config:
        write_completion_marker(project_dir)
        LOGGER.info("Update applied. Start SAIVerse normally to run startup migrations.")
        return

    process: subprocess.Popen[Any] | None = None
    try:
        process = restart_application(config)
        payload = wait_for_healthy_restart(process, config)
        write_completion_marker(project_dir)
        LOGGER.info(
            "Update complete: City=%s version=%s PID=%s",
            payload.get("city_name"),
            payload.get("version"),
            process.pid,
        )
    except UpdateError:
        if process is not None:
            _terminate_spawned(process)
        _rollback_code_and_dependencies(project_dir, python, old_revision)
        rollback_process = restart_application(config)
        try:
            wait_for_healthy_restart(rollback_process, config)
            LOGGER.error("Previous revision was restored and restarted successfully")
        except UpdateError:
            LOGGER.exception("Previous revision also failed to restart")
        raise


def _load_config(config_path: Path) -> dict[str, Any]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Update config is unreadable: {exc}") from exc
    project_dir = Path(config.get("project_dir", "")).resolve()
    expected = Path(__file__).resolve().parent.parent
    if project_dir != expected:
        raise UpdateError(f"Update config project mismatch: {project_dir} != {expected}")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical SAIVerse updater")
    parser.add_argument("--manual", action="store_true", help="Update while SAIVerse is stopped")
    parser.add_argument("--config", type=Path, help="Detached updater config path")
    parser.add_argument(
        "--check-complete",
        action="store_true",
        help=(
            "Report whether the last update finished, without changing code or packages. "
            f"Exit {CHECK_READY}: ready to start. Exit {CHECK_NEEDS_FINISH}: run --manual first. "
            f"Exit {CHECK_INCONCLUSIVE}: could not tell, start anyway."
        ),
    )
    args = parser.parse_args(argv)

    project_dir = Path(__file__).resolve().parent.parent
    if args.check_complete:
        setup_logging(project_dir, to_file=False)
        return check_update_complete(project_dir)

    setup_logging(project_dir)
    config_path = args.config or project_dir / ".update_config.json"
    try:
        config = None if args.manual else _load_config(config_path)
        run_update(config, project_dir)
    except UpdateError as exc:
        LOGGER.error("Update aborted: %s", exc)
        return 1

    if not args.manual:
        try:
            config_path.unlink()
        except OSError:
            LOGGER.warning("Update succeeded but config cleanup failed", exc_info=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
