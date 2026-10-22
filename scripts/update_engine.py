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

try:
    from packaging.version import InvalidVersion as _InvalidVersion
    from packaging.version import Version as _Version
except Exception:  # pragma: no cover - packaging ships with pip
    _Version = None  # type: ignore[assignment]
    _InvalidVersion = ValueError  # type: ignore[assignment,misc]

LOGGER = logging.getLogger("saiverse.update")

# Written next to the other self-update state files (``.update_config.json``,
# ``self_update.log``) and gitignored with them. It records what the checkout
# was *fully* updated to -- code, Python packages and frontend packages
# together -- so a launcher can tell a finished update from one that died
# half-way. See docs/issues/v0229_update_bat_truncates_after_git_pull.md.
UPDATE_COMPLETE_MARKER = ".update_complete"

# Written by the ``--install-dependencies`` child once ``update_dependencies``
# of the engine on disk has finished, holding the ``completion_fingerprint`` of
# the code it installed for. Kept apart from ``UPDATE_COMPLETE_MARKER`` because
# that marker is rewritten at the end of every update by the *parent* updater,
# which can be an engine from before this record existed and would drop any
# field this engine added to it. ``check_update_complete`` reads the pair: a
# marker that matches the code without this record means an older updater
# finished the update with its own, older dependency step
# (docs/intent/update_completes_with_new_code.md section 3).
DEPENDENCIES_RECORD = ".update_dependencies_complete"

# Written when the dependency step did not fully finish: by ``--check-complete``
# when installing the packages once more (``reinstall_dependencies_once``)
# failed, and by the ``--install-dependencies`` child when a step that does not
# stop the update (``DEPENDENCY_STEP_PORTABLE_NODE`` / ``DEPENDENCY_STEP_SEARXNG``)
# failed. The backend reads it at startup and shows it in the alert banner
# (``saiverse/update_dependencies_alert.py``); the next fully successful
# ``--install-dependencies`` run removes it.
DEPENDENCIES_FAILURE_RECORD = ".update_dependencies_failed.json"

# Steps of ``update_dependencies`` whose failure does not stop the update. They
# are reported back as ``DependencyStepFailure`` and written to
# ``DEPENDENCIES_FAILURE_RECORD`` under ``"steps"``; the alert names them.
DEPENDENCY_STEP_PORTABLE_NODE = "portable_node"
DEPENDENCY_STEP_SEARXNG = "searxng"


class DependencyStepFailure(NamedTuple):
    """A step of ``update_dependencies`` that failed without stopping the update.

    ``retry`` False: running the step again cannot help (a file the checkout
    itself lacks), so the dependency record is still written -- the next start
    does not reinstall every package again -- but the failure stays on screen
    until a later run of the child finishes without failures.
    """

    step: str
    error: str
    retry: bool = True

# The pinned, verified package set. Every install path (setup.bat / setup.sh,
# this engine, addon installs as constraints) reads this file, never
# requirements.txt, so all users get the same combination. It is universal:
# lines carry environment markers (``pywin32==311 ; sys_platform == 'win32'``)
# and ``scan_requirements`` evaluates them for the running interpreter.
# See docs/intent/dependency_management.md.
REQUIREMENTS_LOCK = "requirements.lock"

# The pre-lock install target. This constant exists *only* so that a rollback
# to a revision older than the lock (v0.3.3 and earlier have no
# requirements.lock) can still reinstall that revision's packages. It is never
# the target of a normal update -- ``update_dependencies`` defaults to the lock
# and does not fall back to this file when the lock is missing; the rollback
# path is the single caller that picks it, explicitly, after ``git reset``.
LEGACY_REQUIREMENTS = "requirements.txt"

# Core packages that left requirements.lock and are removed from the venv on
# every update. ``pip install -r`` never removes a package the lock no longer
# lists, so a package dropped from the lock stays in every user's .venv.
# fastembed left the lock with the EmbeddingGemma 2 migration on 2026-10-12
# (docs/intent/embedding_gemma2_migration.md); after the Pillow 13 update
# (2026-10-22) ``pip check`` reports its Pillow<12 requirement as a conflict.
# A name here must not appear in requirements.lock (a test holds that), or the
# update would install it and remove it again.
RETIRED_CORE_PACKAGES: tuple[str, ...] = ("fastembed",)

# Exit codes of ``--check-complete``. The launchers branch on these, so they are
# part of the contract with start.bat / start.sh.
CHECK_READY = 0
CHECK_NEEDS_FINISH = 10
CHECK_INCONCLUSIVE = 11
# The packages had to be installed once more and that failed. The launchers
# start SAIVerse anyway; the failure is in ``DEPENDENCIES_FAILURE_RECORD``.
CHECK_DEPENDENCIES_FAILED = 12

# Release channels (docs/intent/early_access_release.md §3-1). The branch the
# checkout is on is the *only* place that decides which releases it receives
# (invariant 4) -- there is no separate setting that could drift from it. Any
# other branch (a developer's ``develop``, a detached HEAD) counts as stable
# for update notices, and cannot be switched from.
CHANNEL_STABLE = "stable"
CHANNEL_EARLY_ACCESS = "early_access"
CHANNEL_BRANCHES = {
    CHANNEL_STABLE: "main",
    CHANNEL_EARLY_ACCESS: "early-access",
}
# The remote every install tracks: ``git clone`` names it origin, and
# setup.bat / setup.sh add it under that name for ZIP installs.
UPDATE_REMOTE = "origin"
# Snapshot name prefix per switch direction, so a restore point taken before
# joining (or leaving) early access is told apart from a routine
# ``auto_before_update_*`` one.
SWITCH_SNAPSHOT_PREFIX = {
    CHANNEL_EARLY_ACCESS: "ea_optin",
    CHANNEL_STABLE: "ea_return",
}


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

    A marker left by an older build (a bare VERSION string), one whose contents
    are damaged, or one holding a JSON document that is not an object reads
    back as an empty dict: a marker file exists but says nothing usable. The
    caller (``check_update_complete``) must not read that as "a record that does
    not match" -- it verifies the checkout directly, exactly as it does when
    there is no marker, and rewrites the marker in this format once the
    checkout is verified complete. Treating it as a mismatch used to send a
    fully updated checkout into the full update, which a large world could not
    finish (docs/issues/archive/update_marker_format_change_demands_full_update.md).
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
    try:
        _write_json_atomically(target, fingerprint)
    except OSError:
        LOGGER.warning(
            "Could not write %s. The update itself succeeded; the next start will most "
            "likely run an unnecessary finishing pass.",
            target,
            exc_info=True,
        )


def _write_json_atomically(target: Path, document: Any) -> None:
    """Write ``document`` to ``target`` through a temporary file and ``os.replace``.

    The temporary file starts with the target's name, so the .gitignore entry
    for the target covers it too: a leftover temp file must never make the
    working tree dirty, because the updater refuses to run on a dirty tree. On
    failure the temporary file is removed and the ``OSError`` is raised.
    """
    temporary = target.with_name(f"{target.name}.tmp{os.getpid()}")
    try:
        payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _remove_file(target: Path, *, purpose: str) -> None:
    """Remove ``target`` if it exists; a failure is logged, never raised."""
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError:
        LOGGER.warning("Could not remove %s (%s)", target, purpose, exc_info=True)


def _read_json_object(target: Path) -> dict[str, Any] | None:
    """The JSON object in ``target``; None when it is absent, {} when it is
    present but empty, damaged, or not an object."""
    try:
        text = target.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    if not text:
        return {}
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return document if isinstance(document, dict) else {}


def dependencies_record_path(project_dir: Path) -> Path:
    return project_dir / DEPENDENCIES_RECORD


def read_dependencies_record(project_dir: Path) -> dict[str, Any] | None:
    """The fingerprint ``DEPENDENCIES_RECORD`` holds, or None when there is no
    usable record (absent, damaged, or not an object)."""
    recorded = _read_json_object(dependencies_record_path(project_dir))
    return recorded or None


def write_dependencies_record(project_dir: Path) -> None:
    """Record that ``update_dependencies`` of this engine finished for the code
    on disk. Never raises: a write failure only means the next start installs
    the packages once more."""
    fingerprint = completion_fingerprint(project_dir)
    if fingerprint is None:
        LOGGER.warning("VERSION is unreadable; the finished dependency step was not recorded")
        return
    target = dependencies_record_path(project_dir)
    try:
        _write_json_atomically(target, fingerprint)
    except OSError:
        LOGGER.warning(
            "Could not write %s; the next start will install the packages once more.",
            target,
            exc_info=True,
        )


def invalidate_dependencies_record(project_dir: Path) -> None:
    """Drop ``DEPENDENCIES_RECORD`` when the code moves, for the same reason as
    ``invalidate_completion_marker``: until the dependency step of the new code
    has finished, the record would describe something that no longer holds."""
    _remove_file(dependencies_record_path(project_dir), purpose="before updating packages")


def dependencies_failure_path(project_dir: Path) -> Path:
    return project_dir / DEPENDENCIES_FAILURE_RECORD


def read_dependencies_failure(project_dir: Path) -> dict[str, Any] | None:
    """What the last unfinished dependency step recorded; None when nothing
    failed. A file that is present but unreadable reads as {} -- something
    failed, without details."""
    return _read_json_object(dependencies_failure_path(project_dir))


def write_dependencies_failure(
    project_dir: Path, error: str, *, steps: list[str] | None = None
) -> None:
    """Record an unfinished dependency step for the backend to show. Never raises.

    ``steps`` names the steps that failed without stopping the update
    (``DEPENDENCY_STEP_*``). It is left out when the dependency step as a
    whole failed (``reinstall_dependencies_once``), where ``error`` says what
    stopped it.
    """
    document: dict[str, Any] = {
        "version": read_version(project_dir),
        "error": error.strip()[-2000:],
        "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if steps:
        document["steps"] = list(steps)
    target = dependencies_failure_path(project_dir)
    try:
        _write_json_atomically(target, document)
    except OSError:
        LOGGER.error(
            "Could not write %s, so the SAIVerse screen will not show this failure",
            target,
            exc_info=True,
        )


def clear_dependencies_failure(project_dir: Path) -> None:
    _remove_file(dependencies_failure_path(project_dir), purpose="a resolved package install failure")


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


def missing_frontend_packages(project_dir: Path) -> list[str] | None:
    """Packages ``frontend/package.json`` declares that ``node_modules`` lacks.

    Every name in ``dependencies`` (scoped names included) must have its own
    ``node_modules/<name>/package.json``. ``devDependencies`` are left out on
    purpose: ``npm ci`` skips them under ``NODE_ENV=production``, and counting
    them would then send every start into the finishing pass forever. A
    half-deleted ``node_modules`` loses the ``dependencies`` too, so they are
    enough to catch what this guards against. Checking
    only that the ``node_modules`` folder exists is not enough: a ``npm ci``
    that fails half-way on Windows (a file held open by a running frontend
    server) leaves the folder behind with almost nothing in it, and that
    folder used to be read as a finished install
    (docs/issues/archive/ui_update_fails_while_frontend_runs.md). Versions are not
    compared -- that is npm's job and ``package-lock.json`` is already part of
    the completion fingerprint; what this guards against is a package that is
    simply not there.

    Returns None when the answer cannot be determined: no frontend directory,
    or a ``package.json`` that cannot be read or does not have the expected
    shape.
    """
    frontend = project_dir / "frontend"
    if not frontend.is_dir():
        return None
    try:
        manifest = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    names: list[str] = []
    declared = manifest.get("dependencies")
    if declared is not None:
        if not isinstance(declared, dict):
            return None
        names.extend(name for name in declared if isinstance(name, str) and name)
    node_modules = frontend / "node_modules"
    missing: list[str] = []
    for name in names:
        if not (node_modules / name / "package.json").is_file():
            missing.append(name)
    return missing


def frontend_packages_installed(project_dir: Path) -> bool | None:
    """Whether every package the frontend declares is present in node_modules.

    Returns None when that cannot be determined (see
    ``missing_frontend_packages``).
    """
    missing = missing_frontend_packages(project_dir)
    if missing is None:
        return None
    return not missing


def _log_missing_frontend_packages(missing: list[str]) -> None:
    LOGGER.warning(
        "Update was interrupted: %d frontend package(s) are missing from frontend/node_modules (%s)",
        len(missing),
        ", ".join(missing[:5]),
    )


def check_update_complete(project_dir: Path) -> int:
    """Decide whether this checkout is safe to start.

    Returns one of CHECK_READY / CHECK_NEEDS_FINISH / CHECK_INCONCLUSIVE /
    CHECK_DEPENDENCIES_FAILED. When the checkout is verified complete the
    marker is (re)written, so the cheap comparison answers every later start.

    A marker that matches the code while ``DEPENDENCIES_RECORD`` does not
    means an older updater finished the update with its own dependency step,
    so a step added since (upgrading pip, removing a retired package, the
    portable Node.js, SearXNG) never ran -- or that this engine's dependency
    step ran but the portable Node.js or SearXNG step failed, which leaves the
    record unwritten on purpose. Either case installs the packages once with
    this engine (``reinstall_dependencies_once``) instead of starting the full
    update, which would fetch a newer release
    (docs/intent/update_completes_with_new_code.md section 2).
    """
    fingerprint = completion_fingerprint(project_dir)
    if fingerprint is None:
        LOGGER.warning("VERSION is unreadable; cannot verify that the update finished")
        return CHECK_INCONCLUSIVE

    recorded = read_completion_marker(project_dir)
    if recorded == {}:
        # A marker file exists but is in the pre-JSON format (bare VERSION
        # string) or is damaged. It records nothing to compare against, so it
        # is judged exactly like a missing marker below: on what is installed.
        LOGGER.info(
            "Completion marker is in an old or unreadable format; verifying the "
            "installed state directly"
        )
    if recorded:
        if recorded == fingerprint:
            # The marker says the last update finished, but a marker cannot see
            # packages that were removed afterwards -- a failed button update
            # whose ``npm ci`` emptied node_modules and whose rollback failed
            # too, for instance. The package check is cheap, so it runs even
            # here. None (cannot tell) keeps trusting the marker.
            missing_frontend = missing_frontend_packages(project_dir)
            if missing_frontend:
                _log_missing_frontend_packages(missing_frontend)
                return CHECK_NEEDS_FINISH
            if read_dependencies_record(project_dir) != fingerprint:
                return reinstall_dependencies_once(project_dir)
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

    # No usable marker. Either this install predates the marker (v0.2.x, whose
    # broken update.bat stopped right after `git pull`), someone removed it, or
    # the file is in the old / unreadable format handled above. Decide on the
    # things that actually break a start: packages the new code needs. When
    # that verification passes, the marker is written in the current format.
    missing_frontend = missing_frontend_packages(project_dir)
    frontend_ready = None if missing_frontend is None else not missing_frontend
    if missing_frontend:
        _log_missing_frontend_packages(missing_frontend)
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
        LOGGER.warning(
            "The frontend packages could not be verified (no frontend directory, or an "
            "unreadable frontend/package.json); starting without recording a version"
        )
        return CHECK_INCONCLUSIVE
    write_completion_marker(project_dir)
    # No marker means no update ran here that could have skipped a step (a new
    # install, or one from before the marker); what is installed was just
    # verified, so the packages are not installed again -- only recorded.
    if read_dependencies_record(project_dir) != fingerprint:
        write_dependencies_record(project_dir)
    return CHECK_READY


def _saiverse_may_be_running(project_dir: Path) -> str | None:
    """Why the reinstall before start must wait, or None when SAIVerse is stopped.

    The frontend check alone does not cover a backend left running (the
    frontend window closed, or ``python main.py`` started by hand); pip would
    then replace packages the backend has loaded. ``saiverse.runtime_marker``
    is the record a running backend keeps; not being able to read it counts as
    running, so the reinstall waits for the next start instead of guessing.
    """
    root = str(project_dir)
    added = root not in sys.path
    try:
        if added:
            sys.path.insert(0, root)
        try:
            from saiverse.runtime_marker import marker_status
        finally:
            if added:
                sys.path.remove(root)
        state, reason = marker_status()
    except Exception as exc:  # cannot tell -> treat as running
        return f"whether SAIVerse is running could not be checked ({exc})"
    if state == "stopped":
        return None
    return f"SAIVerse may be running ({reason})"


def reinstall_dependencies_once(project_dir: Path) -> int:
    """Run ``update_dependencies`` of this engine once, for the code as it is.

    No code is fetched and VERSION does not move: only the
    ``INSTALL_DEPENDENCIES_OPTION`` child runs, the same step an update runs
    after its code phase. The child writes ``DEPENDENCIES_RECORD`` and removes
    ``DEPENDENCIES_FAILURE_RECORD`` when it succeeds, so the next start does
    not run this again.

    A failure does not stop the start: it is written to
    ``DEPENDENCIES_FAILURE_RECORD`` for the backend to show, the record stays
    missing, and the next start tries again. That includes a child that exits
    0 after the portable Node.js or SearXNG step failed: it wrote the failure
    record itself and no ``DEPENDENCIES_RECORD``. Returns CHECK_READY or
    CHECK_DEPENDENCIES_FAILED.
    """
    LOGGER.warning(
        "The package installation of version %s has not finished: an older updater "
        "skipped part of it, or a step of it failed last time. Installing the packages "
        "once more with this version's procedure; SAIVerse itself is not updated. This "
        "can take several minutes.",
        read_version(project_dir) or "(unknown)",
    )
    running = _saiverse_may_be_running(project_dir)
    if running is not None:
        LOGGER.error("Installing the packages once more is postponed: %s", running)
        write_dependencies_failure(
            project_dir,
            f"{running}. The packages were not installed once more, so nothing running is "
            "changed under it. Close every SAIVerse window, then run start.bat (start.sh on "
            "macOS / Linux) again.\nSAIVerse が動いている (か確かめられない) ため、パッケージの"
            "入れ直しを次の起動へ延ばしました。SAIVerse の窓をすべて閉じてから、start.bat "
            "(Mac / Linux は start.sh) をもう一度実行してください。",
        )
        return CHECK_DEPENDENCIES_FAILED
    try:
        # npm ci beside a running frontend would empty node_modules on Windows.
        refuse_while_frontend_runs(project_dir)
        _run_dependencies_from_disk(project_dir, sys.executable)
    except UpdateError as exc:
        LOGGER.error(
            "Installing the packages once more failed; SAIVerse starts anyway and the "
            "screen shows the failure: %s",
            exc,
        )
        write_dependencies_failure(project_dir, str(exc))
        return CHECK_DEPENDENCIES_FAILED
    failure = read_dependencies_failure(project_dir)
    if failure is not None:
        # The child finished but a step that does not stop it failed; it wrote
        # the failure record and left DEPENDENCIES_RECORD unwritten, so the next
        # start tries again.
        LOGGER.error(
            "Installing the packages once more did not fully finish; SAIVerse starts anyway "
            "and the screen shows the failure: %s",
            failure.get("error") or "(no details recorded)",
        )
        return CHECK_DEPENDENCIES_FAILED
    LOGGER.info("The packages of version %s are installed", read_version(project_dir) or "(unknown)")
    return CHECK_READY


def _run(
    command: list[str],
    *,
    cwd: Path,
    label: str,
    timeout: int = 900,
    check: bool = True,
    encoding: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one phase. A non-zero exit raises ``UpdateError`` unless ``check`` is
    False, for commands whose non-zero exit is an answer rather than a failure
    (``pip check`` exits 1 when it finds conflicts). ``encoding`` overrides the
    locale default for commands whose output is known to be UTF-8 regardless of
    the console code page (git with ``-z``); undecodable bytes are replaced
    rather than crashing the phase. ``env`` replaces the inherited environment
    (None inherits it)."""
    LOGGER.info("Phase: %s", label)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding=encoding,
            errors="replace" if encoding else None,
            timeout=timeout,
            env=env,
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
    """Whether ``pid`` is still running, via psutil only.

    Fail closed: without psutil "cannot check" is indistinguishable from
    "already exited", and guessing "exited" once let the updater run beside a
    live backend (docs/issues/archive/self_update_unsafe_without_psutil.md), so the
    honest answer is to abort the update instead of guessing.
    """
    try:
        import psutil
    except Exception as exc:
        # ImportError に限定しない: 壊れた拡張モジュールは import 自体が
        # OSError 等で失敗しうる。main() は UpdateError しか捕まえないので、
        # ここで変換しないと更新プロセスが記録なしの未処理例外で終わる。
        raise UpdateError(
            "psutil is unavailable or broken, so the main process cannot be"
            " confirmed to have exited; aborting the update instead of guessing"
        ) from exc
    try:
        return bool(psutil.pid_exists(pid))
    except Exception as exc:
        # 壊れたインストールや ABI 不整合で probe 自体が失敗することがある。
        # main() は UpdateError しか捕まえないので、素通しすると DETACHED
        # プロセスでは痕跡の無い未処理例外になる — 判定できないなら中止する。
        raise UpdateError(
            f"psutil could not determine whether PID {pid} is still running"
            f" ({exc}); aborting the update instead of guessing"
        ) from exc


def _identity_matches(pid: int, expected_created_at: float | None) -> bool:
    if expected_created_at is None:
        return False
    actual = _process_create_time(pid)
    return actual is not None and abs(actual - expected_created_at) <= 0.01


def wait_for_owned_process_exit(
    pid: int,
    expected_created_at: float | None,
    *,
    timeout: float = 120.0,
) -> None:
    """Wait for the recorded process; only terminate that verified identity.

    ``_process_alive`` raising (psutil missing) propagates as-is — fail closed
    rather than proceeding beside a possibly-live backend. The timeout is long
    because a large world's shutdown can exceed 30 seconds and overrunning it
    ends in terminating a backend that is still writing its records.
    """
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


LOCAL_CHANGES_LIST_LIMIT = 20


def _parse_porcelain_z(status: str) -> list[str]:
    """Paths from ``git status --porcelain -z`` output, one entry per change.

    ``-z`` is used instead of the line format because the line format quotes
    any non-ASCII path as octal escapes (``"\\350\\250\\255..."``) unless
    ``core.quotePath`` is off, which would make a Japanese file name unreadable
    in the message meant to tell the user which file to deal with. With ``-z``
    entries are ``XY <path>`` separated by NUL, and a rename / copy (``R`` or
    ``C`` in either column: staged in ``X``, detected in the work tree in
    ``Y``) is followed by one more NUL-terminated field holding the original
    path; that pair is rendered as ``old -> new``.
    """
    fields = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if ("R" in code or "C" in code) and index < len(fields):
            original = fields[index]
            index += 1
            path = f"{original} -> {path}"
        paths.append(path)
    return paths


def _format_local_changes(status: str) -> str:
    """Turn ``git status --porcelain -z`` output into a readable path list.

    At most ``LOCAL_CHANGES_LIST_LIMIT`` paths are listed so a huge diff does
    not flood the UI toast / terminal; the remainder is summarised as a count.
    """
    paths = _parse_porcelain_z(status)
    shown = paths[:LOCAL_CHANGES_LIST_LIMIT]
    text = "\n".join(f"  {p}" for p in shown)
    hidden = len(paths) - len(shown)
    if hidden > 0:
        text += f"\n  ... and {hidden} more"
    return text


def _discard_command(status: str) -> str:
    """A copy-pasteable ``git checkout --`` command that discards the refused
    changes, built from the same paths the message lists.

    When every change can be named — the list was not truncated at
    ``LOCAL_CHANGES_LIST_LIMIT`` and no entry is a rename (rendered
    ``old -> new``, which is not a path) — the command names each file, so the
    user can see exactly what it touches. Otherwise the per-file form would
    discard only part of the changes and the update would refuse again, so the
    catch-all ``git checkout -- .`` is offered instead.
    """
    paths = _parse_porcelain_z(status)
    plain = [p for p in paths[:LOCAL_CHANGES_LIST_LIMIT] if " -> " not in p]
    if plain and len(plain) == len(paths):
        return "git checkout -- " + " ".join(f'"{p}"' if " " in p else p for p in plain)
    return "git checkout -- ."


def assert_git_update_ready(project_dir: Path, *, switching: bool = False) -> str:
    """Refuse to update unless the checkout is a Git repo with no modified
    tracked files, and return the current ``HEAD`` revision.

    ``switching`` words the refusal for a channel switch (the same check guards
    it, docs/intent/early_access_release.md §3-2-3): the user is told to switch
    again, not to run an update they never asked for.

    Only *tracked* files are inspected (``--untracked-files=no``). Untracked
    files (macOS ``.DS_Store``, a diagnostics script dropped into the folder,
    ...) do not interfere with a fast-forward; the one case where they would —
    an untracked *or ignored* file at a path the new revision adds — is refused
    by the merge in ``update_code`` (``--no-overwrite-ignore``), which names the
    file and leaves the checkout untouched. Modified tracked files still refuse
    because the updater never stashes or resets user work; the refusal message
    names the files and shows both exits — discard with ``git checkout --`` or
    keep by committing — so a non-developer can act on it (history:
    docs/issues/archive/update_refuses_on_tracked_local_changes_without_exit.md).
    """
    if not (project_dir / ".git").is_dir() or shutil.which("git") is None:
        if switching:
            raise UpdateError(
                "Switching the release channel requires a Git checkout of SAIVerse."
            )
        raise UpdateError(
            "Automatic update requires a Git checkout. The former ZIP overlay path is "
            "disabled because it cannot safely remove retired files without deleting "
            "unknown user files."
        )
    status = _run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=no"],
        cwd=project_dir,
        label="verify clean working tree",
        timeout=60,
        encoding="utf-8",
    ).stdout
    if status.strip("\0 \r\n"):
        if switching:
            raise UpdateError(
                "Working tree has local changes. The channel switch was not started; "
                "the updater never stashes or resets user work, and switching would "
                "carry these changes onto the other release line.\n"
                "Modified files:\n" + _format_local_changes(status) + "\n"
                "If you do not need these changes, discard them by running this "
                "command in the SAIVerse folder, then switch the channel again:\n"
                "  " + _discard_command(status) + "\n"
                "If you want to keep the changes, commit them first, then switch "
                "the channel again."
            )
        raise UpdateError(
            "Working tree has local changes. Update was not started; the updater "
            "never stashes or resets user work.\n"
            "Modified files:\n" + _format_local_changes(status) + "\n"
            "If you do not need these changes, discard them by running this "
            "command in the SAIVerse folder, then run the update again:\n"
            "  " + _discard_command(status) + "\n"
            "If you want to keep the changes, commit them first, then run the "
            "update again."
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


def create_pre_update_snapshot(
    project_dir: Path,
    python: str,
    *,
    prefix: str = "auto_before_update",
    note: str = "Automatic restore point before code update",
) -> str:
    """Save and validate a whole-world snapshot before any code moves.

    ``prefix`` / ``note`` let a channel switch label its restore point
    (``ea_optin_*`` / ``ea_return_*``) so it can be told apart from a routine
    update's later.
    """
    name = datetime.now(timezone.utc).strftime(f"{prefix}_%Y%m%d_%H%M%S_%f")
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
                note,
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
    """Fast-forward the checkout to its upstream without touching user files.

    ``fetch`` + ``merge`` instead of ``pull`` because only ``merge`` accepts
    ``--no-overwrite-ignore``. By default git silently overwrites an *ignored*
    file when the new revision starts tracking that path (``git pull`` cannot
    turn that off); ``assert_git_update_ready`` does not see ignored files
    either, so without this flag an update could destroy e.g. a user's notes in
    an ignored directory. With the flag the merge refuses and names the file,
    leaving the checkout at the old revision, exactly like it already does for
    plain untracked files. The flag is documented since Git 2.25 (2020); an
    older git rejects it as an unknown option, which also stops the update
    before anything is written.
    """
    _run(
        ["git", "fetch"],
        cwd=project_dir,
        label="fetch code update",
        timeout=300,
    )
    _run(
        ["git", "merge", "--ff-only", "--no-overwrite-ignore", "@{upstream}"],
        cwd=project_dir,
        label="fast-forward code update",
        timeout=300,
    )


# --- Release channels -------------------------------------------------------


def read_checkout_branch(project_dir: Path) -> str | None:
    """The branch ``HEAD`` points at, read from the git metadata files.

    For callers that must stay cheap and must not depend on a ``git`` binary
    (the version endpoint is polled, and a PortableGit-only install may not
    have git on the server's PATH). Handles a normal ``.git`` directory and the
    ``gitdir:`` pointer file of a linked worktree. Returns None for a detached
    HEAD, a checkout that is not a git repo, or anything unreadable -- callers
    treat that as the stable channel.
    """
    git_path = project_dir / ".git"
    try:
        if git_path.is_dir():
            git_dir = git_path
        elif git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = Path(pointer[len("gitdir:"):].strip())
            if not git_dir.is_absolute():
                git_dir = project_dir / git_dir
        else:
            return None
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return None  # detached HEAD
    branch = head[len(prefix):].strip()
    return branch or None


def channel_for_branch(branch: str | None) -> str:
    """Which release channel a checkout on ``branch`` receives.

    Only the early-access branch is early access; everything else -- main, a
    development branch, a detached or unreadable HEAD -- keeps the stable
    behaviour.
    """
    if branch == CHANNEL_BRANCHES[CHANNEL_EARLY_ACCESS]:
        return CHANNEL_EARLY_ACCESS
    return CHANNEL_STABLE


def _current_branch(project_dir: Path) -> str | None:
    """The checked-out branch according to git itself (None when detached)."""
    result = _run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=project_dir,
        label="record current branch",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_ref_sha(project_dir: Path, ref: str) -> str | None:
    """The commit ``ref`` names, or None when the ref does not exist."""
    result = _run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=project_dir,
        label=f"resolve {ref}",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _parse_version_text(text: str | None, what: str) -> Any:
    """``packaging`` Version of ``text``; fail closed when it cannot be read.

    The switch refuses rather than guesses: moving a world onto code whose
    version cannot be compared is exactly the downgrade this check exists to
    stop.
    """
    if _Version is None:
        raise UpdateError(
            "The packaging library is unavailable, so the versions of the two release "
            "lines cannot be compared; the channel switch was not started"
        )
    if not text:
        raise UpdateError(f"The version of {what} could not be read; the channel switch was not started")
    try:
        return _Version(text.strip())
    except _InvalidVersion as exc:
        raise UpdateError(
            f"The version of {what} ({text.strip()!r}) is not a valid version; "
            "the channel switch was not started"
        ) from exc


class SwitchPlan(NamedTuple):
    """What ``preflight_switch`` verified, for the switch itself to act on."""

    channel: str
    target_branch: str
    original_branch: str
    old_revision: str


def preflight_switch(project_dir: Path, channel: str) -> SwitchPlan:
    """Verify a channel switch can be made, without touching the working tree.

    Run twice: by the API before the backend shuts down (so the usual refusal
    -- no early-access branch published yet -- costs the user nothing), and by
    the engine after it, to close the race. Checks, in order:

    1. the same clean-tree check an update uses (reworded for a switch);
    2. the checkout is on the *other* channel's branch -- joining early access
       starts from main and leaving it starts from early-access. A development
       branch or a detached HEAD is refused rather than silently moved;
    3. ``git fetch`` succeeds and the remote has the target branch. Until the
       first early-access release there is no ``origin/early-access``, and this
       is the normal refusal;
    4. the target line's VERSION is not older than this checkout's. The world
       only moves forward (startup refuses to open a newer world with older
       code), so a switch onto an older version would leave SAIVerse unable to
       start. For the return to stable this *is* the rule of
       docs/intent/early_access_release.md §3-3; for joining it stops opting
       into an early-access line the stable release has already overtaken.

    Fetching updates remote-tracking refs only, never the working tree.
    """
    if channel not in CHANNEL_BRANCHES:
        raise UpdateError(f"Unknown release channel {channel!r}")
    old_revision = assert_git_update_ready(project_dir, switching=True)
    target_branch = CHANNEL_BRANCHES[channel]
    source_channel = CHANNEL_STABLE if channel == CHANNEL_EARLY_ACCESS else CHANNEL_EARLY_ACCESS
    source_branch = CHANNEL_BRANCHES[source_channel]

    original_branch = _current_branch(project_dir)
    if original_branch == target_branch:
        raise UpdateError(f"This SAIVerse is already on the {target_branch} branch; nothing to switch")
    if original_branch != source_branch:
        raise UpdateError(
            f"Switching to the {target_branch} branch is only possible from the "
            f"{source_branch} branch, but this checkout is on "
            f"{original_branch or 'a detached HEAD'}. The channel switch was not started."
        )

    _run(
        ["git", "fetch", UPDATE_REMOTE],
        cwd=project_dir,
        label="fetch release branches",
        timeout=300,
    )
    remote_ref = f"refs/remotes/{UPDATE_REMOTE}/{target_branch}"
    if _git_ref_sha(project_dir, remote_ref) is None:
        raise UpdateError(
            f"The {target_branch} branch has not been published on {UPDATE_REMOTE} yet, so "
            "there is nothing to switch to. The channel switch was not started."
        )

    target_version_text = _run(
        ["git", "show", f"{remote_ref}:VERSION"],
        cwd=project_dir,
        label=f"read the version of {target_branch}",
        timeout=60,
        check=False,
        encoding="utf-8",
    )
    target_version = _parse_version_text(
        target_version_text.stdout if target_version_text.returncode == 0 else None,
        f"the {target_branch} branch",
    )
    current_version = _parse_version_text(read_version(project_dir), "this SAIVerse")
    if target_version < current_version:
        raise UpdateError(
            f"The {target_branch} branch is at version {target_version}, older than this "
            f"SAIVerse ({current_version}). The world data only moves forward, so switching "
            "now would leave SAIVerse unable to start. The channel switch was not started; "
            f"it becomes possible once {target_branch} reaches {current_version} or later."
        )
    return SwitchPlan(channel, target_branch, original_branch, old_revision)


def prepare_switch_branch(project_dir: Path, target_branch: str) -> None:
    """Point the local ``target_branch`` at the fetched remote head, with tracking.

    Only refs change here, never the working tree, so a failure leaves nothing
    to undo. A local branch that does not exist yet is created tracking the
    remote one (so later updates fast-forward it through ``@{upstream}``). One
    that exists -- ``main`` when returning from early access -- is
    fast-forwarded to the remote head; if it has commits the remote lacks the
    switch refuses, because moving the ref would drop them (the updater never
    resets user work).
    """
    remote_branch = f"{UPDATE_REMOTE}/{target_branch}"
    remote_sha = _git_ref_sha(project_dir, f"refs/remotes/{remote_branch}")
    if remote_sha is None:
        raise UpdateError(f"{remote_branch} disappeared after fetching; the channel switch was not started")
    local_ref = f"refs/heads/{target_branch}"
    local_sha = _git_ref_sha(project_dir, local_ref)
    if local_sha is None:
        _run(
            ["git", "branch", "--track", target_branch, remote_branch],
            cwd=project_dir,
            label=f"create local {target_branch} tracking {remote_branch}",
            timeout=60,
        )
        return
    if local_sha != remote_sha:
        is_ancestor = _run(
            ["git", "merge-base", "--is-ancestor", local_sha, remote_sha],
            cwd=project_dir,
            label=f"check that {target_branch} can fast-forward",
            timeout=60,
            check=False,
        )
        if is_ancestor.returncode != 0:
            raise UpdateError(
                f"The local {target_branch} branch has commits that {remote_branch} does "
                "not have, so it cannot be moved forward without dropping them. The "
                "channel switch was not started."
            )
        _run(
            ["git", "update-ref", local_ref, remote_sha, local_sha],
            cwd=project_dir,
            label=f"fast-forward local {target_branch}",
            timeout=60,
        )
    _run(
        ["git", "branch", f"--set-upstream-to={remote_branch}", target_branch],
        cwd=project_dir,
        label=f"track {remote_branch}",
        timeout=60,
    )


def switch_code(project_dir: Path, target_branch: str) -> None:
    """Check out ``target_branch`` -- the one step of a switch that moves code.

    ``git switch`` is all-or-nothing: when it refuses, the checkout is left on
    the original branch untouched. ``--no-overwrite-ignore`` is the same
    protection ``update_code`` gets from merge: by default git silently
    overwrites an *ignored* file when the other branch tracks that path, and
    with the flag it refuses and names the file instead.
    """
    _run(
        ["git", "switch", "--no-overwrite-ignore", target_branch],
        cwd=project_dir,
        label=f"switch code to {target_branch}",
        timeout=300,
    )


def _checkout_untouched(project_dir: Path, original_branch: str, old_revision: str) -> bool:
    """Whether a failed switch left the checkout exactly as it found it.

    ``git switch`` normally refuses before writing anything, but on Windows a
    file held open by another program can stop it half-way through writing the
    tree. Rather than trust the refusal, look: same branch, same commit, no
    modified tracked files. Anything else -- including not being able to tell
    -- counts as touched, and the caller rolls back to the recorded branch.
    """
    try:
        if _current_branch(project_dir) != original_branch:
            return False
        if _git_ref_sha(project_dir, "HEAD") != old_revision:
            return False
        status = _run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=no"],
            cwd=project_dir,
            label="verify the checkout after a failed switch",
            timeout=60,
            encoding="utf-8",
        ).stdout
    except UpdateError:
        return False
    return not status.strip("\0 \r\n")


_PIP_CHECK_CLEAN = "No broken requirements found."


def _upgrade_pip(project_dir: Path, python: str) -> None:
    """Upgrade the .venv's pip before the requirements are installed.

    pip carries its own copy of certifi (``pip._vendor.certifi``) and verifies
    every download's TLS certificate with it. requirements.lock has no hashes,
    so that TLS check is what makes a download authentic. setup.bat / setup.sh
    upgrade pip on every run (setup.bat:161), but a user who updates through the
    UI update button (this engine) never reruns setup, so the pip -- and its
    certifi -- stayed at the version first installed. On 2026-10-21 the
    intermediate key of the eMudhra (emSign) CA leaked and certifi 2026.10.19
    removed that CA; an old pip would go on trusting it. Upgrading here makes
    both install / update paths keep pip current.

    A failure is logged and the update goes on: the pip already installed still
    works, and the lock install right after decides whether the update succeeds.
    """
    try:
        result = _run(
            [python, "-m", "pip", "install", "--upgrade", "pip"],
            cwd=project_dir,
            label="upgrade pip",
            check=False,
        )
    except UpdateError as exc:
        LOGGER.warning("[deps] pip upgrade could not run; continuing with the installed pip: %s", exc)
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        LOGGER.warning(
            "[deps] pip upgrade failed (exit %s); continuing with the installed pip%s",
            result.returncode,
            f": {detail}" if detail else "",
        )


def _remove_retired_core_packages(project_dir: Path, python: str, requirements: str) -> None:
    """Uninstall ``RETIRED_CORE_PACKAGES`` after the requirements are installed.

    ``pip uninstall -y`` only warns, and exits 0, for a package that is not
    installed, so an environment without them is left as it is. A name the
    requirements file just installed is kept: a rollback to a revision from
    before the package was retired reinstalls that revision's own lock, and
    removing the package would break the code it was rolled back to. A failure
    is logged and the update goes on.
    """
    scan = scan_requirements(project_dir / requirements)
    if scan is None:
        # The file was just installed from, so an unreadable file here is an
        # abnormal state. Removing on a failed read could strip a package the
        # rolled-back code still imports, so the safe side is to keep.
        LOGGER.warning(
            "[deps] %s could not be read back; the retired packages were left as they are",
            requirements,
        )
        return
    wanted = {_canonical_package_name(dist.name) for dist in scan.required}
    retired = [name for name in RETIRED_CORE_PACKAGES if _canonical_package_name(name) not in wanted]
    if not retired:
        return
    try:
        result = _run(
            [python, "-m", "pip", "uninstall", "-y", *retired],
            cwd=project_dir,
            label="remove retired core packages",
            check=False,
        )
    except UpdateError as exc:
        LOGGER.warning("[deps] retired packages could not be removed: %s", exc)
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        LOGGER.warning(
            "[deps] removing retired packages %s failed (exit %s)%s",
            ", ".join(retired),
            result.returncode,
            f": {detail}" if detail else "",
        )


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
    if result.returncode != 1:
        # Only exit 1 is pip's answer "conflicts found". 2 is a usage / internal
        # error, a negative code is a signal: in both cases nothing was checked,
        # and reporting the output as conflicts would put a pip crash in the log
        # dressed up as an addon problem.
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        LOGGER.warning(
            "[deps] pip check could not run (exit %s); addon conflicts were not checked%s",
            result.returncode,
            f": {detail}" if detail else "",
        )
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


def update_dependencies(
    project_dir: Path,
    python: str,
    requirements_file: str | None = None,
    *,
    replace_portable_node: bool = True,
) -> list[DependencyStepFailure]:
    """Install everything the code on disk needs besides itself: the portable
    Node.js, the Python packages, the frontend packages and SearXNG.

    Returns the steps that failed without stopping the update (the portable
    Node.js and SearXNG, see ``refresh_portable_node`` / ``refresh_searxng``);
    an empty list means everything finished. Any other failure raises
    ``UpdateError``. ``install_dependencies_main`` turns a non-empty list into
    ``DEPENDENCIES_FAILURE_RECORD`` and leaves ``DEPENDENCIES_RECORD``
    unwritten, so the failure is shown on the screen and the next start tries
    again.

    ``requirements_file`` defaults to the lock and is *not* chosen by looking at
    the working tree: a checkout that has no lock is a broken update target, and
    pip failing loudly on the missing file is the correct outcome. The only
    caller that passes something else is ``_rollback_code_and_dependencies``,
    for revisions that predate the lock.

    The update sequence does not call this directly: it runs it through
    ``_run_dependencies_from_disk``, which starts the engine file on disk with
    ``INSTALL_DEPENDENCIES_OPTION`` (``main`` -> ``install_dependencies_main``).
    Every replacement an update needs belongs in here, because this is the one
    step that runs with the procedure of the code just fetched
    (docs/intent/update_completes_with_new_code.md section 4).

    Order: replace the portable Node.js (before npm ci, so the packages are
    installed with the Node.js the code pins; skipped with
    ``replace_portable_node=False``, which the rollback passes), upgrade pip,
    install the requirements file, remove ``RETIRED_CORE_PACKAGES``, ``pip
    check``, npm, then SearXNG.
    """
    failures: list[DependencyStepFailure] = []
    if replace_portable_node:
        node_failure = refresh_portable_node(project_dir)
        if node_failure is not None:
            failures.append(node_failure)
    _upgrade_pip(project_dir, python)
    requirements = requirements_file or REQUIREMENTS_LOCK
    _run(
        [python, "-m", "pip", "install", "-r", requirements],
        cwd=project_dir,
        label="install Python dependencies",
    )
    _remove_retired_core_packages(project_dir, python, requirements)
    _report_addon_conflicts(project_dir, python)
    frontend = project_dir / "frontend"
    if not frontend.is_dir():
        raise UpdateError("frontend directory is missing after code update")
    npm = _find_npm(project_dir)
    npm_command = "ci" if (frontend / "package-lock.json").is_file() else "install"
    _run(
        [npm, npm_command],
        cwd=frontend,
        label=f"npm {npm_command}",
        # npm and next write UTF-8 whatever the console code page is; decoding
        # with the Windows locale (cp932) crashed the output reader on the
        # build's check marks (2026-09-30, found running the real build).
        encoding="utf-8",
        # Lifecycle scripts run ``node`` by name; it must be npm's own.
        env=_env_with_npm_on_path(npm),
    )
    searxng_failure = refresh_searxng(project_dir)
    if searxng_failure is not None:
        failures.append(searxng_failure)
    return failures


# This engine as it is on disk, and the option of ``main`` that runs only
# ``update_dependencies``. ``_run_dependencies_from_disk`` starts the file at
# this path with this option.
ENGINE_SCRIPT = Path("scripts") / "update_engine.py"
INSTALL_DEPENDENCIES_OPTION = "--install-dependencies"
# With INSTALL_DEPENDENCIES_OPTION: leave the portable Node.js as it is. Passed
# by the rollback only. It was added together with moving
# ``refresh_portable_node`` into ``update_dependencies``, so an engine on disk
# that does not have it never replaces the portable Node.js in that step and
# needs nothing passed.
KEEP_PORTABLE_NODE_OPTION = "--keep-portable-node"


def _engine_on_disk_text(script: Path) -> str:
    """The engine file on disk, for telling which options it has.

    Revisions from before ``INSTALL_DEPENDENCIES_OPTION`` have an engine
    without it (or, for the oldest ones, no engine file at all: ""); started
    with an option it lacks, such a file would exit with an argparse usage
    error.
    """
    try:
        return script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run_dependencies_from_disk(
    project_dir: Path,
    python: str,
    requirements_file: str | None = None,
    *,
    keep_portable_node: bool = False,
) -> None:
    """Run ``update_dependencies`` of the engine file on disk, in a child process.

    This process loaded this module before ``update_code`` / ``switch_code``
    replaced the files, so its own ``update_dependencies`` is the procedure of
    the revision the update started from. The child runs the procedure of the
    code that is on disk now -- the fetched revision on an update, the
    previous revision after ``_rollback_code_and_dependencies`` reset the
    checkout -- so the code and the procedure that installs its packages
    always come from the same revision.

    A non-zero exit of the child raises ``UpdateError``, as a failure of the
    in-process ``update_dependencies`` did. When the engine on disk has no
    ``INSTALL_DEPENDENCIES_OPTION`` (a revision from before it), this
    process's own ``update_dependencies`` runs instead, which is what every
    update did before the option existed.

    ``keep_portable_node`` (the rollback) leaves the portable Node.js as it is
    (``KEEP_PORTABLE_NODE_OPTION``; see ``refresh_portable_node`` for why).
    """
    script = project_dir / ENGINE_SCRIPT
    engine_text = _engine_on_disk_text(script)
    if INSTALL_DEPENDENCIES_OPTION not in engine_text:
        LOGGER.warning(
            "[deps] %s on disk has no %s; installing the packages with the running updater's procedure",
            ENGINE_SCRIPT,
            INSTALL_DEPENDENCIES_OPTION,
        )
        # The failures it returns are already in the update log. They are not
        # written to DEPENDENCIES_FAILURE_RECORD: the code on disk predates the
        # option, so it has no backend that reads that file, and its
        # ``--check-complete`` does not retry.
        update_dependencies(
            project_dir, python, requirements_file, replace_portable_node=not keep_portable_node
        )
        return
    command = [python, str(script), INSTALL_DEPENDENCIES_OPTION]
    if requirements_file is not None:
        command.append(requirements_file)
    if keep_portable_node and KEEP_PORTABLE_NODE_OPTION in engine_text:
        command.append(KEEP_PORTABLE_NODE_OPTION)
    result = _run(
        command,
        cwd=project_dir,
        label=f"install dependencies with {ENGINE_SCRIPT} on disk",
        # The child runs each phase with its own timeout: the portable Node.js
        # 600 s; the pip upgrade, the lock install, the retired-package removal
        # and npm 900 s each; pip check 300 s; SearXNG 300 s to check and
        # 1200 s to reinstall. 7200 s is above their sum (6000 s), so the inner
        # timeouts fire first.
        timeout=7200,
        check=False,
        # The child writes its log as UTF-8 (``install_dependencies_main``),
        # and npm output is part of it.
        encoding="utf-8",
    )
    # The child logs to stdout only; this process puts it into the update log.
    for line in (result.stdout or "").splitlines():
        if line.strip():
            LOGGER.info("%s", line.rstrip())
    for line in (result.stderr or "").splitlines():
        if line.strip():
            LOGGER.warning("%s", line.rstrip())
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise UpdateError(f"installing dependencies failed with exit {result.returncode}: {detail}")


def _find_npm(project_dir: Path) -> str:
    """The npm to run: the portable one setup installed, else the one on PATH.

    The portable one wins when both exist, as in start.bat / start-dev.bat,
    which put ``.node`` first on PATH: the frontend runs on that Node.js, so
    its packages are installed and built with it too -- also by update.bat,
    whose PATH does not have ``.node``. Shared by the package install, the
    frontend build and the frontend restart, so all three use the same npm.
    """
    portable_npm = project_dir / ".node" / ("npm.cmd" if sys.platform == "win32" else "npm")
    if portable_npm.is_file():
        return str(portable_npm)
    npm = shutil.which("npm")
    if npm is None:
        raise UpdateError("npm is required to update the frontend")
    return npm


# Installs the Node.js version it pins into .node, replacing a .node whose
# version differs (docs/intent/dependency_management.md section 2-6).
PORTABLE_NODE_INSTALLER = Path("scripts") / "install_node_portable.ps1"


def refresh_portable_node(project_dir: Path) -> DependencyStepFailure | None:
    """Replace ``.node`` when its Node.js is not the version the updated code pins.

    Only a Windows checkout that has a ``.node`` (setup.bat installed it
    because there was no system Node.js) is touched; a system Node.js is the
    user's to update, and macOS / Linux have no portable install. Runs the
    ``install_node_portable.ps1`` the update just brought in, so a new pin
    reaches existing users with the same update that ships it. Called from
    ``update_dependencies``, so it runs with the procedure of the code on disk
    too, before npm ci. Not in the rollback: the previous code runs on the
    newer Node.js too, and putting the old one back would bring back the
    version this replaced (docs/intent/dependency_management.md section 2-6).

    Never raises. The script swaps by renaming and leaves the previous
    ``.node`` intact when it fails (download, checksum, archive, or a ``.node``
    still in use), so the update goes on with the Node.js it had -- a working
    one. Failing the update over it would hold back the code, and the fixes it
    carries, on nodejs.org being reachable. A failure is returned instead, and
    ``install_dependencies_main`` shows it on the screen and leaves the
    dependency record unwritten, so the next start.bat tries again. Returns
    None when nothing failed (or there was nothing to do).
    """
    if not _is_windows():
        return None
    # ``.node.old`` alone is a replacement that died between its two renames;
    # the script puts it back before anything else.
    if not any((project_dir / name / "node.exe").is_file() for name in (".node", ".node.old")):
        return None
    script = project_dir / PORTABLE_NODE_INSTALLER
    if not script.is_file():
        # A checkout without its own installer cannot be fixed by retrying at
        # the next start: shown on screen, not retried.
        LOGGER.warning("[node] %s is missing; the portable Node.js was left as it is", PORTABLE_NODE_INSTALLER)
        return DependencyStepFailure(
            DEPENDENCY_STEP_PORTABLE_NODE,
            f"{PORTABLE_NODE_INSTALLER} is missing from this SAIVerse folder. "
            "Run update.bat (update.sh on macOS / Linux) to restore the files of SAIVerse.\n"
            f"SAIVerse のフォルダから {PORTABLE_NODE_INSTALLER} が欠けています。"
            "update.bat (Mac / Linux は update.sh) で SAIVerse のファイルを直してください。",
            retry=False,
        )
    try:
        result = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=project_dir,
            label="refresh portable Node.js",
            timeout=600,
            check=False,
        )
    except UpdateError as exc:
        LOGGER.warning(
            "[node] The portable Node.js could not be checked; the update continues with the existing .node",
            exc_info=True,
        )
        return DependencyStepFailure(
            DEPENDENCY_STEP_PORTABLE_NODE, f"The portable Node.js could not be checked: {exc}"
        )
    for line in (result.stdout or "").splitlines():
        if line.strip():
            LOGGER.info("[node] %s", line.strip())
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-2000:]
        LOGGER.warning(
            "[node] The portable Node.js could not be replaced (exit %d); the update continues with "
            "the existing .node, and the next start.bat tries again.%s",
            result.returncode,
            f" {detail}" if detail else "",
        )
        # The installer reports its errors as ``[ERROR] ...`` lines on stdout.
        reason = (detail or (result.stdout or "").strip()[-2000:]).strip()
        return DependencyStepFailure(
            DEPENDENCY_STEP_PORTABLE_NODE,
            f"The portable Node.js could not be replaced (exit {result.returncode})"
            + (f": {reason}" if reason else ""),
        )
    return None


# --- SearXNG --------------------------------------------------------------------
#
# setup.bat / setup.sh install SearXNG (the local web search server) into its
# own venv, ``scripts/.searxng-venv`` unless SEARXNG_VENV_DIR names another
# folder (relative to the checkout, as scripts/searxng_config.py resolves it).
# The SearXNG release is pinned in scripts/searxng_pin.json; whether the
# installed one matches and how to reinstall it is scripts/searxng_install.py
# (``check`` / ``setup``), the same steps run_searxng_server.ps1 / .sh run
# before starting the server. A checkout without that venv has no SearXNG and
# is left alone.

SEARXNG_DEFAULT_VENV = Path("scripts") / ".searxng-venv"
SEARXNG_INSTALLER = Path("scripts") / "searxng_install.py"
SEARXNG_LAUNCHER_SH = Path("scripts") / "run_searxng_server.sh"
# start.bat / start-dev.bat open SearXNG with exactly this in
# ``cmd /k "..."``; ``start_searxng`` opens the same window again.
SEARXNG_WINDOW_COMMAND = (
    "title SearXNG && powershell -ExecutionPolicy Bypass -File scripts\\run_searxng_server.ps1"
)
SEARXNG_WINDOW_SIGNATURE = "title SearXNG"
SEARXNG_LAUNCHER_PS1_NAME = "run_searxng_server.ps1"
_SEARXNG_MODULE = "searx.webapp"
_POWERSHELL_NAMES = frozenset({"powershell.exe", "pwsh.exe"})
SEARXNG_CHECK_TIMEOUT_SECONDS = 300
SEARXNG_SETUP_TIMEOUT_SECONDS = 1200


def _searxng_venv_dir(project_dir: Path) -> Path:
    configured = os.environ.get("SEARXNG_VENV_DIR")
    path = Path(configured) if configured else SEARXNG_DEFAULT_VENV
    return path if path.is_absolute() else project_dir / path


def _searxng_venv_python(project_dir: Path) -> Path:
    venv = _searxng_venv_dir(project_dir)
    return venv / ("Scripts/python.exe" if _is_windows() else "bin/python")


def searxng_installed(project_dir: Path) -> bool:
    """Whether setup installed SearXNG into this checkout (its venv has a python)."""
    return _searxng_venv_python(project_dir).is_file()


def _searxng_running_for_reinstall(project_dir: Path) -> DependencyStepFailure | None:
    """A retryable failure when this checkout's SearXNG may be running, else None.

    Not knowing (the process list cannot be read) counts as running: the
    reinstall is then tried again at the next start instead of risking a
    venv replaced under a running server.
    """
    try:
        running = find_searxng_servers(project_dir)
    except FrontendCheckUnavailable as exc:
        detail = f"whether SearXNG is running could not be checked ({exc})"
    else:
        if not running:
            return None
        detail = "SearXNG is running (" + _describe_processes(running) + ")"
    LOGGER.warning("[searxng] %s; SearXNG was not reinstalled", detail)
    return DependencyStepFailure(
        DEPENDENCY_STEP_SEARXNG,
        f"SearXNG needs to be reinstalled, but {detail}. Close the SearXNG window and run "
        "start.bat (start.sh on macOS / Linux) again.\n"
        "SearXNG の入れ直しが必要ですが、SearXNG が動いているため入れ直していません。"
        "SearXNG の窓を閉じてから、start.bat (Mac / Linux は start.sh) をもう一度実行してください。",
    )


def refresh_searxng(project_dir: Path) -> DependencyStepFailure | None:
    """Reinstall SearXNG when it does not match the release the code pins.

    Runs ``searxng_install.py check`` with the SearXNG venv's python, and
    ``searxng_install.py setup`` when the check fails -- what
    run_searxng_server.ps1 / .sh do before starting the server, done here so
    the update, not the next start, brings in a new pin. Nothing happens
    without a SearXNG venv.

    Never raises, for the same reason ``refresh_portable_node`` does not:
    SearXNG is an optional search server, and failing the whole update over
    it (GitHub unreachable, for instance) would hold back the code. The
    failure is logged and returned, like ``refresh_portable_node``'s: it is
    shown on the screen and the next start.bat / start.sh tries again
    (run_searxng_server.ps1 / .sh also try the same setup when SearXNG is next
    started). Returns None when nothing failed (or there was nothing to do).
    """
    if not searxng_installed(project_dir):
        return None
    installer = project_dir / SEARXNG_INSTALLER
    if not installer.is_file():
        # As in ``refresh_portable_node``: retrying at the next start cannot
        # bring back a file the checkout lacks, so this is shown, not retried.
        LOGGER.warning("[searxng] %s is missing; SearXNG was left as it is", SEARXNG_INSTALLER)
        return DependencyStepFailure(
            DEPENDENCY_STEP_SEARXNG,
            f"{SEARXNG_INSTALLER} is missing from this SAIVerse folder. "
            "Run update.bat (update.sh on macOS / Linux) to restore the files of SAIVerse.\n"
            f"SAIVerse のフォルダから {SEARXNG_INSTALLER} が欠けています。"
            "update.bat (Mac / Linux は update.sh) で SAIVerse のファイルを直してください。",
            retry=False,
        )
    python = str(_searxng_venv_python(project_dir))
    try:
        check = _run(
            [python, str(installer), "check"],
            cwd=project_dir,
            label="check SearXNG",
            timeout=SEARXNG_CHECK_TIMEOUT_SECONDS,
            check=False,
            encoding="utf-8",
        )
        if check.returncode == 0:
            LOGGER.info("[searxng] SearXNG matches the pinned release")
            return None
        # Only the Update button's update stops SearXNG first. update.bat, the
        # reinstall before start, and an update run by an engine from before
        # SearXNG was stopped reach here with SearXNG possibly running, and the
        # setup would replace the packages of the venv it is running from.
        running = _searxng_running_for_reinstall(project_dir)
        if running is not None:
            return running
        result = _run(
            [python, str(installer), "setup"],
            cwd=project_dir,
            label="reinstall SearXNG",
            timeout=SEARXNG_SETUP_TIMEOUT_SECONDS,
            check=False,
            encoding="utf-8",
        )
    except UpdateError as exc:
        LOGGER.warning(
            "[searxng] SearXNG could not be checked or reinstalled; the update continues, "
            "and SearXNG tries again when it is next started",
            exc_info=True,
        )
        return DependencyStepFailure(
            DEPENDENCY_STEP_SEARXNG, f"SearXNG could not be checked or reinstalled: {exc}"
        )
    for line in (result.stdout or "").splitlines():
        if line.strip():
            LOGGER.info("[searxng] %s", line.strip())
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-2000:]
        LOGGER.warning(
            "[searxng] SearXNG could not be reinstalled (exit %d); the update continues, and "
            "SearXNG tries again when it is next started.%s",
            result.returncode,
            f" {detail}" if detail else "",
        )
        reason = (detail or (result.stdout or "").strip()[-2000:]).strip()
        return DependencyStepFailure(
            DEPENDENCY_STEP_SEARXNG,
            f"SearXNG could not be reinstalled (exit {result.returncode})"
            + (f": {reason}" if reason else ""),
        )
    return None


def build_frontend(project_dir: Path) -> None:
    """``npm run build`` -- what ``next start`` serves has to match the new code.

    Only run when the frontend that was stopped for the update was a
    production server (``next start``, what start.bat / start.sh run); the
    dev server compiles on demand and needs no build.
    """
    npm = _find_npm(project_dir)
    _run(
        [npm, "run", "build"],
        cwd=project_dir / "frontend",
        label="npm run build",
        encoding="utf-8",  # same reason as npm ci in update_dependencies
        # ``prebuild`` and ``next build`` run ``node`` by name.
        env=_env_with_npm_on_path(npm),
    )


def _rollback_code_and_dependencies(
    project_dir: Path,
    python: str,
    old_revision: str,
    *,
    branch: str | None = None,
) -> None:
    """Best-effort repair used only after the initial clean-tree invariant.

    ``branch`` is set only for a channel switch: the checkout is first put back
    on the branch recorded before the switch, and only then reset to the old
    revision. Without it the reset would land on the *new* branch and leave the
    checkout on the other release line at the old code (intent
    early_access_release.md §3-2-3).
    """
    if branch is not None:
        LOGGER.error("Switching code back to the %s branch", branch)
        _run(
            ["git", "switch", "--discard-changes", branch],
            cwd=project_dir,
            label="rollback branch",
            timeout=120,
        )
    LOGGER.error("Rolling code back to %s", old_revision)
    _run(
        ["git", "reset", "--hard", old_revision],
        cwd=project_dir,
        label="rollback code revision",
        timeout=120,
    )
    requirements_file: str | None = None
    if not (project_dir / REQUIREMENTS_LOCK).is_file():
        # Updating *from* v0.3.3 or earlier *to* the first lock release: the
        # reset just removed the lock together with the new code, so the old
        # revision's own package list is the only thing that describes what it
        # needs. This is the sole place that installs from that file.
        LOGGER.warning(
            "Revision %s predates %s; reinstalling its packages from %s instead",
            old_revision,
            REQUIREMENTS_LOCK,
            LEGACY_REQUIREMENTS,
        )
        requirements_file = LEGACY_REQUIREMENTS
    try:
        # The previous revision's own procedure, from the file the reset just
        # put back (see ``_run_dependencies_from_disk``), with the portable
        # Node.js left as the update replaced it (``refresh_portable_node``).
        _run_dependencies_from_disk(project_dir, python, requirements_file, keep_portable_node=True)
    except UpdateError:
        LOGGER.exception("Dependency repair for the previous revision also failed")


_CREATE_NEW_CONSOLE = 0x00000010


def _is_windows() -> bool:
    return sys.platform == "win32"


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
    if _is_windows():
        # A visible console window of its own, like the "SAIVerse Backend"
        # window start.bat opens, so the user can see the backend and stop it
        # by closing the window. It used to be DETACHED_PROCESS: no window, and
        # no way to stop it short of the task manager.
        #
        # python is started directly, not wrapped in ``cmd /k``: the health
        # check (``process.poll()``) and ``_terminate_spawned`` act on this
        # Popen, and with a cmd wrapper they would watch and kill cmd.exe while
        # python lived on.
        #
        # No stdin / stdout / stderr are passed on purpose. With none given,
        # Popen neither sets STARTF_USESTDHANDLES nor (because close_fds=True)
        # lets the child inherit handles, so the child writes to its own new
        # console and holds nothing of this updater's. Redirecting them to
        # pipes or DEVNULL would blank the window the change exists to show.
        kwargs["creationflags"] = _CREATE_NEW_CONSOLE
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


# --- The frontend server ------------------------------------------------------
#
# The frontend (``next start`` from start.bat / start.sh, ``next dev`` from
# start-dev.*) runs as its own node process, separate from the backend. On
# Windows it keeps ``node_modules/@next/swc-*/next-swc.*.node`` loaded, so a
# ``npm ci`` beside it fails with EPERM after deleting every other package
# (docs/issues/archive/ui_update_fails_while_frontend_runs.md). A detached update
# therefore stops this checkout's frontend before touching anything, and starts
# it again -- in the mode it was running in -- once the backend is back.

FRONTEND_MODE_START = "start"
FRONTEND_MODE_DEV = "dev"
# start.bat / start-dev.bat open the frontend in ``cmd /k "title SAIVerse
# Frontend && ..."``. Only a cmd.exe carrying this text in its command line, and
# only as an ancestor of a frontend node process, is stopped with it -- so the
# window closes instead of being left at a prompt. Any other shell is the
# user's and is never touched.
FRONTEND_WINDOW_SIGNATURE = "title SAIVerse Frontend"
_FRONTEND_WINDOW_TITLES = {
    FRONTEND_MODE_START: "SAIVerse Frontend",
    FRONTEND_MODE_DEV: "SAIVerse Frontend (Dev)",
}
_NODE_PROCESS_NAMES = frozenset({"node", "node.exe"})
_WINDOW_SHELL_NAMES = frozenset({"cmd.exe"})
FRONTEND_STOP_TIMEOUT_SECONDS = 15.0


class FrontendCheckUnavailable(UpdateError):
    """psutil is missing or broken, so running frontend / SearXNG servers cannot be listed."""


class FrontendServers(NamedTuple):
    """This checkout's frontend processes, and the mode they were serving in."""

    # psutil.Process objects: node processes, plus their start.bat window.
    processes: list[Any]
    # FRONTEND_MODE_START / FRONTEND_MODE_DEV, or None when it could not be told.
    mode: str | None


def _import_psutil() -> Any:
    try:
        import psutil
    except Exception as exc:  # a broken extension can fail with OSError, not ImportError
        raise FrontendCheckUnavailable(f"psutil is unavailable or broken ({exc})") from exc
    return psutil


def _normalized_absolute(value: str) -> str | None:
    """``value`` as a comparable absolute path, or None when it is not one."""
    if not value or not os.path.isabs(value):
        return None
    try:
        return os.path.normcase(os.path.normpath(value))
    except (TypeError, ValueError):
        return None


def _directory_spellings(directory: Path) -> set[str]:
    """The ways a directory of this checkout (its frontend, the SearXNG venv)
    can be spelled.

    Both the path as given and its resolved form, so a checkout reached
    through a junction or symlink is still recognised.
    """
    roots: set[str] = set()
    candidates = [str(directory)]
    try:
        candidates.append(os.path.realpath(directory))
    except (OSError, ValueError):
        pass
    for candidate in candidates:
        normalized = _normalized_absolute(candidate)
        if normalized:
            roots.add(normalized)
    return roots


def _is_within(value: str, roots: set[str]) -> bool:
    """Whether ``value`` is one of ``roots`` or a path below one of them.

    Decided on path boundaries, never by substring: ``...\\SAIVerse\\frontend``
    and ``...\\SAIVerse\\.worktrees\\x\\frontend`` are different frontends.
    """
    normalized = _normalized_absolute(value)
    if normalized is None:
        return False
    for root in roots:
        if normalized == root or normalized.startswith(root.rstrip("\\/") + os.sep):
            return True
    return False


def _frontend_mode_from_cmdline(args: list[str]) -> str | None:
    """"start" / "dev" when a command line shows which server it runs.

    Recognises ``next start`` / ``next dev`` (the next bin script and the words
    after it), ``npm start`` / ``npm run dev`` (npm-cli.js, npm, npm.cmd), and
    the same words inside a start.bat window's ``cmd /k "..."``. Arguments are
    split on whitespace first, because a window's whole ``/k`` string is one
    argument and Linux shows a process title such as ``npm start`` as one.
    """
    tokens: list[str] = []
    for arg in args:
        tokens.extend(str(arg).replace('"', " ").split())
    lowered = [token.lower() for token in tokens]
    for index, token in enumerate(lowered):
        base = re.split(r"[\\/]", token)[-1]
        following = lowered[index + 1:index + 3]
        if base == "next" and following and following[0] in (FRONTEND_MODE_START, FRONTEND_MODE_DEV):
            return following[0]
        if base in ("npm", "npm.cmd", "npm-cli.js") and following:
            if following[0] == "start":
                return FRONTEND_MODE_START
            if (
                len(following) == 2
                and following[0] in ("run", "run-script")
                and following[1] in (FRONTEND_MODE_START, FRONTEND_MODE_DEV)
            ):
                return following[1]
    return None


def _frontend_windows(process: Any, psutil: Any) -> list[tuple[Any, list[str]]]:
    """start.bat / start-dev.bat windows among ``process``'s ancestors."""
    try:
        parents = process.parents()
    except (psutil.Error, OSError):
        return []
    signature = FRONTEND_WINDOW_SIGNATURE.lower()
    windows: list[tuple[Any, list[str]]] = []
    for parent in parents:
        try:
            if (parent.name() or "").lower() not in _WINDOW_SHELL_NAMES:
                continue
            cmdline = list(parent.cmdline() or [])
        except (psutil.Error, OSError):
            continue
        if signature in " ".join(cmdline).lower():
            windows.append((parent, cmdline))
    return windows


def find_frontend_servers(
    project_dir: Path,
    *,
    processes: Any | None = None,
) -> FrontendServers:
    """This checkout's running frontend server processes.

    A process belongs to it when it is node and its working directory, or any
    absolute path in its command line, lies inside ``<project>/frontend``
    (path boundaries, not substrings). The start.bat window that runs it is
    included through its ancestry (``FRONTEND_WINDOW_SIGNATURE``). Processes
    that vanish or deny access while being inspected are skipped.

    ``processes`` replaces ``psutil.process_iter()`` in tests.
    """
    frontend = project_dir / "frontend"
    if not frontend.is_dir():
        return FrontendServers([], None)
    psutil = _import_psutil()
    roots = _directory_spellings(frontend)
    if processes is None:
        try:
            processes = list(psutil.process_iter())
        except Exception as exc:
            raise FrontendCheckUnavailable(f"running processes could not be listed ({exc})") from exc

    matched: dict[int, Any] = {}
    modes: set[str] = set()
    for process in processes:
        try:
            if (process.name() or "").lower() not in _NODE_PROCESS_NAMES:
                continue
            try:
                cmdline = list(process.cmdline() or [])
            except psutil.AccessDenied:
                cmdline = []
            try:
                cwd = process.cwd()
            except psutil.AccessDenied:
                cwd = None
            pid = process.pid
        except (psutil.Error, OSError):
            continue  # gone, a zombie, or not ours to inspect
        belongs = bool(cwd) and _is_within(cwd, roots)
        if not belongs:
            belongs = any(_is_within(arg, roots) for arg in cmdline)
        if not belongs:
            continue
        matched[pid] = process
        mode = _frontend_mode_from_cmdline(cmdline)
        if mode:
            modes.add(mode)
        for window, window_cmdline in _frontend_windows(process, psutil):
            try:
                matched[window.pid] = window
            except (psutil.Error, OSError):
                continue
            window_mode = _frontend_mode_from_cmdline(window_cmdline)
            if window_mode:
                modes.add(window_mode)

    if len(modes) > 1:
        LOGGER.warning(
            "Frontend processes of this checkout run in more than one mode (%s); "
            "the frontend will not be restarted automatically",
            ", ".join(sorted(modes)),
        )
    mode = next(iter(modes)) if len(modes) == 1 else None
    return FrontendServers(list(matched.values()), mode)


def _describe_processes(processes: list[Any]) -> str:
    parts: list[str] = []
    for process in processes:
        try:
            parts.append(f"{process.name()} PID {process.pid}")
        except Exception:
            parts.append(f"PID {getattr(process, 'pid', '?')}")
    return ", ".join(parts)


def _terminate_processes(processes: list[Any], psutil: Any, timeout: float) -> list[Any]:
    """terminate, wait, kill what is left, wait again. Returns the survivors."""
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            LOGGER.warning("Could not ask PID %s to stop: %s", getattr(process, "pid", "?"), exc)
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    if not alive:
        return []
    LOGGER.warning("Process(es) did not stop in time; killing: %s", _describe_processes(alive))
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            LOGGER.warning("Could not kill PID %s: %s", getattr(process, "pid", "?"), exc)
    _, alive = psutil.wait_procs(alive, timeout=5)
    return list(alive)


def stop_frontend_servers(
    project_dir: Path,
    *,
    timeout: float = FRONTEND_STOP_TIMEOUT_SECONDS,
) -> FrontendServers:
    """Stop this checkout's frontend server; return what was stopped.

    Raises UpdateError when a process survives being killed. Callers run this
    before anything is changed, so a refusal here leaves the checkout as it
    was. After stopping, the processes are listed once more and anything that
    appeared meanwhile (a child the server started while it was being
    collected) is stopped too.
    """
    found = find_frontend_servers(project_dir)
    if not found.processes:
        LOGGER.info("No frontend server of this checkout is running")
        return found
    psutil = _import_psutil()
    LOGGER.info(
        "Stopping the frontend server (mode=%s): %s",
        found.mode or "unknown",
        _describe_processes(found.processes),
    )
    if found.mode is None:
        LOGGER.warning(
            "Could not tell which frontend server these processes run; they are stopped so "
            "the packages can be replaced, but will not be started again automatically"
        )
    survivors = _terminate_processes(found.processes, psutil, timeout)
    if not survivors:
        survivors = find_frontend_servers(project_dir).processes
        if survivors:
            survivors = _terminate_processes(survivors, psutil, timeout)
    if survivors:
        raise UpdateError(
            "The frontend server could not be stopped (" + _describe_processes(survivors) + "). "
            "Nothing was changed. Close the SAIVerse Frontend window and run the update again."
        )
    return found


def refuse_while_frontend_runs(project_dir: Path) -> None:
    """The manual-update entrance: refuse while this checkout's frontend runs.

    A manual update (update.bat / update.sh / the start-time finishing pass)
    must not stop the user's windows for them; it refuses before anything is
    changed and tells them what to close. When psutil cannot be used the check
    is skipped with a warning rather than blocking update.bat, which is the
    path that installs psutil in the first place.
    """
    try:
        found = find_frontend_servers(project_dir)
    except FrontendCheckUnavailable as exc:
        LOGGER.warning("Could not check whether the SAIVerse frontend is running: %s", exc)
        return
    if not found.processes:
        return
    raise UpdateError(
        "The SAIVerse frontend of this folder is still running ("
        + _describe_processes(found.processes)
        + "). Nothing was changed: on Windows the frontend packages cannot be replaced while "
        "it holds them open. Close the SAIVerse windows (Frontend) first, then run the update "
        "again.\n"
        "SAIVerse の画面のサーバーが動いています。何も変更していません。"
        "SAIVerse の窓 (Frontend) を閉じてから、もう一度実行してください。"
    )


def _env_with_npm_on_path(npm: str) -> dict[str, str]:
    """This environment, with npm's own directory first on PATH.

    The restarted frontend runs ``npm`` by name inside its window, and npm's
    lifecycle scripts and ``next build`` run ``node`` by name; all of them
    must find the Node.js next to the chosen npm. Being on PATH is not
    enough: with a system Node.js listed before ``.node``, ``node`` would
    resolve to the system one while npm is the portable one. So the
    directory is moved to the front, not only added when missing.
    """
    env = os.environ.copy()
    if not os.path.isabs(npm):
        return env
    npm_dir = os.path.dirname(npm)
    if not npm_dir:
        return env
    wanted = os.path.normcase(os.path.normpath(npm_dir))
    rest = [
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if entry and os.path.normcase(os.path.normpath(entry)) != wanted
    ]
    env["PATH"] = os.pathsep.join([npm_dir, *rest])
    return env


def start_frontend(project_dir: Path, mode: str) -> subprocess.Popen[Any]:
    """Start the frontend server again, the way the start scripts do.

    Windows: a new visible window, ``cmd /k "title SAIVerse Frontend && npm
    start"`` (``(Dev)`` / ``npm run dev`` for the dev server), the same window
    start.bat / start-dev.bat open, so the user can see it and stop it by
    closing it. As with the backend, no standard handles are passed: the
    window's own console is what the server writes to.

    macOS / Linux: ``npm start`` / ``npm run dev`` in ``frontend`` in a new
    session, the same way the backend is restarted.
    """
    if mode not in _FRONTEND_WINDOW_TITLES:
        raise UpdateError(f"Unknown frontend mode {mode!r}")
    frontend = project_dir / "frontend"
    npm = _find_npm(project_dir)
    script = ["start"] if mode == FRONTEND_MODE_START else ["run", "dev"]
    kwargs: dict[str, Any] = {
        "cwd": str(frontend),
        "close_fds": True,
        "env": _env_with_npm_on_path(npm),
    }
    if _is_windows():
        shell = os.environ.get("COMSPEC") or "cmd.exe"
        title = _FRONTEND_WINDOW_TITLES[mode]
        command = [shell, "/k", f"title {title} && npm {' '.join(script)}"]
        kwargs["creationflags"] = _CREATE_NEW_CONSOLE
    else:
        command = [npm, *script]
        kwargs["start_new_session"] = True
    LOGGER.info("Starting the frontend server again (mode=%s)", mode)
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise UpdateError(f"Could not start the frontend server: {exc}") from exc


def _restart_frontend(project_dir: Path, mode: str | None) -> None:
    """Start the stopped frontend again; never raises (the update's outcome is
    already decided when this runs)."""
    if mode is None:
        return
    try:
        process = start_frontend(project_dir, mode)
    except Exception:
        LOGGER.exception(
            "The frontend server could not be started again; start SAIVerse with start.bat / "
            "start.sh to bring the screen back"
        )
        return
    LOGGER.info("Frontend server started again (mode=%s, PID %s)", mode, process.pid)


# --- The SearXNG server ---------------------------------------------------------
#
# start.bat / start-dev.bat open SearXNG as ``cmd /k "title SearXNG &&
# powershell ... -File scripts\run_searxng_server.ps1"``; the script ends in
# ``<SearXNG venv python> -m searx.webapp``. start.sh (with SAIVERSE_SEARXNG=1)
# runs run_searxng_server.sh, which ``exec``s the same command. A detached
# update stops it before anything changes, so ``refresh_searxng`` can replace
# the packages of its venv (on Windows a running python holds them open), and
# starts it again at the end, as it does for the frontend.
#
# A process is this checkout's SearXNG server when it is python, its command
# line runs ``-m searx.webapp``, and the python it runs is the one in this
# checkout's SearXNG venv (checked on ``cmdline[0]`` and on ``exe()``, on path
# boundaries). On Windows the venv's python.exe is a launcher that starts the
# base interpreter as a child with the same command line, so both are found.
# Of their ancestors only the start.bat window (``SEARXNG_WINDOW_SIGNATURE`` in
# a cmd.exe command line) and the PowerShell running run_searxng_server.ps1 are
# included, so the window closes instead of being left at a prompt. Any other
# process -- another checkout's SearXNG, a python the user started -- is never
# touched.


def _runs_searxng_webapp(cmdline: list[str]) -> bool:
    return any(
        cmdline[index] == "-m" and cmdline[index + 1] == _SEARXNG_MODULE
        for index in range(len(cmdline) - 1)
    )


def _searxng_launchers(process: Any, psutil: Any) -> list[Any]:
    """The start.bat window and the PowerShell of run_searxng_server.ps1 among
    ``process``'s ancestors."""
    try:
        parents = process.parents()
    except (psutil.Error, OSError):
        return []
    signature = SEARXNG_WINDOW_SIGNATURE.lower()
    script = SEARXNG_LAUNCHER_PS1_NAME.lower()
    launchers: list[Any] = []
    for parent in parents:
        try:
            name = (parent.name() or "").lower()
            if name not in _WINDOW_SHELL_NAMES and name not in _POWERSHELL_NAMES:
                continue
            text = " ".join(parent.cmdline() or []).lower()
        except (psutil.Error, OSError):
            continue
        if (name in _WINDOW_SHELL_NAMES and signature in text) or (
            name in _POWERSHELL_NAMES and script in text
        ):
            launchers.append(parent)
    return launchers


def find_searxng_servers(project_dir: Path, *, processes: Any | None = None) -> list[Any]:
    """This checkout's running SearXNG server processes (see the section
    comment for what counts). Empty without a SearXNG venv, without listing
    any process. ``processes`` replaces ``psutil.process_iter()`` in tests."""
    venv = _searxng_venv_dir(project_dir)
    if not venv.is_dir():
        return []
    psutil = _import_psutil()
    roots = _directory_spellings(venv)
    if processes is None:
        try:
            processes = list(psutil.process_iter())
        except Exception as exc:
            raise FrontendCheckUnavailable(f"running processes could not be listed ({exc})") from exc

    matched: dict[int, Any] = {}
    for process in processes:
        try:
            if not (process.name() or "").lower().startswith("python"):
                continue
            cmdline = list(process.cmdline() or [])
            try:
                exe = process.exe() or ""
            except psutil.AccessDenied:
                exe = ""
            pid = process.pid
        except (psutil.Error, OSError):
            continue  # gone, a zombie, or not ours to inspect
        if not _runs_searxng_webapp(cmdline):
            continue
        if not ((cmdline and _is_within(cmdline[0], roots)) or (exe and _is_within(exe, roots))):
            continue
        matched[pid] = process
        for launcher in _searxng_launchers(process, psutil):
            try:
                matched[launcher.pid] = launcher
            except (psutil.Error, OSError):
                continue
    return list(matched.values())


def stop_searxng_servers(
    project_dir: Path,
    *,
    timeout: float = FRONTEND_STOP_TIMEOUT_SECONDS,
) -> bool:
    """Stop this checkout's SearXNG server; return whether one was running.

    Same handling as ``stop_frontend_servers``: terminate, kill what is left,
    look once more, and raise UpdateError when a process survives -- callers
    run this before anything is changed.
    """
    found = find_searxng_servers(project_dir)
    if not found:
        LOGGER.info("No SearXNG server of this checkout is running")
        return False
    psutil = _import_psutil()
    LOGGER.info("Stopping the SearXNG server: %s", _describe_processes(found))
    survivors = _terminate_processes(found, psutil, timeout)
    if not survivors:
        survivors = find_searxng_servers(project_dir)
        if survivors:
            survivors = _terminate_processes(survivors, psutil, timeout)
    if survivors:
        raise UpdateError(
            "The SearXNG server could not be stopped (" + _describe_processes(survivors) + "). "
            "Nothing was changed. Close the SearXNG window and run the update again.\n"
            "SearXNG のサーバーを止められませんでした。何も変更していません。"
            "SearXNG の窓を閉じてから、もう一度更新してください。"
        )
    return True


def start_searxng(project_dir: Path) -> subprocess.Popen[Any]:
    """Start the SearXNG server again, the way the start scripts do.

    Windows: a new visible window with ``SEARXNG_WINDOW_COMMAND``, the window
    start.bat opens, in the checkout folder (the command names the script by a
    relative path). macOS / Linux: run_searxng_server.sh in a new session.
    The script checks the installed release and reinstalls it if needed
    before it starts the server.
    """
    kwargs: dict[str, Any] = {"cwd": str(project_dir), "close_fds": True}
    if _is_windows():
        shell = os.environ.get("COMSPEC") or "cmd.exe"
        command = [shell, "/k", SEARXNG_WINDOW_COMMAND]
        kwargs["creationflags"] = _CREATE_NEW_CONSOLE
    else:
        command = ["bash", str(project_dir / SEARXNG_LAUNCHER_SH)]
        kwargs["start_new_session"] = True
    LOGGER.info("Starting the SearXNG server again")
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise UpdateError(f"Could not start the SearXNG server: {exc}") from exc


def _restart_searxng(project_dir: Path, was_running: bool) -> None:
    """Start the stopped SearXNG again; never raises (the update's outcome is
    already decided when this runs)."""
    if not was_running:
        return
    try:
        process = start_searxng(project_dir)
    except Exception:
        LOGGER.exception(
            "The SearXNG server could not be started again; start SAIVerse with start.bat / "
            "start.sh to bring web search back"
        )
        return
    LOGGER.info("SearXNG server started again (PID %s)", process.pid)


# --- The update sequence ----------------------------------------------------


class _UpdateProgress:
    """How far an update got, so a failure knows what to undo."""

    def __init__(self) -> None:
        self.plan: SwitchPlan | None = None
        self.old_revision: str | None = None
        # The code phase began: for a switch, git may have written part of the
        # tree even when it then refused.
        self.code_phase_started = False
        # The code moved to the new revision / branch.
        self.code_moved = False


def _apply_code_and_dependencies(project_dir: Path, python: str, progress: _UpdateProgress) -> None:
    """Move the code to the new revision / branch, then install its packages.

    The packages are installed by ``_run_dependencies_from_disk``, i.e. by the
    engine file the code phase just fetched, started as a child process -- not
    by ``update_dependencies`` of this process. This process is the engine of
    the revision the update started from, loaded before the fetch; installing
    with it would run the previous procedure over the new code, and an update
    that brings a new dependency step (upgrading pip, removing
    ``RETIRED_CORE_PACKAGES``) would not run that step on the update that
    ships it -- while still writing the completion marker. The Codex review of
    PR #414 found this, and PR #413 met the same kind of gap with the portable
    Node.js replacement, which is why that replacement now runs inside the
    child too (``update_dependencies``).
    """
    progress.code_phase_started = True
    if progress.plan is None:
        update_code(project_dir)
    else:
        # Ref-only preparation first: if it fails, nothing needs undoing.
        prepare_switch_branch(project_dir, progress.plan.target_branch)
        switch_code(project_dir, progress.plan.target_branch)
    progress.code_moved = True
    # The code moved, so whatever the marker recorded no longer holds. Drop
    # it here -- before the first phase that can leave packages half
    # installed -- so no failure path can end with a stale marker claiming
    # this checkout is finished. Both the manual and the detached run reach
    # the final write through this same function.
    invalidate_completion_marker(project_dir)
    invalidate_dependencies_record(project_dir)
    _run_dependencies_from_disk(project_dir, python)


def _needs_rollback(project_dir: Path, progress: _UpdateProgress) -> bool:
    if progress.code_moved:
        return True
    plan = progress.plan
    return (
        plan is not None
        and progress.code_phase_started
        and progress.old_revision is not None
        and not _checkout_untouched(project_dir, plan.original_branch, progress.old_revision)
    )


def _rollback(project_dir: Path, python: str, progress: _UpdateProgress) -> None:
    if progress.old_revision is None:
        raise UpdateError("The revision to roll back to was never recorded")
    if progress.plan is None:
        _rollback_code_and_dependencies(project_dir, python, progress.old_revision)
    else:
        _rollback_code_and_dependencies(
            project_dir, python, progress.old_revision, branch=progress.plan.original_branch
        )


def _preflight(project_dir: Path, switch_channel: str | None, progress: _UpdateProgress) -> None:
    if switch_channel is None:
        progress.old_revision = assert_git_update_ready(project_dir)
        return
    progress.plan = preflight_switch(project_dir, switch_channel)
    progress.old_revision = progress.plan.old_revision
    LOGGER.info(
        "Switching release channel to %s: %s -> %s",
        progress.plan.channel,
        progress.plan.original_branch,
        progress.plan.target_branch,
    )


def _snapshot(project_dir: Path, python: str, progress: _UpdateProgress) -> None:
    plan = progress.plan
    if plan is None:
        snapshot_name = create_pre_update_snapshot(project_dir, python)
    else:
        snapshot_name = create_pre_update_snapshot(
            project_dir,
            python,
            prefix=SWITCH_SNAPSHOT_PREFIX[plan.channel],
            note=f"Automatic restore point before switching to the {plan.target_branch} branch",
        )
    LOGGER.info("Pre-update restore point: %s", snapshot_name)


def _restore_previous_version(
    config: dict[str, Any],
    project_dir: Path,
    python: str,
    progress: _UpdateProgress,
    frontend_mode: str | None,
    searxng_was_running: bool = False,
) -> None:
    """After a failure with the backend stopped: bring the previous version back.

    Rolls back when the checkout moved, restarts the previous backend and
    checks its health, starts the stopped SearXNG again, then the stopped
    frontend -- rebuilt first when it is a production server and the code was
    rolled back, since the build on disk may be the new version's or
    half-written. Never raises: the caller re-raises the original failure
    afterwards.

    When the code itself cannot be rolled back, nothing is started: the
    checkout is in an unknown state, and starting it could migrate the world
    with code that is neither version. The next start.bat / start.sh finds no
    completion marker and finishes or verifies the update first.
    """
    rolled_back = False
    try:
        needs_rollback = _needs_rollback(project_dir, progress)
    except Exception:
        # Not knowing whether the checkout moved counts as moved: rolling back
        # to the recorded revision is safe either way.
        LOGGER.exception("Could not tell whether the checkout changed; rolling back to be safe")
        needs_rollback = progress.old_revision is not None
    if needs_rollback:
        try:
            _rollback(project_dir, python, progress)
        except Exception:
            LOGGER.exception(
                "The code could not be rolled back; SAIVerse is left stopped. Start it with "
                "start.bat / start.sh, which finishes or repairs the update first"
            )
            return
        rolled_back = True

    try:
        backend = restart_application(config)
        wait_for_healthy_restart(backend, config)
        LOGGER.error("Previous revision was restored and restarted successfully")
    except Exception:
        LOGGER.exception("Previous revision also failed to restart")

    _restart_searxng(project_dir, searxng_was_running)
    if frontend_mode is None:
        return
    if rolled_back and frontend_mode == FRONTEND_MODE_START:
        try:
            build_frontend(project_dir)
        except Exception:
            LOGGER.exception(
                "The previous frontend could not be built again, so it was not started; "
                "start SAIVerse with start.bat / start.sh"
            )
            return
    _restart_frontend(project_dir, frontend_mode)


def run_update(
    config: dict[str, Any] | None,
    project_dir: Path,
    *,
    switch_channel: str | None = None,
) -> None:
    """Update the checkout, or -- with ``switch_channel`` -- move it to the
    other release line.

    A channel switch rides the same sequence as an update (snapshot, code,
    dependencies, completion marker, restart) so it inherits every safety net
    the update has (docs/intent/early_access_release.md §3-2). The differences
    are confined to the code step (check out the other branch instead of
    fast-forwarding this one), the snapshot label, and the rollback, which puts
    the checkout back on the branch recorded before the switch.
    """
    python = str(config.get("venv_python", sys.executable)) if config else sys.executable
    _ensure_portable_git_on_path(project_dir)
    if config:
        _run_detached_update(config, project_dir, python, switch_channel)
    else:
        _run_manual_update(project_dir, python, switch_channel)


def _run_manual_update(project_dir: Path, python: str, switch_channel: str | None) -> None:
    """update.bat / update.sh / the start-time finishing pass: SAIVerse is
    stopped, nothing is restarted afterwards."""
    # Before anything else: with the frontend still running, npm ci would fail
    # half-way and take most of node_modules with it.
    refuse_while_frontend_runs(project_dir)
    progress = _UpdateProgress()
    _preflight(project_dir, switch_channel, progress)
    _snapshot(project_dir, python, progress)
    try:
        _apply_code_and_dependencies(project_dir, python, progress)
    except UpdateError:
        if _needs_rollback(project_dir, progress):
            _rollback(project_dir, python, progress)
        raise
    write_completion_marker(project_dir)
    LOGGER.info("Update applied. Start SAIVerse normally to run startup migrations.")


def _run_detached_update(
    config: dict[str, Any],
    project_dir: Path,
    python: str,
    switch_channel: str | None,
) -> None:
    """The UI's update / channel switch: the backend stops itself, this process
    does the work, then starts the backend -- and the frontend, if it was
    running -- again.

    Once the old backend has exited, every failure ends with the previous
    version running again (``_restore_previous_version``); before this, a
    failed button update left SAIVerse stopped
    (docs/issues/updater_failure_leaves_backend_stopped.md).
    """
    # Outside the recovery below: if the old backend cannot be confirmed gone,
    # starting another one could run two over the same world.
    wait_for_owned_process_exit(
        int(config["main_pid"]),
        config.get("main_process_created_at"),
    )
    progress = _UpdateProgress()
    stopped_frontend_mode: str | None = None
    searxng_was_running = False
    try:
        # The preflight runs *after* the old backend has fully exited:
        # everything the dying process (or the user, in that window) still
        # wrote to tracked files is seen by this check, not just by the
        # API-side one that ran before shutdown (adversarial review
        # 2026-09-25, TOCTOU finding). Nothing has been mutated yet.
        _preflight(project_dir, switch_channel, progress)
        # The frontend holds node_modules open on Windows, and SearXNG the
        # packages of its venv; stop both before anything changes. A failure
        # here also leaves everything untouched.
        stopped_frontend_mode = stop_frontend_servers(project_dir).mode
        searxng_was_running = stop_searxng_servers(project_dir)
        _snapshot(project_dir, python, progress)
        _apply_code_and_dependencies(project_dir, python, progress)
        if stopped_frontend_mode == FRONTEND_MODE_START:
            build_frontend(project_dir)
    except Exception:
        # Not only UpdateError: whatever stops the update here, the backend is
        # already gone and must come back.
        _restore_previous_version(
            config, project_dir, python, progress, stopped_frontend_mode, searxng_was_running
        )
        raise

    process: subprocess.Popen[Any] | None = None
    try:
        process = restart_application(config)
        payload = wait_for_healthy_restart(process, config)
    except Exception:
        if process is not None:
            _terminate_spawned(process)
        _restore_previous_version(
            config, project_dir, python, progress, stopped_frontend_mode, searxng_was_running
        )
        raise
    write_completion_marker(project_dir)
    LOGGER.info(
        "Update complete: City=%s version=%s PID=%s",
        payload.get("city_name"),
        payload.get("version"),
        process.pid,
    )
    # After the backend is healthy, so the screen comes back to a backend that
    # answers.
    _restart_searxng(project_dir, searxng_was_running)
    _restart_frontend(project_dir, stopped_frontend_mode)


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


def _config_switch_channel(config: dict[str, Any] | None) -> str | None:
    """The channel a detached run was asked to switch to, or None for an update."""
    if not config:
        return None
    channel = config.get("switch_channel")
    if channel is None:
        return None
    if channel not in CHANNEL_BRANCHES:
        raise UpdateError(f"Update config names an unknown release channel: {channel!r}")
    return str(channel)


def install_dependencies_main(
    project_dir: Path,
    requirements_file: str | None,
    *,
    replace_portable_node: bool = True,
) -> int:
    """``INSTALL_DEPENDENCIES_OPTION``: run ``update_dependencies`` with this
    interpreter and return the exit code.

    Started by ``_run_dependencies_from_disk``, which reads stdout and stderr
    and puts them into the update log, so this logs to stdout only (no
    ``self_update.log`` handler: the parent already writes that file). Both
    streams are switched to UTF-8 so the parent's ``encoding="utf-8"`` decodes
    them whatever the console code page is; only this process's streams
    change, not those of the pip / npm it starts.

    On success the failure left by an earlier run is removed and
    ``DEPENDENCIES_RECORD`` is written, in that order: a crash between the two
    leaves neither, and the next start simply installs once more, rather than
    a record without a failure to clear and a banner that never goes away.

    When a step that does not stop the update failed (the portable Node.js or
    SearXNG, returned by ``update_dependencies``), the exit code stays 0 --
    the update itself goes on -- but the failure is written to
    ``DEPENDENCIES_FAILURE_RECORD`` for the screen and ``DEPENDENCIES_RECORD``
    is not written, so the next start.bat / start.sh ``--check-complete``
    runs the dependency step once more (``reinstall_dependencies_once``).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    setup_logging(project_dir, to_file=False)
    try:
        failures = update_dependencies(
            project_dir,
            sys.executable,
            requirements_file,
            replace_portable_node=replace_portable_node,
        )
    except UpdateError as exc:
        print(f"UpdateError: {exc}", file=sys.stderr)
        return 1
    if failures:
        write_dependencies_failure(
            project_dir,
            "\n".join(failure.error for failure in failures),
            steps=[failure.step for failure in failures],
        )
        if any(failure.retry for failure in failures):
            # No record may claim a finished step. Usually there is none that
            # matches (the update drops it when the code moves, and the
            # reinstall at start runs only when it does not match); this makes sure.
            _remove_file(dependencies_record_path(project_dir), purpose="a dependency step that did not finish")
            LOGGER.warning(
                "[deps] Finished, but %s failed; the screen shows it, and the next start.bat / "
                "start.sh installs the packages once more",
                ", ".join(failure.step for failure in failures),
            )
        else:
            # Retrying cannot help: record the packages as installed so the
            # next start does not reinstall them all, and keep the failure on
            # screen until a later child run finishes without failures.
            write_dependencies_record(project_dir)
            LOGGER.warning(
                "[deps] Finished, but %s could not run (a file of SAIVerse is missing); "
                "the screen shows it",
                ", ".join(failure.step for failure in failures),
            )
        return 0
    clear_dependencies_failure(project_dir)
    write_dependencies_record(project_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical SAIVerse updater")
    parser.add_argument("--manual", action="store_true", help="Update while SAIVerse is stopped")
    parser.add_argument("--config", type=Path, help="Detached updater config path")
    parser.add_argument(
        "--check-complete",
        action="store_true",
        help=(
            "Report whether the last update finished, without changing the code. When an "
            "older updater skipped part of the package installation, install the packages "
            "once more for the code as it is. "
            f"Exit {CHECK_READY}: ready to start. Exit {CHECK_NEEDS_FINISH}: run --manual first. "
            f"Exit {CHECK_INCONCLUSIVE}: could not tell, start anyway. "
            f"Exit {CHECK_DEPENDENCIES_FAILED}: installing the packages once more failed, "
            f"start anyway (the failure is in {DEPENDENCIES_FAILURE_RECORD})."
        ),
    )
    parser.add_argument(
        INSTALL_DEPENDENCIES_OPTION,
        nargs="?",
        const="",
        default=None,
        metavar="REQUIREMENTS_FILE",
        help=(
            "Internal, started by the updater after the code changed: run only the dependency "
            f"step with this file's procedure, from REQUIREMENTS_FILE (default {REQUIREMENTS_LOCK}). "
            "Logs to stdout. Exit 0 when the step finished, 1 on failure. A failed portable "
            f"Node.js or SearXNG step keeps exit 0 and is written to {DEPENDENCIES_FAILURE_RECORD}."
        ),
    )
    parser.add_argument(
        KEEP_PORTABLE_NODE_OPTION,
        action="store_true",
        help=f"Internal, with {INSTALL_DEPENDENCIES_OPTION}: leave the portable Node.js as it is (rollback).",
    )
    parser.add_argument(
        "--switch-channel",
        choices=sorted(CHANNEL_BRANCHES),
        help=(
            "With --manual: switch this checkout to the other release line "
            f"({CHANNEL_EARLY_ACCESS} = the {CHANNEL_BRANCHES[CHANNEL_EARLY_ACCESS]} branch, "
            f"{CHANNEL_STABLE} = {CHANNEL_BRANCHES[CHANNEL_STABLE]}) instead of updating it. "
            "The UI passes the same request through the detached config."
        ),
    )
    args = parser.parse_args(argv)

    project_dir = Path(__file__).resolve().parent.parent
    if args.install_dependencies is not None:
        return install_dependencies_main(
            project_dir,
            args.install_dependencies or None,
            replace_portable_node=not args.keep_portable_node,
        )
    if args.check_complete:
        setup_logging(project_dir, to_file=False)
        return check_update_complete(project_dir)

    setup_logging(project_dir)
    config_path = args.config or project_dir / ".update_config.json"
    try:
        if args.switch_channel is not None and not args.manual:
            raise UpdateError(
                "--switch-channel is only accepted with --manual; the UI passes the "
                "channel through the update config"
            )
        config = None if args.manual else _load_config(config_path)
        switch_channel = args.switch_channel if args.manual else _config_switch_channel(config)
        run_update(config, project_dir, switch_channel=switch_channel)
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
