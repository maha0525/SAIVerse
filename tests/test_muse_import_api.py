"""Muse 公式エクスポート取り込み API (import_muse) のテスト。

隔離した偽 manager と、test_muse_importer が生成する架空のフィクスチャ ZIP だけを
使う。実データや本番ペルソナには触れない。設計: docs/intent/muse_export_import.md
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
from api.routes.people import import_muse
from sai_memory.memory.storage import SYSTEM_NOTICE_KEY
from saiverse_memory.adapter import EMBEDDING_DEFERRED
from tests.test_muse_importer import FIXTURE_ZIP
from tools.utilities.muse_importer import MuseExport

PERSONA_ID = "test_persona_muse"


@pytest.fixture(autouse=True)
def _clean_muse_state():
    """preview が残した一時ファイルとモジュール状態をテストごとに片付ける。"""
    import_muse._muse_export_cache.clear()
    import_muse._muse_import_status.clear()
    yield
    for cached in list(import_muse._muse_export_cache.values()):
        tmp_path = cached.get("tmp_path")
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()
    import_muse._muse_export_cache.clear()
    import_muse._muse_import_status.clear()


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(import_muse.router, prefix="/api/people")
    manager = SimpleNamespace(personas={PERSONA_ID: object()})
    app.dependency_overrides[get_manager] = lambda: manager
    with TestClient(app) as test_client:
        yield test_client


def _post_preview(client: TestClient):
    return client.post(
        f"/api/people/{PERSONA_ID}/import/muse/preview",
        files={"file": ("EYI_Package_test.zip", FIXTURE_ZIP.read_bytes(), "application/zip")},
    )


def test_preview_returns_conversation_and_workspace_metadata(client):
    res = _post_preview(client)
    assert res.status_code == 200

    body = res.json()
    assert body["total_count"] == 1
    assert body["workspace_file_count"] == 3
    assert body["exported_at"] == "2027-01-15T00:00:00Z"
    assert len(body["conversations"]) == 1

    conversation = body["conversations"][0]
    assert conversation["idx"] == 0
    assert conversation["message_count"] == 14
    assert conversation["title"] == "Conversation with Muse AI_01-15-2027_0000000001"
    assert conversation["preview"].startswith("ルナ、はじめまして。山田花子です。")
    assert body["cache_key"] in import_muse._muse_export_cache


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("notes.txt", b"This is not a Muse export."),
        ("broken.zip", b"not a zip archive"),
    ],
)
def test_preview_rejects_non_muse_files(client, filename, payload):
    res = client.post(
        f"/api/people/{PERSONA_ID}/import/muse/preview",
        files={"file": (filename, payload, "application/octet-stream")},
    )
    assert res.status_code == 400


def test_resolve_records_accepts_identifier_and_numeric_idx():
    export = MuseExport(FIXTURE_ZIP)
    record = export.conversations[0]
    assert import_muse._resolve_records(export, ["0"]) == [record]
    assert import_muse._resolve_records(export, [record.identifier]) == [record]
    assert import_muse._resolve_records(export, ["no-such", "0"]) == [record]


def test_run_muse_import_task_saves_payloads_and_hands_off_embedding(client, monkeypatch):
    preview = _post_preview(client).json()
    cache_key = preview["cache_key"]

    class FakeAdapter:
        def __init__(self, persona_id: str):
            self.persona_id = persona_id
            self.payloads = []
            self.titles = []
            self.closed = False

        def append_persona_message(self, payload, thread_suffix=None):
            self.payloads.append((payload, thread_suffix))
            return f"msg-{len(self.payloads)}"

        def set_thread_title(self, thread_suffix, title):
            self.titles.append((thread_suffix, title))

        def close(self):
            self.closed = True

    created = []

    def fake_adapter(persona_id: str):
        adapter = FakeAdapter(persona_id)
        created.append(adapter)
        return adapter

    monkeypatch.setattr("saiverse_memory.SAIMemoryAdapter", fake_adapter)
    handed_off = []
    monkeypatch.setattr(
        import_muse,
        "_launch_import_embedding",
        lambda persona_id, adapter, count: handed_off.append((persona_id, adapter, count)) or True,
    )

    import_muse._run_muse_import_task(PERSONA_ID, cache_key, ["0"], False)

    status = import_muse._muse_import_status[PERSONA_ID]
    assert status["success"] is True
    assert status["conversations"] == 1
    assert status["messages"] == 15
    assert status["embedding"] is True

    adapter = created[0]
    assert len(adapter.payloads) == 15
    assert all(payload["embedding_chunks"] == EMBEDDING_DEFERRED for payload, _ in adapter.payloads)
    # 先頭は CLI と同じ出典見出し (システム通知、系統名 muse_import)。
    header, _header_suffix = adapter.payloads[0]
    assert header["role"] == "system"
    assert header["metadata"][SYSTEM_NOTICE_KEY] == "muse_import"
    assert header["content"].startswith("[Imported Muse conversation ")
    assert adapter.titles == [(
        "EYI Package_01-15-2027_0000000001",
        "Conversation with Muse AI_01-15-2027_0000000001",
    )]
    assert handed_off == [(PERSONA_ID, adapter, 15)]
    # handed_off のときは背景スレッドが adapter を引き取るため、ここでは閉じない。
    assert adapter.closed is False
    assert cache_key not in import_muse._muse_export_cache


def test_import_reserves_running_state_before_task_starts(client, monkeypatch):
    """受理した時点で実行中を予約し、タスク開始前の二重要求を 409 で弾く。"""
    preview = _post_preview(client).json()
    cache_key = preview["cache_key"]

    # 背景タスクを無力化し、「タスクがまだ状態を立てていない」状況を作る。
    monkeypatch.setattr(import_muse, "_run_muse_import_task", lambda *a, **k: None)

    body = {"cache_key": cache_key, "conversation_ids": ["0"], "skip_embedding": True}
    res = client.post(f"/api/people/{PERSONA_ID}/import/muse", json=body)
    assert res.status_code == 200
    # タスクは何もしていないのに、予約によって実行中になっている。
    assert import_muse._muse_import_status[PERSONA_ID]["running"] is True

    res = client.post(f"/api/people/{PERSONA_ID}/import/muse", json=body)
    assert res.status_code == 409
