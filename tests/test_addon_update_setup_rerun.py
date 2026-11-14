"""アドオン更新で step が途中失敗したときの setup 再実行と、既失敗環境の修復 (repair)。

関連: docs/issues/addon_update_step_failure_skips_rerun.md
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_manager
from api.routes import addon_catalog
from saiverse import addon_installer, addon_paths
from saiverse.addon_installer import (
    AddonInstallError,
    confirm_install,
    confirm_update,
    execute_repair_setup_plan,
    is_addon_setup_incomplete,
    load_completed_setup_version,
    plan_repair_setup,
    prepare_install,
    prepare_repair_setup,
    prepare_update,
)
from scripts import setup_in_progress

ADDON_ID = "sample-addon"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    home_dir = tmp_path / "home"
    monkeypatch.setenv("SAIVERSE_HOME", str(home_dir))
    monkeypatch.setattr(addon_paths, "USER_DATA_DIR", home_dir / "user_data")
    return home_dir


@pytest.fixture
def expansion(tmp_path: Path) -> Path:
    path = tmp_path / "expansion_data"
    path.mkdir()
    return path


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _commit_manifest(
    repo: Path,
    version: str,
    setup_version: int,
    steps: list[dict],
) -> str:
    manifest = {
        "name": ADDON_ID,
        "display_name": "Sample Addon",
        "version": version,
        "manifest_version": 2,
        "setup_version": setup_version,
        "setup": {"steps": steps},
    }
    (repo / "addon.json").write_text(json.dumps(manifest), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"v{version}")
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init")
    (repo / "step_mark.py").write_text(
        "import sys, pathlib\n"
        "if len(sys.argv) > 2 and sys.argv[2] == 'fail' and pathlib.Path('should_fail.flag').exists():\n"
        "    raise SystemExit(1)\n"
        "with pathlib.Path('ran.txt').open('a', encoding='utf-8') as f:\n"
        "    f.write(sys.argv[1] + '\\n')\n",
        encoding="utf-8",
    )
    return repo


def _step(name: str, arg: str, *, mode: str = "ok") -> dict:
    return {
        "name": name,
        "type": "python_script",
        "script": "step_mark.py",
        "args": [arg, mode],
    }


def test_failed_update_step_forces_setup_rerun_on_next_update(tmp_path, home, expansion):
    repo = _make_repo(tmp_path)
    c1 = _commit_manifest(repo, "1.0.0", 1, [_step("s1", "v1")])
    prepare_install(str(repo), c1, ADDON_ID, expansion_dir=expansion)
    confirm_install(ADDON_ID, expansion_dir=expansion)
    assert load_completed_setup_version(ADDON_ID) == 1
    assert not is_addon_setup_incomplete(ADDON_ID, 1)

    # v2 bumps setup_version to 2, but step 2 fails mid-way after checkout
    c2 = _commit_manifest(
        repo,
        "2.0.0",
        2,
        [_step("s1", "v2-a"), _step("s2", "v2-b", mode="fail")],
    )
    addon_dir = expansion / ADDON_ID
    (addon_dir / "should_fail.flag").write_text("1", encoding="utf-8")

    prep2 = prepare_update(ADDON_ID, str(repo), c2, expansion_dir=expansion)
    assert prep2.needs_setup is True
    with pytest.raises(AddonInstallError):
        confirm_update(ADDON_ID, expansion_dir=expansion)

    # Checkout already moved addon_dir to setup_version=2, but setup did not complete
    assert load_completed_setup_version(ADDON_ID) == 1
    assert is_addon_setup_incomplete(ADDON_ID, 2)

    # Clear the transient failure cause and update to v2.0.1 (same setup_version=2)
    (addon_dir / "should_fail.flag").unlink()
    c3 = _commit_manifest(
        repo,
        "2.0.1",
        2,
        [_step("s1", "v2-a"), _step("s2", "v2-b", mode="fail")],
    )
    prep3 = prepare_update(ADDON_ID, str(repo), c3, expansion_dir=expansion)
    assert prep3.needs_setup is True
    confirm_update(ADDON_ID, expansion_dir=expansion)

    assert load_completed_setup_version(ADDON_ID) == 2
    assert not is_addon_setup_incomplete(ADDON_ID, 2)
    ran = (addon_dir / "ran.txt").read_text(encoding="utf-8").splitlines()
    assert ran == ["v1", "v2-a", "v2-a", "v2-b"]

    # Subsequent update with same setup_version=2 skips setup again
    c4 = _commit_manifest(repo, "2.0.2", 2, [_step("s1", "v2-a")])
    prep4 = prepare_update(ADDON_ID, str(repo), c4, expansion_dir=expansion)
    assert prep4.needs_setup is False


def test_preexisting_env_in_progress_record_triggers_setup_rerun(tmp_path, home, expansion):
    repo = _make_repo(tmp_path)
    c1 = _commit_manifest(repo, "1.0.0", 2, [_step("s1", "v1")])
    prepare_install(str(repo), c1, ADDON_ID, expansion_dir=expansion)
    confirm_install(ADDON_ID, expansion_dir=expansion)

    # Simulate a legacy environment before setup_state.json existed, with leftover env-* entry
    state_file = addon_paths.get_addon_install_dir(ADDON_ID) / addon_installer.SETUP_STATE_NAME
    state_file.unlink()
    setup_in_progress.write_entry(addon_paths.get_addon_install_dir(ADDON_ID), "env-eng", {})

    assert is_addon_setup_incomplete(ADDON_ID, 2)

    c2 = _commit_manifest(repo, "1.0.1", 2, [_step("s1", "v1-rerun")])
    prep = prepare_update(ADDON_ID, str(repo), c2, expansion_dir=expansion)
    assert prep.needs_setup is True


def test_healthy_legacy_addon_without_setup_state_is_not_flagged_incomplete(tmp_path, home, expansion):
    repo = _make_repo(tmp_path)
    c1 = _commit_manifest(repo, "1.0.0", 2, [_step("s1", "v1")])
    prepare_install(str(repo), c1, ADDON_ID, expansion_dir=expansion)
    confirm_install(ADDON_ID, expansion_dir=expansion)

    state_file = addon_paths.get_addon_install_dir(ADDON_ID) / addon_installer.SETUP_STATE_NAME
    state_file.unlink()

    assert not is_addon_setup_incomplete(ADDON_ID, 2)
    c2 = _commit_manifest(repo, "1.0.1", 2, [_step("s1", "v1")])
    prep = prepare_update(ADDON_ID, str(repo), c2, expansion_dir=expansion)
    assert prep.needs_setup is False


def test_repair_setup_recovers_legacy_failed_addon_without_uninstall(tmp_path, home, expansion):
    repo = _make_repo(tmp_path)
    c1 = _commit_manifest(repo, "1.0.0", 2, [_step("s1", "repaired")])
    prepare_install(str(repo), c1, ADDON_ID, expansion_dir=expansion)
    confirm_install(ADDON_ID, expansion_dir=expansion)

    # Simulate a legacy user whose setup failed mid-way before setup_state/update-setup existed
    state_file = addon_paths.get_addon_install_dir(ADDON_ID) / addon_installer.SETUP_STATE_NAME
    state_file.unlink()
    addon_dir = expansion / ADDON_ID
    (addon_dir / "ran.txt").unlink()

    prep = prepare_repair_setup(ADDON_ID, expansion_dir=expansion)
    assert prep.needs_setup is True
    plan = plan_repair_setup(ADDON_ID, {}, expansion_dir=expansion)
    execute_repair_setup_plan(plan)

    assert load_completed_setup_version(ADDON_ID) == 2
    assert not is_addon_setup_incomplete(ADDON_ID, 2)
    assert (addon_dir / "ran.txt").read_text(encoding="utf-8").splitlines() == ["repaired"]


def test_api_installed_flag_and_repair_endpoints(tmp_path, home, expansion, monkeypatch):
    repo = _make_repo(tmp_path)
    c1 = _commit_manifest(repo, "1.0.0", 1, [_step("s1", "v1")])
    prepare_install(str(repo), c1, ADDON_ID, expansion_dir=expansion)
    confirm_install(ADDON_ID, expansion_dir=expansion)

    c2 = _commit_manifest(repo, "2.0.0", 2, [_step("s1", "v2-ok")])
    prepare_update(ADDON_ID, str(repo), c2, expansion_dir=expansion)
    # Force an incomplete update state
    setup_in_progress.write_entry(
        addon_paths.get_addon_install_dir(ADDON_ID),
        addon_installer.UPDATE_SETUP_ENTRY,
        {"setup_version": 2},
    )

    monkeypatch.setattr(addon_catalog, "EXPANSION_DATA_DIR", expansion)
    app = FastAPI()
    app.include_router(addon_catalog.router, prefix="/api/addon-catalog")
    app.dependency_overrides[get_manager] = lambda: SimpleNamespace(
        addon_loader=None,
        mcp_manager=None,
        tool_registry=None,
    )
    client = TestClient(app)

    res_list = client.get("/api/addon-catalog/installed")
    assert res_list.status_code == 200
    items = {item["addon_id"]: item for item in res_list.json()}
    assert items[ADDON_ID]["setup_incomplete"] is True

    res_prep = client.get(f"/api/addon-catalog/installed/{ADDON_ID}/repair")
    assert res_prep.status_code == 200
    assert res_prep.json()["needs_setup"] is True

    res_run = client.post(
        f"/api/addon-catalog/installed/{ADDON_ID}/repair",
        json={"answers": {}},
    )
    assert res_run.status_code == 200
    assert "done" in res_run.text

    res_list_after = client.get("/api/addon-catalog/installed")
    items_after = {item["addon_id"]: item for item in res_list_after.json()}
    assert items_after[ADDON_ID]["setup_incomplete"] is False
