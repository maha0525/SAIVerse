"""ペルソナまるごとパック (persona-pack) の書き出しのテスト。

合成の saiverse.db (ai 行など) と memory.db (threads / messages / memopedia /
clips など) と添付ファイルを tmp_path に作り、書き出した ZIP を検証する。
正典: docs/intent/persona_pack.md。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database.models import (
    AI,
    AddonPersonaConfig,
    Base,
    Building,
    BuildingMessage,
    Item,
    ItemLocation,
    PersonaTask,
    TaskBookEntry,
)
from sai_memory.clips import init_clips_tables
from sai_memory.desk import init_desk_tables
from sai_memory.memopedia.storage import init_memopedia_tables
from sai_memory.memory.storage import init_db
from sai_memory.perception_buffer import init_perception_buffer_table
from sai_memory.purpose_tags import init_purpose_tags_tables
from saiverse_memory.persona_pack import (
    FORMAT_NAME,
    FORMAT_VERSION,
    MANIFEST_NAME,
    decode_value,
    export_persona_pack,
)
from tools.utilities.chatgpt_importer import ChatGPTExport

PERSONA_ID = "tester_city_a"
ROOM_ID = "tester_room_city_a"
SECRET = "sk-test-secret-should-never-leave"
SIG_A = b"\x00\x01\x02\xffsignature-a\x80"
SIG_B = bytes(range(256))
T0 = 1_760_000_000  # 同じ秒に複数スレッドへ書かれた記録の時刻


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_world(tmp_path: Path) -> Dict[str, Path]:
    home = tmp_path / "home"
    (home / "image").mkdir(parents=True)
    (home / "user_data" / "icons").mkdir(parents=True)
    (home / "user_data" / "database").mkdir(parents=True)
    persona_dir = home / "personas" / PERSONA_ID
    persona_dir.mkdir(parents=True)

    # 共有メディア (添付) とアイコン
    (home / "image" / "pic.png").write_bytes(b"\x89PNG fake image")
    (home / "image" / "pic.png.summary.txt").write_text("絵の要約", encoding="utf-8")
    icon = home / "user_data" / "icons" / "tester_icon.webp"
    icon.write_bytes(b"RIFF fake webp")
    (home / "documents").mkdir()
    (home / "documents" / "note.txt").write_text("持ち物の文書", encoding="utf-8")

    # ペルソナフォルダのファイル
    (persona_dir / "active_state.json").write_text('{"thread": "a"}', encoding="utf-8")
    (persona_dir / "log.json").write_text("[]", encoding="utf-8")
    (persona_dir / "voice_calls" / "20261001_120000").mkdir(parents=True)
    (persona_dir / "voice_calls" / "20261001_120000" / "001_user.wav").write_bytes(b"RIFFwav")
    (persona_dir / "day_reports").mkdir()
    (persona_dir / "day_reports" / "2026-10-01.md").write_text("# 一日新聞", encoding="utf-8")

    # saiverse.db (本物のスキーマ)
    world_db = home / "user_data" / "database" / "saiverse.db"
    engine = create_engine(f"sqlite:///{world_db.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(AI(
            AIID=PERSONA_ID, HOME_CITYID=1, AINAME="テスト子",
            SYSTEMPROMPT="あなたはテスト子です。", DESCRIPTION="説明",
            AVATAR_IMAGE=str(icon), DEFAULT_MODEL="gemini-3-flash",
            PRIVATE_ROOM_ID=ROOM_ID,
        ))
        s.add(Building(
            CITYID=1, BUILDINGID=ROOM_ID, BUILDINGNAME="テスト子の部屋",
        ))
        s.add(Item(ITEM_ID="item-doc", NAME="ノート", TYPE="document",
                   FILE_PATH="documents/note.txt"))
        s.add(Item(ITEM_ID="item-bag", NAME="鞄", TYPE="bag"))
        s.add(Item(ITEM_ID="item-in-bag", NAME="小石", TYPE="object"))
        s.add(Item(ITEM_ID="item-elsewhere", NAME="他人の物", TYPE="object"))
        s.add(ItemLocation(ITEM_ID="item-doc", OWNER_KIND="building", OWNER_ID=ROOM_ID, SLOT_NUMBER=1))
        s.add(ItemLocation(ITEM_ID="item-bag", OWNER_KIND="persona", OWNER_ID=PERSONA_ID, SLOT_NUMBER=1))
        s.add(ItemLocation(ITEM_ID="item-in-bag", OWNER_KIND="bag", OWNER_ID="item-bag", SLOT_NUMBER=1))
        s.add(ItemLocation(ITEM_ID="item-elsewhere", OWNER_KIND="building", OWNER_ID="plaza", SLOT_NUMBER=1))
        s.add(PersonaTask(id="task-1", persona_id=PERSONA_ID, title="やること"))
        s.add(TaskBookEntry(
            TASK_ID="tb-1", PERSONA_ID=PERSONA_ID, CONTENT="明日また話す",
            ORIGIN="conversation", CREATED_AT=T0, DUE_AT=T0 + 86400,
        ))
        s.add(BuildingMessage(
            building_id=ROOM_ID, seq=1, role="user", content="建物の会話は運ばない",
            timestamp="2026-10-01T00:00:00",
        ))
        s.add(AddonPersonaConfig(
            addon_name="x", persona_id=PERSONA_ID,
            params_json=json.dumps({"api_key": SECRET}),
        ))
        s.commit()
    engine.dispose()

    # memory.db (本物のスキーマ)
    memory_db = persona_dir / "memory.db"
    conn = init_db(str(memory_db))
    init_memopedia_tables(conn)
    init_clips_tables(conn)
    init_purpose_tags_tables(conn)
    init_desk_tables(conn)
    init_perception_buffer_table(conn)
    conn.execute(
        "INSERT INTO threads(id, resource_id, overview, overview_updated_at, title) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"{PERSONA_ID}:room_a", PERSONA_ID, "概要", T0, "部屋 A の会話"),
    )
    conn.execute(
        "INSERT INTO threads(id, resource_id) VALUES (?, ?)",
        (f"{PERSONA_ID}:room_b", PERSONA_ID),
    )

    def add(mid: str, thread: str, role: str, content: str, created: int,
            metadata: Dict[str, Any], **cols: Any) -> None:
        names = ["id", "thread_id", "role", "content", "resource_id", "created_at", "metadata"]
        values: List[Any] = [mid, f"{PERSONA_ID}:{thread}", role, content, PERSONA_ID,
                             created, json.dumps(metadata, ensure_ascii=False)]
        for k, v in cols.items():
            names.append(k)
            values.append(v)
        conn.execute(
            f"INSERT INTO messages({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            values,
        )

    # 同じ秒 (T0) に二つのスレッドへ交互に書かれた 3 行
    add("m1", "room_a", "user", "こんにちは", T0,
        {"tags": ["conversation"],
         "images": [{"uri": "saiverse://image/pic.png", "mime_type": "image/png"}]})
    add("m2", "room_b", "assistant", "B の返事", T0, {"tags": ["conversation"]},
        thought_signature=SIG_B, pulse_id="pulse-1")
    add("m3", "room_a", "assistant", "  A の返事（前後の空白も本文）  ", T0,
        {"tags": ["conversation"]}, thought_signature=SIG_A, line_role="main_line")
    # 会話として写さない行
    add("m4", "room_a", "assistant", "内心のつぶやき", T0 + 1, {"tags": ["internal"]})
    add("m5", "room_a", "assistant", "一時的な行", T0 + 2, {"tags": ["conversation"]},
        scope="volatile")
    add("m6", "room_a", "user", "見つからない添付", T0 + 3,
        {"tags": ["conversation"],
         "media": [{"type": "audio", "uri": "saiverse://audio/gone.ogg"}]})

    conn.execute(
        "INSERT INTO memopedia_pages(id, parent_id, title, summary, content, category, "
        "created_at, updated_at, keywords, metadata) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("page-core-1", None, "忘れないこと", "", "本文", "core", T0, T0 + 5,
         '["a"]', json.dumps({"core_id": 1})),
    )
    conn.execute(
        "INSERT INTO clips(clip_id, message_id, quote, created_at) VALUES (?,?,?,?)",
        ("clip-1", "m1", "こんにちは", T0),
    )
    conn.execute(
        "INSERT INTO desk_items(ref, opened_at, last_touched_at) VALUES (?,?,?)",
        ("m:1", T0, T0),
    )
    conn.execute(
        "INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES (?,?,?)",
        ("m1", 0, "[0.1, 0.2]"),
    )
    conn.commit()
    conn.close()
    return {"home": home, "world_db": world_db, "memory_db": memory_db}


@pytest.fixture()
def exported(tmp_path: Path) -> Dict[str, Any]:
    paths = _build_world(tmp_path)
    before = {k: _sha(paths[k]) for k in ("world_db", "memory_db")}
    out = tmp_path / "out" / "tester.zip"
    result = export_persona_pack(
        PERSONA_ID,
        saiverse_db_path=paths["world_db"],
        saiverse_home=paths["home"],
        output_path=out,
        generator_version="9.9.9",
        now=datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
    )
    after = {k: _sha(paths[k]) for k in ("world_db", "memory_db")}
    return {"out": out, "result": result, "paths": paths, "before": before, "after": after}


def _read_json(zf: zipfile.ZipFile, name: str) -> Any:
    return json.loads(zf.read(name).decode("utf-8"))


def _folders(names: List[str]) -> set:
    folders = set()
    for name in names:
        parts = name.split("/")
        if len(parts) == 1:
            continue
        folders.add("/".join(parts[:2]) if parts[0] == "extensions" else parts[0])
    return folders


def test_manifest_contents_match_folders(exported: Dict[str, Any]) -> None:
    with zipfile.ZipFile(exported["out"]) as zf:
        manifest = _read_json(zf, MANIFEST_NAME)
        assert manifest["format"] == FORMAT_NAME
        assert manifest["version"] == FORMAT_VERSION
        assert manifest["generator"] == {"name": "SAIVerse", "version": "9.9.9"}
        assert manifest["persona_id"] == PERSONA_ID
        assert manifest["persona_name"] == "テスト子"
        assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None
        assert set(manifest["contents"]) == _folders(zf.namelist())
        assert manifest["contents"] == [
            "profile", "chatlog", "knowledge", "attachments", "items", "journal",
            "extensions/saiverse", "chatgpt_compat",
        ]
    assert exported["result"].contents == manifest["contents"]


def test_export_is_read_only(exported: Dict[str, Any]) -> None:
    assert exported["before"] == exported["after"]


def test_bytes_round_trip_through_base64(exported: Dict[str, Any]) -> None:
    with zipfile.ZipFile(exported["out"]) as zf:
        messages = {m["id"]: m for m in _read_json(zf, "chatlog/messages.json")}
    assert messages["m2"]["thought_signature"].keys() == {"__bytes_b64__"}
    assert decode_value(messages["m2"]["thought_signature"]) == SIG_B
    assert decode_value(messages["m3"]["thought_signature"]) == SIG_A
    assert messages["m1"]["thought_signature"] is None


def test_messages_keep_all_columns_and_same_second_order(exported: Dict[str, Any]) -> None:
    paths = exported["paths"]
    conn = sqlite3.connect(paths["memory_db"])
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        rowids = dict(conn.execute("SELECT id, rowid FROM messages"))
    finally:
        conn.close()
    with zipfile.ZipFile(exported["out"]) as zf:
        messages = _read_json(zf, "chatlog/messages.json")
    for m in messages:
        assert set(columns) <= set(m), f"columns missing from {m['id']}"
        assert m["seq"] == rowids[m["id"]]
    # 同じ秒に二つのスレッドへ交互に書かれた 3 行が、元の挿入順 (seq) のまま並ぶ
    assert [m["id"] for m in messages[:3]] == ["m1", "m2", "m3"]
    assert [m["seq"] for m in messages] == sorted(m["seq"] for m in messages)
    assert messages[0]["created_at"] == messages[1]["created_at"] == messages[2]["created_at"]
    assert datetime.fromisoformat(messages[0]["created_at"]).timestamp() == T0
    by_id = {m["id"]: m for m in messages}
    assert by_id["m2"]["pulse_id"] == "pulse-1"
    assert by_id["m3"]["line_role"] == "main_line"
    assert by_id["m5"]["scope"] == "volatile"
    assert by_id["m3"]["content"] == "  A の返事（前後の空白も本文）  "
    assert by_id["m1"]["metadata"]["images"][0]["uri"] == "saiverse://image/pic.png"


def test_chatgpt_compat_reads_with_existing_importer(exported: Dict[str, Any]) -> None:
    # 既存の取り込み口は ZIP 内の conversations.json をそのまま探して読む
    export = ChatGPTExport(exported["out"])
    by_id = {c.identifier: c for c in export.conversations}
    assert set(by_id) == {f"{PERSONA_ID}:room_a", f"{PERSONA_ID}:room_b"}
    room_a = by_id[f"{PERSONA_ID}:room_a"]
    assert room_a.title == "部屋 A の会話"
    assert [(m.role, m.content) for m in room_a.messages] == [
        ("user", "こんにちは"),
        ("assistant", "A の返事（前後の空白も本文）"),  # 取り込み口側が strip する
        ("user", "見つからない添付"),
    ]
    room_b = by_id[f"{PERSONA_ID}:room_b"]
    assert room_b.title == "room_b"
    assert [(m.role, m.content) for m in room_b.messages] == [("assistant", "B の返事")]
    assert room_a.messages[0].create_time.timestamp() == T0


def test_attachments_profile_items_journal(exported: Dict[str, Any]) -> None:
    with zipfile.ZipFile(exported["out"]) as zf:
        names = set(zf.namelist())
        index = _read_json(zf, "attachments/index.json")
        by_ref = {e["ref"]: e for e in index}
        pic = by_ref["saiverse://image/pic.png"]
        assert pic["path"] == "attachments/image/pic.png"
        assert zf.read(pic["path"]) == b"\x89PNG fake image"
        assert pic["summary_path"] == "attachments/image/pic.png.summary.txt"
        assert pic["message_ids"] == ["m1"]
        assert by_ref["saiverse://audio/gone.ogg"]["path"] is None

        profile = _read_json(zf, "profile/profile.json")
        assert profile["ai"]["AINAME"] == "テスト子"
        assert profile["ai"]["DEFAULT_MODEL"] == "gemini-3-flash"
        assert zf.read(profile["files"]["AVATAR_IMAGE"]) == b"RIFF fake webp"

        items = _read_json(zf, "items/items.json")
        assert {i["ITEM_ID"] for i in items["items"]} == {"item-doc", "item-bag", "item-in-bag"}
        assert zf.read(items["files"]["item-doc"]).decode("utf-8") == "持ち物の文書"
        room = _read_json(zf, "items/private_room.json")
        assert room["building"]["BUILDINGID"] == ROOM_ID

        task_book = _read_json(zf, "journal/task_book.json")
        assert task_book[0]["CONTENT"] == "明日また話す"
        assert datetime.fromisoformat(task_book[0]["CREATED_AT"]).timestamp() == T0
        assert "journal/day_reports/2026-10-01.md" in names
        assert _read_json(zf, "journal/clips.json")[0]["clip_id"] == "clip-1"
        pages = {p["id"]: p for p in _read_json(zf, "knowledge/pages.json")}
        assert pages["page-core-1"]["metadata"] == {"core_id": 1}
        assert pages["page-core-1"]["keywords"] == ["a"]

        assert "extensions/saiverse/memory_db/desk_items.json" in names
        assert "extensions/saiverse/saiverse_db/persona_task.json" in names
        assert "extensions/saiverse/persona_dir/active_state.json" in names
        assert "extensions/saiverse/persona_dir/voice_calls/20261001_120000/001_user.wav" in names


def test_not_carried_data_is_absent(exported: Dict[str, Any]) -> None:
    with zipfile.ZipFile(exported["out"]) as zf:
        names = zf.namelist()
        blob = b"".join(zf.read(n) for n in names)
    # 認証情報 (アドオンのペルソナ別設定) は決して入らない
    assert SECRET.encode() not in blob
    # 建物の会話履歴・embeddings・memory.db 本体は入らない
    assert "建物の会話は運ばない".encode() not in blob
    assert not any("message_embeddings" in n for n in names)
    assert not any(n.endswith("memory.db") for n in names)
    not_carried = set(exported["result"].not_carried_tables)
    assert {"message_embeddings", "embeddings"} <= not_carried
    # 知覚台帳 (perception_*) は永続の証跡なので extensions/ で運ぶ
    assert "extensions/saiverse/memory_db/perception_buffer.json" in names
    assert "perception_buffer" not in not_carried


# ---------------------------------------------------------------------------
# SAIVERSE_HOME の外のファイル・symlink・所持品の欠けの記録
# ---------------------------------------------------------------------------

def _export_world(paths: Dict[str, Path], out: Path):
    return export_persona_pack(
        PERSONA_ID,
        saiverse_db_path=paths["world_db"],
        saiverse_home=paths["home"],
        output_path=out,
        generator_version="9.9.9",
        now=datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
    )


def test_files_outside_home_are_not_packed(tmp_path: Path) -> None:
    paths = _build_world(tmp_path)
    secret = b"outside-home secret bytes"
    # 共有メディアフォルダと同じ名前のフォルダでも、SAIVERSE_HOME の外なら入れない
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "image").mkdir(parents=True)
    outside_image = elsewhere / "image" / "secret.png"
    outside_image.write_bytes(secret)
    outside_doc = elsewhere / "secret.txt"
    outside_doc.write_bytes(secret)

    world = sqlite3.connect(paths["world_db"])
    world.execute("UPDATE building SET IMAGE_PATH = ? WHERE BUILDINGID = ?", (str(outside_image), ROOM_ID))
    world.execute("UPDATE item SET FILE_PATH = ? WHERE ITEM_ID = 'item-doc'", (str(outside_doc),))
    world.execute("UPDATE ai SET APPEARANCE_IMAGE_PATH = ? WHERE AIID = ?", (str(outside_image), PERSONA_ID))
    world.commit()
    world.close()
    memory = sqlite3.connect(paths["memory_db"])
    memory.execute(
        "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("m-out", f"{PERSONA_ID}:room_a", "user", "外の絵", PERSONA_ID, T0 + 10,
         json.dumps({"images": [{"path": str(outside_image), "mime_type": "image/png"}]})),
    )
    # 相対パスで SAIVERSE_HOME の外へ出るもの
    memory.execute(
        "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("m-rel", f"{PERSONA_ID}:room_a", "user", "外の文書", PERSONA_ID, T0 + 11,
         json.dumps({"documents": [{"path": "../elsewhere/secret.txt"}]})),
    )
    memory.commit()
    memory.close()

    out = tmp_path / "out" / "outside.zip"
    result = _export_world(paths, out)
    with zipfile.ZipFile(out) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
        index = {e["ref"]: e for e in _read_json(zf, "attachments/index.json")}
        items = _read_json(zf, "items/items.json")
        room = _read_json(zf, "items/private_room.json")
    assert secret not in blob
    assert index[str(outside_image)]["path"] is None
    assert index["../elsewhere/secret.txt"]["path"] is None
    assert "item-doc" not in items["files"]
    assert room["files"]["IMAGE_PATH"] is None

    wheres = {(m["ref"], m["where"]) for m in result.missing_files}
    assert (str(outside_image), "messages.metadata (outside SAIVERSE_HOME)") in wheres
    assert ("../elsewhere/secret.txt", "messages.metadata (outside SAIVERSE_HOME)") in wheres
    assert (str(outside_image), "building.IMAGE_PATH (outside SAIVERSE_HOME)") in wheres
    assert (str(outside_doc), "item.FILE_PATH (outside SAIVERSE_HOME)") in wheres
    assert (str(outside_image), "ai.APPEARANCE_IMAGE_PATH (outside SAIVERSE_HOME)") in wheres


def test_files_inside_home_but_outside_their_folders_are_not_packed(tmp_path: Path) -> None:
    """本体 DB や他のペルソナのフォルダは SAIVERSE_HOME の内側でも入れない。"""
    paths = _build_world(tmp_path)
    home = paths["home"]
    world_db = paths["world_db"]  # home/user_data/database/saiverse.db (認証情報入り)
    other = home / "personas" / "someone_else"
    other.mkdir(parents=True)
    other_file = other / "secret.png"
    other_file.write_bytes(b"another persona's bytes")

    world = sqlite3.connect(world_db)
    world.execute("UPDATE building SET IMAGE_PATH = ? WHERE BUILDINGID = ?", (str(other_file), ROOM_ID))
    world.execute("UPDATE item SET FILE_PATH = ? WHERE ITEM_ID = 'item-doc'", ("user_data/database/saiverse.db",))
    # アイコンの置き場は user_data/icons と image だけ (documents の画像は認めない)
    (home / "documents" / "face.png").write_bytes(b"\x89PNG in documents")
    world.execute("UPDATE ai SET APPEARANCE_IMAGE_PATH = ? WHERE AIID = ?", (str(home / "documents" / "face.png"), PERSONA_ID))
    world.commit()
    world.close()
    memory = sqlite3.connect(paths["memory_db"])
    memory.execute(
        "INSERT INTO messages(id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("m-db", f"{PERSONA_ID}:room_a", "user", "DB を指す添付", PERSONA_ID, T0 + 20,
         json.dumps({"documents": [{"path": str(world_db)}]})),
    )
    memory.commit()
    memory.close()

    out = tmp_path / "out" / "inside.zip"
    result = _export_world(paths, out)
    with zipfile.ZipFile(out) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
        index = {e["ref"]: e for e in _read_json(zf, "attachments/index.json")}
        items = _read_json(zf, "items/items.json")
        room = _read_json(zf, "items/private_room.json")
        profile = _read_json(zf, "profile/profile.json")
    assert SECRET.encode() not in blob
    assert b"SQLite format 3" not in blob
    assert b"another persona's bytes" not in blob
    assert index[str(world_db)]["path"] is None
    assert "item-doc" not in items["files"]
    assert room["files"]["IMAGE_PATH"] is None
    assert profile["files"]["APPEARANCE_IMAGE_PATH"] is None
    wheres = {(m["ref"], m["where"]) for m in result.missing_files}
    assert (str(world_db), "messages.metadata (outside the shared media folders and user_data/icons)") in wheres
    assert ("user_data/database/saiverse.db", "item.FILE_PATH (outside the shared media folders)") in wheres
    assert (str(other_file), "building.IMAGE_PATH (outside the shared media folders)") in wheres
    assert (str(home / "documents" / "face.png"), "ai.APPEARANCE_IMAGE_PATH (outside user_data/icons and image)") in wheres


def test_symlinks_in_persona_folder_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _build_world(tmp_path)
    persona_dir = paths["home"] / "personas" / PERSONA_ID
    target = tmp_path / "elsewhere.txt"
    target.write_text("symlink target outside", encoding="utf-8")
    link = persona_dir / "linked.txt"
    report_link = persona_dir / "day_reports" / "linked.md"
    try:
        link.symlink_to(target)
        report_link.symlink_to(target)
    except (OSError, NotImplementedError):
        # symlink を作れない環境 (権限の無い Windows) では、普通のファイルを
        # symlink として見せて同じ経路を通す
        link.write_text("stand-in", encoding="utf-8")
        report_link.write_text("stand-in", encoding="utf-8")
        links = {link, report_link}
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self in links or original(self))
    out = tmp_path / "out" / "links.zip"
    result = _export_world(paths, out)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "extensions/saiverse/persona_dir/linked.txt" not in names
    assert "journal/day_reports/linked.md" not in names
    wheres = {(m["ref"], m["where"]) for m in result.missing_files}
    assert (str(link), "symlink skipped") in wheres
    assert (str(report_link), "symlink skipped") in wheres


def test_items_missing_rows_and_containers_of_any_type(tmp_path: Path) -> None:
    paths = _build_world(tmp_path)
    engine = create_engine(f"sqlite:///{paths['world_db'].as_posix()}")
    with Session(engine) as s:
        # TYPE が bag でない入れ物の中身も辿る (OWNER_KIND='bag' が入れ物の印)
        s.add(Item(ITEM_ID="item-box", NAME="箱", TYPE="object"))
        s.add(Item(ITEM_ID="item-in-box", NAME="指輪", TYPE="object"))
        s.add(ItemLocation(ITEM_ID="item-box", OWNER_KIND="persona", OWNER_ID=PERSONA_ID, SLOT_NUMBER=2))
        s.add(ItemLocation(ITEM_ID="item-in-box", OWNER_KIND="bag", OWNER_ID="item-box", SLOT_NUMBER=1))
        # item 行の無い置き場所と、三つのどれでもない OWNER_KIND の行
        s.add(ItemLocation(ITEM_ID="item-ghost", OWNER_KIND="persona", OWNER_ID=PERSONA_ID, SLOT_NUMBER=3))
        s.add(ItemLocation(ITEM_ID="item-odd", OWNER_KIND="shelf", OWNER_ID=PERSONA_ID, SLOT_NUMBER=1))
        s.commit()
    engine.dispose()
    out = tmp_path / "out" / "items.zip"
    result = _export_world(paths, out)
    with zipfile.ZipFile(out) as zf:
        items = _read_json(zf, "items/items.json")
    assert {i["ITEM_ID"] for i in items["items"]} == {
        "item-doc", "item-bag", "item-in-bag", "item-box", "item-in-box",
    }
    wheres = {(m["ref"], m["where"]) for m in result.missing_files}
    assert ("item-ghost", "item (no row for item_location.ITEM_ID)") in wheres
    assert ("item-odd", "item_location.OWNER_KIND='shelf' not traversed") in wheres


def test_read_only_connection_waits_for_writers(tmp_path: Path) -> None:
    from saiverse_memory.persona_pack.export import _open_read_only

    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    conn = _open_read_only(db)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.rollback()
        conn.close()


def test_json_text_scalars_are_kept_as_raw_text() -> None:
    from saiverse_memory.persona_pack.format import RAW_TEXT_MARKER, encode_json_text

    for text in ("null", "123", " 1e2 ", '"abc"', "true", "not json"):
        assert encode_json_text(text) == {RAW_TEXT_MARKER: text}
        assert decode_value(encode_json_text(text)) == text
    assert encode_json_text('{"a": 1}') == {"a": 1}
    assert encode_json_text("[1, 2]") == [1, 2]
    assert encode_json_text(None) is None


def test_marker_shaped_user_dicts_are_escaped_and_round_trip() -> None:
    """利用者の JSON の中の、印と同じ形の dict は __escaped__ に包まれ、decode で元の dict に戻る。"""
    from saiverse_memory.persona_pack.format import ESCAPED_MARKER, encode_json_text, encode_value

    cases = [
        {"__raw_text__": "hello"},
        {"__bytes_b64__": "aGVsbG8="},
        {"__escaped__": {"__raw_text__": "x"}},
        {"__escaped__": 5},
        {"a": {"b": [{"__bytes_b64__": "aGVsbG8="}, {"__raw_text__": "deep"}]}},
        [{"__escaped__": {"__escaped__": {"__bytes_b64__": "AA=="}}}],
        # キーが二つ以上なら印の形ではないので包まない
        {"__raw_text__": "x", "other": 1},
    ]
    for original in cases:
        encoded = encode_json_text(json.dumps(original))
        # 書き出した形は JSON を通しても同じ値に戻る
        assert decode_value(json.loads(json.dumps(encoded))) == original
    assert encode_json_text('{"__raw_text__": "hello"}') == {ESCAPED_MARKER: {"__raw_text__": "hello"}}
    assert encode_json_text('{"__escaped__": 5}') == {ESCAPED_MARKER: {ESCAPED_MARKER: 5}}
    assert encode_json_text('{"__raw_text__": "x", "other": 1}') == {"__raw_text__": "x", "other": 1}
    # 本物の印は従来どおり
    assert decode_value(encode_value(b"\x00\x01")) == b"\x00\x01"
    assert encode_value(b"\x00\x01") == {"__bytes_b64__": "AAE="}
