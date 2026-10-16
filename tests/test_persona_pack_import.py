"""ペルソナまるごとパック (persona-pack) の取り込み (新規作成と合流) のテスト。

tmp_path に隔離した二つの環境 (SAIVERSE_HOME 相当のルート・saiverse.db・
memory.db) を作り、片方で書き出した ZIP をもう片方へ取り込んで検証する。
正典: docs/intent/persona_pack.md §5。
"""
from __future__ import annotations

import copy
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database.models import AI, Base, Building, TaskBookEntry
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
    engine = create_engine(f"sqlite:///{Path(env['world_db']).as_posix()}")
    with Session(engine) as s:
        s.add(TaskBookEntry(TASK_ID="tb-1", PERSONA_ID=pid, CONTENT="明日また話す",
                            ORIGIN="conversation", CREATED_AT=T0, DUE_AT=T0 + 86400))
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
    assert (target["persona_dir"] / "imported_items" / "private_room.json").is_file()


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

        # Chronicle は合流では取り込まない (arasuji_levels.md §7)
        assert conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE category = 'chronicle' AND content = ?",
            ("A での続きの話",),
        ).fetchone()[0] == 0
        assert result.counts["chronicle_pages"].skipped == 2
        assert result.counts["chronicle_pages"].added == 0

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
        assert world.execute("SELECT PERSONA_ID FROM task_book WHERE TASK_ID = 'tb-1'").fetchone()[0] == other
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
