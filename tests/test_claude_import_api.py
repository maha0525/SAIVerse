"""Claude 公式エクスポート取り込み API (import_claude) の実行枠の予約のテスト。

隔離した偽 manager と、合成した projects ZIP だけを使う。実データや本番ペルソナには
触れない。設計: docs/intent/claude_import.md
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.deps import get_manager
from api.routes.people import import_claude
from tests.test_claude_importer import _zip_with

PERSONA_ID = "test_persona_claude"

PROJECT = {
    "uuid": "proj-1",
    "name": "架空のプロジェクト",
    "prompt_template": "あなたは架空のアシスタントです。",
    "created_at": "2025-09-30T11:11:28Z",
    "updated_at": "2025-09-30T15:51:13Z",
    "docs": [],
}


@pytest.fixture(autouse=True)
def _clean_claude_state():
    import_claude._claude_export_cache.clear()
    import_claude._claude_import_status.clear()
    yield
    for cached in list(import_claude._claude_export_cache.values()):
        tmp_path = cached.get("tmp_path")
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()
    import_claude._claude_export_cache.clear()
    import_claude._claude_import_status.clear()


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(import_claude.router, prefix="/api/people")
    manager = SimpleNamespace(personas={PERSONA_ID: object()})
    app.dependency_overrides[get_manager] = lambda: manager
    with TestClient(app) as test_client:
        yield test_client


def _preview_projects(client: TestClient, tmp_path: Path) -> str:
    src = _zip_with(tmp_path / "projects-000.zip", {"projects/proj-1.json": PROJECT})
    res = client.post(
        f"/api/people/{PERSONA_ID}/import/claude/preview",
        files={"file": ("projects-000.zip", src.read_bytes(), "application/zip")},
    )
    assert res.status_code == 200, res.text
    return res.json()["cache_key"]


def test_import_reserves_running_state_before_task_and_instruction_append(
    client, monkeypatch, tmp_path
):
    """受理した時点で実行中を予約し、タスク開始前の二重要求を 409 で弾く。
    プロジェクト指示の追記も、弾かれた二度目では行わない。"""
    cache_key = _preview_projects(client, tmp_path)

    # 背景タスクを無力化し、「タスクがまだ状態を立てていない」状況を作る。
    monkeypatch.setattr(import_claude, "_run_claude_import_task", lambda *a, **k: None)
    appended: list = []
    monkeypatch.setattr(
        import_claude,
        "_append_instructions_to_system_prompt",
        lambda manager, persona_id, instructions: appended.append(list(instructions)),
    )

    body = {
        "cache_key": cache_key,
        "conversation_ids": ["proj-1"],
        "skip_embedding": True,
        "append_instructions_to_system_prompt": True,
    }
    res = client.post(f"/api/people/{PERSONA_ID}/import/claude", json=body)
    assert res.status_code == 200, res.text
    assert import_claude._claude_import_status[PERSONA_ID]["running"] is True
    assert appended == [["あなたは架空のアシスタントです。"]]

    res = client.post(f"/api/people/{PERSONA_ID}/import/claude", json=body)
    assert res.status_code == 409
    # 二度目は指示を追記しない
    assert len(appended) == 1


def test_failed_instruction_append_releases_reservation(client, monkeypatch, tmp_path):
    """指示の追記に失敗したら取り込みを始めず、予約も外して次の要求を受け付ける。"""
    cache_key = _preview_projects(client, tmp_path)
    started: list = []
    monkeypatch.setattr(
        import_claude, "_run_claude_import_task", lambda *a, **k: started.append(a)
    )

    def _fail(manager, persona_id, instructions):
        raise RuntimeError("update_ai failed")

    monkeypatch.setattr(import_claude, "_append_instructions_to_system_prompt", _fail)

    body = {
        "cache_key": cache_key,
        "conversation_ids": ["proj-1"],
        "skip_embedding": True,
        "append_instructions_to_system_prompt": True,
    }
    with pytest.raises(RuntimeError):
        client.post(f"/api/people/{PERSONA_ID}/import/claude", json=body)
    assert started == []
    assert import_claude._claude_import_status[PERSONA_ID]["running"] is False

    # 予約が外れているので、追記をやめた次の要求は受け付けられる。
    body["append_instructions_to_system_prompt"] = False
    res = client.post(f"/api/people/{PERSONA_ID}/import/claude", json=body)
    assert res.status_code == 200, res.text
    assert len(started) == 1
