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


def test_legacy_bare_version_marker_demands_the_update_be_finished(tmp_path: Path) -> None:
    """The first marker format recorded VERSION only; one finishing pass fixes it."""
    project = _make_project(tmp_path)
    update_engine.marker_path(project).write_text("0.3.0\n", encoding="utf-8")
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


def test_missing_dependencies_is_unknown_without_a_lock_file(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest>=9.0.0\n", encoding="utf-8")
    assert update_engine.missing_dependencies(tmp_path) is None


# --- what the update installs ------------------------------------------------


def test_update_dependencies_installs_from_the_lock(tmp_path: Path) -> None:
    """The lock is what makes every user's set identical; pip must read it."""
    project = _make_project(tmp_path)
    commands: list[list[str]] = []

    def recording_run(command, *, cwd, label, timeout=900):  # type: ignore[no-untyped-def]
        commands.append(list(command))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(update_engine, "_run", side_effect=recording_run), patch.object(
        update_engine.shutil, "which", return_value="npm"
    ):
        update_engine.update_dependencies(project, "python")

    assert commands[0] == ["python", "-m", "pip", "install", "-r", "requirements.lock"]
    assert not any("requirements.txt" in part for command in commands for part in command)
