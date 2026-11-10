"""ChatGPT 公式エクスポートと拡張機能の取り込み API (import_chatlog) の実行枠の予約のテスト。

受け付けた時点で実行中を予約し、背景タスクが状態を立てる前の二重要求を 409 で弾く
ことを固定する (Claude / Muse の取り込みと同じ形)。隔離した偽 manager と偽の解析結果
だけを使い、実データや本番ペルソナには触れない。
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
from api.routes.people import import_chatlog
import tools.utilities.chatlog_exporter_importer as exporter_importer

PERSONA_ID = "test_persona_chatlog"


@pytest.fixture(autouse=True)
def _clean_state():
    import_chatlog._chatgpt_export_cache.clear()
    import_chatlog._official_import_status.clear()
    import_chatlog._extension_import_status.clear()
    yield
    import_chatlog._chatgpt_export_cache.clear()
    import_chatlog._official_import_status.clear()
    import_chatlog._extension_import_status.clear()


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(import_chatlog.router, prefix="/api/people")
    manager = SimpleNamespace(personas={PERSONA_ID: object()})
    app.dependency_overrides[get_manager] = lambda: manager
    with TestClient(app) as test_client:
        yield test_client


def test_official_import_reserves_running_state_before_task_starts(client, monkeypatch):
    record = SimpleNamespace(conversation_id="conv-1", identifier="conv-1")
    import_chatlog._chatgpt_export_cache["key-1"] = {
        "export": SimpleNamespace(conversations=[record]),
        "persona_id": PERSONA_ID,
    }
    started: list = []
    # 背景タスクを無力化し、「タスクがまだ状態を立てていない」状況を作る。
    monkeypatch.setattr(
        import_chatlog, "_run_official_import_task", lambda *a, **k: started.append(a)
    )

    body = {"cache_key": "key-1", "conversation_ids": ["conv-1"], "skip_embedding": True}
    res = client.post(f"/api/people/{PERSONA_ID}/import/official", json=body)
    assert res.status_code == 200, res.text
    assert import_chatlog._official_import_status[PERSONA_ID]["running"] is True

    res = client.post(f"/api/people/{PERSONA_ID}/import/official", json=body)
    assert res.status_code == 409
    assert len(started) == 1


def test_extension_import_reserves_running_state_before_task_starts(client, monkeypatch):
    fake = SimpleNamespace(title="架空の会話", iter_memory_payloads=lambda: iter([{}, {}]))
    monkeypatch.setattr(exporter_importer, "parse_exporter_file", lambda path: fake)
    started: list = []
    monkeypatch.setattr(
        import_chatlog, "_run_extension_import_task", lambda *a, **k: started.append(a)
    )

    def _post():
        return client.post(
            f"/api/people/{PERSONA_ID}/import/extension",
            files={"file": ("chat.json", b"{}", "application/json")},
            data={"skip_embedding": "true"},
        )

    res = _post()
    assert res.status_code == 200, res.text
    assert import_chatlog._extension_import_status[PERSONA_ID]["running"] is True

    res = _post()
    assert res.status_code == 409
    assert len(started) == 1
    # 受け付けた一度目のファイルは背景タスクが片付ける (ここでは無力化しているので消す)。
    for args in started:
        Path(args[1]).unlink(missing_ok=True)
