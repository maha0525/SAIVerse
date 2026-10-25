"""起動時の環境の失敗の警告 (saiverse/environment_alerts.py)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from saiverse import addon_installer, data_paths, environment_alerts
from scripts import setup_in_progress, update_engine

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_real_addons_or_data(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """The collectors read the addons of this checkout and the SAIVerse data
    folder by default; point them at tmp_path instead."""
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(addon_installer, "EXPANSION_DATA_DIR", tmp_path / "expansion_data")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "expansion_data")
    monkeypatch.setattr(
        addon_installer, "ENV_REBUILD_FAILURES_PATH", tmp_path / ".addon_env_rebuild_failures.json")
    # The test interpreter may be of another series than the recommended one;
    # the tests of that banner below remove this.
    monkeypatch.setenv(environment_alerts.ALLOW_NON_RECOMMENDED_PYTHON_ENV, "1")


def _write_entry(base: Path, record: Path) -> None:
    """Write the in-progress record ``record`` (a path under ``.setup_in_progress``)."""
    setup_in_progress.write_entry(base, record.stem, {})


def test_the_record_names_are_the_ones_the_scripts_write() -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import searxng_install
        import setup_venv
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
    assert environment_alerts.SEARXNG_SETUP_IN_PROGRESS_RECORD == setup_in_progress.entry_path(
        Path(), searxng_install.SET_ASIDE_ENTRY)
    assert environment_alerts.CORE_VENV_SETUP_IN_PROGRESS_RECORD == setup_in_progress.entry_path(
        Path(), setup_venv.VENV_ENTRY)
    assert environment_alerts.SEARXNG_SETUP_IN_PROGRESS_RECORD == update_engine.SEARXNG_SETUP_IN_PROGRESS_RECORD
    assert environment_alerts.SEARXNG_DEPENDENCY_STEP == update_engine.DEPENDENCY_STEP_SEARXNG


def test_no_rebuild_failure_file_means_no_banner(tmp_path: Path) -> None:
    assert environment_alerts.addon_env_rebuild_alerts(tmp_path / "missing.json") == []


def test_a_rebuild_failure_names_the_addon_and_what_to_do(tmp_path: Path) -> None:
    record = tmp_path / ".addon_env_rebuild_failures.json"
    record.write_text(json.dumps({
        "created_at": "2026-10-23T00:00:00+00:00",
        "python_version": "3.13.17",
        "failures": [{"addon_id": "saiverse-voice-tts", "envs": ["gpt_sovits"],
                      "steps": ["gpt-sovits-packages"], "error": "pip failed"}],
    }), encoding="utf-8")
    (alert,) = environment_alerts.addon_env_rebuild_alerts(record)
    assert alert["id"] == environment_alerts.ADDON_ENV_REBUILD_ALERT_ID
    assert "saiverse-voice-tts" in alert["message"]
    assert "setup.bat" in alert["message"] and "アドオン管理" in alert["message"]
    assert alert["details"]["reason"] == ["saiverse-voice-tts"]
    assert "pip failed" in alert["details"]["error"]


def test_a_damaged_rebuild_failure_file_still_shows_a_banner(tmp_path: Path) -> None:
    record = tmp_path / ".addon_env_rebuild_failures.json"
    record.write_text("{not json", encoding="utf-8")
    (alert,) = environment_alerts.addon_env_rebuild_alerts(record)
    assert alert["id"] == environment_alerts.ADDON_ENV_REBUILD_ALERT_ID


def test_the_searxng_failures_reach_the_banner(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(environment_alerts, "addon_env_rebuild_alerts", lambda: [])
    (tmp_path / environment_alerts.SEARXNG_SETUP_FAILURE_RECORD).write_text(
        json.dumps({"error": "github unreachable"}), encoding="utf-8")
    (tmp_path / environment_alerts.SEARXNG_RESTART_FAILURE_RECORD).write_text(
        json.dumps({"error": "no cmd.exe"}), encoding="utf-8")

    alerts = environment_alerts.environment_alerts(tmp_path)

    assert [a["id"] for a in alerts] == [
        environment_alerts.SEARXNG_SETUP_ALERT_ID, environment_alerts.SEARXNG_RESTART_ALERT_ID,
    ]
    assert alerts[0]["details"]["error"] == "github unreachable"
    assert alerts[1]["details"]["error"] == "no cmd.exe"
    # The setup failure stays until a setup succeeds; the restart failure was that restart's news.
    assert (tmp_path / environment_alerts.SEARXNG_SETUP_FAILURE_RECORD).is_file()
    assert not (tmp_path / environment_alerts.SEARXNG_RESTART_FAILURE_RECORD).exists()


def test_a_damaged_searxng_record_still_shows_a_banner(tmp_path: Path) -> None:
    (tmp_path / environment_alerts.SEARXNG_SETUP_FAILURE_RECORD).write_text("{not json", encoding="utf-8")
    (alert,) = environment_alerts.searxng_setup_alerts(tmp_path)
    assert alert["id"] == environment_alerts.SEARXNG_SETUP_ALERT_ID
    assert alert["details"] == {}
    assert environment_alerts.searxng_restart_alerts(tmp_path) == []


def test_one_broken_collector_does_not_hide_the_other(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def broken(_project_dir):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    monkeypatch.setattr(environment_alerts, "update_dependencies_alerts", broken)
    monkeypatch.setattr(environment_alerts, "addon_env_rebuild_alerts", lambda: [{"id": "x"}])
    assert environment_alerts.environment_alerts(tmp_path) == [{"id": "x"}]


# --- records of a setup that stopped part-way (scripts/setup_in_progress.py) ---------------


def test_a_searxng_setup_stopped_part_way_reaches_the_banner(tmp_path: Path) -> None:
    """Closed right after the set-aside: no failure record was written, only the
    in-progress record is left."""
    _write_entry(tmp_path, environment_alerts.SEARXNG_SETUP_IN_PROGRESS_RECORD)
    (alert,) = environment_alerts.environment_alerts(tmp_path)
    assert alert["id"] == environment_alerts.SEARXNG_SETUP_STOPPED_ALERT_ID
    assert "Web 検索 (SearXNG)" in alert["message"] and "setup.bat" in alert["message"]
    assert alert["details"] == {}


def test_a_stopped_searxng_setup_and_its_failure_are_one_alert(tmp_path: Path) -> None:
    """The setup failed and could not put the old venv back: both records point
    at the same SearXNG, and the old venv is not back, so the stopped wording."""
    _write_entry(tmp_path, environment_alerts.SEARXNG_SETUP_IN_PROGRESS_RECORD)
    (tmp_path / environment_alerts.SEARXNG_SETUP_FAILURE_RECORD).write_text(
        json.dumps({"error": "could not be put back"}), encoding="utf-8")
    (alert,) = environment_alerts.environment_alerts(tmp_path)
    assert alert["id"] == environment_alerts.SEARXNG_SETUP_STOPPED_ALERT_ID
    assert alert["details"]["error"] == "could not be put back"


def test_an_unreadable_searxng_record_still_shows_a_banner(tmp_path: Path) -> None:
    record = tmp_path / environment_alerts.SEARXNG_SETUP_IN_PROGRESS_RECORD
    record.parent.mkdir(parents=True)
    record.write_text("{not json", encoding="utf-8")
    (alert,) = environment_alerts.searxng_setup_alerts(tmp_path)
    assert alert["id"] == environment_alerts.SEARXNG_SETUP_STOPPED_ALERT_ID


def test_searxng_named_by_the_dependency_failure_is_shown_once(tmp_path: Path) -> None:
    """The update's reinstall of SearXNG failed: its alert already names SearXNG
    and the next start retries it, so no second SearXNG alert."""
    (tmp_path / "VERSION").write_text("0.3.26\n", encoding="utf-8")
    update_engine.write_dependencies_failure(
        tmp_path, "SearXNG could not be reinstalled", steps=[update_engine.DEPENDENCY_STEP_SEARXNG])
    _write_entry(tmp_path, environment_alerts.SEARXNG_SETUP_IN_PROGRESS_RECORD)
    (tmp_path / environment_alerts.SEARXNG_SETUP_FAILURE_RECORD).write_text("{}", encoding="utf-8")
    alerts = environment_alerts.environment_alerts(tmp_path)
    assert [alert["id"] for alert in alerts] == ["update_dependencies_failed"]


def test_a_dependency_failure_of_another_step_keeps_the_searxng_alert(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("0.3.26\n", encoding="utf-8")
    update_engine.write_dependencies_failure(
        tmp_path, "node failed", steps=[update_engine.DEPENDENCY_STEP_PORTABLE_NODE])
    _write_entry(tmp_path, environment_alerts.SEARXNG_SETUP_IN_PROGRESS_RECORD)
    ids = [alert["id"] for alert in environment_alerts.environment_alerts(tmp_path)]
    assert ids == ["update_dependencies_failed", environment_alerts.SEARXNG_SETUP_STOPPED_ALERT_ID]


def test_a_core_venv_setup_stopped_part_way_reaches_the_banner(tmp_path: Path) -> None:
    """SAIVerse starts on the new .venv, but setup stopped before finish: the
    addons' packages may not have been installed into it again."""
    _write_entry(tmp_path, environment_alerts.CORE_VENV_SETUP_IN_PROGRESS_RECORD)
    (alert,) = environment_alerts.environment_alerts(tmp_path)
    assert alert["id"] == environment_alerts.SETUP_STOPPED_ALERT_ID
    assert "setup.bat" in alert["message"] and "アドオンの一部" in alert["message"]


def test_a_stopped_core_venv_and_stopped_addon_envs_are_one_alert(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _write_entry(tmp_path, environment_alerts.CORE_VENV_SETUP_IN_PROGRESS_RECORD)
    monkeypatch.setattr(
        addon_installer, "find_addon_envs_left_part_way", lambda _expansion=None: {"a-addon": ["eng"]})
    (alert,) = environment_alerts.setup_stopped_alerts(tmp_path)
    assert alert["id"] == environment_alerts.SETUP_STOPPED_ALERT_ID
    assert "a-addon" in alert["message"]
    assert alert["details"] == {"reason": ["a-addon"], "envs": ["a-addon: eng"]}


def test_an_addon_already_in_the_failure_record_is_not_named_twice(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A rebuild that failed leaves both the failure record and the in-progress
    record of the env; the failure alert names the addon, once. With the
    failure record there, the .venv record alone adds no alert either."""
    monkeypatch.setattr(
        addon_installer, "find_addon_envs_left_part_way",
        lambda _expansion=None: {"a-addon": ["eng"], "b-addon": ["tts"]})
    (tmp_path / ".addon_env_rebuild_failures.json").write_text(json.dumps({
        "failures": [{"addon_id": "a-addon", "envs": ["eng"], "error": "pip failed"}],
    }), encoding="utf-8")
    _write_entry(tmp_path, environment_alerts.CORE_VENV_SETUP_IN_PROGRESS_RECORD)

    alerts = environment_alerts.environment_alerts(tmp_path)

    assert [alert["id"] for alert in alerts] == [
        environment_alerts.ADDON_ENV_REBUILD_ALERT_ID, environment_alerts.ADDON_ENV_STOPPED_ALERT_ID,
    ]
    assert alerts[0]["details"]["reason"] == ["a-addon"]
    assert alerts[1]["details"]["reason"] == ["b-addon"]
    assert "a-addon" not in alerts[1]["message"]


def test_an_unreadable_addon_record_does_not_stop_the_start(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def broken(_expansion=None):  # type: ignore[no-untyped-def]
        raise OSError("unreadable")

    monkeypatch.setattr(addon_installer, "find_addon_envs_left_part_way", broken)
    _write_entry(tmp_path, environment_alerts.CORE_VENV_SETUP_IN_PROGRESS_RECORD)
    (alert,) = environment_alerts.setup_stopped_alerts(tmp_path)
    assert alert["id"] == environment_alerts.SETUP_STOPPED_ALERT_ID


def test_no_records_mean_no_setup_alert(tmp_path: Path) -> None:
    assert environment_alerts.setup_stopped_alerts(tmp_path) == []
    assert environment_alerts.searxng_setup_alerts(tmp_path) == []


def test_the_recommended_series_is_the_one_setup_names() -> None:
    series = "%d.%d" % environment_alerts.RECOMMENDED_PYTHON_SERIES
    assert rf"PythonCore\{series}\InstallPath" in (REPO_ROOT / "setup.bat").read_text(encoding="utf-8")
    setup_sh = (REPO_ROOT / "setup.sh").read_text(encoding="utf-8")
    assert f"--expect-series {series}" in setup_sh and f"python{series}" in setup_sh


def test_python_of_another_series_reaches_the_banner(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A .venv made with 3.12 keeps running 3.12 after updates; only setup makes
    it again, so the start tells the user."""
    monkeypatch.delenv(environment_alerts.ALLOW_NON_RECOMMENDED_PYTHON_ENV)
    monkeypatch.setattr(environment_alerts.sys, "version_info", (3, 12, 9, "final", 0))
    alerts = environment_alerts.environment_alerts(tmp_path)
    assert [alert["id"] for alert in alerts] == [environment_alerts.PYTHON_SERIES_ALERT_ID]
    (alert,) = alerts
    assert "3.12" in alert["title"]
    assert "3.13" in alert["message"] and "setup.bat" in alert["message"]
    assert environment_alerts.ALLOW_NON_RECOMMENDED_PYTHON_ENV in alert["message"]
    assert alert["details"]["python"] == "3.12" and alert["details"]["recommended"] == "3.13"


@pytest.mark.parametrize("version_info", [(3, 13, 2, "final", 0), (3, 13, 17, "final", 0)])
def test_any_patch_of_the_recommended_series_is_quiet(monkeypatch, version_info) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(environment_alerts.ALLOW_NON_RECOMMENDED_PYTHON_ENV)
    assert environment_alerts.python_series_alerts(version_info) == []


@pytest.mark.parametrize("value, shown", [("1", False), ("0", True), ("", True)])
def test_only_the_allow_setting_hides_the_python_banner(monkeypatch, value, shown) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(environment_alerts.ALLOW_NON_RECOMMENDED_PYTHON_ENV, value)
    alerts = environment_alerts.python_series_alerts((3, 11, 9, "final", 0))
    assert bool(alerts) is shown
