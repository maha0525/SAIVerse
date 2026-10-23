"""The update rebuilds the addon environments the Python of .venv cannot use
(scripts/update_engine.py ``rebuild_addon_envs``)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import update_engine


def _checkout_with_rebuild_script(tmp_path: Path) -> Path:
    script = tmp_path / update_engine.ADDON_ENV_REBUILD_SCRIPT
    script.parent.mkdir(parents=True)
    script.write_text("# stand-in\n", encoding="utf-8")
    return tmp_path


def test_the_update_runs_the_rebuild_script_with_saiverse_python(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    project = _checkout_with_rebuild_script(tmp_path)
    # A parent that waits long enough: the rebuild gets its own full timeout.
    monkeypatch.setenv(update_engine.DEPENDENCIES_DEADLINE_ENV, str(update_engine.time.time() + 10 * 60 * 60))
    calls: list[dict] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"command": list(command), **kwargs})
        return MagicMock(returncode=0, stdout="[OK] Addon packages match\n", stderr="")

    with patch.object(update_engine, "_run", side_effect=fake_run):
        assert update_engine.rebuild_addon_envs(project, "venv-python") is None
    (call,) = calls
    # Without --core-venv-recreated: only the environments the script finds
    # unusable are rebuilt; a usable one is not touched.
    assert call["command"] == ["venv-python", str(project / update_engine.ADDON_ENV_REBUILD_SCRIPT)]
    assert call["check"] is False
    assert call["timeout"] == update_engine.ADDON_ENV_REBUILD_TIMEOUT_SECONDS
    assert call["env"]["PYTHONIOENCODING"] == "utf-8"


def test_the_rebuild_ends_before_the_parent_stops_waiting(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    project = _checkout_with_rebuild_script(tmp_path)
    timeouts: list[int] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        timeouts.append(kwargs["timeout"])
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_engine.time, "time", lambda: 10_000.0)
    monkeypatch.setenv(update_engine.DEPENDENCIES_DEADLINE_ENV, str(10_000.0 + 1_000))
    with patch.object(update_engine, "_run", side_effect=fake_run):
        assert update_engine.rebuild_addon_envs(project, "venv-python") is None
    assert timeouts == [1_000 - update_engine._DEADLINE_MARGIN_SECONDS]


def test_without_a_deadline_the_older_parents_timeout_counts(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The update from v0.3.25 is run by an engine that passes no deadline and
    stops waiting 7200 s after it started the child."""
    monkeypatch.delenv(update_engine.DEPENDENCIES_DEADLINE_ENV, raising=False)
    monkeypatch.setattr(update_engine, "_ENGINE_LOADED_AT", 10_000.0)
    monkeypatch.setattr(update_engine.time, "time", lambda: 10_000.0 + 6_000)
    assert update_engine._addon_rebuild_timeout() == (
        update_engine._LEGACY_CHILD_TIMEOUT_SECONDS - 6_000 - update_engine._DEADLINE_MARGIN_SECONDS
    )


def test_no_time_left_is_a_failure_and_the_script_is_not_started(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    project = _checkout_with_rebuild_script(tmp_path)
    monkeypatch.setattr(update_engine.time, "time", lambda: 10_000.0)
    monkeypatch.setenv(update_engine.DEPENDENCIES_DEADLINE_ENV, str(10_000.0 + 100))
    with patch.object(update_engine, "_run") as run:
        failure = update_engine.rebuild_addon_envs(project, "venv-python")
    run.assert_not_called()
    assert failure is not None and failure.step == update_engine.DEPENDENCY_STEP_ADDON_ENVS
    assert failure.retry


def test_a_failed_rebuild_is_left_to_the_scripts_own_record(tmp_path: Path) -> None:
    """The script records its failures (and its crash) for the banner; returning
    one here would show the same failure in a second banner."""
    project = _checkout_with_rebuild_script(tmp_path)
    (project / update_engine.ADDON_ENV_REBUILD_FAILURES_RECORD).write_text("{}", encoding="utf-8")
    with patch.object(update_engine, "_run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
        assert update_engine.rebuild_addon_envs(project, "venv-python") is None


def test_a_script_that_stopped_without_its_record_is_returned(tmp_path: Path) -> None:
    """Stopped before its crash handler (an import failed): nothing else would
    reach the screen."""
    project = _checkout_with_rebuild_script(tmp_path)
    with patch.object(
        update_engine, "_run", return_value=MagicMock(returncode=1, stdout="", stderr="ImportError: x")
    ):
        failure = update_engine.rebuild_addon_envs(project, "venv-python")
    assert failure is not None and failure.step == update_engine.DEPENDENCY_STEP_ADDON_ENVS
    assert "ImportError: x" in failure.error


def test_the_record_name_is_the_one_the_script_writes() -> None:
    from saiverse import addon_installer

    assert addon_installer.ENV_REBUILD_FAILURES_PATH.name == update_engine.ADDON_ENV_REBUILD_FAILURES_RECORD


def test_a_script_that_cannot_start_is_returned(tmp_path: Path) -> None:
    project = _checkout_with_rebuild_script(tmp_path)
    with patch.object(update_engine, "_run", side_effect=update_engine.UpdateError("could not run")):
        failure = update_engine.rebuild_addon_envs(project, "venv-python")
    assert failure is not None
    assert failure.step == update_engine.DEPENDENCY_STEP_ADDON_ENVS
    assert "could not run" in failure.error and failure.retry


def test_a_checkout_without_the_script_has_no_such_step(tmp_path: Path) -> None:
    with patch.object(update_engine, "_run") as run:
        assert update_engine.rebuild_addon_envs(tmp_path, "venv-python") is None
    run.assert_not_called()


def test_the_dependency_step_rebuilds_the_addon_envs_last(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    order: list[str] = []
    failure = update_engine.DependencyStepFailure(update_engine.DEPENDENCY_STEP_ADDON_ENVS, "no python")

    def addon_step(project_dir, python):  # type: ignore[no-untyped-def]
        order.append("addons")
        assert python == "venv-python"
        return failure

    with patch.object(
        update_engine, "_run", return_value=MagicMock(returncode=0, stdout="", stderr="")
    ), patch.object(update_engine, "refresh_portable_node", return_value=None), patch.object(
        update_engine, "_report_addon_conflicts"
    ), patch.object(update_engine.shutil, "which", return_value="npm"), patch.object(
        update_engine, "refresh_searxng", side_effect=lambda *_: order.append("searxng")
    ), patch.object(update_engine, "rebuild_addon_envs", side_effect=addon_step):
        assert update_engine.update_dependencies(tmp_path, "venv-python") == [failure]
    assert order == ["searxng", "addons"]
