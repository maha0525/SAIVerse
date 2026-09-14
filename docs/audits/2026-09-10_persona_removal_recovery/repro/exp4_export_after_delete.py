"""実験 4: 削除されたペルソナの記憶を、後から取り出せるか。

削除後 (= main DB に ai 行が無く、manager.personas にも居ない) の状態で:
  1. `export_threads_native` を直接呼べるか (ライブラリ層)
  2. API 層 `/api/people/{id}/threads/{tid}/export-native` が通るか
  3. API 層 `/api/people/{id}/threads` (一覧) が通るか
  4. API 層 `/api/people/{id}/import/native` が通るか
を **実際に呼んで** 確かめる。

FastAPI の TestClient に、people の該当ルータだけを載せた最小アプリを組む。
`get_manager` は「ai 行が無い / personas に居ない」偽 manager で差し替える。
LLM は呼ばない。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_env import guard_not_production, setup_home  # noqa: E402

HOME = setup_home("exp4")
guard_not_production()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from database.models import Base  # noqa: E402
from api.deps import get_manager  # noqa: E402
from api.routes.people import memory as people_memory  # noqa: E402
from api.routes.people import native_export_import as people_native  # noqa: E402
from saiverse_memory.adapter import SAIMemoryAdapter  # noqa: E402
from saiverse_memory.native_export import export_threads_native  # noqa: E402

DELETED = "probe_deleted_testcity"
THREAD = f"{DELETED}:__persona__"

OUT: dict = {"home": str(HOME)}


def main() -> None:
    # --- 記憶ファイルだけがある状態を作る (ai 行は最初から作らない = 削除後と同じ) ---
    a = SAIMemoryAdapter(
        persona_id=DELETED, persona_dir=HOME / "personas" / DELETED,
        resource_id=DELETED, startup_backup=False, recover_orphaned_thread=False,
    )
    a.append_persona_message({
        "role": "assistant", "content": "PROBE-DELETED-1 削除後も残る記憶",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    a.conn.commit()
    a.close()

    # --- 1. ライブラリ層 ---
    try:
        arc = export_threads_native(DELETED)
        OUT["lib_export_threads_native"] = {
            "ok": True,
            "threads": [t["thread_id"] for t in arc["threads"]],
            "messages": sum(len(t["messages"]) for t in arc["threads"]),
        }
        arc_path = Path(__file__).with_name("exp4_archive.json")
        arc_path.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        OUT["lib_export_threads_native"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        arc_path = None

    # --- API 層 ---
    db_file = HOME / "main.db"
    if db_file.exists():
        db_file.unlink()
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)   # ai 行は無い (= 削除済み)
    SessionLocal = sessionmaker(bind=engine)

    class FakeManager:
        personas: dict = {}            # 削除でインメモリからも消えている

        def __init__(self):
            self.SessionLocal = SessionLocal

    app = FastAPI()
    app.include_router(people_memory.router, prefix="/api/people")
    app.include_router(people_native.router, prefix="/api/people")
    app.dependency_overrides[get_manager] = lambda: FakeManager()
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get(f"/api/people/{DELETED}/threads")
    OUT["api_list_threads"] = {"status": r.status_code, "body": r.text[:200]}

    r = client.get(f"/api/people/{DELETED}/threads/{THREAD}/export-native")
    OUT["api_export_native"] = {
        "status": r.status_code,
        "body_head": r.text[:300],
        "carries_message": "PROBE-DELETED-1" in r.text,
    }

    if arc_path is not None:
        with open(arc_path, "rb") as fh:
            r = client.post(
                f"/api/people/{DELETED}/import/native",
                files={"file": ("arc.json", fh, "application/json")},
                data={"skip_embedding": "true"},
            )
        OUT["api_import_native"] = {"status": r.status_code, "body": r.text[:300]}

    out_path = Path(__file__).with_name("exp4_result.json")
    out_path.write_text(json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(OUT, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
