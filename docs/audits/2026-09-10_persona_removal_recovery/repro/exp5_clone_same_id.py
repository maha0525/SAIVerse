"""実験 5 (追加): 同じ ID のまま「AI 行 + ペルソナディレクトリまるごと」を写すと
別環境で記憶が読めるか。

既存の `scripts/clone_persona_to_test_env.py: clone_persona` を、
**合成した source と合成した dest** の間で実際に走らせる (本番の DB / home は
一切参照しない — --source-db / --source-home / --dest-db / --dest-home を全部
scratchpad へ向ける)。

実験 2 (memory.db だけ差し替え) との対照。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_env import guard_not_production, setup_home  # noqa: E402

HOME = setup_home("exp5")
guard_not_production()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from database.models import Base, City as CityModel  # noqa: E402
from manager.persona import PersonaMixin  # noqa: E402
from manager.admin import AdminService  # noqa: E402
from saiverse_memory.adapter import SAIMemoryAdapter  # noqa: E402
from sai_memory.core_memory import add_core_memory, list_core_memories  # noqa: E402
from scripts.clone_persona_to_test_env import clone_persona  # noqa: E402

CITY = "testcity"
STEM = "probe_a"
AIID = f"{STEM}_{CITY}"
NAME = "プローブ甲"

SRC_HOME = HOME / "src_home"
DST_HOME = HOME / "dst_home"
SRC_DB = SRC_HOME / "user_data" / "database" / "saiverse.db"
DST_DB = DST_HOME / "user_data" / "database" / "saiverse.db"

OUT: dict = {"home": str(HOME)}


class _StubPersonaCore:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.private_room_id = None
        self.persona_role = None


def make_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        db.add(CityModel(CITYID=1, USERID=1, CITY_SLUG=CITY, UI_PORT=3000, API_PORT=8000))
        db.commit()
    finally:
        db.close()
    return engine, SessionLocal


def host(SessionLocal, db_path, home):
    svc = PersonaMixin.__new__(PersonaMixin)
    svc.SessionLocal = SessionLocal
    svc.db_path = str(db_path)
    svc.city_id = 1
    svc.city_name = CITY
    svc.model = "test-model"
    svc._base_model = "test-model"
    svc.saiverse_home = home
    svc.default_avatar = "avatar.png"
    svc.user_room_id = f"user_room_{CITY}"
    svc.timezone_info = None
    svc.timezone_name = "UTC"
    for attr in ("buildings",):
        setattr(svc, attr, [])
    for attr in ("building_map", "capacities", "occupants", "building_memory_paths",
                 "building_histories", "personas", "avatar_map", "id_to_name_map",
                 "persona_map", "items", "items_by_persona"):
        setattr(svc, attr, {})
    svc.get_persona_pending_events = lambda *a, **k: []
    svc.archive_persona_events = lambda *a, **k: None
    svc._on_persona_registered = lambda persona_id: None
    svc._is_seeded_entity = AdminService._is_seeded_entity
    return svc


def create(svc, name, stem):
    with patch("manager.persona.PersonaCore", _StubPersonaCore), \
         patch("manager.persona.get_model_provider", return_value="stub"), \
         patch("manager.persona.get_context_length", return_value=1000):
        return svc._create_persona(name, "PROBE-SYSTEM-PROMPT-V1", custom_ai_id=stem)


def adapter(pid, home):
    return SAIMemoryAdapter(
        persona_id=pid, persona_dir=home / "personas" / pid,
        resource_id=pid, startup_backup=False, recover_orphaned_thread=False,
    )


def main() -> None:
    src_engine, src_sess = make_db(SRC_DB)
    dst_engine, dst_sess = make_db(DST_DB)

    # --- source: ペルソナを作り、記憶を書く ---
    svc = host(src_sess, SRC_DB, SRC_HOME)
    ok, msg, ai_id, room_id = create(svc, NAME, STEM)
    OUT["source_create"] = {"ok": ok, "msg": msg, "ai_id": ai_id, "room_id": room_id}
    assert ok, msg

    a = adapter(ai_id, SRC_HOME)
    a.append_persona_message({
        "role": "assistant", "content": "PROBE-CLONE-1 移す前に書いた記憶",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    add_core_memory(a.conn, "PROBE-CLONE-CORE-1 移す前のコア記憶", kind="note")
    a.set_active_thread(f"{ai_id}:__persona__")
    a.conn.commit()
    a.close()

    # --- dest: 同じ City だけある空環境 (Building は無い) ---
    OUT["dest_before"] = {
        "persona_dir_exists": (DST_HOME / "personas" / ai_id).exists(),
    }

    # --- clone ---
    try:
        summary = clone_persona(
            ai_id, source_db=SRC_DB, source_home=SRC_HOME,
            dest_db=DST_DB, dest_home=DST_HOME, force=True,
        )
        OUT["clone_summary"] = {
            k: (str(v) if isinstance(v, Path) else v) for k, v in summary.items()
        }
    except Exception as exc:  # noqa: BLE001
        OUT["clone_summary"] = {"error": f"{type(exc).__name__}: {exc}"}

    # --- dest で読み出す ---
    try:
        b = adapter(ai_id, DST_HOME)
        OUT["dest_read"] = {
            "db_path": b.settings.db_path,
            "recent_persona_messages": [
                m["content"][:50] for m in b.recent_persona_messages(100000)],
            "thread_summaries": [
                {"thread_id": s["thread_id"], "message_count": s["message_count"]}
                for s in b.list_thread_summaries()],
            "core_memories": [c.content[:50] for c in list_core_memories(b.conn)],
            "current_thread": b.get_current_thread(),
        }
        b.close()
    except Exception as exc:  # noqa: BLE001
        OUT["dest_read"] = {"error": f"{type(exc).__name__}: {exc}"}

    out_path = Path(__file__).with_name("exp5_result.json")
    out_path.write_text(json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(OUT, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
