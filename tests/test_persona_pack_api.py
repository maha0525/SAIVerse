"""ペルソナまるごとパックの API (api/routes/people/persona_pack.py) のテスト。

正典: docs/intent/persona_pack.md。隔離した SAIVERSE_HOME と合成のペルソナだけを使う。
manager は作成・削除・DB の場所だけを持つ薄い偽物で、埋め込みの再生成は起動の記録だけを取る。
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_manager
from database.models import AI, Base, Building
from sai_memory.clips import init_clips_tables
from sai_memory.memopedia.storage import create_page, init_memopedia_tables
from sai_memory.memory.storage import init_db
from sai_memory.purpose_tags import init_purpose_tags_tables

CITY = "city_a"
SRC = f"tester_{CITY}"
T0 = 1_760_000_000


def _init_memory_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(path))
    init_memopedia_tables(conn)
    init_clips_tables(conn)
    init_purpose_tags_tables(conn)
    conn.commit()
    conn.close()


class FakeManager:
    """API が manager に求めるもの (DB の場所・街・作成・削除) だけを持つ。"""

    def __init__(self, home: Path) -> None:
        self.saiverse_home = home
        world_db = home / "user_data" / "database" / "saiverse.db"
        world_db.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(world_db)
        self.city_name = CITY
        self.city_id = 1
        self.engine = create_engine(f"sqlite:///{world_db.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.personas: Dict[str, Any] = {}
        self.created: List[str] = []
        self.deleted: List[str] = []
        # 作成経路 (manager/persona.py の _create_persona) が私室を登録するメモリ上の表
        self.buildings: List[Any] = []
        self.building_map: Dict[str, Any] = {}
        self.capacities: Dict[str, int] = {}
        self.occupants: Dict[str, List[str]] = {}
        self.building_memory_paths: Dict[str, Path] = {}
        self.building_histories: Dict[str, List[Any]] = {}

    def add_persona(self, ai_id: str, name: str) -> str:
        room = f"{ai_id}_room"
        with Session(self.engine) as s:
            s.add(AI(AIID=ai_id, HOME_CITYID=self.city_id, AINAME=name,
                     SYSTEMPROMPT=f"you are {name}", DESCRIPTION="desc",
                     PRIVATE_ROOM_ID=room, AUTO_COUNT=0))
            s.add(Building(CITYID=self.city_id, BUILDINGID=room, BUILDINGNAME=f"{name} room"))
            s.commit()
        _init_memory_db(self.saiverse_home / "personas" / ai_id / "memory.db")
        return room

    def create_ai(self, name: str, system_prompt: str, home_city_id: int,
                  custom_ai_id: Any = None, language: Any = None) -> Tuple[bool, str, Any, Any]:
        stem = custom_ai_id or f"persona_{len(self.created) + 1}"
        ai_id = f"{stem}_{self.city_name}"
        room = self.add_persona(ai_id, name)
        with Session(self.engine) as s:
            s.query(AI).filter(AI.AIID == ai_id).update({"SYSTEMPROMPT": system_prompt})
            s.commit()
        self.created.append(ai_id)
        building = SimpleNamespace(building_id=room)
        self.buildings.append(building)
        self.building_map[room] = building
        self.capacities[room] = 1
        self.occupants[room] = [ai_id]
        self.building_memory_paths[room] = self.saiverse_home / "cities" / CITY / "buildings" / room / "log.json"
        self.building_histories[room] = []
        return True, "created", ai_id, room

    def delete_ai(self, ai_id: str) -> str:
        # 本物 (manager/admin.py の delete_ai) と同じく私室の building 行は消さない
        with Session(self.engine) as s:
            s.query(AI).filter(AI.AIID == ai_id).delete()
            s.commit()
        self.deleted.append(ai_id)
        return "deleted"


def _messages(home: Path, persona_id: str) -> List[Tuple[str, str, str]]:
    conn = sqlite3.connect(home / "personas" / persona_id / "memory.db")
    try:
        return conn.execute(
            "SELECT id, thread_id, content FROM messages ORDER BY created_at, rowid"
        ).fetchall()
    finally:
        conn.close()


def _add_message(home: Path, persona_id: str, mid: str, thread: str, content: str, created: int) -> None:
    conn = sqlite3.connect(home / "personas" / persona_id / "memory.db")
    try:
        conn.execute(
            "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, f"{persona_id}:{thread}", "assistant", content, persona_id, created,
             json.dumps({"tags": ["conversation"]})),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """書き出し元 (src) と取り込み先 (dst) の二つの隔離環境と、それぞれの TestClient。"""
    from api.routes import people
    from api.routes.people import persona_pack as route

    embedding_calls: List[Tuple[str, int]] = []
    monkeypatch.setattr(
        route, "_start_embedding",
        lambda persona_id, count: embedding_calls.append((persona_id, count)) or True,
    )

    def make(name: str) -> Dict[str, Any]:
        home = tmp_path / name / "home"
        home.mkdir(parents=True)
        manager = FakeManager(home)
        app = FastAPI()
        app.include_router(people.router, prefix="/api/people")
        app.dependency_overrides[get_manager] = lambda: manager
        return {"home": home, "manager": manager, "client": TestClient(app)}

    src = make("src")
    src["manager"].add_persona(SRC, "Tester")
    _add_message(src["home"], SRC, "m1", "room_a", "hello", T0)
    _add_message(src["home"], SRC, "m2", "room_a", "again", T0 + 1)
    conn = sqlite3.connect(src["home"] / "personas" / SRC / "memory.db")
    create_page(conn, parent_id="root_terms", title="apple", content="red fruit", category="terms")
    conn.commit()
    conn.close()
    dst = make("dst")

    # validate_persona_id は SAIVERSE_HOME の personas/ を見る。リクエストごとに
    # 取り込み先 / 書き出し元へ向け直す
    def use(env: Dict[str, Any]) -> TestClient:
        monkeypatch.setenv("SAIVERSE_HOME", str(env["home"]))
        return env["client"]

    return {"src": src, "dst": dst, "use": use, "embedding_calls": embedding_calls}


def _export_zip(world: Dict[str, Any]) -> bytes:
    res = world["use"](world["src"]).get(f"/api/people/{SRC}/persona-pack")
    assert res.status_code == 200, res.text
    return res.content


# ---------------------------------------------------------------------------
# (a) エクスポート
# ---------------------------------------------------------------------------

def test_export_returns_zip_with_manifest(world: Dict[str, Any]) -> None:
    res = world["use"](world["src"]).get(f"/api/people/{SRC}/persona-pack")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    disposition = res.headers["content-disposition"]
    assert "Tester_persona_pack_" in disposition and disposition.endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        names = set(zf.namelist())
    assert manifest["format"] == "persona-pack"
    assert manifest["persona_id"] == SRC
    assert manifest["persona_name"] == "Tester"
    assert "chatlog/messages.json" in names


def test_export_unknown_persona_is_404(world: Dict[str, Any]) -> None:
    res = world["use"](world["src"]).get("/api/people/nobody_city_a/persona-pack")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# (b) 合流インポート
# ---------------------------------------------------------------------------

def test_merge_import_returns_breakdown_and_writes(world: Dict[str, Any]) -> None:
    pack = _export_zip(world)
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    _add_message(dst["home"], target, "x1", "room_a", "already here", T0 - 100)

    client = world["use"](dst)
    res = client.post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "merge"
    assert body["source_persona_id"] == SRC
    assert body["target_persona_id"] == target
    assert body["counts"]["messages"] == {"added": 2, "updated": 0, "skipped": 0}
    assert body["counts"]["pages"]["added"] >= 1
    assert body["counts"]["profile"] == {"added": 0, "updated": 0, "skipped": 1}
    assert isinstance(body["warnings"], list)
    assert body["embedding_started"] is True
    assert world["embedding_calls"] == [(target, 2)]

    rows = _messages(dst["home"], target)
    assert [(r[0], r[1]) for r in rows] == [
        ("x1", f"{target}:room_a"), ("m1", f"{target}:room_a"), ("m2", f"{target}:room_a"),
    ]
    # プロフィールは既定で上書きしない
    with Session(dst["manager"].engine) as s:
        assert s.get(AI, target).AINAME == "Other"

    # 同じ ZIP をもう一度: 何も足されない
    again = client.post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    ).json()
    assert again["counts"]["messages"] == {"added": 0, "updated": 0, "skipped": 2}
    assert len(_messages(dst["home"], target)) == 3


def test_merge_import_overwrites_profile_when_asked(world: Dict[str, Any]) -> None:
    pack = _export_zip(world)
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
        data={"overwrite_profile": "true"},
    )
    assert res.status_code == 200, res.text
    assert "AINAME" in res.json()["profile_columns_overwritten"]
    with Session(dst["manager"].engine) as s:
        ai = s.get(AI, target)
        assert ai.AINAME == "Tester"
        assert ai.PRIVATE_ROOM_ID == f"{target}_room"  # 環境と結びついた列は触らない


def test_merge_import_refuses_while_chronicle_job_runs(world: Dict[str, Any]) -> None:
    from api.routes.people import arasuji

    pack = _export_zip(world)
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    job_id = arasuji._create_job(target)
    try:
        res = world["use"](dst).post(
            f"/api/people/{target}/persona-pack/import",
            files={"file": ("pack.zip", pack, "application/zip")},
        )
        assert res.status_code == 409, res.text
        assert _messages(dst["home"], target) == []
        # 終端したジョブは妨げない
        arasuji._update_job(job_id, status="completed")
        res = world["use"](dst).post(
            f"/api/people/{target}/persona-pack/import",
            files={"file": ("pack.zip", pack, "application/zip")},
        )
        assert res.status_code == 200, res.text
    finally:
        with arasuji._jobs_lock:
            arasuji._generation_jobs.pop(job_id, None)


def test_merge_with_chronicle_marks_cold_windows_and_predicts(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    from types import SimpleNamespace

    from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
    from sea import coverage_repair

    src_db = world["src"]["home"] / "personas" / SRC / "memory.db"
    conn = sqlite3.connect(src_db)
    init_arasuji_tables(conn)
    create_entry(conn, level=1, content="two lines", source_ids=["m1", "m2"],
                 start_time=T0, end_time=T0 + 1, source_count=2, message_count=2)
    conn.close()
    pack = _export_zip(world)

    dst = world["dst"]
    manager = dst["manager"]
    target = f"other_{CITY}"
    manager.add_persona(target, "Other")

    held: List[str] = []

    class Gate:
        @contextlib.contextmanager
        def hold(self, persona_id: str, *, purpose: str, check_gate: bool = True):
            held.append(purpose)
            try:
                yield self
            finally:
                held.pop()

    persona = SimpleNamespace(persona_id=target)
    lifecycle = object()
    manager.beat_gate = Gate()
    manager.personas[target] = persona
    manager.sea_runtime = SimpleNamespace(session_lifecycle=lifecycle)
    calls: List[Tuple[Any, Any, List[str]]] = []

    def fake_mark(lc: Any, p: Any) -> Tuple[int, int]:
        calls.append((lc, p, list(held)))
        return (1, 0)

    monkeypatch.setattr(coverage_repair, "mark_covered_cold_windows", fake_mark)
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counts"]["chronicle_pages"]["added"] == 1
    # 印は補修の完了処理と同じ関数で、Beat ロックの内側で書く
    assert calls == [(lifecycle, persona, ["persona_pack_merge"])]
    after = body["chronicle_after_merge"]
    assert after["cold_windows_marked"] == 1
    assert after["cold_window_mark_failures"] == 0
    assert after["cold_window_marks_skipped"] is False
    assert after["fold_runs_predicted"] == 0
    assert after["stale_entries"] == 0

    # Chronicle を取り込まなかった合流では何もしない
    again = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    ).json()
    assert again["chronicle_after_merge"] is None
    assert len(calls) == 1


def test_merge_import_rejects_non_pack(world: Dict[str, Any]) -> None:
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not a pack")
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("x.zip", buf.getvalue(), "application/zip")},
    )
    assert res.status_code == 400
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("x.zip", b"plain bytes", "application/zip")},
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# (c) 新しいペルソナとして迎える
# ---------------------------------------------------------------------------

def test_import_new_creates_persona_with_memories(world: Dict[str, Any]) -> None:
    pack = _export_zip(world)
    dst = world["dst"]
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    new_id = f"tester_{CITY}"
    assert body["persona_id"] == new_id
    assert body["room_id"] == f"{new_id}_room"
    assert body["persona_name"] == "Tester"
    assert body["mode"] == "new"
    assert body["counts"]["messages"]["added"] == 2
    assert body["counts"]["profile"]["updated"] == 1
    assert dst["manager"].created == [new_id]
    assert [r[0] for r in _messages(dst["home"], new_id)] == ["m1", "m2"]
    with Session(dst["manager"].engine) as s:
        assert s.get(AI, new_id).SYSTEMPROMPT == "you are Tester"
    assert world["embedding_calls"] == [(new_id, 2)]


def test_import_new_numbers_the_id_when_taken(world: Dict[str, Any]) -> None:
    pack = _export_zip(world)
    dst = world["dst"]
    # 同じ ID (tester_city_a) を別の名前のペルソナが使っている
    dst["manager"].add_persona(f"tester_{CITY}", "Someone else")
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["persona_id"] == f"tester_2_{CITY}"


def test_import_new_refuses_same_name(world: Dict[str, Any]) -> None:
    pack = _export_zip(world)
    dst = world["dst"]
    dst["manager"].add_persona(f"someone_{CITY}", "Tester")
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 409
    assert dst["manager"].created == []


def _with_broken_messages(pack: bytes) -> bytes:
    """manifest は正しいが chatlog/messages.json が壊れた ZIP。"""
    broken = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(pack)) as src, zipfile.ZipFile(broken, "w") as out:
        for item in src.infolist():
            data = b"{broken" if item.filename == "chatlog/messages.json" else src.read(item.filename)
            out.writestr(item, data)
    return broken.getvalue()


def _buildings(manager: FakeManager) -> List[str]:
    with Session(manager.engine) as s:
        return sorted(b.BUILDINGID for b in s.query(Building).all())


def test_import_new_refuses_a_broken_pack_before_creating_the_persona(world: Dict[str, Any]) -> None:
    """壊れた JSON は validate_pack が作成の前に断る (ペルソナも私室も作らない)。"""
    pack = _export_zip(world)
    dst = world["dst"]
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", _with_broken_messages(pack), "application/zip")},
    )
    assert res.status_code == 400, res.text
    assert "chatlog/messages.json" in res.json()["detail"]
    assert dst["manager"].created == []
    assert _buildings(dst["manager"]) == []


def test_import_new_removes_the_persona_and_its_room_when_import_fails(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """作成後に取り込みが失敗したら、ペルソナも私室も残さず、同じ名前で入れ直せる。"""
    from saiverse_memory.persona_pack import importer

    pack = _export_zip(world)
    dst = world["dst"]
    manager = dst["manager"]
    new_id = f"tester_{CITY}"
    room = f"{new_id}_room"
    # 作成前の検査が見逃した壊れ方 (作成後の失敗) を作るため、検査だけ素通しにする
    original_validate = importer.validate_pack
    monkeypatch.setattr(importer, "validate_pack", lambda _zip_path: None)
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", _with_broken_messages(pack), "application/zip")},
    )
    assert res.status_code == 400, res.text
    assert manager.created == [new_id]
    assert manager.deleted == [new_id]
    with Session(manager.engine) as s:
        assert s.get(AI, new_id) is None
    assert _buildings(manager) == []
    for table in (manager.building_map, manager.capacities, manager.occupants,
                  manager.building_memory_paths, manager.building_histories):
        assert room not in table
    assert [b.building_id for b in manager.buildings] == []

    # 正しい ZIP で同じ名前の新規取り込みが通る (私室の名前が衝突しない)
    monkeypatch.setattr(importer, "validate_pack", original_validate)
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["persona_id"] == new_id
    assert res.json()["room_id"] == room
    assert [r[0] for r in _messages(dst["home"], new_id)] == ["m1", "m2"]
    assert _buildings(manager) == [room]


def test_import_new_keeps_the_persona_when_its_memory_cannot_be_checked(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes.people import persona_pack as route

    pack = _export_zip(world)

    def failing_import(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom during import")

    monkeypatch.setattr(route, "_run_import", failing_import)
    monkeypatch.setattr(route, "_memory_has_messages", lambda _home, _pid: None)
    dst = world["dst"]
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    new_id = f"tester_{CITY}"
    assert res.status_code == 500, res.text
    assert new_id in res.json()["detail"]
    assert "merge" in res.json()["detail"]
    assert dst["manager"].created == [new_id]
    assert dst["manager"].deleted == []


def test_import_new_keeps_the_persona_when_the_world_part_is_pending(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory.db の commit 後に saiverse.db が失敗した回は、印を見てペルソナを残す。"""
    from api.routes.people import persona_pack as route
    from saiverse_memory.persona_pack.export import WORLD_PENDING_MARKER

    pack = _export_zip(world)
    dst = world["dst"]

    def import_then_fail(manager: Any, zip_path: Path, *, persona_id: str, **_kw: Any) -> Any:
        (dst["home"] / "personas" / persona_id / WORLD_PENDING_MARKER).write_text("{}", encoding="utf-8")
        raise RuntimeError("saiverse.db commit failed")

    monkeypatch.setattr(route, "_run_import", import_then_fail)
    monkeypatch.setattr(route, "_memory_has_messages", lambda _home, _pid: False)
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    new_id = f"tester_{CITY}"
    assert res.status_code == 500, res.text
    assert new_id in res.json()["detail"]
    assert "merge" in res.json()["detail"] and "unfinished part" in res.json()["detail"]
    assert dst["manager"].deleted == []


def test_resume_merge_starts_the_embedding_left_by_the_failed_import(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """世界側の確定に失敗した新規作成を合流で再開すると、足す message が 0 件でも
    memory.db に残った空のベクトル行の埋め込みを起こす。"""
    from saiverse_memory.persona_pack import importer

    pack = _export_zip(world)
    dst = world["dst"]

    def fail(self: Any, _world: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error (injected)")

    monkeypatch.setattr(importer._ImportJob, "commit_world", fail)
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 500, res.text
    new_id = f"tester_{CITY}"
    assert dst["manager"].deleted == []
    assert world["embedding_calls"] == []  # 失敗した回は起こさない
    monkeypatch.setattr(importer._ImportJob, "commit_world", lambda self, w: w.execute("COMMIT"))

    res = world["use"](dst).post(
        f"/api/people/{new_id}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counts"]["messages"] == {"added": 0, "updated": 0, "skipped": 2}
    assert body["embeddings_deferred"] == 0
    assert body["embedding_started"] is True
    assert world["embedding_calls"] == [(new_id, 2)]


def test_memory_has_messages_is_three_valued(tmp_path: Path) -> None:
    from api.routes.people.persona_pack import _memory_has_messages

    home = tmp_path / "home"
    assert _memory_has_messages(home, "nobody") is False  # memory.db が無い = 空
    _init_memory_db(home / "personas" / "empty" / "memory.db")
    assert _memory_has_messages(home, "empty") is False
    _init_memory_db(home / "personas" / "full" / "memory.db")
    _add_message(home, "full", "m1", "room_a", "hi", T0)
    assert _memory_has_messages(home, "full") is True
    broken = home / "personas" / "broken" / "memory.db"
    broken.parent.mkdir(parents=True)
    sqlite3.connect(broken).close()  # messages 表が無い
    assert _memory_has_messages(home, "broken") is None


def test_import_new_refuses_a_second_import_of_the_same_pack(world: Dict[str, Any]) -> None:
    from api.routes.people import persona_pack as route

    pack = _export_zip(world)
    dst = world["dst"]
    with route._claim_import(f"import-new:persona:{SRC}"):
        res = world["use"](dst).post(
            "/api/people/persona-pack/import-new",
            files={"file": ("pack.zip", pack, "application/zip")},
        )
    assert res.status_code == 409, res.text
    assert dst["manager"].created == []
    # ガードが外れたら通る
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text


def test_imports_into_different_personas_do_not_run_at_the_same_time(world: Dict[str, Any]) -> None:
    """取り込みはペルソナを問わず一度に一つ (共有メディアフォルダのファイルを並走で消し合わない)。"""
    from api.routes.people import persona_pack as route

    pack = _export_zip(world)
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    # 別のペルソナ宛ての取り込みが走っている間
    with route._claim_import(f"busy_{CITY}"):
        res = world["use"](dst).post(
            f"/api/people/{target}/persona-pack/import",
            files={"file": ("pack.zip", pack, "application/zip")},
        )
        assert res.status_code == 409, res.text
        assert "Another persona pack import" in res.json()["detail"]
        res = world["use"](dst).post(
            "/api/people/persona-pack/import-new",
            files={"file": ("pack.zip", pack, "application/zip")},
        )
        assert res.status_code == 409, res.text
        assert dst["manager"].created == []
    assert route._importing == set()
    # ガードが外れたら通る
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text


def test_failures_after_commit_become_warnings(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes.people import persona_pack as route

    def failing_embedding(_persona_id: str, _count: int) -> bool:
        raise RuntimeError("embedding launcher broke")

    monkeypatch.setattr(route, "_start_embedding", failing_embedding)
    pack = _export_zip(world)
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["embedding_started"] is False
    assert any("starting the embedding" in w and "embedding launcher broke" in w for w in body["warnings"])
    assert [r[0] for r in _messages(dst["home"], target)] == ["m1", "m2"]


def test_chronicle_follow_up_failure_reports_unknown_marks(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
    from api.routes.people import persona_pack as route

    src_db = world["src"]["home"] / "personas" / SRC / "memory.db"
    conn = sqlite3.connect(src_db)
    init_arasuji_tables(conn)
    create_entry(conn, level=1, content="two lines", source_ids=["m1", "m2"],
                 start_time=T0, end_time=T0 + 1, source_count=2, message_count=2)
    conn.close()
    pack = _export_zip(world)

    def failing_follow_up(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("follow-up broke")

    monkeypatch.setattr(route, "_chronicle_after_merge", failing_follow_up)
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counts"]["chronicle_pages"]["added"] == 1
    assert body["chronicle_after_merge"] == {
        "cold_windows_marked": None,
        "cold_window_mark_failures": None,
        "cold_window_marks_skipped": False,
        "fold_runs_predicted": None,
        "stale_entries": None,
    }
    assert any("Chronicle follow-up" in w for w in body["warnings"])


def test_start_embedding_closes_the_adapter_when_launch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import saiverse_memory
    from api.routes.people import import_chatlog
    from api.routes.people.persona_pack import _start_embedding

    closed: List[str] = []

    class FakeAdapter:
        def __init__(self, persona_id: str) -> None:
            self.persona_id = persona_id

        def close(self) -> None:
            closed.append(self.persona_id)

    def broken_launch(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("could not create the thread")

    monkeypatch.setattr(saiverse_memory, "SAIMemoryAdapter", FakeAdapter)
    monkeypatch.setattr(import_chatlog, "_launch_import_embedding", broken_launch)
    with pytest.raises(RuntimeError, match="could not create the thread"):
        _start_embedding("someone_city_a", 3)
    assert closed == ["someone_city_a"]

    monkeypatch.setattr(import_chatlog, "_launch_import_embedding", lambda *_a: True)
    assert _start_embedding("someone_city_a", 3) is True
    assert closed == ["someone_city_a"]  # 起動できたら adapter はスレッドのもの


def test_import_refuses_an_archive_that_expands_too_far(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    pack = _export_zip(world)
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_BYTES", 10)
    dst = world["dst"]
    target = f"other_{CITY}"
    dst["manager"].add_persona(target, "Other")
    res = world["use"](dst).post(
        f"/api/people/{target}/persona-pack/import",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 400, res.text
    assert "over the limit" in res.json()["detail"]
    res = world["use"](dst).post(
        "/api/people/persona-pack/import-new",
        files={"file": ("pack.zip", pack, "application/zip")},
    )
    assert res.status_code == 400, res.text
    assert dst["manager"].created == []


# ---------------------------------------------------------------------------
# (d) 旧スレッド単位エクスポートの撤去
# ---------------------------------------------------------------------------

def test_old_thread_export_endpoint_is_gone_and_import_remains(world: Dict[str, Any]) -> None:
    client = world["use"](world["src"])
    res = client.get(f"/api/people/{SRC}/threads/{SRC}:room_a/export-native")
    assert res.status_code == 404
    paths = set(client.app.openapi()["paths"])
    assert "/api/people/{persona_id}/threads/{thread_id}/export-native" not in paths
    assert "/api/people/{persona_id}/import/native" in paths
    assert "/api/people/{persona_id}/import/native/preview" in paths
    # 旧形式の取り込み口は生きている (プレビューは DB に触らない)
    archive = {"format": "saiverse_saimemory_v1", "persona_id": SRC, "threads": []}
    res = client.post(
        f"/api/people/{SRC}/import/native/preview",
        files={"file": ("old.json", json.dumps(archive).encode(), "application/json")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["format"] == "saiverse_saimemory_v1"
