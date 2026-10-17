"""ペルソナまるごとパック (persona-pack) の取り込み (新規作成と合流) のテスト。

tmp_path に隔離した二つの環境 (SAIVERSE_HOME 相当のルート・saiverse.db・
memory.db) を作り、片方で書き出した ZIP をもう片方へ取り込んで検証する。
正典: docs/intent/persona_pack.md §5。
"""
from __future__ import annotations

import base64
import copy
import json
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database.models import (
    AI,
    AddonPersonaConfig,
    Base,
    Building,
    Fixture,
    Item,
    ItemLocation,
    PersonaEventLog,
    PersonaTask,
    PersonaTaskStep,
    TaskBookEntry,
)
from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
from sai_memory.clips import add_clip, init_clips_tables
from sai_memory.core_memory import add_core_memory, init_core_memory_table
from sai_memory.desk import init_desk_tables
from sai_memory.memopedia.storage import create_page, init_memopedia_tables
from sai_memory.memory.pocketbook import add_activity, add_memo
from sai_memory.memory.storage import DEFERRED_VECTOR, init_db
from sai_memory.perception_buffer import init_perception_buffer_table
from sai_memory.purpose_tags import init_purpose_tags_tables
from saiverse_memory.persona_pack import (
    PROFILE_PROTECTED_COLUMNS,
    export_persona_pack,
    import_persona_pack,
)

PID = "tester_city_a"
SIG_A = b"\x00\x01\x02\xffsignature-a\x80"
SIG_B = bytes(range(256))
T0 = 1_760_000_000


# ---------------------------------------------------------------------------
# 合成の環境
# ---------------------------------------------------------------------------

def _make_env(root: Path, persona_id: str, *, name: str = "テスト子", city_id: int = 1) -> Dict[str, Any]:
    """空のペルソナが一体いる隔離環境を作る (ai 行・私室・初期化済みの memory.db)。"""
    home = root / "home"
    for sub in ("image", "documents", "user_data/icons", "user_data/database"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    persona_dir = home / "personas" / persona_id
    persona_dir.mkdir(parents=True)
    world_db = home / "user_data" / "database" / "saiverse.db"
    engine = create_engine(f"sqlite:///{world_db.as_posix()}")
    Base.metadata.create_all(engine)
    room_id = f"{persona_id}_room"
    with Session(engine) as s:
        s.add(AI(
            AIID=persona_id, HOME_CITYID=city_id, AINAME=name,
            SYSTEMPROMPT=f"あなたは{name}です。", DESCRIPTION="説明",
            PRIVATE_ROOM_ID=room_id, AUTO_COUNT=0,
        ))
        s.add(Building(CITYID=city_id, BUILDINGID=room_id, BUILDINGNAME=f"{name}の部屋"))
        s.commit()
    engine.dispose()
    memory_db = persona_dir / "memory.db"
    conn = init_db(str(memory_db))
    init_memopedia_tables(conn)
    init_clips_tables(conn)
    init_purpose_tags_tables(conn)
    init_desk_tables(conn)
    init_perception_buffer_table(conn)
    init_arasuji_tables(conn)
    init_core_memory_table(conn)
    conn.commit()
    conn.close()
    return {
        "home": home, "world_db": world_db, "memory_db": memory_db,
        "persona_dir": persona_dir, "persona_id": persona_id, "room_id": room_id,
    }


def _mem(env: Dict[str, Any]) -> sqlite3.Connection:
    return sqlite3.connect(env["memory_db"])


def _add_message(conn: sqlite3.Connection, persona_id: str, mid: str, thread: str, role: str,
                 content: str, created: int, metadata: Dict[str, Any], **cols: Any) -> None:
    names = ["id", "thread_id", "role", "content", "resource_id", "created_at", "metadata"]
    values: List[Any] = [mid, f"{persona_id}:{thread}", role, content, persona_id, created,
                         json.dumps(metadata, ensure_ascii=False)]
    for k, v in cols.items():
        names.append(k)
        values.append(v)
    conn.execute(
        f"INSERT INTO messages({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        values,
    )


def _populate(env: Dict[str, Any]) -> Dict[str, Any]:
    """書き出し元のペルソナに、全カテゴリの記録を一通り書く。"""
    pid = env["persona_id"]
    home: Path = env["home"]
    (home / "image" / "pic.png").write_bytes(b"\x89PNG original image")
    (home / "image" / "pic.png.summary.txt").write_text("絵の要約", encoding="utf-8")
    icon = home / "user_data" / "icons" / f"{pid}_icon.webp"
    icon.write_bytes(b"RIFF icon bytes")
    (env["persona_dir"] / "active_state.json").write_text('{"active_thread_id": "room_a"}', encoding="utf-8")
    (env["persona_dir"] / "day_reports").mkdir()
    (env["persona_dir"] / "day_reports" / "2026-10-01.md").write_text("# 十月一日", encoding="utf-8")

    conn = _mem(env)
    conn.execute(
        "INSERT INTO threads(id, resource_id, overview, overview_updated_at, title) VALUES (?,?,?,?,?)",
        (f"{pid}:room_a", pid, "概要", T0, "部屋 A の会話"),
    )
    conn.execute("INSERT INTO threads(id, resource_id) VALUES (?, ?)", (f"{pid}:room_b", pid))
    conn.execute("INSERT INTO threads(id, resource_id) VALUES (?, ?)", (f"{pid}:stelis_1", pid))
    conn.execute(
        "INSERT INTO stelis_threads(thread_id, parent_thread_id, depth, status, created_at, label) "
        "VALUES (?,?,?,?,?,?)",
        (f"{pid}:stelis_1", f"{pid}:room_a", 1, "active", T0, "寄り道"),
    )
    pic_meta = {"uri": "saiverse://image/pic.png", "path": str(home / "image" / "pic.png"),
                "mime_type": "image/png"}
    # 同じ秒 (T0) に二つのスレッドへ交互に書かれた 3 行
    _add_message(conn, pid, "m1", "room_a", "user", "こんにちは", T0,
                 {"tags": ["conversation"], "images": [pic_meta]})
    _add_message(conn, pid, "m2", "room_b", "assistant", "B の返事", T0, {"tags": ["conversation"]},
                 thought_signature=SIG_B, pulse_id="pulse-1")
    _add_message(conn, pid, "m3", "room_a", "assistant", "  A の返事  ", T0,
                 {"tags": ["conversation"]}, thought_signature=SIG_A, line_role="main_line")
    _add_message(conn, pid, "m4", "room_a", "assistant", "内心", T0 + 1, {"tags": ["internal"]})
    _add_message(conn, pid, "m5", "stelis_1", "assistant", "寄り道の話", T0 + 2, {"tags": ["conversation"]})
    _add_message(conn, pid, "m6", "room_a", "user", "", T0 + 3, {"tags": ["conversation"]})
    conn.commit()

    apple = create_page(conn, parent_id="root_terms", title="りんご", content="赤い果物",
                        category="terms", keywords=["果物"])
    banana = create_page(conn, parent_id="root_terms", title="ばなな", content="黄色い果物",
                         category="terms")
    conn.execute("UPDATE memopedia_pages SET created_at = ?, updated_at = ? WHERE id IN (?, ?)",
                 (T0, T0 + 10, apple.id, banana.id))
    core_id = add_core_memory(conn, "大事なこと")
    chronicle = create_entry(conn, level=1, content="三人で話した", source_ids=["m1", "m2", "m3"],
                             start_time=T0, end_time=T0, source_count=3, message_count=3)
    add_clip(conn, message_id="m1", quote="こんにちは")
    conn.execute(
        "INSERT INTO purpose_tags(tag_id, target_ref, purpose_ref, layer, created_at) VALUES (?,?,?,?,?)",
        ("tag-1", "memopedia:1", "purpose:x", 1, T0),
    )
    activity = add_activity(conn, "読書", "user", born_at=T0)
    add_memo(conn, activity.id, "2026-10-01", "did", "本を読んだ")
    conn.execute(
        "INSERT INTO desk_items(ref, opened_at, last_touched_at) VALUES (?,?,?)", ("memopedia:1", T0, T0)
    )
    conn.commit()
    conn.close()

    world = sqlite3.connect(env["world_db"])
    world.execute("UPDATE ai SET AVATAR_IMAGE = ?, DEFAULT_MODEL = ? WHERE AIID = ?",
                  (str(icon), "gemini-3-flash", pid))
    world.commit()
    world.close()
    # 私室の見た目と中の物、所持品 (鞄とその中身)、世界側の生活の記録
    (home / "image" / "room.png").write_bytes(b"\x89PNG room image")
    (home / "documents" / "note.txt").write_text("持ち物の文書", encoding="utf-8")
    (home / "documents" / "shelf.txt").write_text("本棚の目録", encoding="utf-8")
    engine = create_engine(f"sqlite:///{Path(env['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(TaskBookEntry(TASK_ID="tb-1", PERSONA_ID=pid, CONTENT="明日また話す",
                            ORIGIN="conversation", CREATED_AT=T0, DUE_AT=T0 + 86400))
        room = s.get(Building, env["room_id"])
        room.IMAGE_PATH = "image/room.png"
        room.DESCRIPTION = "窓辺に机のある部屋"
        room.SYSTEM_INSTRUCTION = "静かな部屋です。"
        s.add(Fixture(FIXTURE_ID="fx-1", BUILDING_ID=env["room_id"], NAME="本棚",
                      FILE_PATH="documents/shelf.txt"))
        s.add(Item(ITEM_ID="item-doc", NAME="ノート", TYPE="document", DESCRIPTION="日記帳",
                   FILE_PATH="documents/note.txt", CREATOR_ID=pid))
        s.add(Item(ITEM_ID="item-bag", NAME="鞄", TYPE="bag"))
        s.add(Item(ITEM_ID="item-stone", NAME="小石", TYPE="object"))
        s.add(ItemLocation(ITEM_ID="item-doc", OWNER_KIND="building", OWNER_ID=env["room_id"], SLOT_NUMBER=1))
        s.add(ItemLocation(ITEM_ID="item-bag", OWNER_KIND="persona", OWNER_ID=pid, SLOT_NUMBER=1))
        s.add(ItemLocation(ITEM_ID="item-stone", OWNER_KIND="bag", OWNER_ID="item-bag", SLOT_NUMBER=1))
        s.add(PersonaEventLog(PERSONA_ID=pid, CONTENT="メールが届いた"))
        s.add(PersonaTask(id="task-1", persona_id=pid, title="本を読む"))
        s.add(PersonaTaskStep(id="step-1", task_id="task-1", position=1, title="一章"))
        s.commit()
    engine.dispose()
    return {"apple": apple.id, "banana": banana.id, "core_id": core_id, "chronicle": chronicle.id}


def _export(env: Dict[str, Any], out: Path) -> Path:
    export_persona_pack(
        env["persona_id"],
        saiverse_db_path=env["world_db"],
        saiverse_home=env["home"],
        output_path=out,
        generator_version="9.9.9",
        now=datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
    )
    return out


def _import(env: Dict[str, Any], zip_path: Path, mode: str, **kw: Any):
    return import_persona_pack(
        zip_path,
        mode=mode,
        target_persona_id=env["persona_id"],
        saiverse_db_path=env["world_db"],
        saiverse_home=env["home"],
        **kw,
    )


def _zjson(zip_path: Path, name: str) -> Any:
    with zipfile.ZipFile(zip_path) as zf:
        if name not in zf.namelist():
            return None
        return json.loads(zf.read(name).decode("utf-8"))


def _dump(env: Dict[str, Any]) -> Dict[str, Any]:
    """両方の DB の全テーブルと SAIVERSE_HOME のファイル一覧 (DB ファイル自体を除く)。"""
    out: Dict[str, Any] = {}
    for key in ("memory_db", "world_db"):
        conn = sqlite3.connect(env[key])
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )]
            for t in tables:
                out[f"{key}:{t}"] = sorted(repr(r) for r in conn.execute(f'SELECT rowid, * FROM "{t}"'))
        finally:
            conn.close()
    home: Path = env["home"]
    out["files"] = sorted(
        (p.relative_to(home).as_posix(), p.read_bytes())
        for p in home.rglob("*")
        if p.is_file() and not p.name.startswith("memory.db") and not p.name.startswith("saiverse.db")
    )
    out["dirs"] = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_dir())
    return out


def _items_view(zip_path: Path) -> Dict[str, Any]:
    """items/items.json を ID に依らない形にする: {名前: (種類, 説明, 置き場所, 番号, ファイルの中身, item:N)}。"""
    doc = _zjson(zip_path, "items/items.json")
    by_id = {i["ITEM_ID"]: i for i in doc["items"]}
    view: Dict[str, Any] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for loc in doc["locations"]:
            item = by_id[loc["ITEM_ID"]]
            owner = by_id[loc["OWNER_ID"]]["NAME"] if loc["OWNER_KIND"] == "bag" else loc["OWNER_ID"]
            arc = doc["files"].get(item["ITEM_ID"])
            view[item["NAME"]] = (
                item["TYPE"], item["DESCRIPTION"], item["CREATOR_ID"], loc["OWNER_KIND"], owner,
                loc["SLOT_NUMBER"], zf.read(arc) if arc else None, item["SHORT_ID"],
            )
    return view


def _room_view(zip_path: Path) -> Dict[str, Any]:
    """items/private_room.json の見た目と設定、fixture とそのファイルの中身。"""
    doc = _zjson(zip_path, "items/private_room.json")
    with zipfile.ZipFile(zip_path) as zf:
        image = zf.read(doc["files"]["IMAGE_PATH"]) if doc["files"]["IMAGE_PATH"] else None
        fixtures = {
            f["NAME"]: (f["FIXTURE_ID"], zf.read(doc["files"]["fixtures"][f["FIXTURE_ID"]]))
            for f in doc["fixtures"]
        }
    building = doc["building"]
    return {
        "BUILDINGID": building["BUILDINGID"],
        "DESCRIPTION": building["DESCRIPTION"],
        "SYSTEM_INSTRUCTION": building["SYSTEM_INSTRUCTION"],
        "image": image,
        "fixtures": fixtures,
    }


def _derived(target_persona_id: str, kind: str, original: str) -> str:
    """取り込みが振り直す ID (importer._ImportJob.derived_id と同じ uuid5)。"""
    from saiverse_memory.persona_pack import importer

    return str(uuid.uuid5(importer._ID_NAMESPACE, f"{target_persona_id}/{kind}/{original}"))


def _strip_paths(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = copy.deepcopy(messages)
    for m in out:
        meta = m.get("metadata")
        if isinstance(meta, dict):
            for key in ("images", "documents", "media"):
                for entry in meta.get(key) or []:
                    entry.pop("path", None)
    return out


@pytest.fixture()
def source(tmp_path: Path) -> Dict[str, Any]:
    env = _make_env(tmp_path / "src", PID)
    env["ids"] = _populate(env)
    env["zip"] = _export(env, tmp_path / "out" / "src.zip")
    return env


# ---------------------------------------------------------------------------
# 1. 往復
# ---------------------------------------------------------------------------

def test_round_trip_new_import_then_reexport(source: Dict[str, Any], tmp_path: Path) -> None:
    target = _make_env(tmp_path / "dst", PID)
    result = _import(target, source["zip"], "new")
    assert result.counts["messages"].added == 6
    assert result.counts["threads"].added == 3
    assert result.counts["chronicle_pages"].added == 1
    assert result.renumbered == {}
    assert result.embeddings_deferred == 5  # 本文が空の m6 は除く

    again = _export(target, tmp_path / "out" / "again.zip")
    src_zip = source["zip"]

    for name in ("chatlog/threads.json", "chatlog/stelis_threads.json"):
        assert _zjson(again, name) == _zjson(src_zip, name), name
    src_msgs = _zjson(src_zip, "chatlog/messages.json")
    new_msgs = _zjson(again, "chatlog/messages.json")
    assert _strip_paths(new_msgs) == _strip_paths(src_msgs)
    # 添付の path は取り込み先の共有メディアフォルダを指す
    m1 = next(m for m in new_msgs if m["id"] == "m1")
    assert m1["metadata"]["images"][0]["path"] == str(target["home"] / "image" / "pic.png")
    assert (target["home"] / "image" / "pic.png").read_bytes() == b"\x89PNG original image"
    assert (target["home"] / "image" / "pic.png.summary.txt").read_text(encoding="utf-8") == "絵の要約"

    # knowledge: 取り込み先の init が作る root_* ページ以外は一致 (short_id も保たれる)
    def pages(zip_path: Path) -> Dict[str, Any]:
        return {p["id"]: p for p in _zjson(zip_path, "knowledge/pages.json") if not p["id"].startswith("root_")}
    assert pages(again) == pages(src_zip)

    # journal
    # task_book の TASK_ID は衝突しなければ元の値のまま
    for name in ("journal/clips.json", "journal/purpose_tags.json", "journal/task_book.json"):
        assert _zjson(again, name) == _zjson(src_zip, name), name

    def acts(zip_path: Path) -> List[Dict[str, Any]]:
        return [{k: v for k, v in a.items() if k != "id"} for a in _zjson(zip_path, "journal/activities.json")]
    assert acts(again) == acts(src_zip)

    def memos(zip_path: Path) -> List[Dict[str, Any]]:
        by_id = {a["id"]: a["uuid"] for a in _zjson(zip_path, "journal/activities.json")}
        return [
            {**{k: v for k, v in m.items() if k not in ("id", "activity_id")},
             "activity_uuid": by_id[m["activity_id"]]}
            for m in _zjson(zip_path, "journal/memos.json")
        ]
    assert memos(again) == memos(src_zip)
    with zipfile.ZipFile(again) as a, zipfile.ZipFile(src_zip) as s:
        assert a.read("journal/day_reports/2026-10-01.md") == s.read("journal/day_reports/2026-10-01.md")

    # DB レベル: bytes と rowid (同じ秒の並び順の鍵) が元のまま
    src = _mem(source)
    dst = _mem(target)
    try:
        assert dst.execute("SELECT thought_signature FROM messages WHERE id='m2'").fetchone()[0] == SIG_B
        assert dst.execute("SELECT thought_signature FROM messages WHERE id='m3'").fetchone()[0] == SIG_A
        assert dict(dst.execute("SELECT id, rowid FROM messages")) == dict(src.execute("SELECT id, rowid FROM messages"))
        # embeddings は運ばれず、空のベクトル行として再生成を待つ
        deferred = {r[0] for r in dst.execute(
            "SELECT message_id FROM message_embeddings WHERE vector = ?", (DEFERRED_VECTOR,)
        )}
        assert deferred == {"m1", "m2", "m3", "m4", "m5"}
        # extensions/ (mode="new") も入る
        assert dst.execute("SELECT ref FROM desk_items").fetchall() == [("memopedia:1",)]
    finally:
        src.close()
        dst.close()
    assert (target["persona_dir"] / "active_state.json").read_text(encoding="utf-8") == '{"active_thread_id": "room_a"}'

    # items/: 私室の見た目と中の物・所持品が世界へ登録され、再エクスポートで同じ形に戻る
    assert _items_view(again) == _items_view(src_zip)
    src_item_ids = {i["ITEM_ID"] for i in _zjson(src_zip, "items/items.json")["items"]}
    new_item_ids = {i["ITEM_ID"] for i in _zjson(again, "items/items.json")["items"]}
    assert not src_item_ids & new_item_ids  # ITEM_ID は取り込み先で振り直す
    assert _room_view(again) == _room_view(src_zip)
    assert not (target["persona_dir"] / "imported_items").exists()
    assert not (target["persona_dir"] / "persona_pack_world_pending.json").exists()
    assert result.counts["items"].added == 4  # item 3 + fixture 1
    assert result.counts["items"].updated == 1  # 私室の設定
    # extensions/saiverse/saiverse_db/ も入る (文字列の主キーと親子の参照は同じ写像で振り直す)
    ext = "extensions/saiverse/saiverse_db/"
    assert _zjson(again, ext + "persona_event_log.json") == _zjson(src_zip, ext + "persona_event_log.json")
    src_steps = _zjson(src_zip, ext + "persona_task_step.json")
    for row in src_steps:
        row["id"] = _derived(PID, "persona_task_step", row["id"])
        row["task_id"] = _derived(PID, "persona_task", row["task_id"])
    assert _zjson(again, ext + "persona_task_step.json") == src_steps
    assert [t["id"] for t in _zjson(again, ext + "persona_task.json")] == [_derived(PID, "persona_task", "task-1")]


def test_new_mode_refuses_persona_with_messages(source: Dict[str, Any], tmp_path: Path) -> None:
    target = _make_env(tmp_path / "dst", PID)
    _import(target, source["zip"], "new")
    before = _dump(target)
    with pytest.raises(ValueError, match="mode='merge'"):
        _import(target, source["zip"], "new")
    assert _dump(target) == before


# ---------------------------------------------------------------------------
# 2. 合流
# ---------------------------------------------------------------------------

@pytest.fixture()
def diverged(source: Dict[str, Any], tmp_path: Path) -> Dict[str, Any]:
    """同じ元から分岐した二つの環境 A (= source) と B を作る。"""
    a = source
    b = _make_env(tmp_path / "b", PID)
    _import(b, a["zip"], "new")
    ids = a["ids"]

    # A 側で起きたこと
    conn = _mem(a)
    _add_message(conn, PID, "mA", "room_a", "user", "A での続き", T0 + 100, {"tags": ["conversation"]})
    conn.execute("UPDATE memopedia_pages SET content = ?, updated_at = ? WHERE id = ?",
                 ("A で書き足した", T0 + 500, ids["apple"]))
    conn.execute("UPDATE memopedia_pages SET content = ?, updated_at = ? WHERE id = ?",
                 ("A の古い改訂", T0 + 550, ids["banana"]))
    conn.commit()
    page_a = create_page(conn, parent_id="root_terms", title="A の新語", content="A", category="terms")
    core_a = add_core_memory(conn, "A のコア")
    create_entry(conn, level=1, content="A での続きの話", source_ids=["mA"],
                 start_time=T0 + 100, end_time=T0 + 100, source_count=1, message_count=1)
    walk_a = add_activity(conn, "散歩", "user", born_at=T0 + 100)
    add_memo(conn, walk_a.id, "2026-10-02", "did", "A で歩いた")
    conn.close()
    (a["persona_dir"] / "day_reports" / "2026-10-02.md").write_text("A の一日", encoding="utf-8")

    # B 側で起きたこと
    conn = _mem(b)
    _add_message(conn, PID, "mB", "room_b", "user", "B での続き", T0 + 101, {"tags": ["conversation"]})
    conn.execute("UPDATE memopedia_pages SET content = ?, updated_at = ? WHERE id = ?",
                 ("B の新しい改訂", T0 + 600, ids["banana"]))
    conn.execute("INSERT INTO memopedia_embeddings(page_id, vector) VALUES (?, ?)", (ids["apple"], "[0.1]"))
    conn.commit()
    page_b = create_page(conn, parent_id="root_terms", title="B の新語", content="B", category="terms")
    core_b = add_core_memory(conn, "B のコア")
    add_activity(conn, "散歩", "user", born_at=T0 + 101)
    conn.close()
    (b["persona_dir"] / "day_reports").mkdir(exist_ok=True)
    (b["persona_dir"] / "day_reports" / "2026-10-02.md").write_text("B の一日", encoding="utf-8")

    a["zip2"] = _export(a, tmp_path / "out" / "a2.zip")
    return {"a": a, "b": b, "page_a": page_a, "page_b": page_b, "core_a": core_a, "core_b": core_b}


def test_merge_mixes_append_only_records_and_takes_newer_pages(diverged: Dict[str, Any]) -> None:
    a, b = diverged["a"], diverged["b"]
    ids = a["ids"]
    result = _import(b, a["zip2"], "merge")

    assert result.counts["messages"].added == 1
    assert result.counts["messages"].skipped == 6
    conn = _mem(b)
    try:
        msg_ids = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY created_at, rowid")]
        assert sorted(msg_ids) == sorted(["m1", "m2", "m3", "m4", "m5", "m6", "mA", "mB"])

        content = dict(conn.execute("SELECT id, content FROM memopedia_pages"))
        # りんご: A の改訂が新しい → 取り込む。ばなな: B の改訂が新しい → B のまま
        assert content[ids["apple"]] == "A で書き足した"
        assert content[ids["banana"]] == "B の新しい改訂"
        assert result.counts["pages"].updated == 1
        hist = conn.execute(
            "SELECT before_content, edit_source FROM memopedia_page_edit_history "
            "WHERE page_id = ? AND edit_source = 'persona_pack_import'", (ids["apple"],)
        ).fetchall()
        assert hist == [("赤い果物", "persona_pack_import")]
        assert conn.execute(
            "SELECT COUNT(*) FROM memopedia_embeddings WHERE page_id = ?", (ids["apple"],)
        ).fetchone()[0] == 0

        # 番号の衝突: A の新しいページは B の MAX+1 から振り直され、元の番号が metadata に残る
        page_a = diverged["page_a"]
        sid, meta = conn.execute(
            "SELECT short_id, metadata FROM memopedia_pages WHERE id = ?", (page_a.id,)
        ).fetchone()
        assert sid != page_a.short_id
        assert json.loads(meta)["original_short_id"] == page_a.short_id
        sids = [r[0] for r in conn.execute("SELECT short_id FROM memopedia_pages WHERE short_id IS NOT NULL")]
        assert len(sids) == len(set(sids))
        # コア記憶の core_id も同じ規則 (機構が書いた表題も合わせる)
        cores = conn.execute(
            "SELECT title, metadata FROM memopedia_pages WHERE category = 'core' AND is_trunk = 0"
        ).fetchall()
        core_ids = [json.loads(m)["core_id"] for _, m in cores]
        assert len(core_ids) == len(set(core_ids)) == 3
        moved = [(t, json.loads(m)) for t, m in cores if json.loads(m).get("original_core_id")]
        assert len(moved) == 1
        title, meta = moved[0]
        assert meta["original_core_id"] == diverged["core_a"]
        assert title == f"コア記憶 c:{meta['core_id']}"

        # Chronicle: A で新しく作られた Lv1 (mA だけを覆う) は入り、共有の Lv1 は
        # 同じ id が取り込み先にあるのでそのまま (intent §5)
        assert conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE category = 'chronicle' AND content = ?",
            ("A での続きの話",),
        ).fetchone()[0] == 1
        assert result.counts["chronicle_pages"].skipped == 1
        assert result.counts["chronicle_pages"].added == 1

        # 手帳: open な同名「散歩」は一つに合流し、A のメモはそちらへ付く
        walks = conn.execute("SELECT id FROM activities WHERE name = '散歩' AND status = 'open'").fetchall()
        assert len(walks) == 1
        assert conn.execute(
            "SELECT activity_id FROM memos WHERE text = 'A で歩いた'"
        ).fetchone()[0] == walks[0][0]
    finally:
        conn.close()

    # 一日新聞: 同じ日付で内容が違えば両方残す
    reports = b["persona_dir"] / "day_reports"
    assert (reports / "2026-10-02.md").read_text(encoding="utf-8") == "B の一日"
    assert (reports / f"2026-10-02.from_{PID}.md").read_text(encoding="utf-8") == "A の一日"
    assert result.counts["day_reports"].added == 1
    assert result.counts["day_reports"].skipped == 1

    # items/ と extensions/ は合流では取り込まず、スキップ件数で報告する
    assert result.counts["items"].added == 0 and result.counts["items"].skipped > 0
    assert result.counts["extensions/memory_db/desk_items"].skipped == 1
    assert result.counts["extensions/persona_dir"].skipped >= 1
    assert result.counts["profile"].skipped == 1
    assert any("散歩" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 3. 冪等
# ---------------------------------------------------------------------------

def test_merge_twice_changes_nothing(diverged: Dict[str, Any]) -> None:
    a, b = diverged["a"], diverged["b"]
    _import(b, a["zip2"], "merge")
    before = _dump(b)
    second = _import(b, a["zip2"], "merge")
    assert all(c.added == 0 and c.updated == 0 for c in second.counts.values()), second.counts
    assert second.renumbered == {}
    assert _dump(b) == before


def test_merged_activity_closed_later_is_not_revived_by_reimport(
    diverged: Dict[str, Any], tmp_path: Path
) -> None:
    """同名合流の対応は memory.db に残り、合流先を閉じた後の再取り込みで空の活動を増やさない。"""
    from saiverse_memory.persona_pack.export import NOT_CARRIED_MEMORY_TABLES
    from saiverse_memory.persona_pack.importer import _ACTIVITY_MERGES_TABLE

    a, b = diverged["a"], diverged["b"]
    conn = _mem(a)
    try:
        (a_walk_uuid,) = conn.execute("SELECT uuid FROM activities WHERE name = '散歩'").fetchone()
    finally:
        conn.close()
    _import(b, a["zip2"], "merge")
    conn = _mem(b)
    try:
        ((walk_id, walk_uuid),) = conn.execute(
            "SELECT id, uuid FROM activities WHERE name = '散歩'"
        ).fetchall()
        assert a_walk_uuid != walk_uuid
        assert conn.execute(
            f"SELECT ORIGIN_UUID, ACTIVITY_UUID FROM {_ACTIVITY_MERGES_TABLE}"
        ).fetchall() == [(a_walk_uuid, walk_uuid)]
        # 取り込み先の利用者が合流先を閉じる
        conn.execute(
            "UPDATE activities SET status = 'closed', closed_at = ? WHERE id = ?", (T0 + 900, walk_id)
        )
        conn.commit()
        activities_before = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        memos_before = conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
    finally:
        conn.close()

    again = _import(b, a["zip2"], "merge")
    assert again.counts["activities"].added == 0
    assert again.counts["memos"].added == 0
    conn = _mem(b)
    try:
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == activities_before
        assert conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0] == memos_before
        assert conn.execute(
            "SELECT id, status FROM activities WHERE name = '散歩'"
        ).fetchall() == [(walk_id, "closed")]
        assert conn.execute(
            "SELECT activity_id FROM memos WHERE text = 'A で歩いた'"
        ).fetchall() == [(walk_id,)]
    finally:
        conn.close()

    # 対応は取り込み先のローカルの事実なので書き出さない
    assert _ACTIVITY_MERGES_TABLE in NOT_CARRIED_MEMORY_TABLES
    out = _export(b, tmp_path / "out" / "b_after_merge.zip")
    with zipfile.ZipFile(out) as zf:
        assert not any(_ACTIVITY_MERGES_TABLE in n for n in zf.namelist())


def test_merge_after_new_import_of_same_pack_changes_nothing(source: Dict[str, Any], tmp_path: Path) -> None:
    target = _make_env(tmp_path / "dst", PID)
    _import(target, source["zip"], "new")
    before = _dump(target)
    second = _import(target, source["zip"], "merge")
    assert all(c.added == 0 and c.updated == 0 for c in second.counts.values()), second.counts
    assert _dump(target) == before


# ---------------------------------------------------------------------------
# 4. ID の置き換え
# ---------------------------------------------------------------------------

def test_merge_from_other_persona_id_remaps_threads_and_keeps_provenance(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    other = "bob_city_b"
    target = _make_env(tmp_path / "bob", other, name="ボブ", city_id=2)
    # 取り込み先に同名で中身の違う画像がある → 改名して衝突を避ける
    (target["home"] / "image" / "pic.png").write_bytes(b"bob's own picture")
    conn = _mem(target)
    conn.execute("INSERT INTO threads(id, resource_id) VALUES (?, ?)", (f"{other}:room_a", other))
    _add_message(conn, other, "b1", "room_a", "user", "ボブの記録", T0 - 50, {"tags": ["conversation"]})
    conn.commit()
    conn.close()

    result = _import(target, source["zip"], "merge")
    assert result.source_persona_id == PID
    assert result.counts["messages"].added == 6

    conn = _mem(target)
    try:
        threads = {r[0]: r[1] for r in conn.execute("SELECT id, resource_id FROM threads")}
        assert set(threads) == {f"{other}:room_a", f"{other}:room_b", f"{other}:stelis_1"}
        assert set(threads.values()) == {other}
        rows = {r[0]: r for r in conn.execute("SELECT id, thread_id, resource_id, metadata FROM messages")}
        assert rows["m3"][1] == f"{other}:room_a"
        assert rows["m5"][1] == f"{other}:stelis_1"
        assert rows["m3"][2] == other
        meta = json.loads(rows["m3"][3])
        assert meta["transplanted_from"] == {"persona_id": PID, "thread_id": f"{PID}:room_a"}
        assert "transplanted_from" not in json.loads(rows["b1"][3])
        parent = conn.execute(
            "SELECT parent_thread_id FROM stelis_threads WHERE thread_id = ?", (f"{other}:stelis_1",)
        ).fetchone()[0]
        assert parent == f"{other}:room_a"
        # 添付は改名して置き、取り込んだ message の uri / path をそこへ合わせる
        image = json.loads(rows["m1"][3])["images"][0]
        assert image["uri"] == "saiverse://image/pic_1.png"
        assert image["path"] == str(target["home"] / "image" / "pic_1.png")
        assert json.loads(rows["m1"][3])["transplanted_from"]["thread_id"] == f"{PID}:room_a"
    finally:
        conn.close()
    assert (target["home"] / "image" / "pic.png").read_bytes() == b"bob's own picture"
    assert (target["home"] / "image" / "pic_1.png").read_bytes() == b"\x89PNG original image"
    assert (target["home"] / "image" / "pic_1.png.summary.txt").is_file()
    world = sqlite3.connect(target["world_db"])
    try:
        # 取り込み先の世界に同じ TASK_ID が無いので元の値のまま
        assert world.execute(
            "SELECT PERSONA_ID FROM task_book WHERE TASK_ID = ?", ("tb-1",)
        ).fetchone()[0] == other
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 5. 単一トランザクション
# ---------------------------------------------------------------------------

def _rewrite_zip(src: Path, dst: Path, replace: Dict[str, bytes]) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, replace.get(item.filename, zin.read(item.filename)))
    return dst


def test_broken_json_changes_nothing(source: Dict[str, Any], tmp_path: Path) -> None:
    broken = _rewrite_zip(source["zip"], tmp_path / "broken.zip", {"journal/clips.json": b"[{\"clip_id\": "})
    target = _make_env(tmp_path / "dst", PID)
    before = _dump(target)
    with pytest.raises(ValueError, match="clips.json"):
        _import(target, broken, "new")
    assert _dump(target) == before


def test_failure_while_writing_rolls_back_everything(source: Dict[str, Any], tmp_path: Path) -> None:
    pages = _zjson(source["zip"], "knowledge/pages.json")
    for p in pages:
        if p["id"] == source["ids"]["banana"]:
            p["title"] = None  # memopedia_pages.title は NOT NULL — messages を書いた後で落ちる
    broken = _rewrite_zip(
        source["zip"], tmp_path / "broken.zip",
        {"knowledge/pages.json": json.dumps(pages, ensure_ascii=False).encode("utf-8")},
    )
    target = _make_env(tmp_path / "dst", PID)
    before = _dump(target)
    with pytest.raises(sqlite3.IntegrityError):
        _import(target, broken, "new")
    assert _dump(target) == before


def test_failure_after_files_are_planned_removes_created_files(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    original = importer._ImportJob.write_files

    def write_then_fail(self: Any) -> None:
        original(self)
        assert self.created_files
        raise RuntimeError("boom after writing files")

    monkeypatch.setattr(importer._ImportJob, "write_files", write_then_fail)
    target = _make_env(tmp_path / "dst", PID)
    before = _dump(target)
    with pytest.raises(RuntimeError, match="boom"):
        _import(target, source["zip"], "new")
    assert _dump(target) == before


# ---------------------------------------------------------------------------
# profile/
# ---------------------------------------------------------------------------

def test_profile_is_overwritten_only_when_asked(source: Dict[str, Any], tmp_path: Path) -> None:
    target = _make_env(tmp_path / "dst", "renamed_city_b", name="仮の名前", city_id=2)

    def ai_row() -> Dict[str, Any]:
        world = sqlite3.connect(target["world_db"])
        world.row_factory = sqlite3.Row
        try:
            return dict(world.execute("SELECT * FROM ai WHERE AIID = ?", (target["persona_id"],)).fetchone())
        finally:
            world.close()

    before = ai_row()
    result = _import(target, source["zip"], "new")
    assert result.counts["profile"].skipped == 1
    assert ai_row() == before

    target2 = _make_env(tmp_path / "dst2", "renamed_city_c", name="仮の名前", city_id=3)
    target = target2
    before = ai_row()
    result = _import(target, source["zip"], "new", overwrite_profile=True)
    after = ai_row()
    assert result.counts["profile"].updated == 1
    assert after["AINAME"] == "テスト子"
    assert after["SYSTEMPROMPT"] == "あなたはテスト子です。"
    assert after["DEFAULT_MODEL"] == "gemini-3-flash"
    for col in PROFILE_PROTECTED_COLUMNS:
        assert after[col] == before[col], col
        assert col not in result.profile_columns_overwritten
    icon = Path(after["AVATAR_IMAGE"])
    assert icon.parent == target["home"] / "user_data" / "icons"
    assert icon.read_bytes() == b"RIFF icon bytes"


def _add_ai(env: Dict[str, Any], persona_id: str, name: str, city_id: int) -> None:
    engine = create_engine(f"sqlite:///{Path(env['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(AI(AIID=persona_id, HOME_CITYID=city_id, AINAME=name, SYSTEMPROMPT="", AUTO_COUNT=0))
        s.commit()
    engine.dispose()


def _ai_row(env: Dict[str, Any]) -> Dict[str, Any]:
    world = sqlite3.connect(env["world_db"])
    world.row_factory = sqlite3.Row
    try:
        return dict(world.execute("SELECT * FROM ai WHERE AIID = ?", (env["persona_id"],)).fetchone())
    finally:
        world.close()


def test_profile_overwrite_keeps_the_name_used_by_another_persona_in_the_city(tmp_path: Path) -> None:
    # manager/persona.py の _create_persona と同じ規則: 同じ HOME_CITYID の同名は大文字小文字無視で拒否
    src = _make_env(tmp_path / "src", PID, name="Alice")
    pack = _export(src, tmp_path / "out" / "alice.zip")
    target = _make_env(tmp_path / "dst", PID, name="仮の名前")
    _add_ai(target, "other_city_a", "ALICE", 1)

    result = _import(target, pack, "merge", overwrite_profile=True)
    after = _ai_row(target)
    assert after["AINAME"] == "仮の名前"
    assert after["SYSTEMPROMPT"] == "あなたはAliceです。"
    assert "AINAME" not in result.profile_columns_overwritten
    assert "SYSTEMPROMPT" in result.profile_columns_overwritten
    assert any(
        "the name 'Alice' is already used by another persona in this city" in w for w in result.warnings
    ), result.warnings


def test_profile_overwrite_takes_the_name_matching_only_itself_or_another_city(tmp_path: Path) -> None:
    src = _make_env(tmp_path / "src", PID, name="Alice")
    pack = _export(src, tmp_path / "out" / "alice.zip")
    target = _make_env(tmp_path / "dst", PID, name="alice")
    _add_ai(target, "other_city_b", "Alice", 2)

    result = _import(target, pack, "merge", overwrite_profile=True)
    after = _ai_row(target)
    assert after["AINAME"] == "Alice"
    assert "AINAME" in result.profile_columns_overwritten
    assert not any("already used by another persona" in w for w in result.warnings), result.warnings


# ---------------------------------------------------------------------------
# 6. 合流の Chronicle (intent §5 — 被覆の照合で取り込む枝を決める)
# ---------------------------------------------------------------------------

def _chronicle_rows(env: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """chronicle 分類の trunk でないページ: {id: {parent_id, title, meta}}。"""
    conn = _mem(env)
    try:
        return {
            r[0]: {"parent_id": r[1], "title": r[2], "meta": json.loads(r[3])}
            for r in conn.execute(
                "SELECT id, parent_id, title, metadata FROM memopedia_pages "
                "WHERE category = 'chronicle' AND is_trunk = 0"
            )
        }
    finally:
        conn.close()


@pytest.fixture()
def chron_branch(tmp_path: Path) -> Dict[str, Any]:
    """分岐した二つの環境 A / B と、A で書き出した ZIP。

    分岐前 (共有): s1..s8 の会話、Lv1 L1a[s1,s2] L1b[s3,s4] L1c[s5,s6]、
    Lv2 U1[L1a,L1b]。s7 s8 は未編纂。
    A で起きたこと: a1..a4 を話し、Lv1 NA1[a1,a2] NA2[a3,a4] とその上の Lv2
    NU[NA1,NA2,(死んだ参照)] を作った。共有の L1c を作り直して R1c[s5,s6,s7]
    にした。共有の U1 と NU を束ねた Lv3 TOP を作った。s8 を覆う Lv1 NA5 を共有の
    U1 へ縁組した。
    B で起きたこと: 共有の L1b を吸収で差し替え、L1b2[s3,s4,s7] にした。
    """
    from sai_memory.arasuji.storage import add_to_parent_source_ids, delete_entry, mark_consolidated
    from sai_memory.memory.recall_edges import add_chunk_page_edge

    a = _make_env(tmp_path / "a", PID)
    conn = _mem(a)
    for i in range(1, 9):
        _add_message(conn, PID, f"s{i}", "room_a", "user" if i % 2 else "assistant",
                     f"共有の話 {i}", T0 + i, {"tags": ["conversation"]})
    conn.commit()
    apple = create_page(conn, parent_id="root_terms", title="りんご", content="赤い果物", category="terms")

    def entry(level: int, sources: List[str], start: int, end: int, content: str):
        return create_entry(conn, level=level, content=content, source_ids=sources,
                            start_time=start, end_time=end, source_count=len(sources),
                            message_count=len(sources))

    l1a = entry(1, ["s1", "s2"], T0 + 1, T0 + 2, "L1a")
    l1b = entry(1, ["s3", "s4"], T0 + 3, T0 + 4, "L1b")
    l1c = entry(1, ["s5", "s6"], T0 + 5, T0 + 6, "L1c")
    u1 = entry(2, [l1a.id, l1b.id], T0 + 1, T0 + 4, "U1")
    mark_consolidated(conn, [l1a.id, l1b.id], u1.id)
    conn.close()
    fork_zip = _export(a, tmp_path / "out" / "fork.zip")

    b = _make_env(tmp_path / "b", PID)
    _import(b, fork_zip, "new")

    # A 側
    conn = _mem(a)
    for i in range(1, 5):
        _add_message(conn, PID, f"a{i}", "room_a", "user", f"A の話 {i}", T0 + 100 + i,
                     {"tags": ["conversation"]})
    conn.commit()
    na1 = entry(1, ["a1", "a2"], T0 + 101, T0 + 102, "NA1")
    na2 = entry(1, ["a3", "a4"], T0 + 103, T0 + 104, "NA2")
    nu = entry(2, [na1.id, na2.id, "ghost-entry"], T0 + 101, T0 + 104, "NU")
    mark_consolidated(conn, [na1.id, na2.id], nu.id)
    delete_entry(conn, l1c.id)
    r1c = entry(1, ["s5", "s6", "s7"], T0 + 5, T0 + 7, "R1c")
    top = entry(3, [u1.id, nu.id], T0 + 1, T0 + 104, "TOP")
    mark_consolidated(conn, [u1.id, nu.id], top.id)
    na5 = entry(1, ["s8"], T0 + 8, T0 + 8, "NA5")
    assert add_to_parent_source_ids(conn, na5.id, u1.id)
    add_chunk_page_edge(conn, na1.id, apple.id, created_at=T0 + 200)
    add_chunk_page_edge(conn, r1c.id, apple.id, created_at=T0 + 201)
    conn.close()

    # B 側
    conn = _mem(b)
    delete_entry(conn, l1b.id)
    l1b2 = create_entry(conn, level=1, content="L1b2", source_ids=["s3", "s4", "s7"],
                        start_time=T0 + 3, end_time=T0 + 7, source_count=3, message_count=3)
    conn.close()

    a_zip = _export(a, tmp_path / "out" / "a.zip")
    ids = {
        "l1a": l1a.id, "l1b": l1b.id, "l1c": l1c.id, "u1": u1.id, "na1": na1.id, "na2": na2.id,
        "nu": nu.id, "r1c": r1c.id, "top": top.id, "na5": na5.id, "l1b2": l1b2.id,
        "apple": apple.id,
    }
    return {"a": a, "b": b, "zip": a_zip, "ids": ids}


def test_merge_chronicle_takes_only_branches_outside_target_coverage(chron_branch: Dict[str, Any]) -> None:
    b, ids = chron_branch["b"], chron_branch["ids"]
    before = _chronicle_rows(b)
    result = _import(b, chron_branch["zip"], "merge")
    after = _chronicle_rows(b)

    imported = set(after) - set(before)
    # 1. A で新しく作られた枝 (NA1 NA2 NU) と、共有の親を持つ新しい子 NA5 が入る。
    # 2. 共有 Lv1 を作り直した R1c、3. B で差し替え済みの L1b、4. 共有の U1 を
    # 束ねた TOP は入らない
    assert imported == {ids["na1"], ids["na2"], ids["nu"], ids["na5"]}
    assert result.counts["chronicle_pages"].added == 4
    # ZIP の chronicle は L1a L1b U1 NA1 NA2 NU R1c TOP NA5 の 9 行
    assert result.counts["chronicle_pages"].skipped == 5
    assert any("overlap the target" in w for w in result.warnings)
    assert any("upper page(s) not imported" in w for w in result.warnings)

    # 1. parent と source_ids が双方向で一致する
    for page_id in imported:
        row = after[page_id]
        if row["parent_id"] != "root_chronicle":
            assert page_id in after[row["parent_id"]]["meta"]["source_ids"]
            assert row["meta"]["is_consolidated"] == 1
        if row["meta"]["level"] >= 2:
            for child in row["meta"]["source_ids"]:
                if child in after:
                    assert after[child]["parent_id"] == page_id
    assert after[ids["na1"]]["parent_id"] == ids["nu"]
    assert after[ids["na2"]]["parent_id"] == ids["nu"]
    # NU の親 TOP は入らないので、NU は root_chronicle の未統合に置かれる
    assert after[ids["nu"]]["parent_id"] == "root_chronicle"
    assert after[ids["nu"]]["meta"]["is_consolidated"] == 0
    # 5. 共有の親 U1 を持つ新しい子 NA5 も root_chronicle の未統合に付く
    assert after[ids["na5"]]["parent_id"] == "root_chronicle"
    assert after[ids["na5"]]["meta"]["is_consolidated"] == 0

    # 3. 全 Lv1 の source_ids に重複が無い
    sources = [s for r in after.values() if r["meta"]["level"] == 1 for s in r["meta"]["source_ids"]]
    assert len(sources) == len(set(sources))
    assert ids["l1b"] not in after

    # 4. 取り込み先の木は変わらない
    assert {k: after[k] for k in before} == before

    # chunk_page_edges: 取り込んだ NA1 の辺だけが入る
    conn = _mem(b)
    try:
        edges = conn.execute("SELECT chronicle_page_id, entity_page_id FROM chunk_page_edges").fetchall()
    finally:
        conn.close()
    assert edges == [(ids["na1"], ids["apple"])]
    assert result.counts["extensions/memory_db/chunk_page_edges"].added == 1
    assert result.counts["extensions/memory_db/chunk_page_edges"].skipped == 1


def test_merge_chronicle_twice_adds_nothing(chron_branch: Dict[str, Any]) -> None:
    b = chron_branch["b"]
    _import(b, chron_branch["zip"], "merge")
    before = _dump(b)
    second = _import(b, chron_branch["zip"], "merge")
    assert second.counts["chronicle_pages"].added == 0
    assert all(c.added == 0 and c.updated == 0 for c in second.counts.values()), second.counts
    assert _dump(b) == before


def test_merge_chronicle_calls_no_llm(chron_branch: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_clients
    import llm_clients.factory

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the import must not call an LLM client")

    monkeypatch.setattr(llm_clients, "get_llm_client", forbidden)
    monkeypatch.setattr(llm_clients.factory, "get_llm_client", forbidden)
    result = _import(chron_branch["b"], chron_branch["zip"], "merge")
    assert result.counts["chronicle_pages"].added == 4


def test_delete_of_imported_upper_leaves_target_entries_alone(chron_branch: Dict[str, Any]) -> None:
    from sai_memory.arasuji.storage import delete_entry

    b, ids = chron_branch["b"], chron_branch["ids"]
    before = _chronicle_rows(b)
    _import(b, chron_branch["zip"], "merge")
    conn = _mem(b)
    try:
        assert delete_entry(conn, ids["nu"])
    finally:
        conn.close()
    after = _chronicle_rows(b)
    assert {k: after[k] for k in before} == before
    # 戻されたのは NU の子 (取り込んだ NA1 NA2) だけ
    assert after[ids["na1"]]["parent_id"] == "root_chronicle"
    assert after[ids["na2"]]["parent_id"] == "root_chronicle"


def test_merge_chronicle_short_ids_are_kept_or_renumbered(chron_branch: Dict[str, Any]) -> None:
    a, b, ids = chron_branch["a"], chron_branch["b"], chron_branch["ids"]
    in_a = _chronicle_rows(a)
    before = _chronicle_rows(b)
    b_max = max(r["meta"]["short_id"] for r in before.values())
    result = _import(b, chron_branch["zip"], "merge")
    after = _chronicle_rows(b)

    short_ids = [r["meta"]["short_id"] for r in after.values()]
    assert len(short_ids) == len(set(short_ids))
    renumbered = 0
    for page_id in set(after) - set(before):
        row, original = after[page_id], in_a[page_id]["meta"]["short_id"]
        if original > b_max:
            # 取り込み先で使われたことのない番号はそのまま
            assert row["meta"]["short_id"] == original
            assert "original_chronicle_short_id" not in row["meta"]
        else:
            renumbered += 1
            assert row["meta"]["short_id"] > b_max
            assert row["meta"]["original_chronicle_short_id"] == original
        # 機構が書いた表題は新しい番号に合わせる
        assert row["title"] == f"chronicle:{row['meta']['short_id']}"
    assert renumbered >= 1
    assert result.renumbered.get("pages.chronicle_short_id") == renumbered
    assert in_a[ids["na1"]]["meta"]["short_id"] <= b_max


# ---------------------------------------------------------------------------
# 7. レビュー指摘の再現 (コロンなしスレッド ID・metadata の合流・検証段階の拒否)
# ---------------------------------------------------------------------------

def test_threads_without_persona_prefix_merge_twice(source: Dict[str, Any], tmp_path: Path) -> None:
    conn = _mem(source)
    conn.execute("INSERT INTO threads(id, resource_id) VALUES (?, ?)", ("legacy", PID))
    conn.execute(
        "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("mL", "legacy", "user", "古い形のスレッド", PID, T0 + 50, json.dumps({"tags": ["conversation"]})),
    )
    conn.commit()
    conn.close()
    pack = _export(source, tmp_path / "out" / "legacy.zip")

    # 新規取り込みの後に同じパックを合流 (取り込み先に同じ ID のスレッドがある)
    target = _make_env(tmp_path / "dst", PID)
    _import(target, pack, "new")
    before = _dump(target)
    again = _import(target, pack, "merge")
    assert again.counts["threads"].added == 0
    assert _dump(target) == before

    # 別のペルソナへ二度合流しても壊れない
    other = _make_env(tmp_path / "other", "bob_city_b", name="ボブ", city_id=2)
    first = _import(other, pack, "merge")
    assert first.counts["messages"].added == 7
    before = _dump(other)
    second = _import(other, pack, "merge")
    assert all(c.added == 0 and c.updated == 0 for c in second.counts.values()), second.counts
    assert _dump(other) == before
    conn = _mem(other)
    try:
        assert conn.execute("SELECT thread_id FROM messages WHERE id = 'mL'").fetchone()[0] == "legacy"
    finally:
        conn.close()
    assert any("no persona prefix" in w for w in first.warnings)


def test_page_update_keeps_target_only_metadata_keys(source: Dict[str, Any], tmp_path: Path) -> None:
    ids = source["ids"]
    target = _make_env(tmp_path / "dst", PID)
    _import(target, source["zip"], "new")

    conn = _mem(target)
    (target_meta,) = conn.execute(
        "SELECT metadata FROM memopedia_pages WHERE id = ?", (ids["apple"],)
    ).fetchone()
    meta = json.loads(target_meta) if target_meta else {}
    meta.update({"target_only": "残る", "shared": "取り込み先"})
    conn.execute("UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
                 (json.dumps(meta, ensure_ascii=False), ids["apple"]))
    conn.commit()
    conn.close()

    conn = _mem(source)
    conn.execute(
        "UPDATE memopedia_pages SET content = ?, updated_at = ?, metadata = ? WHERE id = ?",
        ("書き足した", T0 + 900,
         json.dumps({"zip_key": "入る", "shared": "ZIP 側", "original_short_id": 99}, ensure_ascii=False),
         ids["apple"]),
    )
    conn.commit()
    conn.close()
    pack = _export(source, tmp_path / "out" / "newer.zip")

    result = _import(target, pack, "merge")
    assert result.counts["pages"].updated == 1
    conn = _mem(target)
    try:
        content, merged = conn.execute(
            "SELECT content, metadata FROM memopedia_pages WHERE id = ?", (ids["apple"],)
        ).fetchone()
    finally:
        conn.close()
    merged = json.loads(merged)
    assert content == "書き足した"
    assert merged["target_only"] == "残る"
    assert merged["zip_key"] == "入る"
    assert merged["shared"] == "ZIP 側"
    # 番号系のキーは取り込み先の値を保つ (取り込み先に無いので載らない)
    assert "original_short_id" not in merged


def _page_parents(env: Dict[str, Any]) -> Dict[str, Any]:
    conn = _mem(env)
    try:
        return dict(conn.execute("SELECT id, parent_id FROM memopedia_pages"))
    finally:
        conn.close()


def _pages_reached_from_roots(parents: Dict[str, Any]) -> set:
    """parent_id が NULL の行から子を辿って届く行 (build_tree と同じ辿り方を、全分類で)。"""
    children: Dict[Any, List[str]] = {}
    for page_id, parent_id in parents.items():
        children.setdefault(parent_id, []).append(page_id)
    reached: set = set()
    stack = list(children.get(None, []))
    while stack:
        page_id = stack.pop()
        if page_id in reached:
            continue
        reached.add(page_id)
        stack.extend(children.get(page_id, []))
    return reached


def test_page_update_does_not_make_a_parent_cycle(source: Dict[str, Any], tmp_path: Path) -> None:
    """取り込み先が root→A→B、ZIP が root→B→A で A だけ ZIP 側が新しくても循環しない。

    A の本文は ZIP 側に上書きされ、親だけは取り込み先の値 (root_terms) に戻る。
    """
    from sai_memory.memopedia.storage import build_tree

    ids = source["ids"]
    apple, banana = ids["apple"], ids["banana"]
    conn = _mem(source)
    conn.execute("UPDATE memopedia_pages SET parent_id = ? WHERE id = ?", (apple, banana))
    conn.commit()
    conn.close()
    first = _export(source, tmp_path / "out" / "a_over_b.zip")
    target = _make_env(tmp_path / "dst", PID)
    _import(target, first, "new")
    assert _page_parents(target)[banana] == apple

    # ZIP 側は root→B→A に組み替え、A の本文だけ新しくする (B は本文が同じなのでスキップ)
    conn = _mem(source)
    conn.execute("UPDATE memopedia_pages SET parent_id = 'root_terms' WHERE id = ?", (banana,))
    conn.execute(
        "UPDATE memopedia_pages SET parent_id = ?, content = ?, updated_at = ? WHERE id = ?",
        (banana, "書き足した赤い果物", T0 + 900, apple),
    )
    conn.commit()
    conn.close()
    second = _export(source, tmp_path / "out" / "b_over_a.zip")

    result = _import(target, second, "merge")
    assert result.counts["pages"].updated == 1
    parents = _page_parents(target)
    assert parents[apple] == "root_terms"
    assert parents[banana] == apple
    assert _pages_reached_from_roots(parents) == set(parents)
    assert [w for w in result.warnings if "memopedia page" in w] == [
        f"memopedia page {apple}: the pack's parent {banana!r} would make a cycle or a missing "
        "parent in the page tree; the target's parent 'root_terms' is kept (the rest of the page "
        "was updated)"
    ]
    conn = _mem(target)
    try:
        (content,) = conn.execute(
            "SELECT content FROM memopedia_pages WHERE id = ?", (apple,)
        ).fetchone()
        assert content == "書き足した赤い果物"
        terms = {p.id: p for p in build_tree(conn)["terms"]}
        assert [c.id for c in terms["root_terms"].children] == [apple]
        assert [c.id for c in terms["root_terms"].children[0].children] == [banana]
    finally:
        conn.close()

    # もう一度合流しても親は動かず、何も増えない
    before = _dump(target)
    again = _import(target, second, "merge")
    assert all(c.added == 0 and c.updated == 0 for c in again.counts.values()), again.counts
    assert _dump(target) == before


def test_inserted_page_with_a_missing_parent_is_moved_under_its_trunk(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """取り込んだページの親が無ければ同じ分類の trunk の直下に置き、取り込み前からの欠けは触らない。"""
    conn = _mem(source)
    orphan = create_page(conn, parent_id="root_terms", title="みかん", content="橙の果物", category="terms")
    conn.execute("UPDATE memopedia_pages SET parent_id = 'page-gone' WHERE id = ?", (orphan.id,))
    conn.commit()
    conn.close()
    pack = _export(source, tmp_path / "out" / "orphan.zip")

    target = _make_env(tmp_path / "dst", PID)
    conn = _mem(target)
    old = create_page(conn, parent_id="root_people", title="昔の人", content="", category="people")
    conn.execute("UPDATE memopedia_pages SET parent_id = 'page-lost-before' WHERE id = ?", (old.id,))
    conn.commit()
    conn.close()

    result = _import(target, pack, "new")
    parents = _page_parents(target)
    assert parents[orphan.id] == "root_terms"
    assert parents[old.id] == "page-lost-before"
    assert _pages_reached_from_roots(parents) == set(parents) - {old.id}
    assert [w for w in result.warnings if w.startswith("memopedia")] == [
        f"memopedia page {orphan.id}: its parent 'page-gone' makes a cycle or is missing in the "
        "page tree; moved under 'root_terms'",
        "memopedia: 1 page(s) stay outside the page tree because of a cycle or a missing parent "
        "that existed in the target before this import; left as they are",
    ]


def test_broken_extensions_json_is_refused_in_merge(source: Dict[str, Any], tmp_path: Path) -> None:
    broken = _rewrite_zip(
        source["zip"], tmp_path / "broken_ext.zip",
        {"extensions/saiverse/memory_db/desk_items.json": b"[{\"ref\": "},
    )
    target = _make_env(tmp_path / "dst", PID)
    _import(target, source["zip"], "new")
    before = _dump(target)
    with pytest.raises(ValueError, match="desk_items.json"):
        _import(target, broken, "merge")
    assert _dump(target) == before


def test_archive_over_the_expanded_size_limit_is_refused(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    with zipfile.ZipFile(source["zip"]) as zf:
        total = sum(i.file_size for i in zf.infolist())
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_BYTES", total - 1)
    target = _make_env(tmp_path / "dst", PID)
    before = _dump(target)
    with pytest.raises(ValueError, match="over the limit"):
        _import(target, source["zip"], "new")
    assert _dump(target) == before
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_BYTES", total)
    assert _import(target, source["zip"], "new").counts["messages"].added == 6


def test_json_text_scalar_metadata_round_trips(source: Dict[str, Any], tmp_path: Path) -> None:
    conn = _mem(source)
    conn.execute(
        "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("mN", f"{PID}:room_a", "user", "metadata が null の文字列", PID, T0 + 60, "null"),
    )
    conn.commit()
    conn.close()
    pack = _export(source, tmp_path / "out" / "scalar.zip")
    target = _make_env(tmp_path / "dst", PID)
    _import(target, pack, "new")
    conn = _mem(target)
    try:
        assert conn.execute("SELECT metadata FROM messages WHERE id = 'mN'").fetchone()[0] == "null"
    finally:
        conn.close()


def test_integer_affinity_primary_key_is_renumbered(source: Dict[str, Any], tmp_path: Path) -> None:
    target = _make_env(tmp_path / "dst", PID)
    # 主キーの型名が INTEGER でない (BIGINT) 取り込み先の表
    world = sqlite3.connect(target["world_db"])
    world.execute("DROP TABLE persona_event_log")
    world.execute(
        "CREATE TABLE persona_event_log (EVENT_ID BIGINT PRIMARY KEY, PERSONA_ID TEXT, "
        "CREATED_AT TEXT, CONTENT TEXT, STATUS TEXT, EVENT_TYPE TEXT, PAYLOAD TEXT)"
    )
    world.execute("INSERT INTO persona_event_log (EVENT_ID, PERSONA_ID, CONTENT) VALUES (1, 'someone_else', 'taken')")
    world.commit()
    world.close()
    result = _import(target, source["zip"], "new")
    assert result.counts["extensions/saiverse_db/persona_event_log"].added == 1
    world = sqlite3.connect(target["world_db"])
    try:
        rows_after = sorted(world.execute("SELECT EVENT_ID, CONTENT, PERSONA_ID FROM persona_event_log"))
    finally:
        world.close()
    # BIGINT の主キーは rowid の別名ではないので、取り込み先の MAX+1 から振られる
    assert rows_after == [(1, "taken", "someone_else"), (2, "メールが届いた", PID)]


# ---------------------------------------------------------------------------
# extensions/saiverse/saiverse_db/ の許可リストと持ち主の強制
# ---------------------------------------------------------------------------

def _add_zip_entries(src: Path, dst: Path, entries: Dict[str, Any]) -> Path:
    """ZIP の JSON を差し替え / 追加する (値は JSON にして書く)。"""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            if item.filename not in entries:
                zout.writestr(item, zin.read(item.filename))
        for name, value in entries.items():
            zout.writestr(name, json.dumps(value, ensure_ascii=False))
    return dst


def test_extension_rows_are_owned_by_the_target_and_unlisted_tables_refused(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    victim = "victim_city_a"
    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    engine = create_engine(f"sqlite:///{Path(target['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(AI(AIID=victim, HOME_CITYID=1, AINAME="別の子", SYSTEMPROMPT="元のまま", AUTO_COUNT=0))
        s.add(PersonaTask(id="victim-task", persona_id=victim, title="別の子のタスク"))
        s.commit()
    engine.dispose()

    ext = "extensions/saiverse/saiverse_db/"
    events = _zjson(source["zip"], ext + "persona_event_log.json")
    events.append({"EVENT_ID": 99, "PERSONA_ID": victim, "CREATED_AT": "2026-10-01 00:00:00",
                   "CONTENT": "別の子宛てに見せかけた行", "STATUS": "pending"})
    steps = _zjson(source["zip"], ext + "persona_task_step.json")
    steps.append({"id": "evil-step", "task_id": "victim-task", "position": 9, "title": "差し込み",
                  "status": "pending", "version": 0})
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {
        ext + "persona_event_log.json": events,
        ext + "persona_task_step.json": steps,
        ext + "ai.json": [{"AIID": victim, "AINAME": "乗っ取り", "SYSTEMPROMPT": "書き換え", "HOME_CITYID": 1}],
        ext + "addon_message_metadata.json": [
            {"message_id": "m1", "addon_name": "tts", "key": "audio", "value": "ok"},
            {"message_id": "victim-message", "addon_name": "tts", "key": "audio", "value": "evil"},
        ],
    })
    result = _import(target, crafted, "new")

    world = sqlite3.connect(target["world_db"])
    try:
        owners = dict(world.execute("SELECT CONTENT, PERSONA_ID FROM persona_event_log"))
        assert owners == {"メールが届いた": target["persona_id"], "別の子宛てに見せかけた行": target["persona_id"]}
        # 子表は、同じ取り込みでこのペルソナの行として入った親を指す行だけ
        newcomer = target["persona_id"]
        assert sorted(world.execute("SELECT id, task_id FROM persona_task_step")) == [
            (_derived(newcomer, "persona_task_step", "step-1"), _derived(newcomer, "persona_task", "task-1"))
        ]
        assert world.execute(
            "SELECT persona_id FROM persona_task WHERE id = ?", (_derived(newcomer, "persona_task", "task-1"),)
        ).fetchone()[0] == newcomer
        # 許可リストに無い表 (ai) は取り込まない
        assert world.execute("SELECT AINAME, SYSTEMPROMPT FROM ai WHERE AIID = ?", (victim,)).fetchone() == ("別の子", "元のまま")
        # addon_message_metadata はこのペルソナのメッセージ宛てだけ
        assert sorted(world.execute("SELECT message_id, value FROM addon_message_metadata")) == [("m1", "ok")]
    finally:
        world.close()
    assert result.counts["extensions/saiverse_db/ai"].skipped == 1
    assert result.counts["extensions/saiverse_db/persona_task_step"].skipped == 1
    assert result.counts["extensions/saiverse_db/addon_message_metadata"].skipped == 1
    assert any("ai.json is not a table" in w for w in result.warnings)


def test_export_owner_rules_cover_exactly_the_exported_tables() -> None:
    from saiverse_memory.persona_pack.export import SAIVERSE_DB_EXTENSION_TABLES, SAIVERSE_DB_TABLE_OWNERS

    exported = {t for t, _ in SAIVERSE_DB_EXTENSION_TABLES} | {"addon_message_metadata"}
    assert set(SAIVERSE_DB_TABLE_OWNERS) == exported
    # 親は子より先に処理される (dict の並び)
    order = list(SAIVERSE_DB_TABLE_OWNERS)
    for table, rule in SAIVERSE_DB_TABLE_OWNERS.items():
        if rule[0] == "parent":
            assert order.index(rule[2]) < order.index(table), table
    # 持ち主の列は database/models.py の列
    for table, rule in SAIVERSE_DB_TABLE_OWNERS.items():
        assert rule[1] in Base.metadata.tables[table].columns, (table, rule[1])


# ---------------------------------------------------------------------------
# 添付の参照: ZIP のホスト側のパスを取り込み先に残さない
# ---------------------------------------------------------------------------

def test_attachment_path_to_the_world_db_does_not_leak_into_a_reexport(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    secret = "sk-target-secret-must-not-leave"
    target = _make_env(tmp_path / "dst", PID)
    engine = create_engine(f"sqlite:///{Path(target['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(AddonPersonaConfig(addon_name="x", persona_id=PID, params_json=json.dumps({"api_key": secret})))
        s.commit()
    engine.dispose()

    messages = _zjson(source["zip"], "chatlog/messages.json")
    messages.append({
        "id": "m-evil", "thread_id": f"{PID}:room_a", "role": "user", "content": "添付",
        "resource_id": PID, "created_at": "2026-10-10T00:00:00+00:00", "seq": 999,
        "metadata": {"documents": [
            {"path": str(target["world_db"]), "mime_type": "application/octet-stream"},
            {"uri": "saiverse://document/../user_data/database/saiverse.db"},
            {"uri": "saiverse://document/kept.txt", "path": "/elsewhere/kept.txt"},
        ]},
    })
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {"chatlog/messages.json": messages})
    _import(target, crafted, "new")

    conn = _mem(target)
    try:
        meta = json.loads(conn.execute("SELECT metadata FROM messages WHERE id = 'm-evil'").fetchone()[0])
    finally:
        conn.close()
    assert meta["documents"] == [
        {"path": None, "uri": None, "mime_type": "application/octet-stream"},
        {"uri": None, "path": None},
        {"uri": None, "path": None},
    ]

    again = _export(target, tmp_path / "out" / "again.zip")
    with zipfile.ZipFile(again) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
    assert secret.encode() not in blob
    assert b"SQLite format 3" not in blob


def _without_attachments(src: Path, dst: Path) -> Path:
    """attachments/ を同梱しない ZIP (manifest はそのまま)。"""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            if not item.filename.startswith("attachments/"):
                zout.writestr(item, zin.read(item.filename))
    return dst


def test_unrestored_attachment_uri_does_not_resolve_to_a_same_named_target_file(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """復元できなかった添付は uri も path も None — 取り込み先の同名の別ファイルを指さない。"""
    bare = _without_attachments(source["zip"], tmp_path / "bare.zip")
    target = _make_env(tmp_path / "dst", PID)
    # 取り込み先に、同じ名前の別の画像がある
    (target["home"] / "image" / "pic.png").write_bytes(b"\x89PNG somebody else's picture")
    result = _import(target, bare, "new")

    conn = _mem(target)
    try:
        meta = json.loads(conn.execute("SELECT metadata FROM messages WHERE id = 'm1'").fetchone()[0])
    finally:
        conn.close()
    assert meta["images"] == [{"uri": None, "path": None, "mime_type": "image/png"}]
    assert any(
        "1 attachment reference(s) could not be restored" in w for w in result.warnings
    ), result.warnings
    assert (target["home"] / "image" / "pic.png").read_bytes() == b"\x89PNG somebody else's picture"


def test_restored_attachment_avoids_a_same_named_file_with_other_content(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """正規の往復: 実体を同梱した添付は復元先を指す。同名で中身の違うファイルは再利用せず避ける。"""
    target = _make_env(tmp_path / "dst", PID)
    home: Path = target["home"]
    (home / "image" / "pic.png").write_bytes(b"\x89PNG somebody else's picture")
    result = _import(target, source["zip"], "new")

    conn = _mem(target)
    try:
        meta = json.loads(conn.execute("SELECT metadata FROM messages WHERE id = 'm1'").fetchone()[0])
    finally:
        conn.close()
    assert meta["images"] == [{"uri": "saiverse://image/pic_1.png", "path": str(home / "image" / "pic_1.png"),
                               "mime_type": "image/png"}]
    assert (home / "image" / "pic_1.png").read_bytes() == b"\x89PNG original image"
    assert (home / "image" / "pic.png").read_bytes() == b"\x89PNG somebody else's picture"
    assert not any("could not be restored" in w for w in result.warnings), result.warnings


# ---------------------------------------------------------------------------
# 片側だけ commit された取り込みの再開
# ---------------------------------------------------------------------------

def test_world_commit_failure_is_finished_by_a_merge(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    original = importer._ImportJob.commit_world

    def fail_once(self: Any, world: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error (injected)")

    monkeypatch.setattr(importer._ImportJob, "commit_world", fail_once)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        _import(target, source["zip"], "new", overwrite_profile=True)
    monkeypatch.setattr(importer._ImportJob, "commit_world", original)

    def world_rows() -> Dict[str, Any]:
        world = sqlite3.connect(target["world_db"])
        try:
            return {
                "events": world.execute("SELECT CONTENT, PERSONA_ID FROM persona_event_log").fetchall(),
                "items": sorted(r[0] for r in world.execute("SELECT NAME FROM item")),
                "room": world.execute(
                    "SELECT DESCRIPTION FROM building WHERE BUILDINGID = ?", (target["room_id"],)
                ).fetchone()[0],
                "name": world.execute("SELECT AINAME FROM ai WHERE AIID = ?", (target["persona_id"],)).fetchone()[0],
                "task_book": world.execute("SELECT COUNT(*) FROM task_book").fetchone()[0],
            }
        finally:
            world.close()

    # memory.db は commit 済み、saiverse.db は巻き戻り、印が残る
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["persona_id"] == PID
    conn = _mem(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 6
    finally:
        conn.close()
    assert world_rows() == {"events": [], "items": [], "room": "", "name": "新入り", "task_book": 0}

    result = _import(target, source["zip"], "merge")
    assert not marker.exists()
    after = world_rows()
    assert after["events"] == [("メールが届いた", target["persona_id"])]
    assert after["items"] == sorted(["小石", "ノート", "鞄"])
    assert after["room"] == "窓辺に机のある部屋"
    assert after["name"] == "テスト子"  # 印に記録した overwrite_profile で上書きされる
    assert after["task_book"] == 1
    assert result.counts["messages"].added == 0
    assert result.counts["items"].added == 4
    assert any("resuming the unfinished part" in w for w in result.warnings)

    # もう一度合流しても世界側は増えない (印が無いので合流の範囲だけ)
    before = _dump(target)
    again = _import(target, source["zip"], "merge")
    assert all(c.added == 0 and c.updated == 0 for c in again.counts.values()), again.counts
    assert _dump(target) == before


def test_marker_of_another_pack_is_not_resumed(source: Dict[str, Any], tmp_path: Path) -> None:
    target = _make_env(tmp_path / "dst", PID)
    _import(target, source["zip"], "new")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    marker.write_text(json.dumps({"persona_id": "someone_else", "format": "persona-pack"}), encoding="utf-8")
    result = _import(target, source["zip"], "merge")
    assert marker.is_file()
    assert result.counts["items"].added == 0
    assert any("does not resume it" in w for w in result.warnings)


def test_pending_marker_is_never_exported_or_restored(source: Dict[str, Any], tmp_path: Path) -> None:
    marker_name = "persona_pack_world_pending.json"
    (source["persona_dir"] / marker_name).write_text('{"persona_id": "x"}', encoding="utf-8")
    pack = _export(source, tmp_path / "out" / "with_marker.zip")
    with zipfile.ZipFile(pack) as zf:
        assert f"extensions/saiverse/persona_dir/{marker_name}" not in zf.namelist()
    crafted = _rewrite_zip(pack, tmp_path / "crafted.zip", {})
    with zipfile.ZipFile(crafted, "a") as zf:
        zf.writestr(f"extensions/saiverse/persona_dir/{marker_name}", '{"persona_id": "x"}')
    target = _make_env(tmp_path / "dst", PID)
    _import(target, crafted, "new")
    assert not (target["persona_dir"] / marker_name).exists()


# ---------------------------------------------------------------------------
# items/: 合流では取り込まない、私室が無ければ部屋のものは入れない
# ---------------------------------------------------------------------------

def test_items_are_not_imported_on_merge(source: Dict[str, Any], tmp_path: Path) -> None:
    target = _make_env(tmp_path / "dst", PID)
    result = _import(target, source["zip"], "merge")
    world = sqlite3.connect(target["world_db"])
    try:
        assert world.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 0
        assert world.execute("SELECT COUNT(*) FROM fixture").fetchone()[0] == 0
    finally:
        world.close()
    assert result.counts["items"].added == 0
    assert result.counts["items"].skipped == 4


def test_new_import_twice_into_fresh_personas_gets_distinct_item_ids(source: Dict[str, Any], tmp_path: Path) -> None:
    t1 = _make_env(tmp_path / "t", "one_city_a", name="一")
    _import(t1, source["zip"], "new")
    # 同じ世界の別のペルソナへもう一度 (同じ世界 DB を共有する二体目)
    t2_dir = t1["home"] / "personas" / "two_city_a"
    t2_dir.mkdir(parents=True)
    conn = init_db(str(t2_dir / "memory.db"))
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{Path(t1['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(AI(AIID="two_city_a", HOME_CITYID=1, AINAME="二", PRIVATE_ROOM_ID="two_room", AUTO_COUNT=0))
        s.add(Building(CITYID=1, BUILDINGID="two_room", BUILDINGNAME="二の部屋"))
        s.commit()
    engine.dispose()
    t2 = {**t1, "persona_id": "two_city_a", "persona_dir": t2_dir, "memory_db": t2_dir / "memory.db"}
    result = _import(t2, source["zip"], "new")
    world = sqlite3.connect(t1["world_db"])
    try:
        rows = world.execute("SELECT ITEM_ID, SHORT_ID FROM item").fetchall()
        owners = sorted(world.execute(
            "SELECT OWNER_KIND, OWNER_ID FROM item_location WHERE OWNER_KIND <> 'bag'"
        ).fetchall())
        names = sorted(r[0] for r in world.execute("SELECT BUILDINGNAME FROM building"))
        fixtures = sorted(world.execute("SELECT FIXTURE_ID, BUILDING_ID FROM fixture").fetchall())
    finally:
        world.close()
    assert len(rows) == 6 and len({r[0] for r in rows}) == 6 and len({r[1] for r in rows}) == 6
    assert owners == [("building", "one_city_a_room"), ("building", "two_room"),
                      ("persona", "one_city_a"), ("persona", "two_city_a")]
    # 部屋の名前は ZIP の名前を写すが、同じ街に同名の建物があれば元のまま
    assert names == ["テスト子の部屋", "二の部屋"]
    assert any("room name was kept" in w for w in result.warnings)
    # FIXTURE_ID は衝突したら振り直す
    assert fixtures[0][1] != fixtures[1][1] and {f[0] for f in fixtures} >= {"fx-1"}
    assert result.renumbered["items.short_id"] == 3


# ---------------------------------------------------------------------------
# 合流の Chronicle: ZIP の中で同じ会話を覆う Lv1
# ---------------------------------------------------------------------------

def test_merge_chronicle_two_zip_lv1_over_the_same_messages(tmp_path: Path) -> None:
    a = _make_env(tmp_path / "a", PID)
    conn = _mem(a)
    for i in range(1, 4):
        _add_message(conn, PID, f"x{i}", "room_a", "user", f"話 {i}", T0 + i, {"tags": ["conversation"]})
    conn.commit()
    first = create_entry(conn, level=1, content="一本目", source_ids=["x1", "x2"],
                         start_time=T0 + 1, end_time=T0 + 2, source_count=2, message_count=2)
    second = create_entry(conn, level=1, content="二本目", source_ids=["x2", "x3"],
                          start_time=T0 + 2, end_time=T0 + 3, source_count=2, message_count=2)
    upper = create_entry(conn, level=2, content="二本目の上", source_ids=[second.id],
                         start_time=T0 + 2, end_time=T0 + 3, source_count=1, message_count=2)
    conn.close()
    pack = _export(a, tmp_path / "out" / "dup.zip")
    target = _make_env(tmp_path / "b", PID)
    result = _import(target, pack, "merge")
    imported = set(_chronicle_rows(target))
    assert imported == {first.id}
    assert second.id not in imported and upper.id not in imported
    assert result.counts["chronicle_pages"].added == 1


# ---------------------------------------------------------------------------
# 添付: 復元できた添付の uri / path は復元先から組み直す
# ---------------------------------------------------------------------------

def test_restored_attachment_uri_is_rebuilt_from_the_restored_file(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    bad_uri = "saiverse://image/../CLAUDE.md"
    bad_path = "../../outside/note.txt"
    messages = _zjson(source["zip"], "chatlog/messages.json")
    messages.append({
        "id": "m-dot", "thread_id": f"{PID}:room_a", "role": "user", "content": "添付",
        "resource_id": PID, "created_at": "2026-10-10T00:00:00+00:00", "seq": 999,
        "metadata": {
            "images": [{"uri": bad_uri, "path": "/elsewhere/CLAUDE.md", "mime_type": "image/png"}],
            "documents": [{"path": bad_path}],
        },
    })
    index = _zjson(source["zip"], "attachments/index.json")
    index += [
        {"ref": bad_uri, "uri": bad_uri, "original_path": None, "mime_type": "image/png",
         "path": "attachments/image/evil.png", "summary_path": None, "message_ids": ["m-dot"]},
        {"ref": bad_path, "uri": None, "original_path": bad_path, "mime_type": None,
         "path": "attachments/documents/note.txt", "summary_path": None, "message_ids": ["m-dot"]},
    ]
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {
        "chatlog/messages.json": messages, "attachments/index.json": index,
    })
    with zipfile.ZipFile(crafted, "a") as zf:
        zf.writestr("attachments/image/evil.png", b"\x89PNG bundled")
        zf.writestr("attachments/documents/note.txt", b"bundled note")
    target = _make_env(tmp_path / "dst", PID)
    _import(target, crafted, "new")

    conn = _mem(target)
    try:
        raw = conn.execute("SELECT metadata FROM messages WHERE id = 'm-dot'").fetchone()[0]
    finally:
        conn.close()
    meta = json.loads(raw)
    home: Path = target["home"]
    assert meta["images"] == [{"uri": "saiverse://image/evil.png", "path": str(home / "image" / "evil.png"),
                               "mime_type": "image/png"}]
    assert meta["documents"] == [{"path": str(home / "documents" / "note.txt"),
                                  "uri": "saiverse://document/note.txt"}]
    assert ".." not in raw
    assert (home / "image" / "evil.png").read_bytes() == b"\x89PNG bundled"


# ---------------------------------------------------------------------------
# extensions/saiverse/memory_db/: 書き出す側がこのフォルダへ書く表だけを受ける
# ---------------------------------------------------------------------------

def test_extensions_memory_db_refuses_common_folder_tables(source: Dict[str, Any], tmp_path: Path) -> None:
    from saiverse_memory.persona_pack.export import (
        COMMON_FOLDER_MEMORY_TABLES,
        NOT_CARRIED_MEMORY_TABLES,
        is_extension_memory_table,
    )

    assert not any(is_extension_memory_table(t) for t in COMMON_FOLDER_MEMORY_TABLES | NOT_CARRIED_MEMORY_TABLES)
    assert is_extension_memory_table("desk_items")
    smuggled = {
        "id": "m-smuggled", "thread_id": f"{PID}:room_a", "role": "user", "content": "迂回",
        "resource_id": PID, "created_at": T0,
        "metadata": json.dumps({"images": [{"uri": "saiverse://image/../CLAUDE.md", "path": "/etc/passwd"}]}),
    }
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {
        "extensions/saiverse/memory_db/messages.json": [smuggled],
        "extensions/saiverse/memory_db/message_embeddings.json": [{"message_id": "m1", "vector": "x"}],
    })
    target = _make_env(tmp_path / "dst", PID)
    result = _import(target, crafted, "new")
    conn = _mem(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE id = 'm-smuggled'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 6
        # 机の上 (extensions の表) はそのまま入る
        assert conn.execute("SELECT ref FROM desk_items").fetchall() == [("memopedia:1",)]
    finally:
        conn.close()
    assert result.counts["extensions/memory_db/messages"].skipped == 1
    assert result.counts["extensions/memory_db/messages"].added == 0
    assert result.counts["extensions/memory_db/message_embeddings"].skipped == 1
    assert any("memory_db/messages.json is not a table" in w for w in result.warnings)

    # 合流でも同じ警告を出す (合流は extensions/ を取り込まない)
    other = _make_env(tmp_path / "other", PID)
    merged = _import(other, crafted, "merge")
    assert merged.counts["extensions/memory_db/messages"].skipped == 1
    assert any("memory_db/messages.json is not a table" in w for w in merged.warnings)


# ---------------------------------------------------------------------------
# 再開: 世界側が適用済みなら印だけ消す / 別の ZIP では再開しない
# ---------------------------------------------------------------------------

def _add_world_life(env: Dict[str, Any]) -> None:
    """世界側の生活の記録を足す (予定・Track とそのローカルログ・Track に付いたタスク・出来事)。"""
    from database.models import ActionTrack, Episode, EpisodeInheritance, PersonaSchedule, TrackLocalLog

    pid = env["persona_id"]
    engine = create_engine(f"sqlite:///{Path(env['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(PersonaSchedule(PERSONA_ID=pid, SCHEDULE_TYPE="periodic", META_PLAYBOOK="meta_auto",
                              DESCRIPTION="朝の散歩", TIME_OF_DAY="07:00"))
        s.add(ActionTrack(track_id="track-1", persona_id=pid, track_type="user_conversation", title="会話"))
        s.add(TrackLocalLog(track_id="track-1", log_id="log-1", log_kind="note"))
        s.add(PersonaTask(id="task-2", persona_id=pid, title="Track の小目標", track_id="track-1",
                          active_step_id="step-2"))
        s.add(PersonaTaskStep(id="step-2", task_id="task-2", position=1, title="はじめの一歩"))
        s.add(Episode(EPISODE_ID="ep-1", PERSONA_ID=pid, KIND="conversation", STARTED_AT=T0))
        s.add(Episode(EPISODE_ID="ep-2", PERSONA_ID=pid, KIND="conversation", STARTED_AT=T0 + 1))
        s.add(EpisodeInheritance(EDGE_ID="edge-1", PERSONA_ID=pid, CHILD_EPISODE_ID="ep-2",
                                 PARENT_EPISODE_ID="ep-1", LAYER="day", CREATED_AT=T0))
        s.commit()
    engine.dispose()


def _world_counts(env: Dict[str, Any], persona_id: str) -> Dict[str, int]:
    world = sqlite3.connect(env["world_db"])
    try:
        return {
            table: world.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" = ?', (persona_id,)).fetchone()[0]
            for table, col in (
                ("persona_schedule", "PERSONA_ID"), ("persona_event_log", "PERSONA_ID"),
                ("persona_task", "persona_id"), ("action_track", "persona_id"),
                ("episodes", "PERSONA_ID"), ("episode_inheritance", "PERSONA_ID"),
                ("task_book", "PERSONA_ID"),
            )
        }
    finally:
        world.close()


def test_resume_after_world_commit_removes_only_the_marker(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    _add_world_life(source)
    pack = _export(source, tmp_path / "out" / "life.zip")
    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    # models.py より古い DB: 表が無くても取り込みが作る
    world = sqlite3.connect(target["world_db"])
    world.execute("DROP TABLE persona_pack_imports")
    world.commit()
    world.close()
    marker = target["persona_dir"] / "persona_pack_world_pending.json"

    # 世界側の commit は成功し、印の削除の前に止まった状態を作る
    monkeypatch.setattr(importer._ImportJob, "remove_pending_marker", lambda self: None)
    _import(target, pack, "new")
    monkeypatch.undo()
    assert marker.is_file()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["zip_sha256"] == importer._sha256_file(pack)
    assert payload["import_id"] == importer._import_id(payload["zip_sha256"], target["persona_id"])
    before = _world_counts(target, target["persona_id"])
    assert before["persona_schedule"] == 1 and before["persona_event_log"] == 1
    world = sqlite3.connect(target["world_db"])
    try:
        assert world.execute("SELECT IMPORT_ID, PERSONA_ID FROM persona_pack_imports").fetchall() == [
            (payload["import_id"], target["persona_id"])
        ]
    finally:
        world.close()

    result = _import(target, pack, "merge")
    assert not marker.exists()
    assert _world_counts(target, target["persona_id"]) == before
    assert result.counts["items"].added == 0
    assert any("already applied" in w for w in result.warnings)


def test_marker_of_a_different_zip_of_the_same_persona_is_not_resumed(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    original = importer._ImportJob.commit_world

    def fail(self: Any, world: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error (injected)")

    monkeypatch.setattr(importer._ImportJob, "commit_world", fail)
    with pytest.raises(sqlite3.OperationalError):
        _import(target, source["zip"], "new", overwrite_profile=True)
    monkeypatch.setattr(importer._ImportJob, "commit_world", original)
    marker_text = marker.read_text(encoding="utf-8")

    # 同じペルソナの、別の時点の ZIP
    conn = _mem(source)
    _add_message(conn, PID, "m-later", "room_a", "user", "後の話", T0 + 999, {"tags": ["conversation"]})
    conn.commit()
    conn.close()
    later = _export(source, tmp_path / "out" / "later.zip")

    result = _import(target, later, "merge")
    assert marker.read_text(encoding="utf-8") == marker_text  # 印は残る
    assert any("does not resume it" in w for w in result.warnings)
    assert _world_counts(target, target["persona_id"])["persona_event_log"] == 0
    assert result.counts["items"].added == 0
    world = sqlite3.connect(target["world_db"])
    try:
        # 印の overwrite_profile も使わない
        assert world.execute("SELECT AINAME FROM ai WHERE AIID = ?", (target["persona_id"],)).fetchone()[0] == "新入り"
    finally:
        world.close()
    # 合流そのものは行われる
    assert result.counts["messages"].added == 1

    # 正しい ZIP で入れ直せば再開する
    finished = _import(target, source["zip"], "merge")
    assert not marker.exists()
    assert any("resuming the unfinished part" in w for w in finished.warnings)
    assert _world_counts(target, target["persona_id"])["persona_event_log"] == 1


def _table_counts(db_path: Path) -> Dict[str, int]:
    """全テーブルの行数 (適用済み記録の表を除く)。"""
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'persona_pack_imports'"
        )]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def _home_files(env: Dict[str, Any]) -> List[Tuple[str, bytes]]:
    home: Path = env["home"]
    return sorted(
        (p.relative_to(home).as_posix(), p.read_bytes())
        for p in home.rglob("*")
        if p.is_file() and not p.name.startswith("memory.db") and not p.name.startswith("saiverse.db")
    )


def test_stop_before_the_memory_commit_is_finished_by_a_merge(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ファイルを書き終えた直後・memory.db の COMMIT 前に止まった新規作成を、同じ ZIP の合流が全段やり直す。

    印はファイルを書く前に置かれ、ファイルなので memory.db と一緒に巻き戻らない。memory.db の適用済み記録が
    無いので、再開の合流は memory.db 側の mode="new" の工程 (知覚台帳を含む
    extensions/saiverse/memory_db/ とペルソナフォルダのファイル) も行う。
    """
    from saiverse_memory.persona_pack import importer

    home: Path = source["home"]
    (home / "image" / "seen.png").write_bytes(b"\x89PNG seen only by perception")
    seen = {"path": str(home / "image" / "seen.png"), "mime_type": "image/png", "type": "image"}
    conn = _mem(source)
    try:
        conn.execute(
            "INSERT INTO perception_buffer(kind, content, media, created_at) VALUES (?,?,?,?)",
            ("room_visit", "部屋に入った", json.dumps([seen], ensure_ascii=False), T0),
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "perception.zip")

    # 比べる相手: 止まらずに終わった新規作成
    reference = _make_env(tmp_path / "ref", "newcomer_city_a", name="新入り")
    _import(reference, pack, "new", overwrite_profile=True)

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    original_write = importer._ImportJob.write_files

    def write_then_stop(self: Any) -> None:
        original_write(self)
        raise SystemExit("process stopped before the memory.db COMMIT (injected)")

    # プロセスが止まった状態を作る: ファイルを書いた直後に落ち、後始末 (書いたファイルの削除) も走らない
    monkeypatch.setattr(importer._ImportJob, "write_files", write_then_stop)
    monkeypatch.setattr(importer._ImportJob, "remove_created_files", lambda self: None)
    with pytest.raises(SystemExit, match="injected"):
        _import(target, pack, "new", overwrite_profile=True)
    monkeypatch.undo()

    # 印は残り、memory.db と saiverse.db は両方巻き戻っている
    assert marker.is_file()
    counts = _table_counts(target["memory_db"])
    assert counts["messages"] == 0 and counts["perception_buffer"] == 0
    mem = _mem(target)
    try:
        assert mem.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'persona_pack_imports'"
        ).fetchone()[0] == 0
    finally:
        mem.close()
    assert _world_counts(target, target["persona_id"])["persona_event_log"] == 0
    # 書きかけのファイル (添付など) は残っている
    assert (target["home"] / "image" / "seen.png").is_file()

    result = _import(target, pack, "merge")
    assert not marker.exists()
    assert sum("resuming the unfinished part" in w for w in result.warnings) == 2
    # 止まらずに終わった新規作成と同じ行数・同じファイル (書きかけのファイルは再利用され、_1 が増えない)
    assert _table_counts(target["memory_db"]) == _table_counts(reference["memory_db"])
    assert _table_counts(target["world_db"]) == _table_counts(reference["world_db"])
    assert _home_files(target) == _home_files(reference)
    assert result.counts["extensions/memory_db/perception_buffer"].added == 1
    assert result.counts["extensions/memory_db/desk_items"].added == 1
    assert result.counts["extensions/persona_dir"].added == 0  # 書きかけの active_state.json と同じ中身
    assert result.counts["extensions/persona_dir"].skipped == 1
    mem = _mem(target)
    try:
        (media,) = mem.execute("SELECT media FROM perception_buffer").fetchone()
        assert json.loads(media) == [{
            "path": str(target["home"] / "image" / "seen.png"), "uri": "saiverse://image/seen.png",
            "mime_type": "image/png", "type": "image",
        }]
        # memory.db 側の適用済み記録が同じトランザクションで書かれた
        assert mem.execute("SELECT IMPORT_ID FROM persona_pack_imports").fetchall() == [
            (importer._import_id(importer._sha256_file(pack), target["persona_id"]),)
        ]
    finally:
        mem.close()
    world = sqlite3.connect(target["world_db"])
    try:
        assert world.execute("SELECT AINAME FROM ai WHERE AIID = ?", (target["persona_id"],)).fetchone()[0] == "テスト子"
    finally:
        world.close()

    # もう一度合流しても何も増えない (印が無いので合流の範囲だけ)
    before = _dump(target)
    again = _import(target, pack, "merge")
    assert all(c.added == 0 and c.updated == 0 for c in again.counts.values()), again.counts
    assert _dump(target) == before


def test_stop_while_writing_files_leaves_the_marker_and_is_finished_by_a_merge(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新規作成がファイル (添付など) を書いている途中で止まっても印が残り、
    同じ ZIP の合流が新規作成の全工程 (memory.db 側・世界側・ファイル) を復旧して印を消す。"""
    from saiverse_memory.persona_pack import importer

    reference = _make_env(tmp_path / "ref", "newcomer_city_a", name="新入り")
    _import(reference, source["zip"], "new")

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    original_write = importer._ImportJob.write_files
    planned: List[int] = []

    def write_first_file_then_stop(self: Any) -> None:
        plans = self.file_plans
        planned.append(len(plans))
        self.file_plans = plans[:1]
        original_write(self)
        self.file_plans = plans
        raise SystemExit("process stopped while writing files (injected)")

    # プロセスが止まった状態を作る: 1 個目のファイルを書いた所で落ち、後始末も走らない
    monkeypatch.setattr(importer._ImportJob, "write_files", write_first_file_then_stop)
    monkeypatch.setattr(importer._ImportJob, "remove_created_files", lambda self: None)
    with pytest.raises(SystemExit, match="injected"):
        _import(target, source["zip"], "new")
    monkeypatch.undo()

    assert planned and planned[0] >= 2  # 途中で止まった (書いていないファイルが残っている)
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["zip_sha256"] == importer._sha256_file(source["zip"])
    assert _table_counts(target["memory_db"])["messages"] == 0
    assert _world_counts(target, target["persona_id"])["persona_event_log"] == 0
    assert len(_home_files(target)) < len(_home_files(reference))

    result = _import(target, source["zip"], "merge")
    assert not marker.exists()
    assert sum("resuming the unfinished part" in w for w in result.warnings) == 2
    assert _table_counts(target["memory_db"]) == _table_counts(reference["memory_db"])
    assert _table_counts(target["world_db"]) == _table_counts(reference["world_db"])
    assert _home_files(target) == _home_files(reference)
    assert result.counts["items"].added == 4


@pytest.mark.parametrize(
    "rel, counts_key",
    [
        (("personas", "newcomer_city_a", "active_state.json"), "extensions/persona_dir"),
        (("image", "pic.png"), "attachments"),
    ],
)
def test_stop_inside_a_file_write_leaves_no_partial_file_at_the_destination(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rel: Tuple[str, ...], counts_key: str,
) -> None:
    """ファイルの中身を一部だけ書いた所でプロセスが止まっても、保存先には書きかけのファイルが
    現れず (write_files は一時ファイルに書き切ってから os.replace で置く)、同じ ZIP の合流が
    そのファイルを完全な中身で復元する。保存先に書きかけが残ると、再開の合流はペルソナ
    フォルダのファイルを「中身の違う既存ファイル」としてスキップし、添付は ``_1`` に避けて、
    壊れたファイルが残る。"""
    from saiverse_memory.persona_pack import importer

    reference = _make_env(tmp_path / "ref", "newcomer_city_a", name="新入り")
    _import(reference, source["zip"], "new")

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    dest = target["home"].joinpath(*rel)
    expected = reference["home"].joinpath(*rel).read_bytes()
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    original_open = Path.open
    partial_writes: List[Path] = []

    class _StopMidWrite:
        def __init__(self, f: Any) -> None:
            self._f = f

        def __enter__(self) -> "_StopMidWrite":
            return self

        def __exit__(self, *exc: Any) -> None:
            self._f.close()

        def write(self, data: bytes) -> int:
            self._f.write(data[: len(data) // 2])
            self._f.flush()
            raise SystemExit("process stopped inside a file write (injected)")

    def open_and_stop_mid_write(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        f = original_open(self, mode, *args, **kwargs)
        # 保存先そのもの、または保存先の一時ファイルへの書き込みで落とす
        if ("w" in mode or "x" in mode) and self.parent == dest.parent and (
            self.name == dest.name or (self.name.startswith(dest.name + ".") and self.name.endswith(".partial"))
        ):
            partial_writes.append(self)
            return _StopMidWrite(f)
        return f

    # プロセスが止まった状態を作る: dest の中身を半分だけ書いた所で落ち、後始末も走らない
    monkeypatch.setattr(Path, "open", open_and_stop_mid_write)
    monkeypatch.setattr(importer._ImportJob, "remove_created_files", lambda self: None)
    with pytest.raises(SystemExit, match="injected"):
        _import(target, source["zip"], "new")
    monkeypatch.undo()

    assert len(partial_writes) == 1
    assert marker.is_file()
    # 保存先には何も無い (書きかけは一時ファイルの側にだけある)
    assert not dest.exists()
    assert partial_writes[0] != dest
    assert 0 < len(partial_writes[0].read_bytes()) < len(expected)

    result = _import(target, source["zip"], "merge")
    assert not marker.exists()
    assert dest.read_bytes() == expected
    # 同じ取り込みのやり直しは同じ一時ファイルを使い、書き切って置くので残らない
    assert not partial_writes[0].exists()
    assert _home_files(target) == _home_files(reference)
    assert not any(f"{dest.name} already exists" in w for w in result.warnings)
    assert result.counts[counts_key].added >= 1


def test_write_files_does_not_overwrite_a_destination_that_appears_before_it_is_placed(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """計画の後、一時ファイルを置く直前に保存先が現れたら、置かずに取り込みを巻き戻す。
    現れたファイルは上書きも削除もせず、一時ファイルは後始末で消える。"""
    from saiverse_memory.persona_pack import importer

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    dest = target["persona_dir"] / "active_state.json"
    original_partial_path = importer._ImportJob.partial_path

    def partial_path_after_someone_placed_the_file(self: Any, path: Path) -> Path:
        if path == dest:
            dest.write_text("someone else's state", encoding="utf-8")
        return original_partial_path(self, path)

    monkeypatch.setattr(importer._ImportJob, "partial_path", partial_path_after_someone_placed_the_file)
    with pytest.raises(FileExistsError):
        _import(target, source["zip"], "new")
    monkeypatch.undo()

    assert dest.read_text(encoding="utf-8") == "someone else's state"
    assert not list(dest.parent.glob(f"{dest.name}.*.partial"))
    assert _table_counts(target["memory_db"])["messages"] == 0


def test_resume_when_only_the_world_part_is_missing_does_not_redo_the_memory_part(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory.db 側が commit 済み (適用済み記録あり) なら、再開は世界側だけを行う。"""
    from saiverse_memory.persona_pack import importer

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")

    def fail(self: Any, world: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error (injected)")

    monkeypatch.setattr(importer._ImportJob, "commit_world", fail)
    with pytest.raises(sqlite3.OperationalError):
        _import(target, source["zip"], "new")
    monkeypatch.undo()
    result = _import(target, source["zip"], "merge")
    assert [w for w in result.warnings if "resuming the unfinished part" in w] == [
        "resuming the unfinished part of an earlier new-persona import of this pack "
        "(tasks, schedules, event log, items and private room)"
    ]
    # memory.db 側の extensions/ は合流の扱い (スキップ件数) のまま
    assert result.counts["extensions/memory_db/desk_items"].added == 0
    assert result.counts["extensions/memory_db/desk_items"].skipped == 1
    assert result.counts["items"].added == 4


# ---------------------------------------------------------------------------
# 同じ世界に同じパックから二体: 文字列の主キーを振り直す
# ---------------------------------------------------------------------------

def test_two_personas_from_the_same_pack_in_one_world_both_get_world_rows(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    _add_world_life(source)
    pack = _export(source, tmp_path / "out" / "life.zip")
    t1 = _make_env(tmp_path / "t", "one_city_a", name="一")
    _import(t1, pack, "new")
    t2_dir = t1["home"] / "personas" / "two_city_a"
    t2_dir.mkdir(parents=True)
    conn = init_db(str(t2_dir / "memory.db"))
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{Path(t1['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(AI(AIID="two_city_a", HOME_CITYID=1, AINAME="二", PRIVATE_ROOM_ID="two_room", AUTO_COUNT=0))
        s.add(Building(CITYID=1, BUILDINGID="two_room", BUILDINGNAME="二の部屋"))
        s.commit()
    engine.dispose()
    t2 = {**t1, "persona_id": "two_city_a", "persona_dir": t2_dir, "memory_db": t2_dir / "memory.db"}
    result = _import(t2, pack, "new")

    expected = {"persona_schedule": 1, "persona_event_log": 1, "persona_task": 2, "action_track": 1,
                "episodes": 2, "episode_inheritance": 1, "task_book": 1}
    assert _world_counts(t1, "one_city_a") == expected
    assert _world_counts(t1, "two_city_a") == expected
    for table in ("persona_task", "persona_task_step", "action_track", "track_local_log",
                  "episodes", "episode_inheritance"):
        assert result.counts[f"extensions/saiverse_db/{table}"].skipped == 0, table

    world = sqlite3.connect(t1["world_db"])
    try:
        for pid in ("one_city_a", "two_city_a"):
            # 親子の参照は振り直した親を指す
            task_id = _derived(pid, "persona_task", "task-2")
            track_id, step_id = world.execute(
                "SELECT track_id, active_step_id FROM persona_task WHERE id = ? AND persona_id = ?", (task_id, pid)
            ).fetchone()
            assert track_id == _derived(pid, "action_track", "track-1")
            assert step_id == _derived(pid, "persona_task_step", "step-2")
            assert world.execute(
                "SELECT task_id FROM persona_task_step WHERE id = ?", (step_id,)
            ).fetchone()[0] == task_id
            assert world.execute(
                "SELECT persona_id FROM action_track WHERE track_id = ?", (track_id,)
            ).fetchone()[0] == pid
            assert world.execute(
                "SELECT COUNT(*) FROM track_local_log WHERE track_id = ?", (track_id,)
            ).fetchone()[0] == 1
            assert world.execute(
                "SELECT CHILD_EPISODE_ID, PARENT_EPISODE_ID FROM episode_inheritance WHERE PERSONA_ID = ?", (pid,)
            ).fetchone() == (_derived(pid, "episodes", "ep-2"), _derived(pid, "episodes", "ep-1"))
        # task_book: 一体目は元の TASK_ID のまま、二体目は一体目の行と衝突するので振り直す
        books = dict(world.execute("SELECT PERSONA_ID, TASK_ID FROM task_book").fetchall())
        assert books == {"one_city_a": "tb-1", "two_city_a": _derived("two_city_a", "task_book", "tb-1")}
    finally:
        world.close()

    # 同じペルソナへの再実行 (合流) では二重に登録されない (一体目・二体目とも)
    for env, pid in ((t2, "two_city_a"), (t1, "one_city_a")):
        again = _import(env, pack, "merge")
        assert again.counts["task_book"].added == 0
        assert again.counts["task_book"].skipped == 1
        assert _world_counts(t1, pid) == expected


def test_task_book_round_trip_back_by_merge_does_not_duplicate(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """環境 A → B へ新規取り込み、B で書き出して A へ合流で戻しても約束は二重にならない。

    IDEM_KEY の無い行 (tb-1) で確かめる — IDEM_KEY の照合に頼らず TASK_ID で照合されること。
    """
    world_a = sqlite3.connect(source["world_db"])
    try:
        assert world_a.execute(
            "SELECT IDEM_KEY FROM task_book WHERE TASK_ID = 'tb-1'"
        ).fetchone() == (None,)
    finally:
        world_a.close()
    b = _make_env(tmp_path / "b", PID)
    _import(b, source["zip"], "new")
    back = _export(b, tmp_path / "out" / "back.zip")
    assert [r["TASK_ID"] for r in _zjson(back, "journal/task_book.json")] == ["tb-1"]

    result = _import(source, back, "merge")
    assert result.counts["task_book"].added == 0
    assert result.counts["task_book"].skipped == 1
    world_a = sqlite3.connect(source["world_db"])
    try:
        assert world_a.execute("SELECT TASK_ID, PERSONA_ID FROM task_book").fetchall() == [("tb-1", PID)]
    finally:
        world_a.close()


def test_world_string_key_tables_match_the_models() -> None:
    from sqlalchemy import Integer

    from saiverse_memory.persona_pack.export import SAIVERSE_DB_TABLE_OWNERS
    from saiverse_memory.persona_pack.importer import WORLD_STRING_KEY_TABLES

    # task_book は WORLD_STRING_KEY_TABLES に載せず、TASK_ID を衝突したときだけ振り直す
    # (stage_task_book)。元の値を保てる前提は、task_book を参照する表が無いこと。
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.name != "task_book", (table.name, fk.parent.name)
    carried = set(SAIVERSE_DB_TABLE_OWNERS)
    string_pk = {}
    for table in carried:
        pk = list(Base.metadata.tables[table].primary_key.columns)
        if len(pk) == 1 and not isinstance(pk[0].type, Integer):
            string_pk[table] = pk[0].name
    assert {t: spec[0] for t, spec in WORLD_STRING_KEY_TABLES.items()} == string_pk
    for table, (_pk, refs) in WORLD_STRING_KEY_TABLES.items():
        columns = Base.metadata.tables[table].columns
        listed = dict(refs)
        for col, ref_table in refs:
            assert col in columns, (table, col)
            assert ref_table in WORLD_STRING_KEY_TABLES, (table, col, ref_table)
        # ForeignKey で振り直す表の主キーを指す列は全部載っている
        for fk in Base.metadata.tables[table].foreign_keys:
            ref_table = fk.column.table.name
            if ref_table in WORLD_STRING_KEY_TABLES and fk.column.name == WORLD_STRING_KEY_TABLES[ref_table][0]:
                assert listed.get(fk.parent.name) == ref_table, (table, fk.parent.name)


def test_renumbered_task_book_entry_does_not_duplicate_when_it_comes_back(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """A → B (衝突で振り直し) → B で書き出し → A へ合流、で A の約束が増えない (ORIGIN_TASK_ID)。"""
    b = _make_env(tmp_path / "b", "newcomer_city_a", name="新入り")
    engine = create_engine(f"sqlite:///{Path(b['world_db']).as_posix()}")
    with Session(engine) as s:
        # B の世界で別のペルソナが同じ TASK_ID を使っている
        s.add(AI(AIID="other_city_a", HOME_CITYID=1, AINAME="別の子", SYSTEMPROMPT="", AUTO_COUNT=0))
        s.add(TaskBookEntry(TASK_ID="tb-1", PERSONA_ID="other_city_a", CONTENT="別の約束",
                            ORIGIN="conversation", CREATED_AT=T0))
        s.commit()
    engine.dispose()
    _import(b, source["zip"], "new")
    renumbered = _derived("newcomer_city_a", "task_book", "tb-1")
    world_b = sqlite3.connect(b["world_db"])
    try:
        assert world_b.execute(
            "SELECT TASK_ID, ORIGIN_TASK_ID FROM task_book WHERE PERSONA_ID = 'newcomer_city_a'"
        ).fetchall() == [(renumbered, "tb-1")]
    finally:
        world_b.close()

    back = _export(b, tmp_path / "out" / "back.zip")
    rows = _zjson(back, "journal/task_book.json")
    assert [(r["TASK_ID"], r["ORIGIN_TASK_ID"]) for r in rows] == [(renumbered, "tb-1")]

    result = _import(source, back, "merge")
    assert result.counts["task_book"].added == 0
    assert result.counts["task_book"].skipped == 1
    world_a = sqlite3.connect(source["world_db"])
    try:
        assert world_a.execute("SELECT TASK_ID, PERSONA_ID FROM task_book").fetchall() == [("tb-1", PID)]
    finally:
        world_a.close()

    # B の振り直した行を、振り直した ID のまま B へもう一度入れても二重にならない
    again = _import(b, back, "merge")
    assert again.counts["task_book"].added == 0


# ---------------------------------------------------------------------------
# __raw_text__ が object / array を包んだ行は取り込まない
# ---------------------------------------------------------------------------

def test_raw_text_wrapping_an_object_is_refused(source: Dict[str, Any], tmp_path: Path) -> None:
    attachments = {"images": [{"uri": "saiverse://image/../CLAUDE.md", "path": "/etc/passwd",
                               "mime_type": "image/png"}]}
    messages = _zjson(source["zip"], "chatlog/messages.json")
    messages += [
        {"id": "m-wrapped", "thread_id": f"{PID}:room_a", "role": "user", "content": "包み",
         "resource_id": PID, "created_at": "2026-10-10T00:00:00+00:00", "seq": 900,
         "metadata": {"__raw_text__": json.dumps(attachments)}},
        {"id": "m-wrapped-list", "thread_id": f"{PID}:room_a", "role": "user", "content": "包み",
         "resource_id": PID, "created_at": "2026-10-10T00:00:01+00:00", "seq": 901,
         "metadata": {"__raw_text__": json.dumps([attachments])}},
        # 正規の包み (解釈できない文字列) はそのまま入る
        {"id": "m-plain-raw", "thread_id": f"{PID}:room_a", "role": "user", "content": "素の文字列",
         "resource_id": PID, "created_at": "2026-10-10T00:00:02+00:00", "seq": 902,
         "metadata": {"__raw_text__": "not json {"}},
    ]
    pages = _zjson(source["zip"], "knowledge/pages.json")
    wrapped_page = dict(pages[0])
    wrapped_page["id"] = "page-wrapped"
    wrapped_page["metadata"] = {"__raw_text__": json.dumps({"thread_id": "x:y"})}
    pages.append(wrapped_page)
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {
        "chatlog/messages.json": messages, "knowledge/pages.json": pages,
    })
    target = _make_env(tmp_path / "dst", PID)
    result = _import(target, crafted, "new")

    conn = _mem(target)
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM messages")}
        assert "m-wrapped" not in ids and "m-wrapped-list" not in ids
        assert conn.execute(
            "SELECT metadata FROM messages WHERE id = 'm-plain-raw'"
        ).fetchone() == ("not json {",)
        assert conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE id = 'page-wrapped'"
        ).fetchone()[0] == 0
        assert "/etc/passwd" not in json.dumps(
            [r[0] for r in conn.execute("SELECT metadata FROM messages")]
        )
    finally:
        conn.close()
    assert result.counts["messages"].skipped == 2
    assert sum("__raw_text__" in w for w in result.warnings) == 3
    assert any("knowledge/pages.json" in w and "__raw_text__" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 知覚台帳の media: 添付と同じく運び、取り込み先で組み直す
# ---------------------------------------------------------------------------

def test_perception_media_is_carried_and_relocated(source: Dict[str, Any], tmp_path: Path) -> None:
    home: Path = source["home"]
    (home / "image" / "seen.png").write_bytes(b"\x89PNG seen only by perception")
    seen = {"path": str(home / "image" / "seen.png"), "mime_type": "image/png", "type": "image"}
    # 共有メディアフォルダの外 (本体 DB) を .. で指す項目と、.. 入りの uri
    escape = {"path": str(home / "image" / ".." / "user_data" / "database" / "saiverse.db"),
              "uri": "saiverse://image/../CLAUDE.md", "mime_type": "image/png", "type": "image"}
    conn = _mem(source)
    try:
        conn.execute(
            "INSERT INTO perception_buffer(kind, content, media, created_at) VALUES (?,?,?,?)",
            ("room_visit", "部屋に入った", json.dumps([seen], ensure_ascii=False), T0),
        )
        conn.execute(
            "INSERT INTO perception_batches(consumed_at, rendered_text, media) VALUES (?,?,?)",
            (T0, "部屋に入った", json.dumps([seen, escape], ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "perception.zip")

    index = {e["ref"]: e for e in _zjson(pack, "attachments/index.json")}
    entry = index[seen["path"]]
    assert entry["message_ids"] == []
    assert entry["perception_rows"] == ["perception_buffer:1", "perception_batches:1"]
    with zipfile.ZipFile(pack) as zf:
        assert zf.read(entry["path"]) == b"\x89PNG seen only by perception"
    assert index[escape["uri"]]["path"] is None  # 置き場の外は入れない

    target = _make_env(tmp_path / "dst", PID)
    _import(target, pack, "new")
    dst_home: Path = target["home"]
    assert (dst_home / "image" / "seen.png").read_bytes() == b"\x89PNG seen only by perception"
    relocated = {"path": str(dst_home / "image" / "seen.png"), "uri": "saiverse://image/seen.png",
                 "mime_type": "image/png", "type": "image"}
    conn = _mem(target)
    try:
        (buffer_media,) = conn.execute("SELECT media FROM perception_buffer").fetchone()
        (batch_media,) = conn.execute("SELECT media FROM perception_batches").fetchone()
    finally:
        conn.close()
    assert json.loads(buffer_media) == [relocated]
    batch = json.loads(batch_media)
    assert batch[0] == relocated
    assert batch[1] == {"path": None, "uri": None, "mime_type": "image/png", "type": "image"}
    assert ".." not in batch_media


def test_perception_media_in_a_crafted_pack_is_neutralised(source: Dict[str, Any], tmp_path: Path) -> None:
    """ZIP に直接書いた知覚台帳の行の .. 入りの media も無害化される (list のままの値も)。"""
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {
        "extensions/saiverse/memory_db/perception_buffer.json": [
            {"id": 1, "kind": "room_visit", "content": "a", "created_at": T0, "salient": 0,
             "media": json.dumps([{"path": "../../user_data/database/saiverse.db", "mime_type": "image/png"}])},
            {"id": 2, "kind": "room_visit", "content": "b", "created_at": T0, "salient": 0,
             "media": [{"uri": "saiverse://image/../x.png", "path": "/etc/passwd"}]},
        ],
    })
    target = _make_env(tmp_path / "dst", PID)
    _import(target, crafted, "new")
    conn = _mem(target)
    try:
        rows = dict(conn.execute("SELECT id, media FROM perception_buffer").fetchall())
    finally:
        conn.close()
    assert json.loads(rows[1]) == [{"path": None, "uri": None, "mime_type": "image/png"}]
    assert json.loads(rows[2]) == [{"uri": None, "path": None}]


# ---------------------------------------------------------------------------
# 部屋の様子の記帳 (room_state_json / perception_buffer.metadata) の束の media
# ---------------------------------------------------------------------------

def _room_bundle(room_media: List[Dict[str, Any]], *, extra_lines: List[str] = ()) -> Dict[str, Any]:
    return {
        "building_id": "room_x", "building_name": "部屋 X",
        "packages": [
            {"key": "building:interior", "family": "interior", "label": "内装",
             "lines": ["内装の説明", *extra_lines], "media": room_media, "state": None},
            {"key": "item:1", "family": "item", "label": "本", "lines": ["本がある"],
             "media": [], "state": "closed"},
        ],
    }


def _room_entries(conn: sqlite3.Connection) -> Dict[int, List[Dict[str, Any]]]:
    return {
        int(batch_id): json.loads(raw)
        for batch_id, raw in conn.execute("SELECT id, room_state_json FROM perception_batches")
    }


def test_room_state_media_is_carried_and_relocated(source: Dict[str, Any], tmp_path: Path) -> None:
    """正規の往復: 束の media は復元先を指し、差分の base_digest は書き換えた土台に付け直される。"""
    from sai_memory.room_state import (
        ROOM_STATE_META_KEY,
        bundle_is_valid,
        bundle_media,
        chain_is_intact,
        restore_room_state_bases,
        snapshot_digest,
    )

    home: Path = source["home"]
    (home / "image" / "room.png").write_bytes(b"\x89PNG room interior")
    room = {"path": str(home / "image" / "room.png"), "mime_type": "image/png", "type": "image"}
    # 共有メディアフォルダの外 (本体 DB) を .. で指す項目 — 書き出しは入れない
    escape = {"path": str(home / "image" / ".." / "user_data" / "database" / "saiverse.db"),
              "mime_type": "image/png", "type": "image"}
    full = _room_bundle([room, escape])
    diff = _room_bundle([room, escape], extra_lines=["棚が増えた"])
    entries_1 = [{"key": "building:room_x", "is_diff": False, "block": "部屋 X の様子", "snapshot": full}]
    entries_2 = [{"key": "building:room_x", "is_diff": True, "block": "部屋 X の変化",
                  "snapshot": diff, "base_digest": snapshot_digest(full)}]
    conn = _mem(source)
    try:
        conn.execute(
            "INSERT INTO perception_batches(consumed_at, rendered_text, room_state_json) VALUES (?,?,?)",
            (T0, "部屋 X の様子", json.dumps(entries_1, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO perception_batches(consumed_at, rendered_text, room_state_json) VALUES (?,?,?)",
            (T0 + 1, "部屋 X の変化", json.dumps(entries_2, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO perception_buffer(kind, content, metadata, created_at) VALUES (?,?,?,?)",
            ("surroundings", "部屋 X の様子", json.dumps(
                {ROOM_STATE_META_KEY: {"key": "building:room_x", "snapshot": full, "allow_diff": True}},
                ensure_ascii=False,
            ), T0 + 2),
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "room_state.zip")

    index = {e["ref"]: e for e in _zjson(pack, "attachments/index.json")}
    assert index[room["path"]]["perception_rows"] == [
        "perception_batches:1", "perception_batches:2", "perception_buffer:1",
    ]
    assert index[escape["path"]]["path"] is None  # 置き場の外は入れない

    target = _make_env(tmp_path / "dst", PID)
    _import(target, pack, "new")
    dst_home: Path = target["home"]
    assert (dst_home / "image" / "room.png").read_bytes() == b"\x89PNG room interior"
    relocated = {"path": str(dst_home / "image" / "room.png"), "uri": "saiverse://image/room.png",
                 "mime_type": "image/png", "type": "image"}

    conn = _mem(target)
    try:
        entries = _room_entries(conn)
        (buffer_meta,) = conn.execute("SELECT metadata FROM perception_buffer").fetchone()
        (raw_1,) = conn.execute("SELECT room_state_json FROM perception_batches WHERE id = 1").fetchone()
        # 読み手: 差分の土台が見えているので、全文への開き直しは起きない
        assert restore_room_state_bases(conn) == 0
    finally:
        conn.close()
    first, second = entries[1][0], entries[2][0]
    for snapshot in (first["snapshot"], second["snapshot"],
                     json.loads(buffer_meta)[ROOM_STATE_META_KEY]["snapshot"]):
        assert bundle_is_valid(snapshot)
        assert bundle_media(snapshot) == [relocated]  # 復元できなかった項目は外れる
    assert "saiverse.db" not in raw_1 and ".." not in raw_1
    assert second["base_digest"] == snapshot_digest(first["snapshot"])
    assert chain_is_intact(second, first)


def test_room_state_media_in_a_crafted_pack_is_neutralised(source: Dict[str, Any], tmp_path: Path) -> None:
    """ZIP に直接書いた部屋の様子の記帳の .. 入り・メディアルートの外の path は残らない。"""
    from sai_memory.room_state import ROOM_STATE_META_KEY, bundle_is_valid, bundle_media

    dotdot = {"path": "../../user_data/database/saiverse.db", "mime_type": "image/png", "type": "image"}
    outside = {"path": "/etc/passwd", "mime_type": "image/png", "type": "image"}
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {
        "extensions/saiverse/memory_db/perception_batches.json": [
            {"id": 1, "consumed_at": T0, "rendered_text": "a", "kind": "perception",
             "room_state_json": json.dumps([{"key": "building:room_x", "is_diff": False, "block": "a",
                                             "snapshot": _room_bundle([dotdot])}])},
            # JSON 文字列でなく list のままの値
            {"id": 2, "consumed_at": T0 + 1, "rendered_text": "b", "kind": "perception",
             "room_state_json": [{"key": "building:room_x", "is_diff": False, "block": "b",
                                  "snapshot": _room_bundle([outside, dotdot])}]},
        ],
        "extensions/saiverse/memory_db/perception_buffer.json": [
            {"id": 1, "kind": "surroundings", "content": "c", "created_at": T0, "salient": 0,
             "metadata": json.dumps({ROOM_STATE_META_KEY: {
                 "key": "building:room_x", "snapshot": _room_bundle([outside]), "allow_diff": True}})},
        ],
    })
    target = _make_env(tmp_path / "dst", PID)
    _import(target, crafted, "new")
    conn = _mem(target)
    try:
        entries = _room_entries(conn)
        (buffer_meta,) = conn.execute("SELECT metadata FROM perception_buffer").fetchone()
        raw = json.dumps([r[0] for r in conn.execute("SELECT room_state_json FROM perception_batches")])
    finally:
        conn.close()
    snapshots = [entries[1][0]["snapshot"], entries[2][0]["snapshot"],
                 json.loads(buffer_meta)[ROOM_STATE_META_KEY]["snapshot"]]
    for snapshot in snapshots:
        assert bundle_is_valid(snapshot)
        assert bundle_media(snapshot) == []
    assert "saiverse.db" not in raw and "/etc/passwd" not in raw
    assert "/etc/passwd" not in buffer_meta


# ---------------------------------------------------------------------------
# 画像の置き場所の解決 (Codex 敵対レビュー四巡目)
# ---------------------------------------------------------------------------

def test_profile_image_uploaded_through_the_media_api_round_trips(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """設定画面の ImageUpload が保存する /api/media/images/<名前> の画像も運ばれ、表示経路で解決できる。"""
    home: Path = source["home"]
    (home / "image" / "uploaded_face.png").write_bytes(b"\x89PNG uploaded avatar")
    (home / "image" / "uploaded_look.png").write_bytes(b"\x89PNG uploaded appearance")
    world = sqlite3.connect(source["world_db"])
    world.execute(
        "UPDATE ai SET AVATAR_IMAGE = ?, APPEARANCE_IMAGE_PATH = ? WHERE AIID = ?",
        ("/api/media/images/uploaded_face.png", str(home / "image" / "uploaded_look.png"), PID),
    )
    world.commit()
    world.close()
    pack = _export(source, tmp_path / "out" / "uploaded.zip")
    profile = _zjson(pack, "profile/profile.json")
    with zipfile.ZipFile(pack) as zf:
        assert zf.read(profile["files"]["AVATAR_IMAGE"]) == b"\x89PNG uploaded avatar"
        assert zf.read(profile["files"]["APPEARANCE_IMAGE_PATH"]) == b"\x89PNG uploaded appearance"

    target = _make_env(tmp_path / "dst", "uploaded_city_b", city_id=2)
    _import(target, pack, "new", overwrite_profile=True)
    world = sqlite3.connect(target["world_db"])
    try:
        avatar, appearance = world.execute(
            "SELECT AVATAR_IMAGE, APPEARANCE_IMAGE_PATH FROM ai WHERE AIID = ?",
            (target["persona_id"],),
        ).fetchone()
    finally:
        world.close()
    dst_home: Path = target["home"]

    # 表示経路は api/deps.py の avatar_path_to_url (文字列だけを見る関数。api.deps の
    # import は database.session の engine を作るが、DB へは接続しない —
    # tests/test_persona_pack_api.py も同じ import をしている)
    from api.deps import avatar_path_to_url

    # /api/media/images/<名前> は同じ形で戻り、その配信元 (api/routes/media.py の
    # serve_image = SAIVERSE_HOME/image/) に実体がある
    assert avatar == "/api/media/images/uploaded_face.png"
    assert avatar_path_to_url(avatar) == avatar
    assert (dst_home / "image" / "uploaded_face.png").read_bytes() == b"\x89PNG uploaded avatar"
    # 共有 image フォルダの保存パスは user_data/icons へ置き、表示の URL に変えられる
    assert Path(appearance).parent == dst_home / "user_data" / "icons"
    assert Path(appearance).read_bytes() == b"\x89PNG uploaded appearance"
    assert avatar_path_to_url(appearance) == "/api/static/user_icons/uploaded_look.png"


def test_private_room_image_uploaded_through_the_media_api_round_trips(
    source: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部屋の編集画面の ImageUpload が保存する /api/media/images/<名前> の私室の画像も運ばれ、
    取り込み後に同じ形の URL で指し、読み手 (media_utils._resolve_building_image) が実ファイルに解決する。"""
    from sqlalchemy.orm import sessionmaker

    import database.session
    from saiverse.media_utils import _resolve_building_image
    from saiverse_memory.persona_pack.export import _resolve_stored_path

    home: Path = source["home"]
    world = sqlite3.connect(source["world_db"])
    world.execute(
        "UPDATE building SET IMAGE_PATH = ? WHERE BUILDINGID = ?",
        ("/api/media/images/room.png", source["room_id"]),
    )
    world.commit()
    world.close()
    pack = _export(source, tmp_path / "out" / "room_url.zip")
    assert _room_view(pack)["image"] == b"\x89PNG room image"
    # URL の名前は区切りを含まないファイル名だけ受ける (image/ の外を指させない)
    assert _resolve_stored_path("/api/media/images/../user_data/database/saiverse.db", home) is None
    assert _resolve_stored_path("/api/media/images/missing.png", home) is None

    target = _make_env(tmp_path / "dst", "room_url_city_b", city_id=2)
    _import(target, pack, "new")
    dst_home: Path = target["home"]
    world = sqlite3.connect(target["world_db"])
    try:
        (image_path,) = world.execute(
            "SELECT IMAGE_PATH FROM building WHERE BUILDINGID = ?", (target["room_id"],)
        ).fetchone()
    finally:
        world.close()
    assert image_path == "/api/media/images/room.png"
    assert (dst_home / "image" / "room.png").read_bytes() == b"\x89PNG room image"

    engine = create_engine(f"sqlite:///{Path(target['world_db']).as_posix()}")
    try:
        monkeypatch.setattr(database.session, "SessionLocal", sessionmaker(bind=engine))
        resolved = _resolve_building_image(target["room_id"], dst_home)
    finally:
        engine.dispose()
    assert resolved is not None and resolved.read_bytes() == b"\x89PNG room image"


def test_icons_referenced_by_perception_and_message_media_are_carried(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """user_data/icons のファイルを指す記帳・知覚台帳・messages.metadata の media も運ばれ、復元先を指す。"""
    from sai_memory.room_state import bundle_is_valid, bundle_media

    home: Path = source["home"]
    other_look = home / "user_data" / "icons" / "other_look.png"
    other_look.write_bytes(b"\x89PNG the other persona's appearance")
    seen = {"path": str(other_look), "mime_type": "image/png", "type": "image"}
    entries = [{"key": "building:room_x", "is_diff": False, "block": "部屋 X の様子",
                "snapshot": _room_bundle([seen])}]
    conn = _mem(source)
    try:
        conn.execute(
            "INSERT INTO perception_batches(consumed_at, rendered_text, room_state_json, media) "
            "VALUES (?,?,?,?)",
            (T0, "部屋 X の様子", json.dumps(entries, ensure_ascii=False),
             json.dumps([seen], ensure_ascii=False)),
        )
        _add_message(conn, PID, "m-spell", "room_a", "system", "相手を見た", T0 + 5,
                     {"tags": ["conversation", "spell"], "media": [seen]})
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "icons.zip")
    index = {e["ref"]: e for e in _zjson(pack, "attachments/index.json")}
    entry = index[str(other_look)]
    assert entry["path"] == "attachments/image/other_look.png"
    assert entry["message_ids"] == ["m-spell"]
    assert entry["perception_rows"] == ["perception_batches:1"]

    target = _make_env(tmp_path / "dst", PID)
    _import(target, pack, "new")
    dst_home: Path = target["home"]
    restored = dst_home / "image" / "other_look.png"
    assert restored.read_bytes() == b"\x89PNG the other persona's appearance"
    relocated = {"path": str(restored), "uri": "saiverse://image/other_look.png",
                 "mime_type": "image/png", "type": "image"}
    conn = _mem(target)
    try:
        raw_entries, batch_media = conn.execute(
            "SELECT room_state_json, media FROM perception_batches"
        ).fetchone()
        (metadata,) = conn.execute("SELECT metadata FROM messages WHERE id = 'm-spell'").fetchone()
    finally:
        conn.close()
    snapshot = json.loads(raw_entries)[0]["snapshot"]
    assert bundle_is_valid(snapshot)
    assert bundle_media(snapshot) == [relocated]
    assert json.loads(batch_media) == [relocated]
    assert json.loads(metadata)["media"] == [relocated]


def test_shrunk_room_state_drops_the_same_media_after_import(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    """縮小済みの記帳の dropped_media も付け替わり、取り込み前後で _reduce_rooms が同じ画像を外す。"""
    from types import SimpleNamespace

    from sai_memory.presented_reduction import (
        DROPPED_MEDIA_FIELD,
        SHRUNK_BLOCK_FIELD,
        SHRUNK_FLAG,
        _reduce_rooms,
    )

    home: Path = source["home"]
    (home / "image" / "room.png").write_bytes(b"\x89PNG shrunk room")
    (home / "image" / "keep.png").write_bytes(b"\x89PNG still presented")
    room = {"path": str(home / "image" / "room.png"), "mime_type": "image/png", "type": "image"}
    keep = {"path": str(home / "image" / "keep.png"), "mime_type": "image/png", "type": "image"}
    # 元の環境でもファイルの無い画像 (ZIP に入らず、取り込みで復元されない)
    gone = {"path": str(home / "image" / "gone.png"), "mime_type": "image/png", "type": "image"}
    entries = [{"key": "building:room_x", "is_diff": False, "block": "部屋 X の様子",
                SHRUNK_FLAG: True, SHRUNK_BLOCK_FIELD: "(部屋 X の様子は省略)",
                DROPPED_MEDIA_FIELD: [room["path"], gone["path"]]}]
    text = "見出し\n\n部屋 X の様子\n\n残る話"
    conn = _mem(source)
    try:
        conn.execute(
            "INSERT INTO perception_batches(consumed_at, rendered_text, room_state_json, media) "
            "VALUES (?,?,?,?)",
            (T0, text, json.dumps(entries, ensure_ascii=False),
             json.dumps([room, keep, gone], ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    def reduced(env: Dict[str, Any]) -> List[Dict[str, Any]]:
        conn = _mem(env)
        try:
            batch_id, rendered, raw, media = conn.execute(
                "SELECT id, rendered_text, room_state_json, media FROM perception_batches"
            ).fetchone()
        finally:
            conn.close()
        batch = SimpleNamespace(id=batch_id, room_state_json=raw)
        out_text, kept, changed = _reduce_rooms(batch, rendered, json.loads(media))
        assert changed and "(部屋 X の様子は省略)" in out_text
        return kept

    assert reduced(source) == [keep]
    pack = _export(source, tmp_path / "out" / "shrunk.zip")

    target = _make_env(tmp_path / "dst", PID)
    _import(target, pack, "new")
    dst_home: Path = target["home"]
    conn = _mem(target)
    try:
        (raw,) = conn.execute("SELECT room_state_json FROM perception_batches").fetchone()
    finally:
        conn.close()
    # 復元した画像は復元先の path へ写り、復元しなかった画像の元の path は残らない
    assert json.loads(raw)[0][DROPPED_MEDIA_FIELD] == [str(dst_home / "image" / "room.png")]
    assert "gone.png" not in raw
    kept = reduced(target)
    # 外れるのは取り込み前と同じ部屋の画像で、残すべき画像は残る。gone は取り込みで
    # path も uri も空になり、読みに行くファイルが無い
    assert kept == [
        {"path": str(dst_home / "image" / "keep.png"), "uri": "saiverse://image/keep.png",
         "mime_type": "image/png", "type": "image"},
        {"path": None, "uri": None, "mime_type": "image/png", "type": "image"},
    ]


# ---------------------------------------------------------------------------
# JSON 文字列の列の __bytes_b64__ の包み (Codex 敵対レビュー七巡目)
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> Dict[str, str]:
    return {"__bytes_b64__": base64.b64encode(data).decode("ascii")}


def test_bytes_wrapped_perception_columns_are_neutralised(source: Dict[str, Any], tmp_path: Path) -> None:
    """知覚台帳の media・room_state_json・perception_buffer.metadata を bytes で包んでも無害化を通る。

    読み手 (perception_buffer._decode_media など) は json.loads で bytes も解釈するので、
    bytes のまま保存されると取り込みの書き換えを迂回する。
    """
    from sai_memory.room_state import ROOM_STATE_META_KEY, bundle_is_valid, bundle_media

    # 正規の書き出しはこれらの列を bytes で包まない (保存形が JSON 文字列のため)
    room_entries = [{"key": "building:room_x", "is_diff": False, "block": "a",
                     "snapshot": _room_bundle([])}]
    conn = _mem(source)
    try:
        conn.execute(
            "INSERT INTO perception_buffer(kind, content, media, metadata, created_at) VALUES (?,?,?,?,?)",
            ("room_visit", "部屋に入った", json.dumps([]),
             json.dumps({ROOM_STATE_META_KEY: room_entries[0]}), T0),
        )
        conn.execute(
            "INSERT INTO perception_batches(consumed_at, rendered_text, media, room_state_json) VALUES (?,?,?,?)",
            (T0, "部屋に入った", json.dumps([]), json.dumps(room_entries)),
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "perception.zip")
    (buffer_row,) = _zjson(pack, "extensions/saiverse/memory_db/perception_buffer.json")
    (batch_row,) = _zjson(pack, "extensions/saiverse/memory_db/perception_batches.json")
    assert all(isinstance(v, str) for v in (buffer_row["media"], buffer_row["metadata"],
                                            batch_row["media"], batch_row["room_state_json"]))

    outside = {"path": "/etc/passwd", "uri": "saiverse://image/../x.png", "mime_type": "image/png"}
    dotdot = {"path": "../../user_data/database/saiverse.db", "mime_type": "image/png", "type": "image"}
    crafted_room = [{"key": "building:room_x", "is_diff": False, "block": "b",
                     "snapshot": _room_bundle([dotdot])}]
    crafted = _add_zip_entries(pack, tmp_path / "crafted.zip", {
        "extensions/saiverse/memory_db/perception_buffer.json": [
            {"id": 1, "kind": "room_visit", "content": "a", "created_at": T0, "salient": 0,
             "media": _b64(json.dumps([outside]).encode("utf-8")),
             "metadata": _b64(json.dumps({ROOM_STATE_META_KEY: crafted_room[0]}).encode("utf-8"))},
            # json.loads(bytes) は UTF-16 も判定して解釈する
            {"id": 2, "kind": "room_visit", "content": "b", "created_at": T0, "salient": 0,
             "media": _b64(json.dumps([outside]).encode("utf-16-le"))},
            # JSON の文字列に戻らない bytes と、列の形 (dict) でない JSON は None になる
            {"id": 3, "kind": "room_visit", "content": "c", "created_at": T0, "salient": 0,
             "media": _b64(b"\x80\x81\x82"), "metadata": _b64(b"not json")},
        ],
        "extensions/saiverse/memory_db/perception_batches.json": [
            {"id": 1, "consumed_at": T0, "rendered_text": "d", "kind": "perception",
             "media": _b64(json.dumps([outside]).encode("utf-8")),
             "room_state_json": _b64(json.dumps(crafted_room).encode("utf-8"))},
        ],
    })
    target = _make_env(tmp_path / "dst", PID)
    result = _import(target, crafted, "new")
    conn = _mem(target)
    try:
        buffer = {r[0]: r[1:] for r in conn.execute(
            "SELECT id, media, typeof(media), metadata, typeof(metadata) FROM perception_buffer"
        )}
        batch = conn.execute(
            "SELECT media, typeof(media), room_state_json, typeof(room_state_json) FROM perception_batches"
        ).fetchone()
    finally:
        conn.close()
    neutralised = [{"path": None, "uri": None, "mime_type": "image/png"}]
    assert buffer[1][1] == "text" and json.loads(buffer[1][0]) == neutralised
    assert buffer[2][1] == "text" and json.loads(buffer[2][0]) == neutralised
    assert buffer[3] == (None, "null", None, "null")
    assert batch[1] == "text" and json.loads(batch[0]) == neutralised
    assert buffer[1][3] == "text" and batch[3] == "text"
    for snapshot in (json.loads(buffer[1][2])[ROOM_STATE_META_KEY]["snapshot"],
                     json.loads(batch[2])[0]["snapshot"]):
        assert bundle_is_valid(snapshot)
        assert bundle_media(snapshot) == []
    stored = json.dumps([*buffer.values(), batch])
    assert "/etc/passwd" not in stored and "saiverse.db" not in stored
    assert sum("__bytes_b64__" in w and "set to None" in w for w in result.warnings) == 2


def test_bytes_wrapped_message_metadata_is_checked_like_text(source: Dict[str, Any], tmp_path: Path) -> None:
    """messages.metadata の bytes の包みも JSON として解釈して添付の書き換えに通し、戻せなければ取り込まない。"""
    attachments = {"images": [{"uri": "saiverse://image/../CLAUDE.md", "path": "/etc/passwd",
                               "mime_type": "image/png"}]}
    messages = _zjson(source["zip"], "chatlog/messages.json")
    # 正規の書き出しは包まない
    assert not any(isinstance(m.get("metadata"), dict) and "__bytes_b64__" in m["metadata"]
                   for m in messages)
    messages += [
        {"id": "m-bytes", "thread_id": f"{PID}:room_a", "role": "user", "content": "包み",
         "resource_id": PID, "created_at": "2026-10-10T00:00:00+00:00", "seq": 900,
         "metadata": _b64(json.dumps(attachments).encode("utf-8"))},
        {"id": "m-bytes-scalar", "thread_id": f"{PID}:room_a", "role": "user", "content": "包み",
         "resource_id": PID, "created_at": "2026-10-10T00:00:01+00:00", "seq": 901,
         "metadata": _b64(b"not json {")},
        {"id": "m-bytes-broken", "thread_id": f"{PID}:room_a", "role": "user", "content": "包み",
         "resource_id": PID, "created_at": "2026-10-10T00:00:02+00:00", "seq": 902,
         "metadata": _b64(b"\x80\x81\x82")},
    ]
    crafted = _add_zip_entries(source["zip"], tmp_path / "crafted.zip", {"chatlog/messages.json": messages})
    target = _make_env(tmp_path / "dst", PID)
    result = _import(target, crafted, "new")
    conn = _mem(target)
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT id, metadata, typeof(metadata) FROM messages WHERE id LIKE 'm-bytes%'"
        )}
    finally:
        conn.close()
    assert set(rows) == {"m-bytes", "m-bytes-scalar"}
    assert rows["m-bytes"][1] == "text"
    assert json.loads(rows["m-bytes"][0])["images"] == [
        {"uri": None, "path": None, "mime_type": "image/png"}
    ]
    assert rows["m-bytes-scalar"] == ("not json {", "text")
    assert result.counts["messages"].skipped == 1
    assert any("m-bytes-broken" in w and "__bytes_b64__" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 振り直した短縮番号を指す clips / purpose_tags の参照 (Codex 敵対レビュー七巡目)
# ---------------------------------------------------------------------------

def _page_short_id(env: Dict[str, Any], page_id: str) -> int:
    conn = _mem(env)
    try:
        return conn.execute("SELECT short_id FROM memopedia_pages WHERE id = ?", (page_id,)).fetchone()[0]
    finally:
        conn.close()


def _core_id(env: Dict[str, Any], page_id: str) -> int:
    conn = _mem(env)
    try:
        (meta,) = conn.execute("SELECT metadata FROM memopedia_pages WHERE id = ?", (page_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(meta)["core_id"]


def test_renumbered_page_references_follow_the_page(source: Dict[str, Any], tmp_path: Path) -> None:
    apple = source["ids"]["apple"]
    conn = _mem(source)
    try:
        (core_page,) = conn.execute(
            "SELECT id FROM memopedia_pages WHERE category = 'core' "
            "AND json_extract(metadata, '$.core_id') = ?", (source["ids"]["core_id"],)
        ).fetchone()
        (zip_max,) = conn.execute("SELECT MAX(short_id) FROM memopedia_pages").fetchone()
    finally:
        conn.close()
    apple_sid = _page_short_id(source, apple)
    core_sid = _core_id(source, core_page)
    conn = _mem(source)
    try:
        clip = add_clip(conn, message_id="m2", quote="B の返事",
                        purpose_ref=f"memopedia:{apple_sid}", pasted_to=f"core:{core_sid}")
        conn.executemany(
            "INSERT INTO purpose_tags(tag_id, target_ref, purpose_ref, layer, created_at) VALUES (?,?,?,?,?)",
            [
                ("tag-m", f"memopedia:{apple_sid}", "task:1", 2, T0),
                # 旧 prefix で書いた同じページ — 書き換えると tag-m と同じ組になる
                ("tag-m-alias", f"m:{apple_sid}", "task:1", 2, T0),
                ("tag-purpose", "message:m1", f"m:{apple_sid}", 2, T0),
                ("tag-uri", f"saiverse://self/memopedia/{apple_sid}", "task:2", 2, T0),
                ("tag-core", f"core:{core_sid}", "task:3", 2, T0),
                ("tag-clip", f"clip:{clip.short_id}", "task:4", 2, T0),
                # 別のペルソナのページは書き換えない
                ("tag-other", f"saiverse://city_a/someone/memopedia/{apple_sid}", "task:5", 2, T0),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "refs.zip")

    # 取り込み先には、ZIP のページと同じ番号を持つ別のページ・コア記憶・クリップがある
    target = _make_env(tmp_path / "dst", PID)
    conn = _mem(target)
    try:
        for i in range(zip_max):
            create_page(conn, parent_id="root_terms", title=f"取り込み先のページ {i}",
                        content="取り込み先だけの記事", category="terms")
        add_core_memory(conn, "取り込み先の大事なこと")
        for _ in range(clip.short_id):
            add_clip(conn, message_id="t1", quote="取り込み先の引用")
        conn.commit()
        (target_page_at_apple_sid,) = conn.execute(
            "SELECT id FROM memopedia_pages WHERE short_id = ?", (apple_sid,)
        ).fetchone()
    finally:
        conn.close()
    assert target_page_at_apple_sid != apple
    result = _import(target, pack, "merge")

    apple_new = _page_short_id(target, apple)
    core_new = _core_id(target, core_page)
    assert apple_new != apple_sid and core_new != core_sid
    conn = _mem(target)
    try:
        tags = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT tag_id, target_ref, purpose_ref FROM purpose_tags"
        )}
        clip_row = conn.execute(
            "SELECT short_id, purpose_ref, pasted_to FROM clips WHERE clip_id = ?", (clip.clip_id,)
        ).fetchone()
    finally:
        conn.close()
    assert clip_row[0] != clip.short_id  # クリップの番号も振り直された
    assert tags["tag-m"] == (f"memopedia:{apple_new}", "task:1")
    assert "tag-m-alias" not in tags  # 書き換え後の組が tag-m と同じなので同じ付与
    assert tags["tag-purpose"] == ("message:m1", f"memopedia:{apple_new}")
    assert tags["tag-uri"] == (f"saiverse://self/memopedia/{apple_new}", "task:2")
    assert tags["tag-core"] == (f"core:{core_new}", "task:3")
    assert tags["tag-clip"] == (f"clip:{clip_row[0]}", "task:4")
    assert tags["tag-other"] == (f"saiverse://city_a/someone/memopedia/{apple_sid}", "task:5")
    assert clip_row[1:] == (f"memopedia:{apple_new}", f"core:{core_new}")
    assert result.counts["purpose_tags"].skipped == 1


def test_desk_references_follow_renumbered_pages_on_new_import(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    apple = source["ids"]["apple"]
    apple_sid = _page_short_id(source, apple)
    conn = _mem(source)
    try:
        (zip_max,) = conn.execute("SELECT MAX(short_id) FROM memopedia_pages").fetchone()
        src_ids = dict(conn.execute("SELECT short_id, id FROM memopedia_pages WHERE short_id IS NOT NULL"))
        conn.executemany(
            "INSERT INTO desk_items(ref, opened_at, last_touched_at, purpose_ref) VALUES (?,?,?,?)",
            [
                (f"m:{apple_sid}", T0, T0, "task:1"),
                # ZIP のどのページの番号でもない参照: 取り込み先で別のページを開きうるので入れない
                ("memopedia:999", T0, T0, None),
                # 目的の参照が ZIP のどの所持品の番号でもない行も入れない
                (f"memopedia:{_page_short_id(source, source['ids']['banana'])}", T0, T0, "item:99"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "desk.zip")

    # 取り込み先には会話が無いが、ZIP のページと同じ番号を持つ別のページがある
    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    conn = _mem(target)
    try:
        for i in range(zip_max):
            create_page(conn, parent_id="root_terms", title=f"取り込み先のページ {i}",
                        content="取り込み先だけの記事", category="terms")
        conn.commit()
    finally:
        conn.close()
    result = _import(target, pack, "new")

    apple_new = _page_short_id(target, apple)
    assert apple_new != apple_sid
    conn = _mem(target)
    try:
        desk = dict(conn.execute("SELECT ref, purpose_ref FROM desk_items"))
        dst_numbers = dict(conn.execute("SELECT id, short_id FROM memopedia_pages"))
    finally:
        conn.close()
    # 机の参照は、元の環境で開いていたのと同じページ (id) を指す
    assert desk == {
        f"memopedia:{apple_new}": "task:1",
        f"memopedia:{dst_numbers[src_ids[1]]}": None,
    }
    assert result.counts["extensions/memory_db/desk_items"].added == 2
    assert result.counts["extensions/memory_db/desk_items"].skipped == 2
    assert any(w.startswith("desk_items:") and "not imported" in w for w in result.warnings)


def test_references_to_chronicle_pages_not_merged_are_not_kept(
    chron_branch: Dict[str, Any], tmp_path: Path
) -> None:
    """合流で取り込まない Chronicle を指す参照は、取り込み先の同じ番号の別の行を指さないよう残さない。"""
    a, b, ids = chron_branch["a"], chron_branch["b"], chron_branch["ids"]
    in_a = _chronicle_rows(a)
    r1c_sid = in_a[ids["r1c"]]["meta"]["short_id"]  # R1c は取り込まれない
    na1_sid = in_a[ids["na1"]]["meta"]["short_id"]  # NA1 は取り込まれ、番号が振り直される
    conn = _mem(a)
    try:
        clip = add_clip(conn, message_id="s5", quote="共有の話 5",
                        purpose_ref=f"ch:{na1_sid}", pasted_to=f"chronicle:{r1c_sid}")
        conn.executemany(
            "INSERT INTO purpose_tags(tag_id, target_ref, purpose_ref, layer, created_at) VALUES (?,?,?,?,?)",
            [("tag-r1c", "message:s5", f"chronicle:{r1c_sid}", 2, T0),
             ("tag-na1", f"chronicle:{na1_sid}", "task:1", 2, T0)],
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(a, tmp_path / "out" / "a_refs.zip")
    result = _import(b, pack, "merge")

    after = _chronicle_rows(b)
    na1_new = after[ids["na1"]]["meta"]["short_id"]
    assert ids["r1c"] not in after and na1_new != na1_sid
    conn = _mem(b)
    try:
        tags = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT tag_id, target_ref, purpose_ref FROM purpose_tags"
        )}
        clip_row = conn.execute(
            "SELECT purpose_ref, pasted_to FROM clips WHERE clip_id = ?", (clip.clip_id,)
        ).fetchone()
    finally:
        conn.close()
    assert "tag-r1c" not in tags
    assert tags["tag-na1"] == (f"chronicle:{na1_new}", "task:1")
    assert clip_row == (f"chronicle:{na1_new}", None)
    assert any(w.startswith("purpose_tags:") and "not imported" in w for w in result.warnings)
    assert any(w.startswith("clips:") and "set to None" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# item:N の参照は、所持品の SHORT_ID の振り直しに追従する
# ---------------------------------------------------------------------------

def _item_numbers(env: Dict[str, Any]) -> Dict[str, int]:
    """世界の item の {ITEM_ID: SHORT_ID}。"""
    world = sqlite3.connect(env["world_db"])
    try:
        return dict(world.execute("SELECT ITEM_ID, SHORT_ID FROM item").fetchall())
    finally:
        world.close()


def _add_other_items(env: Dict[str, Any], count: int) -> None:
    """取り込み先の世界に、別のペルソナの所持品を count 個置く (SHORT_ID は 1 から)。"""
    engine = create_engine(f"sqlite:///{Path(env['world_db']).as_posix()}")
    with Session(engine) as s:
        for i in range(count):
            s.add(Item(ITEM_ID=f"other-{i}", NAME=f"他の人の物 {i}", TYPE="object"))
        s.commit()
    engine.dispose()


def _tags_and_clip(env: Dict[str, Any], clip_id: str) -> Tuple[Dict[str, Tuple[str, str]], Any]:
    conn = _mem(env)
    try:
        tags = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT tag_id, target_ref, purpose_ref FROM purpose_tags"
        )}
        clip = conn.execute("SELECT pasted_to FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()
    finally:
        conn.close()
    return tags, clip


@pytest.fixture()
def item_refs(source: Dict[str, Any], tmp_path: Path) -> Dict[str, Any]:
    """所持品を指す目的タグと切り抜きを持つパック。"""
    numbers = _item_numbers(source)
    doc_n, bag_n, stone_n = numbers["item-doc"], numbers["item-bag"], numbers["item-stone"]
    conn = _mem(source)
    try:
        clip = add_clip(conn, message_id="m2", quote="B の返事", pasted_to=f"item:{bag_n}")
        conn.executemany(
            "INSERT INTO purpose_tags(tag_id, target_ref, purpose_ref, layer, created_at) VALUES (?,?,?,?,?)",
            [
                ("tag-doc", f"item:{doc_n}", "task:1", 2, T0),
                ("tag-stone-uri", f"saiverse://item/{stone_n}", "task:2", 2, T0),
                # パックに無い所持品の番号は書き換えない
                ("tag-unknown", "item:99", "task:3", 2, T0),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "item_refs.zip")
    return {"pack": pack, "clip": clip, "doc": doc_n, "bag": bag_n, "stone": stone_n}


def test_item_references_follow_renumbered_items_on_new_import(
    item_refs: Dict[str, Any], tmp_path: Path
) -> None:
    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    # 取り込み先の世界には、ZIP の所持品と同じ番号の別の物がある
    _add_other_items(target, 3)
    assert sorted(_item_numbers(target).values()) == [1, 2, 3]
    result = _import(target, item_refs["pack"], "new")

    numbers = _item_numbers(target)
    tid = target["persona_id"]
    doc_new = numbers[_derived(tid, "item", "item-doc")]
    bag_new = numbers[_derived(tid, "item", "item-bag")]
    stone_new = numbers[_derived(tid, "item", "item-stone")]
    assert {doc_new, bag_new, stone_new}.isdisjoint({1, 2, 3})
    assert result.renumbered["items.short_id"] == 3
    tags, clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    assert tags["tag-doc"] == (f"item:{doc_new}", "task:1")
    assert tags["tag-stone-uri"] == (f"saiverse://item/{stone_new}", "task:2")
    assert tags["tag-unknown"] == ("item:99", "task:3")
    assert clip == (f"item:{bag_new}",)


def test_item_references_are_not_kept_on_merge_unless_the_item_is_in_the_target(
    item_refs: Dict[str, Any], source: Dict[str, Any], tmp_path: Path
) -> None:
    # 合流は所持品を取り込まない: 取り込み先の同じ番号の別の物に結び付けない
    target = _make_env(tmp_path / "dst", PID)
    _add_other_items(target, 3)
    result = _import(target, item_refs["pack"], "merge")
    tags, clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    assert "tag-doc" not in tags and "tag-stone-uri" not in tags
    assert tags["tag-unknown"] == ("item:99", "task:3")
    assert clip == (None,)
    assert any(w.startswith("purpose_tags:") and "not imported" in w for w in result.warnings)
    assert any(w.startswith("clips:") and "set to None" in w for w in result.warnings)

    # 以前の新規取り込みで同じ所持品を登録済みのペルソナへの合流は、その物の番号へ書き換える
    home = _make_env(tmp_path / "home2", "returner_city_a", name="帰還")
    _add_other_items(home, 3)
    _import(home, source["zip"], "new")
    numbers = _item_numbers(home)
    doc_there = numbers[_derived("returner_city_a", "item", "item-doc")]
    stone_there = numbers[_derived("returner_city_a", "item", "item-stone")]
    _import(home, item_refs["pack"], "merge")
    tags, _clip = _tags_and_clip(home, item_refs["clip"].clip_id)
    assert tags["tag-doc"] == (f"item:{doc_there}", "task:1")
    assert tags["tag-stone-uri"] == (f"saiverse://item/{stone_there}", "task:2")


def _fail_world_commit(self: Any, world: sqlite3.Connection) -> None:
    raise sqlite3.OperationalError("disk I/O error (injected)")


def _add_items_meanwhile(env: Dict[str, Any], *short_ids: int) -> None:
    """止まっている間に、取り込み先の世界に別の物を SHORT_ID を指定して作る。"""
    world = sqlite3.connect(env["world_db"])
    try:
        for n in short_ids:
            world.execute(
                "INSERT INTO item (ITEM_ID, SHORT_ID, NAME, TYPE, DESCRIPTION) VALUES (?, ?, ?, ?, ?)",
                (f"made-meanwhile-{n}", n, "その間に作られた物", "object", ""),
            )
        world.commit()
    finally:
        world.close()


def _stop_new_import_after_memory_commit(
    target: Dict[str, Any], pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新規取り込みを、memory.db の commit の後・saiverse.db の commit で失敗させる。"""
    from saiverse_memory.persona_pack import importer

    with monkeypatch.context() as m:
        m.setattr(importer._ImportJob, "commit_world", _fail_world_commit)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            _import(target, pack, "new")


def test_resume_updates_item_references_when_the_recorded_numbers_were_taken(
    item_refs: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    _add_other_items(target, 3)
    _stop_new_import_after_memory_commit(target, item_refs["pack"], monkeypatch)
    tags, clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    # 止まった回が決めた番号: ノート 4・鞄 5・小石 6 (memory.db 側はこの番号で確定済み)
    recorded = json.loads(marker.read_text(encoding="utf-8"))["item_short_ids"]
    assert sorted(recorded.values()) == [4, 5, 6]
    assert tags["tag-doc"][0] == f"item:{recorded['item-doc']}"
    assert clip == (f"item:{recorded['item-bag']}",)

    # 止まっている間に世界で新しい物が作られ、ノートと鞄の番号を取る
    _add_items_meanwhile(target, recorded["item-doc"], recorded["item-bag"])

    result = _import(target, item_refs["pack"], "merge")
    numbers = _item_numbers(target)
    tid = target["persona_id"]
    doc_new = numbers[_derived(tid, "item", "item-doc")]
    bag_new = numbers[_derived(tid, "item", "item-bag")]
    stone_new = numbers[_derived(tid, "item", "item-stone")]
    # 取られた番号は使えないので別の番号で登録し、空いている番号は止まった回の番号のまま
    assert doc_new not in (1, 2, 3, 4, 5, 6) and bag_new not in (1, 2, 3, 4, 5, 6)
    assert stone_new == recorded["item-stone"]
    # 止まった回が確定させた目的タグと切り抜きは、登録した番号を指す
    tags, clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    assert tags["tag-doc"] == (f"item:{doc_new}", "task:1")
    assert tags["tag-stone-uri"] == (f"saiverse://item/{stone_new}", "task:2")
    assert tags["tag-unknown"] == ("item:99", "task:3")
    assert clip == (f"item:{bag_new}",)
    assert not marker.exists()
    assert any("updated to the registered numbers" in w for w in result.warnings)


def test_item_reference_repair_that_fails_is_finished_by_the_next_merge(
    item_refs: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    _add_other_items(target, 3)
    _stop_new_import_after_memory_commit(target, item_refs["pack"], monkeypatch)
    recorded = json.loads(marker.read_text(encoding="utf-8"))["item_short_ids"]
    _add_items_meanwhile(target, recorded["item-doc"])

    # saiverse.db の commit の後、参照の修復の commit の前に止まる
    original_repair = importer._ImportJob.repair_resumed_item_refs

    def stop_before_repair(self: Any, memory: sqlite3.Connection, world: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error in the repair (injected)")

    with monkeypatch.context() as m:
        m.setattr(importer._ImportJob, "repair_resumed_item_refs", stop_before_repair)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            _import(target, item_refs["pack"], "merge")
    tid = target["persona_id"]
    doc_new = _item_numbers(target)[_derived(tid, "item", "item-doc")]
    assert doc_new != recorded["item-doc"]
    # 所持品は登録済みだが参照はまだ旧番号で、印は残る
    assert marker.is_file()
    tags, _clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    assert tags["tag-doc"][0] == f"item:{recorded['item-doc']}"

    # 次の合流は世界側をやり直さず、参照を修復してから印を消す。修復の commit の後、
    # 印を消す前に止まった場合を作るため、一度目は印を消さない
    calls: List[int] = []

    def repair_and_count(self: Any, memory: sqlite3.Connection, world: sqlite3.Connection) -> None:
        calls.append(1)
        original_repair(self, memory, world)

    with monkeypatch.context() as m:
        m.setattr(importer._ImportJob, "repair_resumed_item_refs", repair_and_count)
        m.setattr(importer._ImportJob, "remove_pending_marker", lambda self: None)
        _import(target, item_refs["pack"], "merge")
    assert calls == [1]
    assert _item_numbers(target)[_derived(tid, "item", "item-doc")] == doc_new
    tags, _clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    assert tags["tag-doc"] == (f"item:{doc_new}", "task:1")
    assert marker.is_file()

    # 修復をもう一度行っても結果は変わらない
    before = _dump(target)
    _import(target, item_refs["pack"], "merge")
    assert not marker.exists()
    after = _dump(target)
    assert {k: v for k, v in after.items() if k != "files"} == {
        k: v for k, v in before.items() if k != "files"
    }


def test_resume_that_also_redoes_the_memory_part_records_its_item_numbers_in_the_marker(
    item_refs: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    marker = target["persona_dir"] / "persona_pack_world_pending.json"
    _add_other_items(target, 3)
    original_write = importer._ImportJob.write_files

    def write_then_stop(self: Any) -> None:
        original_write(self)
        raise SystemExit("process stopped before the memory.db COMMIT (injected)")

    # 印だけが残る (memory.db も saiverse.db も確定していない)
    with monkeypatch.context() as m:
        m.setattr(importer._ImportJob, "write_files", write_then_stop)
        m.setattr(importer._ImportJob, "remove_created_files", lambda self: None)
        with pytest.raises(SystemExit, match="injected"):
            _import(target, item_refs["pack"], "new")
    recorded = json.loads(marker.read_text(encoding="utf-8"))["item_short_ids"]
    _add_items_meanwhile(target, recorded["item-doc"])

    # 再開の合流が memory.db 側を別の番号で確定させた後、saiverse.db の commit で失敗する
    with monkeypatch.context() as m:
        m.setattr(importer._ImportJob, "commit_world", _fail_world_commit)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            _import(target, item_refs["pack"], "merge")
    tags, _clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    second = json.loads(marker.read_text(encoding="utf-8"))["item_short_ids"]
    # 印は memory.db 側の参照が使う番号に書き換わっている
    assert second["item-doc"] != recorded["item-doc"]
    assert tags["tag-doc"][0] == f"item:{second['item-doc']}"

    # その番号もまた取られた後の再開: 参照は登録した番号を指す
    _add_items_meanwhile(target, second["item-doc"])
    _import(target, item_refs["pack"], "merge")
    tid = target["persona_id"]
    doc_new = _item_numbers(target)[_derived(tid, "item", "item-doc")]
    assert doc_new not in (recorded["item-doc"], second["item-doc"])
    tags, _clip = _tags_and_clip(target, item_refs["clip"].clip_id)
    assert tags["tag-doc"] == (f"item:{doc_new}", "task:1")
    assert not marker.exists()


def test_items_changed_between_the_plan_and_the_registration_roll_back(
    item_refs: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saiverse_memory.persona_pack import importer

    target = _make_env(tmp_path / "dst", "newcomer_city_a", name="新入り")
    original = importer._ImportJob.stage_task_book

    def add_item_first(self: Any, world: sqlite3.Connection) -> None:
        # 計画の後、登録の前に取り込み先の item が増える
        world.execute(
            "INSERT INTO item (ITEM_ID, SHORT_ID, NAME, TYPE, DESCRIPTION) VALUES (?, ?, ?, ?, ?)",
            ("sneaked-in", 50, "割り込んだ物", "object", ""),
        )
        original(self, world)

    before = _dump(target)
    monkeypatch.setattr(importer._ImportJob, "stage_task_book", add_item_first)
    with pytest.raises(RuntimeError, match="items in saiverse.db changed"):
        _import(target, item_refs["pack"], "new")
    assert _dump(target) == before


# ---------------------------------------------------------------------------
# 削除は伝わらない: ZIP の削除済みの行で取り込み先の生きているページを上書きしない
# ---------------------------------------------------------------------------

def _page_row(env: Dict[str, Any], page_id: str) -> Any:
    conn = _mem(env)
    try:
        return conn.execute(
            "SELECT content, is_deleted, metadata FROM memopedia_pages WHERE id = ?", (page_id,)
        ).fetchone()
    finally:
        conn.close()


def test_page_edited_then_deleted_in_the_pack_does_not_delete_the_target_page(
    source: Dict[str, Any], tmp_path: Path
) -> None:
    from sai_memory.core_memory import remove_core_memory

    apple = source["ids"]["apple"]
    core_id = source["ids"]["core_id"]
    target = _make_env(tmp_path / "dst", PID)
    _import(target, source["zip"], "new")
    conn = _mem(target)
    try:
        (core_page,) = conn.execute(
            "SELECT id FROM memopedia_pages WHERE category = 'core' "
            "AND json_extract(metadata, '$.core_id') = ?", (core_id,)
        ).fetchone()
        # 取り込み先ではコア記憶をごみ箱へ移す
        assert remove_core_memory(conn, core_id)
    finally:
        conn.close()
    later = int(time.time()) + 1000
    conn = _mem(source)
    try:
        # 元の環境: りんごを編集してから削除、新しいページを作って削除、コア記憶を編集
        conn.execute("UPDATE memopedia_pages SET content = ?, updated_at = ? WHERE id = ?",
                     ("編集した本文", later, apple))
        conn.execute("UPDATE memopedia_pages SET is_deleted = 1, updated_at = ? WHERE id = ?",
                     (later + 1, apple))
        gone = create_page(conn, parent_id="root_terms", title="消した記事", content="消した",
                           category="terms")
        conn.execute("UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?", (gone.id,))
        conn.execute("UPDATE memopedia_pages SET content = ?, updated_at = ? WHERE id = ?",
                     ("元の環境で書き直した大事なこと", later, core_page))
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "deleted.zip")

    result = _import(target, pack, "merge")
    content, is_deleted, _meta = _page_row(target, apple)
    assert (content, is_deleted) == ("赤い果物", 0)
    assert _page_row(target, gone.id) is None
    assert any("deleted page(s) not imported" in w for w in result.warnings)
    # 取り込み先で削除済みの行は、本文が上書きされても削除済みのまま (deleted_at も残る)
    content, is_deleted, meta = _page_row(target, core_page)
    assert (content, is_deleted) == ("元の環境で書き直した大事なこと", 1)
    assert json.loads(meta)["deleted_at"] is not None


def test_new_import_keeps_deleted_pages_as_history(source: Dict[str, Any], tmp_path: Path) -> None:
    conn = _mem(source)
    try:
        gone = create_page(conn, parent_id="root_terms", title="消した記事", content="消した",
                           category="terms")
        conn.execute("UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?", (gone.id,))
        conn.commit()
    finally:
        conn.close()
    pack = _export(source, tmp_path / "out" / "with_deleted.zip")
    target = _make_env(tmp_path / "dst", PID)
    _import(target, pack, "new")
    content, is_deleted, _meta = _page_row(target, gone.id)
    assert (content, is_deleted) == ("消した", 1)
