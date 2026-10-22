"""起動時の環境の失敗の警告 (saiverse/environment_alerts.py)。"""
from __future__ import annotations

import json
from pathlib import Path

from saiverse import environment_alerts


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
