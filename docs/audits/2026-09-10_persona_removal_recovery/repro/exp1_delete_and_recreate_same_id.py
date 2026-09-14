"""実験 1: 同じ ID でペルソナを作り直したら記憶と旧行はどうなるか。

縮めた範囲 (報告に明記する):
- SAIVerseManager 全体は組み立てない。`manager/persona.py: PersonaMixin._create_persona`
  と `manager/admin.py: AdminService.delete_ai` を、tests/test_persona_creation_wiring.py
  と同じ形の最小ホスト (PersonaMixin.__new__ + 必要属性) に載せて直接呼ぶ。
- `PersonaCore` はスタブ (本物は SAIMemory・埋め込み・プロンプトを巻き込む)。
  memory.db は SAIMemoryAdapter を直接使って自分で作り、自分で読む。
- main DB は一時ファイルの SQLite。SAIVERSE_HOME は scratchpad。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_env import guard_not_production, setup_home  # noqa: E402

HOME = setup_home("exp1")
guard_not_production()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import database.models as models  # noqa: E402
from database.models import AI as AIModel, Base, Building as BuildingModel, City as CityModel  # noqa: E402
from manager.persona import PersonaMixin  # noqa: E402
from manager.admin import AdminService  # noqa: E402
from saiverse_memory.adapter import SAIMemoryAdapter  # noqa: E402
from sai_memory.core_memory import add_core_memory, list_core_memories  # noqa: E402

CITY = "testcity"
STEM = "probe_a"
AIID = f"{STEM}_{CITY}"
NAME = "プローブ甲"

OUT: dict = {"home": str(HOME), "aiid_expected": AIID}


class _StubPersonaCore:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.private_room_id = None
        self.persona_role = None


def build_host(SessionLocal, db_path):
    svc = PersonaMixin.__new__(PersonaMixin)
    svc.SessionLocal = SessionLocal
    svc.db_path = db_path
    svc.city_id = 1
    svc.city_name = CITY
    svc.model = "test-model"
    svc._base_model = "test-model"
    svc.saiverse_home = HOME
    svc.default_avatar = "avatar.png"
    svc.user_room_id = f"user_room_{CITY}"
    svc.timezone_info = None
    svc.timezone_name = "UTC"
    svc.buildings = []
    svc.building_map = {}
    svc.capacities = {}
    svc.occupants = {}
    svc.building_memory_paths = {}
    svc.building_histories = {}
    svc.personas = {}
    svc.avatar_map = {}
    svc.id_to_name_map = {}
    svc.persona_map = {}
    svc.items = {}
    svc.items_by_persona = {}
    svc.get_persona_pending_events = lambda *a, **k: []
    svc.archive_persona_events = lambda *a, **k: None
    svc._on_persona_registered = lambda persona_id: None
    # delete_ai が使う AdminService 側の実装をそのまま借りる (差し替えない)
    svc._is_seeded_entity = AdminService._is_seeded_entity
    return svc


def create(svc, name, **kwargs):
    with patch("manager.persona.PersonaCore", _StubPersonaCore), \
         patch("manager.persona.get_model_provider", return_value="stub"), \
         patch("manager.persona.get_context_length", return_value=1000):
        return svc._create_persona(name, "PROBE-SYSTEM-PROMPT-V1", **kwargs)


# --- 持ち主 ID を持つテーブルへ 1 行ずつ置く (NOT NULL を型から埋める) ---
PERSONA_ID_COLS = {"AIID", "PERSONA_ID", "persona_id"}


def seed_persona_rows(SessionLocal, ai_id: str) -> dict:
    import datetime as _dt

    from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text

    seeded, failed = {}, {}
    db = SessionLocal()
    try:
        for cls_name, obj in vars(models).items():
            table = getattr(obj, "__tablename__", None)
            if not table or table in ("ai", "city", "building"):
                continue
            cols = list(obj.__table__.columns)
            id_cols = [c for c in cols if c.name in PERSONA_ID_COLS]
            if not id_cols:
                continue
            values = {}
            ok = True
            for c in cols:
                if c.name in PERSONA_ID_COLS:
                    values[c.name] = ai_id
                    continue
                if c.autoincrement is True and c.primary_key:
                    continue
                if c.default is not None or c.server_default is not None:
                    continue
                if c.nullable and not c.primary_key:
                    continue
                t = c.type
                if isinstance(t, (String, Text)):
                    values[c.name] = f"probe_{c.name}"
                elif isinstance(t, Boolean):
                    values[c.name] = False
                elif isinstance(t, Integer):
                    values[c.name] = 1
                elif isinstance(t, Float):
                    values[c.name] = 1.0
                elif isinstance(t, DateTime):
                    values[c.name] = _dt.datetime.now()
                elif c.primary_key:
                    ok = False
                    break
            if not ok:
                failed[table] = "unsupported PK type"
                continue
            try:
                db.add(obj(**values))
                db.flush()
                seeded[table] = values.get("PERSONA_ID") or values.get("AIID") or values.get("persona_id")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failed[table] = f"{type(exc).__name__}: {str(exc)[:90]}"
        db.commit()
    finally:
        db.close()
    return {"seeded_tables": sorted(seeded), "failed_tables": failed}


def count_persona_rows(SessionLocal, ai_id: str) -> dict:
    out = {}
    db = SessionLocal()
    try:
        for cls_name, obj in vars(models).items():
            table = getattr(obj, "__tablename__", None)
            if not table:
                continue
            id_cols = [c for c in obj.__table__.columns if c.name in PERSONA_ID_COLS]
            if not id_cols:
                continue
            col = id_cols[0]
            try:
                n = db.query(obj).filter(col == ai_id).count()
            except Exception as exc:  # noqa: BLE001
                n = f"ERROR {exc}"
            if n:
                out[table] = n
    finally:
        db.close()
    return out


def make_adapter(pid: str) -> SAIMemoryAdapter:
    return SAIMemoryAdapter(
        persona_id=pid, persona_dir=HOME / "personas" / pid,
        resource_id=pid, startup_backup=False, recover_orphaned_thread=False,
    )


def write_memory(pid: str) -> dict:
    a = make_adapter(pid)
    a.append_persona_message({
        "role": "assistant", "content": "PROBE-MEM-1 削除前に書いた記憶",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    add_core_memory(a.conn, "PROBE-CORE-1 削除前に書いたコア記憶", kind="note")
    a.conn.commit()
    info = {
        "db_path": a.settings.db_path,
        "messages": a.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
    }
    a.close()
    return info


def read_memory(pid: str) -> dict:
    a = make_adapter(pid)
    res = {
        "db_path": a.settings.db_path,
        "recent_persona_messages": [
            m["content"][:50] for m in a.recent_persona_messages(100000)],
        "thread_summaries": [
            {"thread_id": s["thread_id"], "message_count": s["message_count"]}
            for s in a.list_thread_summaries()],
        "core_memories": [c.content[:50] for c in list_core_memories(a.conn)],
    }
    a.close()
    return res


def ai_row(SessionLocal, ai_id: str) -> dict | None:
    db = SessionLocal()
    try:
        ai = db.query(AIModel).filter_by(AIID=ai_id).first()
        if not ai:
            return None
        return {
            "AIID": ai.AIID, "AINAME": ai.AINAME,
            "SYSTEMPROMPT": (ai.SYSTEMPROMPT or "")[:60],
            "DEFAULT_MODEL": ai.DEFAULT_MODEL,
            "LIGHTWEIGHT_MODEL": getattr(ai, "LIGHTWEIGHT_MODEL", None),
            "PERSONA_ROLE": ai.PERSONA_ROLE,
            "AUTONOMY_ENABLED": ai.AUTONOMY_ENABLED,
            "CHRONICLE_ENABLED": getattr(ai, "CHRONICLE_ENABLED", None),
            "PRIVATE_ROOM_ID": ai.PRIVATE_ROOM_ID,
            "DESCRIPTION": ai.DESCRIPTION,
        }
    finally:
        db.close()


def buildings(SessionLocal) -> list:
    db = SessionLocal()
    try:
        return [[b.BUILDINGID, b.BUILDINGNAME] for b in db.query(BuildingModel).all()]
    finally:
        db.close()


def main() -> None:
    db_file = HOME / "main.db"
    if db_file.exists():
        db_file.unlink()
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    db = SessionLocal()
    try:
        db.add(CityModel(CITYID=1, USERID=1, CITY_SLUG=CITY, UI_PORT=3000, API_PORT=8000))
        db.commit()
    finally:
        db.close()

    svc = build_host(SessionLocal, str(db_file))

    # --- 1. 作成 (custom_ai_id を明示) ---
    ok, msg, ai_id, room_id = create(svc, NAME, custom_ai_id=STEM)
    OUT["create_1"] = {"ok": ok, "msg": msg, "ai_id": ai_id, "room_id": room_id}
    assert ok, msg

    # --- 2. 記憶を書く + 持ち主 ID を持つ他テーブルにも 1 行ずつ ---
    OUT["memory_write"] = write_memory(ai_id)
    OUT["row_seed"] = seed_persona_rows(SessionLocal, ai_id)
    OUT["rows_before_delete"] = count_persona_rows(SessionLocal, ai_id)
    OUT["ai_row_before_delete"] = ai_row(SessionLocal, ai_id)
    OUT["buildings_before_delete"] = buildings(SessionLocal)

    # --- 3. 削除 ---
    OUT["delete_result"] = AdminService.delete_ai(svc, ai_id)
    OUT["rows_after_delete"] = count_persona_rows(SessionLocal, ai_id)
    OUT["ai_row_after_delete"] = ai_row(SessionLocal, ai_id)
    OUT["buildings_after_delete"] = buildings(SessionLocal)
    OUT["persona_dir_after_delete"] = {
        "exists": (HOME / "personas" / ai_id).exists(),
        "files": sorted(p.name for p in (HOME / "personas" / ai_id).glob("*")),
    }

    # --- 4. 同じ ID で作り直す ---
    ok2, msg2, ai_id2, room_id2 = create(svc, NAME, custom_ai_id=STEM)
    OUT["create_2_same_id_same_name"] = {
        "ok": ok2, "msg": msg2, "ai_id": ai_id2, "room_id": room_id2}

    if not ok2:
        # 名前重複などで弾かれた場合、別名で同じ ID を試す
        ok3, msg3, ai_id3, room_id3 = create(svc, NAME + "・弐", custom_ai_id=STEM)
        OUT["create_3_same_id_new_name"] = {
            "ok": ok3, "msg": msg3, "ai_id": ai_id3, "room_id": room_id3}
        ai_id2, room_id2, ok2 = ai_id3, room_id3, ok3

    if ok2:
        OUT["rows_after_recreate"] = count_persona_rows(SessionLocal, ai_id2)
        OUT["ai_row_after_recreate"] = ai_row(SessionLocal, ai_id2)
        OUT["buildings_after_recreate"] = buildings(SessionLocal)
        OUT["memory_read_after_recreate"] = read_memory(ai_id2)

    out_path = Path(__file__).with_name("exp1_result.json")
    out_path.write_text(json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
    print("WROTE", out_path)


if __name__ == "__main__":
    main()
