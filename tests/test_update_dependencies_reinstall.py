"""The one-time package reinstall at start, and its failure in the banner.

docs/intent/update_completes_with_new_code.md sections 2 and 6. An update
finished by an older updater writes a ``.update_complete`` that matches the
code, but its dependency step was the older one, so steps added since never
ran. ``--check-complete`` tells that case by the missing dependency record and
runs the dependency step of the engine on disk once, without fetching code:

1. such a checkout installs once, gets the record, and keeps its VERSION;
2. the next start does nothing;
3. a new install (no ``.update_complete``) is only recorded, never installed;
4. a failed reinstall does not stop the start, is written to a file the
   backend shows in the banner, and the next success removes it;
5. a failed portable Node.js or SearXNG step inside the dependency step does
   not fail it (exit 0), but leaves no record and writes the failure, so the
   banner names the step and the next start runs the dependency step again.

No real pip / npm / PowerShell runs: the child process is replaced by a fake
``_run`` that calls the child's entrance (``install_dependencies_main``) in
this process, with ``update_dependencies`` mocked (1-4) or real with every
command it runs answered by label (5).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from importlib import metadata
import os
from pathlib import Path
import sys
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from saiverse.update_dependencies_alert import ALERT_ID, update_dependencies_alerts
from scripts import update_engine

_INSTALLED_PYTEST = metadata.version("pytest")



# The real check, before the autouse fixture below replaces it in every test.
_REAL_SAIVERSE_MAY_BE_RUNNING = update_engine._saiverse_may_be_running

@pytest.fixture(autouse=True)
def _saiverse_stopped():  # type: ignore[no-untyped-def]
    """The reinstall waits while SAIVerse may be running; answer "stopped"
    instead of reading this machine's runtime markers."""
    with patch.object(update_engine, "_saiverse_may_be_running", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_running_frontend():  # type: ignore[no-untyped-def]
    """The reinstall refuses while a frontend runs; answer "none running"
    instead of listing this machine's processes."""
    with patch.object(
        update_engine, "find_frontend_servers", return_value=update_engine.FrontendServers([], None)
    ):
        yield


def _make_project(tmp_path: Path, version: str = "0.3.26") -> Path:
    """A checkout whose code and packages match (see test_update_completion_marker)."""
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest>=9.0.0\n", encoding="utf-8")
    (tmp_path / "requirements.lock").write_text(f"pytest=={_INSTALLED_PYTEST}\n", encoding="utf-8")
    frontend = tmp_path / "frontend"
    (frontend / "node_modules" / "next").mkdir(parents=True)
    (frontend / "node_modules" / "next" / "package.json").write_text("{}\n", encoding="utf-8")
    (frontend / "package-lock.json").write_text('{"name": "saiverse"}\n', encoding="utf-8")
    (frontend / "package.json").write_text(json.dumps({"dependencies": {"next": "^16"}}), encoding="utf-8")
    # The engine on disk is the one that runs the child.
    engine = tmp_path / update_engine.ENGINE_SCRIPT
    engine.parent.mkdir(parents=True)
    engine.write_text(Path(update_engine.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _updated_by_an_older_updater(tmp_path: Path) -> Path:
    """v0.3.24 updating to this code: the parent wrote a matching marker, and
    its own (older) dependency step left no dependency record."""
    project = _make_project(tmp_path)
    update_engine.write_completion_marker(project)
    return project


class _Child:
    """Stands in for the ``--install-dependencies`` child process."""

    def __init__(self, project: Path, *, fail: bool = False) -> None:
        self.project = project
        self.fail = fail
        self.commands: list[list[str]] = []
        self.installs = 0

    def run(self, command, *, cwd, label, timeout=900, check=True, encoding=None, env=None):  # type: ignore[no-untyped-def]
        self.commands.append(list(command))
        assert command[1:3] == [str(self.project / update_engine.ENGINE_SCRIPT), "--install-dependencies"]

        def install(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.installs += 1
            if self.fail:
                raise update_engine.UpdateError("install Python dependencies failed with exit 1: network down")
            return []

        with patch.object(update_engine, "update_dependencies", side_effect=install), patch.object(
            update_engine, "setup_logging"
        ):
            code = update_engine.install_dependencies_main(self.project, None)
        return MagicMock(returncode=code, stdout="", stderr="network down" if code else "")

    def check(self) -> int:
        with patch.object(update_engine, "_run", side_effect=self.run), patch.object(
            update_engine, "update_code"
        ) as code, patch.object(update_engine, "create_pre_update_snapshot") as snapshot:
            result = update_engine.check_update_complete(self.project)
        # No release is fetched and no world snapshot is taken: only packages.
        code.assert_not_called()
        snapshot.assert_not_called()
        return result


# --- 1. a checkout updated by an older updater installs once -----------------------


def test_an_older_updaters_checkout_installs_once_and_keeps_its_version(tmp_path: Path) -> None:
    project = _updated_by_an_older_updater(tmp_path)
    child = _Child(project)

    assert child.check() == update_engine.CHECK_READY

    assert child.installs == 1
    assert update_engine.read_dependencies_record(project) == update_engine.completion_fingerprint(project)
    assert update_engine.read_version(project) == "0.3.26"
    # The marker is the parent's record and is left as it was.
    assert update_engine.read_completion_marker(project) == update_engine.completion_fingerprint(project)


def test_the_reinstall_runs_the_engine_on_disk_with_this_interpreter(tmp_path: Path) -> None:
    project = _updated_by_an_older_updater(tmp_path)
    child = _Child(project)
    child.check()
    assert child.commands == [
        [update_engine.sys.executable, str(project / update_engine.ENGINE_SCRIPT), "--install-dependencies"],
    ]


def test_a_record_for_other_code_counts_as_missing(tmp_path: Path) -> None:
    """A record left by an earlier version does not cover this one."""
    project = _updated_by_an_older_updater(tmp_path)
    stale = dict(update_engine.completion_fingerprint(project) or {}, version="0.3.25")
    update_engine.dependencies_record_path(project).write_text(json.dumps(stale), encoding="utf-8")
    child = _Child(project)
    assert child.check() == update_engine.CHECK_READY
    assert child.installs == 1


# --- 2. the next start does nothing ------------------------------------------------


def test_the_next_start_does_nothing(tmp_path: Path) -> None:
    project = _updated_by_an_older_updater(tmp_path)
    _Child(project).check()

    again = _Child(project)
    assert again.check() == update_engine.CHECK_READY
    assert again.installs == 0
    assert again.commands == []


# --- 3. a new install is recorded without installing -----------------------------------


def test_a_new_install_is_recorded_without_installing(tmp_path: Path) -> None:
    project = _make_project(tmp_path)  # setup.bat leaves no .update_complete
    child = _Child(project)

    assert child.check() == update_engine.CHECK_READY

    assert child.installs == 0
    assert child.commands == []
    assert update_engine.read_dependencies_record(project) == update_engine.completion_fingerprint(project)
    # And the start after that compares cheaply and does nothing either.
    assert _Child(project).check() == update_engine.CHECK_READY


def test_a_new_install_with_missing_packages_is_not_recorded(tmp_path: Path) -> None:
    """The existing verification decides; the record is written only when it passes."""
    project = _make_project(tmp_path)
    with patch.object(
        update_engine, "missing_dependencies", return_value=update_engine.DependencyReport(["mcp"], [], False)
    ):
        assert _Child(project).check() == update_engine.CHECK_NEEDS_FINISH
    assert update_engine.read_dependencies_record(project) is None


def test_a_failed_record_write_on_a_new_install_writes_no_marker(tmp_path: Path) -> None:
    """A marker without the record is how a checkout finished by an older
    updater looks; the next start would install every package again."""
    project = _make_project(tmp_path)
    record = update_engine.dependencies_record_path(project)
    real_write = update_engine._write_json_atomically

    def refuse_the_record(target, document):  # type: ignore[no-untyped-def]
        if target == record:
            raise PermissionError(13, "denied")
        return real_write(target, document)

    with patch.object(update_engine, "_write_json_atomically", side_effect=refuse_the_record):
        assert _Child(project).check() == update_engine.CHECK_READY
    assert update_engine.read_completion_marker(project) is None
    assert update_engine.read_dependencies_record(project) is None

    # The next start verifies again and records both, without installing.
    child = _Child(project)
    assert child.check() == update_engine.CHECK_READY
    assert child.installs == 0
    assert update_engine.read_dependencies_record(project) == update_engine.completion_fingerprint(project)
    assert update_engine.read_completion_marker(project) == update_engine.completion_fingerprint(project)


def test_a_new_install_writes_the_record_before_the_marker(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    record_there: list[bool] = []
    real_marker = update_engine.write_completion_marker

    def marker(project_dir):  # type: ignore[no-untyped-def]
        record_there.append(update_engine.read_dependencies_record(project_dir) is not None)
        real_marker(project_dir)

    with patch.object(update_engine, "write_completion_marker", side_effect=marker):
        assert _Child(project).check() == update_engine.CHECK_READY
    assert record_there == [True]


def test_the_record_writer_reports_whether_it_wrote(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    assert update_engine.write_dependencies_record(project) is True
    with patch.object(update_engine, "_write_json_atomically", side_effect=OSError("disk full")):
        assert update_engine.write_dependencies_record(project) is False
    (project / "VERSION").unlink()
    assert update_engine.write_dependencies_record(project) is False


# --- an npm failure in the reinstall is retried by the reinstall, never by --manual -------


def _empty_node_modules(project: Path) -> None:
    """What a failed npm ci leaves: node_modules without the packages."""
    (project / "frontend" / "node_modules" / "next" / "package.json").unlink()


def test_missing_frontend_packages_after_a_failed_reinstall_run_the_reinstall_again(tmp_path: Path) -> None:
    """The first try's npm ci emptied node_modules and failed (exit 12). The next
    start must not go to the full update (exit 10 -> --manual), which can fetch
    a newer release; it installs the packages of this version once more."""
    project = _updated_by_an_older_updater(tmp_path)
    assert _Child(project, fail=True).check() == update_engine.CHECK_DEPENDENCIES_FAILED
    _empty_node_modules(project)

    again = _Child(project, fail=True)
    assert again.check() == update_engine.CHECK_DEPENDENCIES_FAILED  # not CHECK_NEEDS_FINISH
    assert again.installs == 1

    retry = _Child(project)
    assert retry.check() == update_engine.CHECK_READY
    assert retry.installs == 1
    assert update_engine.read_version(project) == "0.3.26"


def test_missing_frontend_packages_with_a_matching_record_and_a_failure_run_the_reinstall(tmp_path: Path) -> None:
    """The record matches (a step that cannot be retried left it, with its
    failure), and node_modules was emptied since: the reinstall, not --manual."""
    project = _updated_by_an_older_updater(tmp_path)
    update_engine.write_dependencies_record(project)
    update_engine.write_dependencies_failure(project, "npm ci failed", steps=["portable_node"])
    _empty_node_modules(project)
    child = _Child(project)
    assert child.check() == update_engine.CHECK_READY
    assert child.installs == 1


def test_missing_frontend_packages_after_a_finished_step_still_need_the_full_update(tmp_path: Path) -> None:
    project = _updated_by_an_older_updater(tmp_path)
    update_engine.write_dependencies_record(project)
    _empty_node_modules(project)
    child = _Child(project)
    assert child.check() == update_engine.CHECK_NEEDS_FINISH
    assert child.installs == 0


def test_the_launchers_say_what_to_do_when_the_screen_does_not_open() -> None:
    root = Path(update_engine.__file__).resolve().parent.parent
    bat = (root / "start.bat").read_text(encoding="ascii")
    block = bat[bat.index("\n:dependencies_failed"):bat.index("\n:finish_update")]
    assert "does not open" in block and "internet" in block and "start.bat again" in block
    sh = (root / "start.sh").read_text(encoding="utf-8")
    block = sh[sh.index('"$UPDATE_CHECK_RC" -eq 12'):]
    assert "does not open" in block and "start.sh again" in block


# --- 4. a failure does not stop the start and is shown ---------------------------------


def test_a_failed_reinstall_starts_anyway_and_is_written_for_the_banner(tmp_path: Path) -> None:
    project = _updated_by_an_older_updater(tmp_path)
    child = _Child(project, fail=True)

    assert child.check() == update_engine.CHECK_DEPENDENCIES_FAILED

    # Not recorded as done, so the next start tries again.
    assert update_engine.read_dependencies_record(project) is None
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None
    assert failure["version"] == "0.3.26"
    assert "network down" in failure["error"]

    alerts = update_dependencies_alerts(project)
    assert [alert["id"] for alert in alerts] == [ALERT_ID]
    alert = alerts[0]
    assert alert["level"] == "warning"
    assert "start.bat" in alert["message"]
    assert "network down" in alert["details"]["error"]


def test_the_next_successful_reinstall_removes_the_banner(tmp_path: Path) -> None:
    project = _updated_by_an_older_updater(tmp_path)
    _Child(project, fail=True).check()
    assert update_dependencies_alerts(project) != []

    retry = _Child(project)
    assert retry.check() == update_engine.CHECK_READY
    assert retry.installs == 1
    assert update_engine.read_dependencies_failure(project) is None
    assert update_dependencies_alerts(project) == []


def test_a_successful_update_removes_the_banner_too(tmp_path: Path) -> None:
    """Any finished dependency step of this engine resolves the failure,
    including the one inside the next update."""
    project = _make_project(tmp_path)
    update_engine.write_dependencies_failure(project, "network down")
    with patch.object(update_engine, "update_dependencies", return_value=[]), patch.object(
        update_engine, "setup_logging"
    ):
        assert update_engine.install_dependencies_main(project, None) == 0
    assert update_dependencies_alerts(project) == []


def test_the_reinstall_refuses_while_the_frontend_runs(tmp_path: Path) -> None:
    """npm ci beside a running frontend would empty node_modules on Windows."""
    project = _updated_by_an_older_updater(tmp_path)
    running = update_engine.FrontendServers([MagicMock(pid=1, **{"name.return_value": "node.exe"})], "start")
    child = _Child(project)
    with patch.object(update_engine, "find_frontend_servers", return_value=running):
        assert child.check() == update_engine.CHECK_DEPENDENCIES_FAILED
    assert child.commands == []
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None and "Frontend" in failure["error"]


def test_a_damaged_failure_file_still_shows_a_banner(tmp_path: Path) -> None:
    update_engine.dependencies_failure_path(tmp_path).write_text("{not json", encoding="utf-8")
    alerts = update_dependencies_alerts(tmp_path)
    assert [alert["id"] for alert in alerts] == [ALERT_ID]
    assert alerts[0]["details"] == {}


def test_no_failure_file_means_no_banner(tmp_path: Path) -> None:
    assert update_dependencies_alerts(tmp_path) == []


# --- 5. a failed portable Node.js / SearXNG step inside the dependency step -----------


_NODE_LABEL = "refresh portable Node.js"
_SEARXNG_SETUP_LABEL = "reinstall SearXNG"


def _windows_checkout_with_node_and_searxng(tmp_path: Path) -> Path:
    """A checkout with a portable Node.js and SearXNG installed, so both steps run."""
    project = _make_project(tmp_path)
    (project / ".node").mkdir()
    (project / ".node" / "node.exe").write_bytes(b"")
    (project / update_engine.PORTABLE_NODE_INSTALLER).write_text("# stand-in\n", encoding="utf-8")
    (project / update_engine.SEARXNG_INSTALLER).write_text("# stand-in\n", encoding="utf-8")
    venv_python = project / update_engine.SEARXNG_DEFAULT_VENV / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    return project


class _Machine:
    """Answers every command of the real ``update_dependencies`` by label.

    The ``--install-dependencies`` child runs in this process
    (``install_dependencies_main``); the installer of the portable Node.js
    exits ``node_exit``, SearXNG always differs from its pin and its setup
    exits ``searxng_exit``, everything else (pip, npm) exits 0.
    """

    def __init__(self, project: Path, *, node_exit: int = 0, searxng_exit: int = 0) -> None:
        self.project = project
        self.node_exit = node_exit
        self.searxng_exit = searxng_exit
        self.labels: list[str] = []
        self.children = 0

    def run(self, command, *, cwd, label, timeout=900, check=True, encoding=None, env=None):  # type: ignore[no-untyped-def]
        self.labels.append(label)
        if command[1:3] == [str(self.project / update_engine.ENGINE_SCRIPT), "--install-dependencies"]:
            self.children += 1
            code = update_engine.install_dependencies_main(self.project, None)
            return MagicMock(returncode=code, stdout="", stderr="")
        if label == _NODE_LABEL:
            stdout = "[ERROR] download failed\n" if self.node_exit else "[OK] done\n"
            return MagicMock(returncode=self.node_exit, stdout=stdout, stderr="")
        if label == "check SearXNG":
            return MagicMock(returncode=1, stdout="", stderr="")
        if label == _SEARXNG_SETUP_LABEL:
            stderr = "[ERROR] SearXNG: github unreachable" if self.searxng_exit else ""
            return MagicMock(returncode=self.searxng_exit, stdout="", stderr=stderr)
        return MagicMock(returncode=0, stdout="", stderr="")

    @contextmanager
    def commands(self) -> Iterator[None]:
        with patch.object(update_engine, "_run", side_effect=self.run), patch.object(
            update_engine, "_is_windows", return_value=True
        ), patch.object(update_engine.shutil, "which", return_value="npm"), patch.object(
            update_engine, "setup_logging"
        ):
            yield

    def install(self) -> int:
        """The child as the update starts it (after the code moved)."""
        with self.commands():
            return update_engine.install_dependencies_main(self.project, None)

    def check(self) -> int:
        """``--check-complete`` at the next start."""
        with self.commands():
            return update_engine.check_update_complete(self.project)


@pytest.fixture
def _default_searxng_venv(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SEARXNG_VENV_DIR", raising=False)


@pytest.mark.usefixtures("_default_searxng_venv")
def test_a_failed_node_replacement_finishes_the_step_without_a_record(tmp_path: Path) -> None:
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    machine = _Machine(project, node_exit=1)

    # The update itself goes on: the child exits 0, and npm ci still ran.
    assert machine.install() == 0
    assert "npm ci" in machine.labels

    assert update_engine.read_dependencies_record(project) is None
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None
    assert failure["steps"] == [update_engine.DEPENDENCY_STEP_PORTABLE_NODE]
    assert "could not be replaced (exit 1)" in failure["error"]
    assert "download failed" in failure["error"]


@pytest.mark.usefixtures("_default_searxng_venv")
def test_a_failed_searxng_reinstall_finishes_the_step_without_a_record(tmp_path: Path) -> None:
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    machine = _Machine(project, searxng_exit=1)

    assert machine.install() == 0

    assert update_engine.read_dependencies_record(project) is None
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None
    assert failure["steps"] == [update_engine.DEPENDENCY_STEP_SEARXNG]
    assert "github unreachable" in failure["error"]


@pytest.mark.usefixtures("_default_searxng_venv")
def test_both_failed_steps_are_recorded(tmp_path: Path) -> None:
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    assert _Machine(project, node_exit=1, searxng_exit=1).install() == 0
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None
    assert failure["steps"] == [update_engine.DEPENDENCY_STEP_PORTABLE_NODE, update_engine.DEPENDENCY_STEP_SEARXNG]
    assert "download failed" in failure["error"] and "github unreachable" in failure["error"]


@pytest.mark.usefixtures("_default_searxng_venv")
def test_the_banner_names_the_failed_step(tmp_path: Path) -> None:
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    _Machine(project, node_exit=1).install()

    alerts = update_dependencies_alerts(project)
    assert [alert["id"] for alert in alerts] == [ALERT_ID]
    alert = alerts[0]
    assert alert["details"]["reason"] == ["同梱の Node.js の入れ替え"]
    assert "download failed" in alert["details"]["error"]
    assert "start.bat" in alert["message"] and "start.sh" in alert["message"]


def test_the_banner_knows_every_step_name_of_the_engine() -> None:
    """The alert holds the step names as strings so it need not import the engine."""
    from saiverse import update_dependencies_alert

    for step in (
        update_engine.DEPENDENCY_STEP_PORTABLE_NODE,
        update_engine.DEPENDENCY_STEP_SEARXNG,
        update_engine.DEPENDENCY_STEP_ADDON_ENVS,
    ):
        assert step in update_dependencies_alert._STEP_LABELS


def test_a_whole_reinstall_failure_is_named_as_such_in_the_banner(tmp_path: Path) -> None:
    update_engine.write_dependencies_failure(tmp_path, "npm ci failed")
    alert = update_dependencies_alerts(tmp_path)[0]
    assert alert["details"]["reason"] == ["パッケージの入れ替え (起動前のやり直し)"]


@pytest.mark.usefixtures("_default_searxng_venv")
def test_the_next_start_runs_the_dependency_step_again_and_clears_the_banner(tmp_path: Path) -> None:
    """The button update: the child's Node.js step fails, the parent writes
    the marker. The next start.bat sees the missing record and runs the
    dependency step again; once it fully succeeds the record is written and
    the failure is gone."""
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    _Machine(project, node_exit=1).install()
    update_engine.write_completion_marker(project)  # the parent finishes the update

    retry = _Machine(project)
    assert retry.check() == update_engine.CHECK_READY
    assert retry.children == 1
    assert _NODE_LABEL in retry.labels

    assert update_engine.read_dependencies_record(project) == update_engine.completion_fingerprint(project)
    assert update_engine.read_dependencies_failure(project) is None
    assert update_dependencies_alerts(project) == []
    # And the start after that does nothing.
    again = _Machine(project)
    assert again.check() == update_engine.CHECK_READY
    assert again.children == 0


@pytest.mark.usefixtures("_default_searxng_venv")
def test_a_retry_that_fails_again_reports_failure_and_retries_at_the_next_start(tmp_path: Path) -> None:
    """Exit 12 makes start.bat / start.sh warn; nothing is recorded, so the
    start after that tries once more."""
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    _Machine(project, searxng_exit=1).install()
    update_engine.write_completion_marker(project)

    retry = _Machine(project, searxng_exit=1)
    assert retry.check() == update_engine.CHECK_DEPENDENCIES_FAILED
    assert retry.children == 1
    assert update_engine.read_dependencies_record(project) is None
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None and failure["steps"] == [update_engine.DEPENDENCY_STEP_SEARXNG]

    third = _Machine(project)
    assert third.check() == update_engine.CHECK_READY
    assert third.children == 1
    assert update_engine.read_dependencies_failure(project) is None


@pytest.mark.usefixtures("_default_searxng_venv")
def test_without_failures_the_step_is_recorded_as_before(tmp_path: Path) -> None:
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    update_engine.write_dependencies_failure(project, "network down")  # from an earlier run
    machine = _Machine(project)

    assert machine.install() == 0

    assert _NODE_LABEL in machine.labels and _SEARXNG_SETUP_LABEL in machine.labels
    assert update_engine.read_dependencies_record(project) == update_engine.completion_fingerprint(project)
    assert update_engine.read_dependencies_failure(project) is None


# --- what the full update leaves --------------------------------------------------------


def test_the_update_drops_the_record_when_the_code_moves(tmp_path: Path) -> None:
    """Until the new code's dependency step has finished, the old record
    describes something that no longer holds."""
    project = _make_project(tmp_path)
    update_engine.write_dependencies_record(project)
    with patch.object(update_engine, "update_code"), patch.object(update_engine, "_run_dependencies_from_disk"):
        update_engine._apply_code_and_dependencies(project, "python", update_engine._UpdateProgress())
    assert update_engine.read_dependencies_record(project) is None


def test_record_files_are_ignored_by_git() -> None:
    """The updater refuses to run on a dirty tree; these files must never count."""
    ignored = (Path(update_engine.__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert ".update_dependencies_*" in ignored.splitlines()
    for name in (update_engine.DEPENDENCIES_RECORD, update_engine.DEPENDENCIES_FAILURE_RECORD):
        assert name.startswith(".update_dependencies_")


def test_the_launchers_start_anyway_on_a_failed_reinstall() -> None:
    root = Path(update_engine.__file__).resolve().parent.parent
    bat = (root / "start.bat").read_text(encoding="ascii")
    # errorlevel N means "N or more", so 12 must be tested before 11 and after 13.
    order = [bat.index(f"if errorlevel {n} goto") for n in (13, 12, 11, 10, 1)]
    assert order == sorted(order)
    assert "if errorlevel 12 goto :dependencies_failed" in bat
    failed_block = bat[bat.index(":dependencies_failed"):]
    assert failed_block.index("goto :update_check_done") < failed_block.index("exit /b")
    sh = (root / "start.sh").read_text(encoding="utf-8")
    assert '"$UPDATE_CHECK_RC" -eq 12' in sh
    assert update_engine.CHECK_DEPENDENCIES_FAILED == 12


@pytest.mark.usefixtures("_default_searxng_venv")
def test_a_missing_installer_is_shown_but_not_retried_at_every_start(tmp_path: Path) -> None:
    """A file the checkout lacks cannot come back by retrying: the banner shows
    it, the packages are recorded as installed, and the next start does not
    reinstall every package again."""
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    (project / update_engine.PORTABLE_NODE_INSTALLER).unlink()
    machine = _Machine(project)

    assert machine.install() == 0
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None
    assert failure["steps"] == [update_engine.DEPENDENCY_STEP_PORTABLE_NODE]
    assert "update.bat" in failure["error"]
    assert update_engine.read_dependencies_record(project) == update_engine.completion_fingerprint(project)

    update_engine.write_completion_marker(project)
    children_before = machine.children
    assert machine.check() == update_engine.CHECK_READY
    assert machine.children == children_before
    assert update_engine.read_dependencies_failure(project) is not None


@pytest.mark.usefixtures("_default_searxng_venv")
def test_a_missing_installer_alongside_a_retryable_failure_is_retried(tmp_path: Path) -> None:
    """One retryable failure is enough to leave the record out."""
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    (project / update_engine.PORTABLE_NODE_INSTALLER).unlink()
    assert _Machine(project, searxng_exit=1).install() == 0
    assert update_engine.read_dependencies_record(project) is None
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None
    assert failure["steps"] == [update_engine.DEPENDENCY_STEP_PORTABLE_NODE, update_engine.DEPENDENCY_STEP_SEARXNG]


@pytest.mark.usefixtures("_default_searxng_venv")
def test_a_running_searxng_is_not_reinstalled_and_is_retried(tmp_path: Path) -> None:
    """update.bat, the reinstall before start and an update run by an older
    engine do not stop SearXNG first: the reinstall is refused, shown, and tried
    again at the next start."""
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    machine = _Machine(project)
    with patch.object(update_engine, "find_searxng_servers", return_value=[MagicMock(pid=4242)]), patch.object(
        update_engine, "_describe_processes", return_value="python.exe (PID 4242)"
    ):
        assert machine.install() == 0
    assert _SEARXNG_SETUP_LABEL not in machine.labels
    assert update_engine.read_dependencies_record(project) is None
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None
    assert failure["steps"] == [update_engine.DEPENDENCY_STEP_SEARXNG]
    assert "SearXNG is running" in failure["error"]


@pytest.mark.usefixtures("_default_searxng_venv")
def test_an_unreadable_process_list_counts_as_running(tmp_path: Path) -> None:
    project = _windows_checkout_with_node_and_searxng(tmp_path)
    machine = _Machine(project)
    with patch.object(
        update_engine, "find_searxng_servers",
        side_effect=update_engine.FrontendCheckUnavailable("no psutil"),
    ):
        assert machine.install() == 0
    assert _SEARXNG_SETUP_LABEL not in machine.labels
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None and "could not be checked" in failure["error"]



def test_the_reinstall_waits_while_saiverse_may_be_running(tmp_path: Path) -> None:
    """A backend left running (frontend closed, or main.py started by hand) has
    loaded the packages: the reinstall is postponed and shown, not run under it."""
    project = _updated_by_an_older_updater(tmp_path)
    child = _Child(project)
    with patch.object(update_engine, "_saiverse_may_be_running", return_value="SAIVerse may be running (pid 1)"):
        assert child.check() == update_engine.CHECK_DEPENDENCIES_FAILED
    assert child.commands == [] and child.installs == 0
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None and "SAIVerse may be running" in failure["error"]
    assert update_engine.read_dependencies_record(project) is None


def test_running_and_stopped_runtime_markers(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from saiverse import runtime_marker

    monkeypatch.setattr(runtime_marker, "marker_status", lambda: ("running", "pid 1"))
    assert "SAIVerse may be running (pid 1)" == _REAL_SAIVERSE_MAY_BE_RUNNING(tmp_path)
    monkeypatch.setattr(runtime_marker, "marker_status", lambda: ("unknown", "unreadable"))
    assert "SAIVerse may be running (unreadable)" == _REAL_SAIVERSE_MAY_BE_RUNNING(tmp_path)
    monkeypatch.setattr(runtime_marker, "marker_status", lambda: ("stopped", "runtime markers absent"))
    assert _REAL_SAIVERSE_MAY_BE_RUNNING(tmp_path) is None


def test_an_unreadable_runtime_marker_module_counts_as_running(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "saiverse.runtime_marker", None)
    reason = _REAL_SAIVERSE_MAY_BE_RUNNING(tmp_path)
    assert reason is not None and "could not be checked" in reason


def test_dotenv_home_variables_are_taken_unless_already_set(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".env").write_text(
        "SAIVERSE_HOME=/from/dotenv\nSAIVERSE_USER_DATA_DIR=/data/dotenv\nOPENAI_API_KEY=sk-secret\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", "/already/set")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    update_engine._apply_dotenv_home(tmp_path)
    assert os.environ["SAIVERSE_HOME"] == "/from/dotenv"
    assert os.environ["SAIVERSE_USER_DATA_DIR"] == "/already/set"
    assert "OPENAI_API_KEY" not in os.environ


def test_dotenv_expansion_lets_the_environment_win_as_main_py_does(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """main.py's load_dotenv() expands ${BASE} with the environment winning;
    dotenv_values lets .env win, and the check then looked at another folder's
    runtime markers. The expected value comes from load_dotenv itself, run in a
    child process on the same .env."""
    import subprocess

    (tmp_path / ".env").write_text("BASE=C:/other\nSAIVERSE_HOME=${BASE}/saiverse\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("SAIVERSE_HOME", "PYTHON_DOTENV_DISABLED")}
    env["BASE"] = "D:/live"
    probe = (
        "import os, sys; from dotenv import load_dotenv; load_dotenv(sys.argv[1]); "
        "print(os.environ.get('SAIVERSE_HOME'))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path / ".env")], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert loaded == "D:/live/saiverse"

    monkeypatch.setenv("BASE", "D:/live")
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    update_engine._apply_dotenv_home(tmp_path)
    assert os.environ["SAIVERSE_HOME"] == loaded


def test_python_dotenv_disabled_loads_nothing_as_in_main_py(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".env").write_text("SAIVERSE_HOME=/from/dotenv\n", encoding="utf-8")
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    update_engine._apply_dotenv_home(tmp_path)
    assert "SAIVERSE_HOME" not in os.environ


def test_an_unreadable_dotenv_postpones_the_reinstall(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Where SAIVerse keeps its runtime markers cannot be told: count it as running."""
    (tmp_path / ".env").write_bytes(b"SAIVERSE_HOME=\xff\xfe\n")
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    reason = _REAL_SAIVERSE_MAY_BE_RUNNING(tmp_path)
    assert reason is not None and "could not be checked" in reason


def test_a_dotenv_without_python_dotenv_postpones_the_reinstall(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".env").write_text("SAIVERSE_HOME=/from/dotenv\n", encoding="utf-8")
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setitem(sys.modules, "dotenv.main", None)
    with pytest.raises(update_engine.DotenvUnavailable):
        update_engine._apply_dotenv_home(tmp_path)
    reason = _REAL_SAIVERSE_MAY_BE_RUNNING(tmp_path)
    assert reason is not None and "could not be checked" in reason


def test_a_dotenv_in_a_parent_folder_is_used_as_main_py_uses_it(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """main.py's load_dotenv() looks for .env from the SAIVerse folder upward;
    a .env only in the parent folder moves SAIVERSE_HOME there too."""
    project = tmp_path / "SAIVerse"
    project.mkdir()
    (tmp_path / ".env").write_text("SAIVERSE_HOME=/from/parent\n", encoding="utf-8")
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    update_engine._apply_dotenv_home(project)
    assert os.environ["SAIVERSE_HOME"] == "/from/parent"


def test_the_reinstall_is_postponed_when_the_servers_cannot_be_checked(tmp_path: Path) -> None:
    """Without psutil the frontend and SearXNG servers cannot be listed; npm ci
    beside a running frontend would empty node_modules, so the reinstall waits
    for the next start and is shown, instead of going on with a warning."""
    project = _updated_by_an_older_updater(tmp_path)
    child = _Child(project)
    with patch.object(
        update_engine, "find_frontend_servers", side_effect=update_engine.FrontendCheckUnavailable("no psutil"),
    ):
        assert child.check() == update_engine.CHECK_DEPENDENCIES_FAILED
    assert child.commands == [] and child.installs == 0
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None and "could not be checked" in failure["error"]
    assert update_engine.read_dependencies_record(project) is None


def test_update_bat_still_goes_on_when_the_frontend_cannot_be_checked(tmp_path: Path) -> None:
    """update.bat is how a broken psutil is installed again: it keeps warning."""
    (tmp_path / "frontend").mkdir()
    with patch.object(
        update_engine, "find_frontend_servers", side_effect=update_engine.FrontendCheckUnavailable("no psutil"),
    ):
        update_engine.refuse_while_frontend_runs(tmp_path)  # must not raise
        with pytest.raises(update_engine.UpdateError, match="could not be checked"):
            update_engine.refuse_while_frontend_runs(tmp_path, refuse_when_unchecked=True)


# --- a SearXNG setup that stopped right after setting its venv aside --------------------
#
# docs/intent/dependency_management.md section 2-7: before scripts/searxng_install.py
# renames a SearXNG venv of another Python series, it writes
# .setup_in_progress/searxng-venv.json. Stopped right after the rename (the window
# closed), only that record and the set-aside venv are left: no venv python. The next
# start must reach the installer's own recovery even though the marker and the
# dependency record both match. The installer below is the real one, run in this
# process; only its source download and its pip / venv commands are replaced.


def _searxng_installer(project: Path, monkeypatch):  # type: ignore[no-untyped-def]
    sys.path.insert(0, str(Path(update_engine.__file__).resolve().parent))
    try:
        import searxng_install
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(searxng_install, "WORKSPACE_ROOT", project)
    monkeypatch.setenv("SEARXNG_VENV_DIR", str(project / update_engine.SEARXNG_DEFAULT_VENV))
    monkeypatch.setenv("SEARXNG_SRC_DIR", str(project / "searxng-src"))
    monkeypatch.delenv("SEARXNG_REF", raising=False)
    for step in ("prepare_source", "prepare_compatibility", "prepare_requirements"):
        monkeypatch.setattr(searxng_install, step, lambda: None)
    return searxng_install


def _stopped_after_the_set_aside(project: Path, installer) -> Path:  # type: ignore[no-untyped-def]
    venv = installer.venv_directory()
    aside = venv.with_name(f"{venv.name}.old-20261023-093000")
    aside.mkdir(parents=True)
    (aside / "pyvenv.cfg").write_text("version = 3.12.10\n", encoding="utf-8")
    (aside / "the-old-venv.txt").write_text("", encoding="utf-8")
    from scripts import setup_in_progress

    setup_in_progress.write_entry(project, installer.SET_ASIDE_ENTRY, {"venv": str(venv), "aside": str(aside)})
    return aside


class _SearxngMachine(_Machine):
    """``_Machine`` whose SearXNG setup is the real installer's ``main``; its
    install into the venv makes the venv python, or fails half-way."""

    def __init__(self, project: Path, installer, *, install_fails: bool = False) -> None:  # type: ignore[no-untyped-def]
        super().__init__(project)
        self.installer = installer
        self.install_fails = install_fails
        self.setup_pythons: list[str] = []

    def run(self, command, *, cwd, label, timeout=900, check=True, encoding=None, env=None):  # type: ignore[no-untyped-def]
        if label != _SEARXNG_SETUP_LABEL:
            return super().run(command, cwd=cwd, label=label, timeout=timeout, check=check, encoding=encoding, env=env)
        self.labels.append(label)
        self.setup_pythons.append(command[0])
        installer = self.installer

        def install_into_venv():  # type: ignore[no-untyped-def]
            python = installer.venv_python()
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"")
            if self.install_fails:
                raise update_engine.subprocess.CalledProcessError(1, ["pip", "install"])

        with patch.object(installer, "install_into_venv", side_effect=install_into_venv), patch.dict(
            os.environ, env or {}
        ), patch.object(sys, "argv", ["searxng_install.py", "setup"]):
            code = installer.main()
        return MagicMock(returncode=code, stdout="", stderr="" if code == 0 else "[ERROR] SearXNG: pip failed")


def _finished_checkout(tmp_path: Path) -> Path:
    """Marker and dependency record both match: only the SearXNG record is left."""
    project = _make_project(tmp_path)
    (project / update_engine.SEARXNG_INSTALLER).write_text("# stand-in\n", encoding="utf-8")
    update_engine.write_completion_marker(project)
    assert update_engine.write_dependencies_record(project)
    return project


def test_a_searxng_setup_stopped_after_the_set_aside_is_finished_at_the_next_start(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    project = _finished_checkout(tmp_path)
    installer = _searxng_installer(project, monkeypatch)
    aside = _stopped_after_the_set_aside(project, installer)
    assert not installer.venv_python().exists()

    machine = _SearxngMachine(project, installer)
    assert machine.check() == update_engine.CHECK_READY

    assert machine.children == 1
    # Run with SAIVerse's python (the child's), since the SearXNG venv had none.
    assert machine.setup_pythons == [update_engine.sys.executable]
    assert installer.venv_python().is_file()
    assert not aside.exists()
    assert not update_engine.searxng_setup_stopped(project)
    assert update_engine.read_dependencies_failure(project) is None
    assert update_engine.read_dependencies_record(project) == update_engine.completion_fingerprint(project)
    # The start after that does nothing.
    again = _SearxngMachine(project, installer)
    assert again.check() == update_engine.CHECK_READY
    assert again.children == 0


def test_a_failed_finish_puts_the_set_aside_venv_back_and_is_shown(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from saiverse import environment_alerts

    project = _finished_checkout(tmp_path)
    installer = _searxng_installer(project, monkeypatch)
    venv = installer.venv_directory()
    _stopped_after_the_set_aside(project, installer)

    machine = _SearxngMachine(project, installer, install_fails=True)
    assert machine.check() == update_engine.CHECK_DEPENDENCIES_FAILED

    # The installer removed the half-made venv and gave the old one its name back.
    assert (venv / "the-old-venv.txt").is_file()
    assert not update_engine.searxng_setup_stopped(project)
    # The failure is the dependency step's, for the banner; the installer's own
    # record is not written (the update asked it not to), so there is one alert.
    failure = update_engine.read_dependencies_failure(project)
    assert failure is not None and failure["steps"] == [update_engine.DEPENDENCY_STEP_SEARXNG]
    assert not (project / installer.SETUP_FAILURE_RECORD).exists()
    assert [alert["id"] for alert in update_dependencies_alerts(project)] == [ALERT_ID]
    assert environment_alerts.searxng_setup_alerts(project) == []
    assert update_engine.read_dependencies_record(project) is None


def test_a_searxng_record_alone_on_a_new_install_is_left_to_the_searxng_window(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No marker: a new install is never reinstalled (section 2 promise 3); the
    record is finished by run_searxng_server.ps1 / .sh, which start.bat /
    start.sh start for it."""
    project = _make_project(tmp_path)
    (project / update_engine.SEARXNG_INSTALLER).write_text("# stand-in\n", encoding="utf-8")
    installer = _searxng_installer(project, monkeypatch)
    _stopped_after_the_set_aside(project, installer)
    child = _Child(project)
    assert child.check() == update_engine.CHECK_READY
    assert child.installs == 0
    assert update_engine.searxng_setup_stopped(project)


def _block(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def test_the_dev_launchers_run_the_same_check_before_starting() -> None:
    """start-dev.bat / start-dev.sh use the same folder as start.bat / start.sh:
    the check that finishes an interrupted update and installs the packages once
    more after an older updater is the same, word for word (PR #420)."""
    root = Path(update_engine.__file__).resolve().parent.parent
    bat_start, bat_end = "REM Finish an interrupted update before starting.", "\n:update_check_done"
    bat_block = _block((root / "start.bat").read_text(encoding="ascii"), bat_start, bat_end)
    # The whole block, to its last label (a "goto :update_check_done" comes earlier).
    assert ":finish_update_failed" in bat_block and ":update_check_skipped" in bat_block
    assert _block((root / "start-dev.bat").read_text(encoding="ascii"), bat_start, bat_end) == bat_block
    sh_start, sh_end = "# 中断された更新を仕上げてから起動する", "# SearXNG (optional"
    sh_block = _block((root / "start.sh").read_text(encoding="utf-8"), sh_start, sh_end)
    assert '"$UPDATE_CHECK_RC" -eq 12' in sh_block and "--manual" in sh_block
    assert _block((root / "start-dev.sh").read_text(encoding="utf-8"), sh_start, sh_end) == sh_block
    # Before anything starts: the backend runs on the packages the check installs.
    for name, check, first_start in (
        ("start-dev.bat", "--check-complete", "start \"SAIVerse Backend\""),
        ("start-dev.sh", "--check-complete", "python main.py"),
    ):
        text = (root / name).read_text(encoding="utf-8")
        assert text.index(check) < text.index(first_start), name
