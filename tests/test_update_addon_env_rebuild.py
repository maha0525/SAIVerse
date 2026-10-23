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


def test_the_update_runs_the_rebuild_script_with_saiverse_python(tmp_path: Path) -> None:
    project = _checkout_with_rebuild_script(tmp_path)
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


def test_a_failed_rebuild_is_left_to_the_scripts_own_record(tmp_path: Path) -> None:
    """The script records its failures (and its crash) for the banner; returning
    one here would show the same failure in a second banner."""
    project = _checkout_with_rebuild_script(tmp_path)
    with patch.object(update_engine, "_run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
        assert update_engine.rebuild_addon_envs(project, "venv-python") is None


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
