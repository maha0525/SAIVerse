"""The update-completion marker and the start-time check that reads it.

Background: the v0.2.29 update.bat died right after ``git pull``, leaving new
code beside old packages (docs/issues/v0229_update_bat_truncates_after_git_pull.md).
start.bat / start.sh now ask ``update_engine.py --check-complete`` whether the
last update finished, so the invariant under test is: the marker exists only
when code *and* packages were installed together, and the check never claims a
finished update it did not actually verify.
"""
from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import update_engine

# The lock pins exact versions, so a fixture lock that this interpreter
# satisfies has to name the version that is actually installed.
_INSTALLED_PYTEST = metadata.version("pytest")


def _make_project(tmp_path: Path, version: str = "0.3.0") -> Path:
    """A checkout that a completed update would leave behind."""
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest>=9.0.0\n", encoding="utf-8")
    (tmp_path / "requirements.lock").write_text(
        f"pytest=={_INSTALLED_PYTEST}\n    # via -r requirements.txt\n", encoding="utf-8"
    )
    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    (frontend / "package-lock.json").write_text('{"name": "saiverse"}\n', encoding="utf-8")
    return tmp_path


def _recorded_version(project: Path) -> str | None:
    marker = update_engine.read_completion_marker(project)
    return None if marker is None else marker.get("version")


# --- the marker is written only when an update actually completed -----------


def test_manual_update_records_the_version_it_installed(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "create_pre_update_snapshot", return_value="snap"), patch.object(
        update_engine, "update_code"
    ), patch.object(update_engine, "update_dependencies"):
        update_engine.run_update(None, project)

    assert _recorded_version(project) == "0.3.0"


def test_dependency_failure_leaves_no_marker(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "create_pre_update_snapshot", return_value="snap"), patch.object(
        update_engine, "update_code"
    ), patch.object(
        update_engine, "update_dependencies", side_effect=update_engine.UpdateError("pip failed")
    ), patch.object(update_engine, "_rollback_code_and_dependencies"):
        try:
            update_engine.run_update(None, project)
        except update_engine.UpdateError:
            pass
        else:  # pragma: no cover - the update must not report success
            raise AssertionError("run_update swallowed the dependency failure")

    assert not update_engine.marker_path(project).exists()


def test_code_pull_failure_leaves_the_previous_marker_untouched(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text(
        json.dumps({"version": "0.2.30"}), encoding="utf-8"
    )
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "create_pre_update_snapshot", return_value="snap"), patch.object(
        update_engine, "update_code", side_effect=update_engine.UpdateError("pull failed")
    ):
        try:
            update_engine.run_update(None, project)
        except update_engine.UpdateError:
            pass
        else:  # pragma: no cover
            raise AssertionError("run_update swallowed the pull failure")

    assert _recorded_version(project) == "0.2.30"


def test_detached_update_records_only_after_a_healthy_restart(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    config = {"venv_python": "python", "main_pid": 10, "main_process_created_at": 1.0}
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "wait_for_owned_process_exit"), patch.object(
        update_engine, "create_pre_update_snapshot", return_value="snap"
    ), patch.object(update_engine, "update_code"), patch.object(
        update_engine, "update_dependencies"
    ), patch.object(
        update_engine, "restart_application", return_value=MagicMock(pid=1)
    ), patch.object(
        update_engine, "wait_for_healthy_restart", return_value={"city_name": "city_a"}
    ):
        update_engine.run_update(config, project)

    assert _recorded_version(project) == "0.3.0"


def test_failed_restart_leaves_no_marker(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    config = {"venv_python": "python", "main_pid": 10, "main_process_created_at": 1.0}
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "wait_for_owned_process_exit"), patch.object(
        update_engine, "create_pre_update_snapshot", return_value="snap"
    ), patch.object(update_engine, "update_code"), patch.object(
        update_engine, "update_dependencies"
    ), patch.object(
        update_engine, "restart_application", return_value=MagicMock(pid=1)
    ), patch.object(
        update_engine,
        "wait_for_healthy_restart",
        side_effect=update_engine.UpdateError("health check failed"),
    ), patch.object(update_engine, "_terminate_spawned"), patch.object(
        update_engine, "_rollback_code_and_dependencies"
    ):
        try:
            update_engine.run_update(config, project)
        except update_engine.UpdateError:
            pass
        else:  # pragma: no cover
            raise AssertionError("run_update swallowed the restart failure")

    assert not update_engine.marker_path(project).exists()


def test_a_failed_update_whose_rollback_also_fails_leaves_no_marker(tmp_path: Path) -> None:
    """The stale-marker trap: pip dies, the rollback dies, the code is back at the
    old revision -- so the previous marker's fingerprint would match again and the
    next start would read READY over a half-installed environment."""
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "create_pre_update_snapshot", return_value="snap"), patch.object(
        update_engine, "update_code"
    ), patch.object(
        update_engine, "update_dependencies", side_effect=update_engine.UpdateError("pip failed")
    ), patch.object(
        update_engine,
        "_rollback_code_and_dependencies",
        side_effect=update_engine.UpdateError("rollback failed"),
    ):
        try:
            update_engine.run_update(None, project)
        except update_engine.UpdateError:
            pass
        else:  # pragma: no cover
            raise AssertionError("run_update swallowed the failure")

    assert not update_engine.marker_path(project).exists()
    # And the next start now judges the environment instead of trusting the
    # marker: with packages actually missing it demands the update be finished,
    # which the stale marker used to short-circuit into READY.
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport(["mcp"], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH


def test_a_failed_detached_update_whose_rollback_also_fails_leaves_no_marker(
    tmp_path: Path,
) -> None:
    """Same trap on the detached path, which restarts the app instead of returning."""
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    config = {"venv_python": "python", "main_pid": 10, "main_process_created_at": 1.0}
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "wait_for_owned_process_exit"), patch.object(
        update_engine, "create_pre_update_snapshot", return_value="snap"
    ), patch.object(update_engine, "update_code"), patch.object(
        update_engine, "update_dependencies", side_effect=update_engine.UpdateError("pip failed")
    ), patch.object(
        update_engine,
        "_rollback_code_and_dependencies",
        side_effect=update_engine.UpdateError("rollback failed"),
    ):
        try:
            update_engine.run_update(config, project)
        except update_engine.UpdateError:
            pass
        else:  # pragma: no cover
            raise AssertionError("run_update swallowed the failure")

    assert not update_engine.marker_path(project).exists()


def test_an_unremovable_marker_does_not_abort_the_update(tmp_path: Path) -> None:
    """Losing the invalidation is a weaker next start, not a reason to stop updating."""
    project = _make_project(tmp_path, version="0.4.0")
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "create_pre_update_snapshot", return_value="snap"), patch.object(
        update_engine, "update_code"
    ), patch.object(update_engine, "update_dependencies"), patch.object(
        update_engine.Path, "unlink", side_effect=OSError("locked")
    ):
        update_engine.run_update(None, project)  # must not raise

    assert _recorded_version(project) == "0.4.0"


# --- how the marker is written ----------------------------------------------


def test_marker_is_written_through_a_temporary_file(tmp_path: Path) -> None:
    """A crash mid-write must not leave a half-written marker behind."""
    project = _make_project(tmp_path)
    replaced: list[tuple[str, str]] = []
    real_replace = update_engine.os.replace

    def recording_replace(src, dst):  # type: ignore[no-untyped-def]
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    with patch.object(update_engine.os, "replace", side_effect=recording_replace):
        update_engine.write_completion_marker(project)

    assert len(replaced) == 1
    source, destination = replaced[0]
    assert source != destination
    assert destination == str(update_engine.marker_path(project))
    assert _recorded_version(project) == "0.3.0"


def test_a_failed_marker_write_leaves_no_leftover_temporary_file(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with patch.object(update_engine.os, "replace", side_effect=OSError("disk full")):
        update_engine.write_completion_marker(project)

    assert not update_engine.marker_path(project).exists()
    leftovers = list(project.glob(f"{update_engine.UPDATE_COMPLETE_MARKER}*"))
    assert leftovers == []


def test_a_failed_marker_write_does_not_fail_the_update(tmp_path: Path) -> None:
    """The update itself succeeded; calling it a failure would be a lie."""
    project = _make_project(tmp_path)
    with patch.object(update_engine, "_ensure_portable_git_on_path"), patch.object(
        update_engine, "assert_git_update_ready", return_value="old-head"
    ), patch.object(update_engine, "create_pre_update_snapshot", return_value="snap"), patch.object(
        update_engine, "update_code"
    ), patch.object(update_engine, "update_dependencies"), patch.object(
        update_engine.os, "replace", side_effect=OSError("disk full")
    ):
        update_engine.run_update(None, project)  # must not raise


# --- the start-time check ---------------------------------------------------


def test_matching_marker_is_ready_to_start(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    assert update_engine.check_update_complete(project) == update_engine.CHECK_READY


def test_stale_marker_demands_the_update_be_finished(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    (project / "VERSION").write_text("0.4.0\n", encoding="utf-8")
    assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH


def test_fingerprint_binds_the_marker_to_the_lock(tmp_path: Path) -> None:
    """What pip installs is requirements.lock, so the marker must carry its digest."""
    project = _make_project(tmp_path)
    fingerprint = update_engine.completion_fingerprint(project)
    assert fingerprint is not None
    assert fingerprint["requirements_lock_sha256"] == update_engine._file_digest(
        project / "requirements.lock"
    )
    assert fingerprint["requirements_lock_sha256"] is not None
    # The intent file stays in too: it changing without a regenerated lock is a
    # gap to surface, not to hide.
    assert fingerprint["requirements_sha256"] is not None


def test_changed_lock_demands_the_update_be_finished(tmp_path: Path) -> None:
    """A pull between releases keeps VERSION but can move requirements.lock."""
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    (project / "requirements.lock").write_text(
        f"pytest=={_INSTALLED_PYTEST}\nfeedparser==6.0.12\n", encoding="utf-8"
    )
    assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH


def test_changed_requirements_demand_the_update_be_finished(tmp_path: Path) -> None:
    """The intent file moving without its lock is still a reason to finish."""
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    (project / "requirements.txt").write_text("pytest>=9.0.0\nfeedparser>=6.0\n", encoding="utf-8")
    assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH


def test_changed_package_lock_demands_the_update_be_finished(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    (project / "frontend" / "package-lock.json").write_text(
        '{"name": "saiverse", "version": "2"}\n', encoding="utf-8"
    )
    assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH


def test_no_marker_with_complete_packages_records_and_starts(tmp_path: Path) -> None:
    """A pre-marker install whose packages are fine must not be sent updating."""
    project = _make_project(tmp_path)
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_READY
    assert _recorded_version(project) == "0.3.0"


def test_no_marker_with_missing_packages_demands_the_update_be_finished(tmp_path: Path) -> None:
    """The v0.2.29 half-update: new code, no marker, packages never installed."""
    project = _make_project(tmp_path)
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport(["mcp", "feedparser"], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    assert not update_engine.marker_path(project).exists()


def test_no_marker_with_unparsable_lines_is_inconclusive_not_ready(tmp_path: Path) -> None:
    """Nothing looked missing, but the check was incomplete: record nothing."""
    project = _make_project(tmp_path)
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], ["some ? nonsense"], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_INCONCLUSIVE
    assert not update_engine.marker_path(project).exists()


def test_no_marker_with_a_degraded_parser_is_inconclusive_not_ready(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], True),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_INCONCLUSIVE
    assert not update_engine.marker_path(project).exists()


def test_no_marker_without_node_modules_demands_the_update_be_finished(tmp_path: Path) -> None:
    """npm never ran: the frontend build would fail after starting."""
    project = _make_project(tmp_path)
    (project / "frontend" / "node_modules").rmdir()
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    assert not update_engine.marker_path(project).exists()


# --- a marker that says nothing usable is judged like no marker at all --------
#
# The first marker format was a bare VERSION string. Reading it as "a record that
# does not match" sent a fully updated checkout into the full update (git pull +
# world snapshot + pip + npm), which a 76 GB world could not finish
# (docs/issues/archive/update_marker_format_change_demands_full_update.md). An
# unreadable marker must instead fall through to the same direct verification a
# missing marker gets, and be rewritten in the current format when that passes.


_UNREADABLE_MARKERS = {
    "legacy bare version": "0.3.0\n",
    "damaged json": "{not json\n",
    "json array": "[1, 2]\n",
    "json string": '"0.3.2"\n',
}


def test_legacy_bare_version_marker_with_complete_packages_is_rewritten_and_starts(
    tmp_path: Path,
) -> None:
    """The defect that stranded the owner's machine: the row that must be READY."""
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text("0.3.0\n", encoding="utf-8")
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_READY
    # Rewritten in the current format: the next start takes the cheap comparison.
    marker = update_engine.read_completion_marker(project)
    assert marker == update_engine.completion_fingerprint(project)


def test_legacy_bare_version_marker_with_missing_packages_demands_the_update_be_finished(
    tmp_path: Path,
) -> None:
    """The safe side is kept: an old marker over missing packages still updates."""
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text("0.3.0\n", encoding="utf-8")
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport(["mcp", "feedparser"], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    # Not rewritten: nothing was verified complete.
    assert update_engine.marker_path(project).read_text(encoding="utf-8") == "0.3.0\n"


def test_legacy_bare_version_marker_without_node_modules_demands_the_update_be_finished(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text("0.3.0\n", encoding="utf-8")
    (project / "frontend" / "node_modules").rmdir()
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    assert update_engine.marker_path(project).read_text(encoding="utf-8") == "0.3.0\n"


@pytest.mark.parametrize("content", list(_UNREADABLE_MARKERS.values()), ids=list(_UNREADABLE_MARKERS))
def test_unreadable_marker_reads_back_as_an_empty_record_not_as_no_marker(
    tmp_path: Path, content: str
) -> None:
    """The reader keeps its contract: {} for a file that says nothing, None for no file."""
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text(content, encoding="utf-8")
    assert update_engine.read_completion_marker(project) == {}
    update_engine.marker_path(project).unlink()
    assert update_engine.read_completion_marker(project) is None


@pytest.mark.parametrize("content", list(_UNREADABLE_MARKERS.values()), ids=list(_UNREADABLE_MARKERS))
def test_unreadable_marker_with_complete_packages_is_rewritten_and_starts(
    tmp_path: Path, content: str
) -> None:
    """Damaged JSON and non-object documents take the same road as the bare string."""
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text(content, encoding="utf-8")
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_READY
    assert update_engine.read_completion_marker(project) == update_engine.completion_fingerprint(project)


@pytest.mark.parametrize("content", list(_UNREADABLE_MARKERS.values()), ids=list(_UNREADABLE_MARKERS))
def test_unreadable_marker_with_missing_packages_demands_the_update_be_finished(
    tmp_path: Path, content: str
) -> None:
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text(content, encoding="utf-8")
    with patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport(["mcp"], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    assert update_engine.marker_path(project).read_text(encoding="utf-8") == content


def test_unreadable_marker_is_logged_before_the_direct_verification(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """The log must say why a marker was ignored, so the next reader is not puzzled."""
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text("0.3.0\n", encoding="utf-8")
    with caplog.at_level("INFO", logger="saiverse.update"), patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], False),
    ):
        update_engine.check_update_complete(project)
    assert any("old or unreadable format" in record.getMessage() for record in caplog.records)


def test_a_valid_but_stale_json_marker_still_demands_the_update_be_finished(
    tmp_path: Path, caplog
) -> None:  # type: ignore[no-untyped-def]
    """Only the unreadable marker changed roads; a real mismatch keeps its warning."""
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text(
        json.dumps({"version": "0.2.30"}), encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="saiverse.update"), patch.object(
        update_engine,
        "missing_dependencies",
        return_value=update_engine.DependencyReport([], [], False),
    ):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    assert any(
        "Update was interrupted" in record.getMessage() and "0.2.30" in record.getMessage()
        for record in caplog.records
    )
    assert _recorded_version(project) == "0.2.30"


def test_unverifiable_packages_start_without_recording_a_version(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with patch.object(update_engine, "missing_dependencies", return_value=None):
        assert update_engine.check_update_complete(project) == update_engine.CHECK_READY
    # No marker: the next start must look again rather than trust a guess.
    assert not update_engine.marker_path(project).exists()


def test_unreadable_version_is_inconclusive_not_a_blocked_start(tmp_path: Path) -> None:
    assert update_engine.check_update_complete(tmp_path) == update_engine.CHECK_INCONCLUSIVE


# --- requirements parsing feeding the package check -------------------------


def test_scan_evaluates_environment_markers_instead_of_skipping_them(tmp_path: Path) -> None:
    """A marked line is checked when it applies here and ignored when it does not."""
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "fastapi==0.116.1",
                "mcp>=1.10.0,<2",
                "tzdata",
                "discord.py>=2.4.0  # trailing comment",
                "--index-url https://example.invalid/simple",
                'applies-here; python_version >= "3.0"',
                'not-applicable-here; python_version < "3.0"',
                "somepkg @ https://example.invalid/somepkg.whl",
            ]
        ),
        encoding="utf-8",
    )
    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == [
        "fastapi",
        "mcp",
        "tzdata",
        "discord.py",
        "applies-here",
        "somepkg",
    ]
    assert scan.unparsed == []
    assert scan.degraded is False


def test_scan_follows_one_level_of_includes(tmp_path: Path) -> None:
    (tmp_path / "base.txt").write_text("fastapi==0.116.1\n", encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r base.txt\ntzdata\n", encoding="utf-8")

    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == ["fastapi", "tzdata"]
    assert scan.unparsed == []


def test_scan_reports_an_unreadable_include_as_unchecked(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r absent.txt\ntzdata\n", encoding="utf-8")

    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == ["tzdata"]
    assert scan.unparsed == ["-r absent.txt"]


def test_scan_reports_a_malformed_line_as_unchecked(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("tzdata\n=== not a requirement ===\n", encoding="utf-8")

    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == ["tzdata"]
    assert scan.unparsed == ["=== not a requirement ==="]


def test_scan_without_packaging_falls_back_and_says_so(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        'fastapi==0.116.1\npywin32; sys_platform == "win32"\n', encoding="utf-8"
    )

    with patch.object(update_engine, "_Requirement", None):
        scan = update_engine.scan_requirements(requirements)

    assert scan is not None
    # Without packaging the marker cannot be evaluated, so the line is reported
    # as unchecked rather than required: otherwise a macOS start would see the
    # Windows-only pin as missing and loop through finishing passes forever.
    assert [dist.name for dist in scan.required] == ["fastapi"]
    assert all(dist.specifier is None for dist in scan.required)
    assert scan.unparsed == ['pywin32; sys_platform == "win32"']
    assert scan.degraded is True


def test_scan_skips_pip_options_that_add_no_distribution(tmp_path: Path) -> None:
    """Fetch/build options change where packages come from, not which are needed."""
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "\n".join(
            [
                "--index-url https://example.invalid/simple",
                "--extra-index-url=https://example.invalid/extra",
                "--trusted-host example.invalid",
                "--find-links https://example.invalid/wheels",
                "--no-binary :all:",
                "--only-binary=:all:",
                "--prefer-binary",
                "--pre",
                "--require-hashes",
                "--hash=sha256:0000000000000000000000000000000000000000000000000000000000000000",
                "tzdata",
            ]
        ),
        encoding="utf-8",
    )
    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == ["tzdata"]
    assert scan.unparsed == []


def test_scan_reports_an_editable_install_as_unchecked(tmp_path: Path) -> None:
    """``-e`` installs a distribution this scan can never verify -- not a no-op."""
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("tzdata\n-e .\n--editable=./packages/thing\n", encoding="utf-8")

    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == ["tzdata"]
    assert scan.unparsed == ["-e .", "--editable=./packages/thing"]


def test_scan_reports_a_constraints_file_as_unchecked(tmp_path: Path) -> None:
    """A constraint file changes which versions satisfy the requirements."""
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("tzdata\n-c constraints.txt\n", encoding="utf-8")

    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == ["tzdata"]
    assert scan.unparsed == ["-c constraints.txt"]


def test_scan_reports_an_unknown_option_as_unchecked(tmp_path: Path) -> None:
    """Whether an unrecognised option adds a distribution cannot be decided here."""
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("tzdata\n--some-future-pip-option value\n", encoding="utf-8")

    scan = update_engine.scan_requirements(requirements)
    assert scan is not None
    assert [dist.name for dist in scan.required] == ["tzdata"]
    assert scan.unparsed == ["--some-future-pip-option value"]


def test_an_editable_line_makes_a_markerless_start_inconclusive(tmp_path: Path) -> None:
    """End to end: an unverifiable line must not be recorded as a finished update."""
    project = _make_project(tmp_path)
    (project / "requirements.lock").write_text("pytest\n-e .\n", encoding="utf-8")
    assert update_engine.check_update_complete(project) == update_engine.CHECK_INCONCLUSIVE
    assert not update_engine.marker_path(project).exists()


def test_a_constraints_line_makes_a_markerless_start_inconclusive(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    (project / "requirements.lock").write_text("pytest\n-c constraints.txt\n", encoding="utf-8")
    assert update_engine.check_update_complete(project) == update_engine.CHECK_INCONCLUSIVE
    assert not update_engine.marker_path(project).exists()


def test_an_unknown_option_makes_a_markerless_start_inconclusive(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    (project / "requirements.lock").write_text(
        "pytest\n--some-future-pip-option value\n", encoding="utf-8"
    )
    assert update_engine.check_update_complete(project) == update_engine.CHECK_INCONCLUSIVE
    assert not update_engine.marker_path(project).exists()


def test_harmless_options_do_not_hold_back_a_markerless_start(tmp_path: Path) -> None:
    """The allowlist exists so a private index does not make every start inconclusive."""
    project = _make_project(tmp_path)
    (project / "requirements.lock").write_text(
        "--index-url https://example.invalid/simple\npytest\n", encoding="utf-8"
    )
    assert update_engine.check_update_complete(project) == update_engine.CHECK_READY
    assert _recorded_version(project) == "0.3.0"


def test_markerless_start_judges_packages_against_the_lock_not_the_intent(tmp_path: Path) -> None:
    """requirements.txt may be satisfied while the lock is not; the lock decides."""
    project = _make_project(tmp_path)
    (project / "requirements.txt").write_text("pytest>=1.0\n", encoding="utf-8")
    (project / "requirements.lock").write_text("pytest==0.0.1\n", encoding="utf-8")
    assert update_engine.check_update_complete(project) == update_engine.CHECK_NEEDS_FINISH
    assert not update_engine.marker_path(project).exists()


def test_missing_dependencies_reads_the_lock_and_evaluates_its_markers(tmp_path: Path) -> None:
    """A universal lock lists every platform; only the lines for this one apply.

    Without marker evaluation a Windows machine would be told it lacks a
    Linux-only package (and vice versa) and every start would demand an update.
    """
    (tmp_path / "requirements.lock").write_text(
        "\n".join(
            [
                "# requirements.lock -- generated",
                f"pytest=={_INSTALLED_PYTEST} ; python_version >= '3.0'",
                "    # via -r requirements.txt",
                "saiverse-other-platform-only==1.0 ; sys_platform == 'saiverse-nowhere'",
                "    # via somepkg",
                "",
            ]
        ),
        encoding="utf-8",
    )
    report = update_engine.missing_dependencies(tmp_path)
    assert report is not None
    assert report.missing == []
    assert report.unchecked == []
    assert report.degraded is False


def test_missing_dependencies_reports_absent_distributions(tmp_path: Path) -> None:
    (tmp_path / "requirements.lock").write_text(
        f"pytest=={_INSTALLED_PYTEST}\nsaiverse-not-a-real-package==1.0\n", encoding="utf-8"
    )
    report = update_engine.missing_dependencies(tmp_path)
    assert report is not None
    assert report.missing == ["saiverse-not-a-real-package"]
    assert report.unchecked == []


def test_missing_dependencies_reports_an_installed_but_differently_pinned_distribution(
    tmp_path: Path,
) -> None:
    """A name-only check would call this healthy; the pinned version must apply."""
    (tmp_path / "requirements.lock").write_text("pytest==999.0.0\n", encoding="utf-8")
    report = update_engine.missing_dependencies(tmp_path)
    assert report is not None
    assert len(report.missing) == 1
    assert report.missing[0].startswith("pytest (installed ")


def test_missing_dependencies_reports_unreadable_installed_version_as_unchecked(
    tmp_path: Path,
) -> None:
    """A dist-info whose version cannot be read must not pass as satisfying a pin."""
    (tmp_path / "requirements.lock").write_text("pytest==9.0.0\n", encoding="utf-8")
    with patch.object(update_engine, "_installed_versions", return_value={"pytest": None}):
        report = update_engine.missing_dependencies(tmp_path)
    assert report is not None
    assert report.missing == []
    assert report.unchecked == ["pytest (installed version unreadable, required ==9.0.0)"]


def test_missing_dependencies_is_unknown_without_a_lock_file(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest>=9.0.0\n", encoding="utf-8")
    assert update_engine.missing_dependencies(tmp_path) is None


# --- what the update installs ------------------------------------------------


def test_update_dependencies_installs_from_the_lock(tmp_path: Path) -> None:
    """The lock is what makes every user's set identical; pip must read it."""
    project = _make_project(tmp_path)
    commands: list[list[str]] = []

    def recording_run(command, *, cwd, label, timeout=900, check=True):  # type: ignore[no-untyped-def]
        commands.append(list(command))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(update_engine, "_run", side_effect=recording_run), patch.object(
        update_engine.shutil, "which", return_value="npm"
    ):
        update_engine.update_dependencies(project, "python")

    assert commands[0] == ["python", "-m", "pip", "install", "-r", "requirements.lock"]
    assert not any("requirements.txt" in part for command in commands for part in command)


def test_update_dependencies_never_falls_back_when_the_lock_is_missing(tmp_path: Path) -> None:
    """A checkout without a lock is a broken update target. pip must still be
    told to read the lock and fail loudly -- the legacy file is only for the
    rollback path, which asks for it explicitly."""
    project = _make_project(tmp_path)
    (project / "requirements.lock").unlink()
    commands: list[list[str]] = []

    def recording_run(command, *, cwd, label, timeout=900, check=True):  # type: ignore[no-untyped-def]
        commands.append(list(command))
        if command[:5] == ["python", "-m", "pip", "install", "-r"]:
            raise update_engine.UpdateError(f"{label} failed with exit 1: no such file {command[-1]}")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(update_engine, "_run", side_effect=recording_run), patch.object(
        update_engine.shutil, "which", return_value="npm"
    ):
        with pytest.raises(update_engine.UpdateError, match="requirements.lock"):
            update_engine.update_dependencies(project, "python")

    assert commands == [["python", "-m", "pip", "install", "-r", "requirements.lock"]]


# --- pip check after the lock is installed: visibility, not enforcement --------


def _fake_run_with_pip_check(returncode: int, stdout: str, stderr: str = ""):  # type: ignore[no-untyped-def]
    """``_run`` that answers ``pip check`` with the given result and 0 to the rest."""
    calls: list[dict] = []

    def fake_run(command, *, cwd, label, timeout=900, check=True):  # type: ignore[no-untyped-def]
        calls.append({"command": list(command), "check": check})
        if command[-2:] == ["pip", "check"]:
            if check and returncode != 0:
                raise update_engine.UpdateError("pip check must be run with check=False")
            return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)
        return MagicMock(returncode=0, stdout="", stderr="")

    return fake_run, calls


def test_update_dependencies_runs_pip_check_after_the_lock(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """A clean environment: pip check runs right after the install and logs nothing."""
    project = _make_project(tmp_path)
    fake_run, calls = _fake_run_with_pip_check(0, "No broken requirements found.\n")

    with caplog.at_level("WARNING", logger="saiverse.update"), patch.object(
        update_engine, "_run", side_effect=fake_run
    ), patch.object(update_engine.shutil, "which", return_value="npm"):
        update_engine.update_dependencies(project, "python")

    commands = [call["command"] for call in calls]
    assert commands[0][-3:] == ["install", "-r", "requirements.lock"]
    assert commands[1] == ["python", "-m", "pip", "check"]
    assert not any("[deps]" in record.getMessage() for record in caplog.records)


def test_update_dependencies_logs_addon_conflicts_without_failing(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """The 2026-09-02 shape: the lock moved numpy, an addon's numba now conflicts.

    pip check exits 1. The update must still complete (addon consistency is the
    addon's job) and every conflict line must be in the log with the fixed prefix.
    """
    project = _make_project(tmp_path)
    conflict_lines = [
        "numba 0.61.0 has requirement numpy<2.5,>=1.24, but you have numpy 2.5.2.",
        "librosa 0.10.2 has requirement soundfile>=0.12.1, but you have soundfile 0.11.0.",
    ]
    fake_run, calls = _fake_run_with_pip_check(1, "\n".join(conflict_lines) + "\n")

    with caplog.at_level("WARNING", logger="saiverse.update"), patch.object(
        update_engine, "_run", side_effect=fake_run
    ), patch.object(update_engine.shutil, "which", return_value="npm"):
        update_engine.update_dependencies(project, "python")  # must not raise

    # npm still ran after the conflicts were reported: the update went on.
    assert calls[-1]["command"][0] == "npm"
    messages = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    for line in conflict_lines:
        assert f"[deps] pip check: {line}" in messages
    summary = [m for m in messages if "アドオンを入れ直す" in m]
    assert len(summary) == 1
    assert "requirements.lock" in summary[0]


def test_pip_check_crash_is_not_reported_as_addon_conflicts(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Only exit 1 means "conflicts found". Exit 2 (usage / internal error) means
    pip did not check anything: the log must say so and must not dress the
    traceback up as an addon problem. The update still completes."""
    project = _make_project(tmp_path)
    fake_run, calls = _fake_run_with_pip_check(
        2, "", stderr="Traceback (most recent call last):\n  ...\nKeyError: 'metadata'\n"
    )

    with caplog.at_level("WARNING", logger="saiverse.update"), patch.object(
        update_engine, "_run", side_effect=fake_run
    ), patch.object(update_engine.shutil, "which", return_value="npm"):
        update_engine.update_dependencies(project, "python")  # must not raise

    assert calls[-1]["command"][0] == "npm"
    messages = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    could_not_run = [m for m in messages if m.startswith("[deps] pip check could not run (exit 2)")]
    assert len(could_not_run) == 1
    assert "addon conflicts were not checked" in could_not_run[0]
    assert "KeyError: 'metadata'" in could_not_run[0]
    assert not any(m.startswith("[deps] pip check: ") for m in messages)
    assert not any("アドオンを入れ直す" in m for m in messages)
