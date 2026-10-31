"""Lite から帰るときの合流のテスト (docs/intent/persona_pack.md §5「Lite から帰るときの合流」)。

隔離した SAIVERSE_HOME と合成のペルソナだけを使う。本体が書き出したパックを、Lite が
書き出す形に手で書き換えて入れる。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_manager
from sai_memory.memory.storage import DEFERRED_VECTOR
from saiverse_memory.persona_pack import compare_persona_pack, validate_pack
from saiverse_memory.persona_pack.return_merge import (
    BACKUP_KEEP,
    CHANGES_NAME,
    KIND_ACTIVE_THREAD,
    PREVIEW_CHARS,
    ReturnMergeBackupError,
    ReturnMergeRequired,
    ReturnMergeStale,
    ReturnSelection,
    read_review_fingerprints,
    read_review_items,
    read_review_summary,
    resolve_choices,
    write_review,
)
from tests.test_persona_pack_api import FakeManager
from tests.test_persona_pack_import import (
    PID,
    T0,
    _add_message,
    _export,
    _import,
    _make_env,
    _mem,
    _populate,
    _rewrite_zip,
)


def _empty_changes() -> Dict[str, Any]:
    return {
        "format": "saiverse-lite-changes",
        "version": 1,
        "messages": {"created": [], "edited": [], "deleted": []},
        "pages": {"created": [], "edited": [], "deleted": []},
        "fragments": {"created": []},
        "profile": {"edited": []},
    }


def _lite_pack(
    src: Path,
    dst: Path,
    changes: Optional[Dict[str, Any]] = None,
    *,
    messages: Optional[Dict[str, str]] = None,
    drop_messages: Optional[List[str]] = None,
    add_messages: Optional[List[Dict[str, Any]]] = None,
    pages: Optional[Dict[str, Dict[str, Any]]] = None,
    drop_pages: Optional[List[str]] = None,
    add_pages: Optional[List[Dict[str, Any]]] = None,
    fragments: Optional[List[Dict[str, Any]]] = None,
    profile: Optional[Dict[str, Any]] = None,
    active: Optional[Dict[str, Any]] = None,
    in_contents: bool = True,
) -> Path:
    """本体のパック src を、Lite が書き出した形に書き換えて dst へ書く。"""
    with zipfile.ZipFile(src) as zf:
        doc = {
            name: json.loads(zf.read(name))
            for name in ("manifest.json", "chatlog/messages.json", "knowledge/pages.json", "profile/profile.json")
        }
    if in_contents:
        doc["manifest.json"]["contents"].append("extensions/lite")
    rows = [r for r in doc["chatlog/messages.json"] if r["id"] not in (drop_messages or [])]
    for row in rows:
        if row["id"] in (messages or {}):
            row["content"] = messages[row["id"]]
    doc["chatlog/messages.json"] = rows + (add_messages or [])
    page_rows = [p for p in doc["knowledge/pages.json"] if p["id"] not in (drop_pages or [])]
    for page in page_rows:
        page.update((pages or {}).get(page["id"], {}))
    doc["knowledge/pages.json"] = page_rows + (add_pages or [])
    doc["profile/profile.json"]["ai"].update(profile or {})
    replace = {name: json.dumps(value, ensure_ascii=False).encode("utf-8") for name, value in doc.items()}
    if active is not None:
        replace["extensions/saiverse/persona_dir/active_state.json"] = json.dumps(active).encode("utf-8")
    if fragments is not None:
        replace["extensions/saiverse/memory_db/memopedia_fragments.json"] = json.dumps(
            fragments, ensure_ascii=False,
        ).encode("utf-8")
    _rewrite_zip(src, dst, replace)
    extra = dict(replace)
    with zipfile.ZipFile(dst, "a") as zf:
        present = set(zf.namelist())
        for name, data in extra.items():
            if name not in present:
                zf.writestr(name, data)
        if changes is not None:
            zf.writestr(CHANGES_NAME, json.dumps(changes))
    return dst


def _compare(env: Dict[str, Any], zip_path: Path):
    return compare_persona_pack(
        zip_path,
        target_persona_id=env["persona_id"],
        saiverse_db_path=env["world_db"],
        saiverse_home=env["home"],
    )


def _select(items: List[Any], bulk: Optional[str] = None, **choices: str) -> ReturnSelection:
    expected = {item.key: item.fingerprint for item in items}
    return ReturnSelection(expected=expected, decisions=resolve_choices(expected, bulk, choices))


def _apply(env: Dict[str, Any], zip_path: Path, selection: ReturnSelection):
    return _import(env, zip_path, "merge", return_selection=selection)


def _message(env: Dict[str, Any], mid: str) -> Optional[tuple]:
    conn = _mem(env)
    try:
        return conn.execute("SELECT content, metadata FROM messages WHERE id = ?", (mid,)).fetchone()
    finally:
        conn.close()


def _vectors(env: Dict[str, Any], mid: str) -> List[str]:
    conn = _mem(env)
    try:
        return [r[0] for r in conn.execute("SELECT vector FROM message_embeddings WHERE message_id = ?", (mid,))]
    finally:
        conn.close()


def _page(env: Dict[str, Any], page_id: str) -> Optional[tuple]:
    conn = _mem(env)
    try:
        return conn.execute(
            "SELECT title, summary, content, is_deleted, metadata FROM memopedia_pages WHERE id = ?",
            (page_id,),
        ).fetchone()
    finally:
        conn.close()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def pack(tmp_path: Path) -> Dict[str, Any]:
    """本体のペルソナと、それが書き出したパック。パックの id は取り込み先と同じ。"""
    env = _make_env(tmp_path / "body", PID)
    ids = _populate(env)
    zip_path = _export(env, tmp_path / "pack.zip")
    return {"env": env, "zip": zip_path, "ids": ids, "dir": tmp_path}


# ---------------------------------------------------------------------------
# changes.json の検査
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mutate", [
    lambda c: c.update(format="other"),
    lambda c: c.update(version=2),
    lambda c: c.pop("messages"),
    lambda c: c["messages"].update(edited="m1"),
    lambda c: c["messages"]["edited"].append(1),
    lambda c: c["messages"]["edited"].append({"id": "m1", "changed_at": "yesterday"}),
    lambda c: c["profile"]["edited"].append("LANGUAGE"),
    lambda c: (c["messages"]["edited"].append("m1"), c["messages"]["deleted"].append("m1")),
    lambda c: c["messages"]["edited"].append("nobody"),
    lambda c: c["messages"]["created"].append("nobody"),
    lambda c: c["pages"]["edited"].append("nothing"),
    lambda c: c["fragments"]["created"].append("nothing"),
])
def test_broken_changes_are_refused_before_writing(pack: Dict[str, Any], mutate: Any) -> None:
    changes = _empty_changes()
    mutate(changes)
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", changes)
    before = _hash(pack["env"]["memory_db"])
    with pytest.raises(ValueError, match="changes.json"):
        validate_pack(zip_path)
    with pytest.raises(ValueError, match="changes.json"):
        _compare(pack["env"], zip_path)
    assert _hash(pack["env"]["memory_db"]) == before


def test_changes_missing_from_contents_is_refused(pack: Dict[str, Any]) -> None:
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", _empty_changes(), in_contents=False)
    with pytest.raises(ValueError, match="manifest.contents"):
        validate_pack(zip_path)


def test_contents_without_the_file_is_refused(pack: Dict[str, Any]) -> None:
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", None)
    with pytest.raises(ValueError, match="the file is missing"):
        validate_pack(zip_path)


def test_page_edited_but_deleted_in_the_pack_is_refused(pack: Dict[str, Any]) -> None:
    changes = _empty_changes()
    changes["pages"]["edited"].append(pack["ids"]["apple"])
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes, pages={pack["ids"]["apple"]: {"is_deleted": 1}},
    )
    with pytest.raises(ValueError, match="as deleted"):
        validate_pack(zip_path)


# ---------------------------------------------------------------------------
# 本体同士のパックと選択の必須
# ---------------------------------------------------------------------------

def test_pack_without_changes_is_not_compared_and_merges_as_before(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    assert _compare(env, pack["zip"]) is None
    conn = _mem(env)
    conn.execute("UPDATE messages SET content = 'body changed' WHERE id = 'm2'")
    conn.execute("DELETE FROM messages WHERE id = 'm4'")
    conn.commit()
    conn.close()
    result = _import(env, pack["zip"], "merge")
    assert result.return_merge is None
    # §5 の規則: 既にある行は変えず、無い行は足す
    assert _message(env, "m2")[0] == "body changed"
    assert _message(env, "m4")[0] == "内心"
    assert not (env["home"] / "backups" / "persona_pack_merge").exists()


def test_pack_with_changes_needs_a_selection(pack: Dict[str, Any]) -> None:
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", _empty_changes())
    with pytest.raises(ReturnMergeRequired):
        _import(pack["env"], zip_path, "merge")


def test_selection_for_a_pack_without_changes_is_refused(pack: Dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="choices"):
        _apply(pack["env"], pack["zip"], ReturnSelection(expected={}, decisions={}))


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------

def test_compare_writes_nothing_and_lists_only_real_differences(pack: Dict[str, Any]) -> None:
    env, apple = pack["env"], pack["ids"]["apple"]
    changes = _empty_changes()
    changes["messages"]["edited"] = ["m2", {"id": "m3", "changed_at": "2026-11-01T00:00:00+00:00"}]
    changes["messages"]["deleted"] = ["m4", "gone-in-both"]
    changes["pages"]["edited"] = [apple, pack["ids"]["banana"]]
    changes["profile"]["edited"] = ["name", "DESCRIPTION"]
    # m2 は Lite で編集の一覧にあるが本文は本体と同じ (一覧に出ない)
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes,
        messages={"m3": "Lite で直した"},
        drop_messages=["m4"],
        pages={apple: {"content": "Lite のりんご"}},
        profile={"AINAME": "Lite 名"},
        active={"active_thread_id": "room_b"},
    )
    memory_before = _hash(env["memory_db"])
    world_before = _hash(env["world_db"])
    items, warnings = _compare(env, zip_path)
    assert _hash(env["memory_db"]) == memory_before
    assert _hash(env["world_db"]) == world_before
    assert warnings == []
    assert [i.key for i in items] == [
        "profile:AINAME", KIND_ACTIVE_THREAD, f"page:{apple}", "message:m3", "message:m4",
    ]
    by_key = {i.key: i.payload for i in items}
    edited = by_key["message:m3"]
    assert edited["body"]["content"] == "  A の返事  "
    assert edited["lite"]["content"] == "Lite で直した"
    assert edited["lite"]["updated_at"] == 1793491200
    assert edited["body"]["updated_at"] == T0
    deleted = by_key["message:m4"]
    assert deleted["change"] == "deleted"
    assert deleted["lite"]["exists"] is False
    assert by_key[KIND_ACTIVE_THREAD]["body"]["title"] == "room_a"
    assert by_key[KIND_ACTIVE_THREAD]["lite"]["title"] == "room_b"
    assert by_key[KIND_ACTIVE_THREAD]["lite"]["content"] == "B の返事"


def test_chronicle_and_root_pages_are_not_listed(pack: Dict[str, Any]) -> None:
    changes = _empty_changes()
    changes["pages"]["deleted"] = [pack["ids"]["chronicle"], "root_terms"]
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", changes)
    items, warnings = _compare(pack["env"], zip_path)
    assert items == []
    assert len(warnings) == 2


def test_review_store_returns_pages_and_shortens_long_text(pack: Dict[str, Any]) -> None:
    long_text = "長" * (PREVIEW_CHARS + 5)
    changes = _empty_changes()
    changes["messages"]["edited"] = ["m2", "m3"]
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes, messages={"m2": long_text, "m3": "短い"},
    )
    items, warnings = _compare(pack["env"], zip_path)
    path = pack["dir"] / "review.sqlite"
    write_review(path, items, warnings)
    assert read_review_summary(path)["total"] == 2
    assert read_review_summary(path)["counts"]["message"] == 2
    assert read_review_fingerprints(path) == {i.key: i.fingerprint for i in items}
    first, second = read_review_items(path, 0, 2, full=False)
    assert first["lite"]["truncated"] is True
    assert len(first["lite"]["content"]) == PREVIEW_CHARS
    assert second["lite"]["truncated"] is False
    assert read_review_items(path, 1, 1, full=False)[0]["key"] == second["key"]
    assert read_review_items(path, 0, 1, full=True)[0]["lite"]["content"] == long_text


# ---------------------------------------------------------------------------
# 選択の適用
# ---------------------------------------------------------------------------

def _edited_message_pack(pack: Dict[str, Any]) -> Path:
    changes = _empty_changes()
    changes["messages"]["edited"] = ["m2"]
    return _lite_pack(pack["zip"], pack["dir"] / "lite.zip", changes, messages={"m2": "Lite の本文"})


def test_edited_message_takes_lite_and_keeps_body_metadata(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    conn = _mem(env)
    conn.execute("UPDATE messages SET metadata = ? WHERE id = 'm2'", (json.dumps({"tags": ["body"]}),))
    conn.execute("INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES ('m2', 0, '[0.5]')")
    conn.commit()
    conn.close()
    zip_path = _edited_message_pack(pack)
    items, _ = _compare(env, zip_path)
    result = _apply(env, zip_path, _select(items, "lite"))
    content, metadata = _message(env, "m2")
    assert content == "Lite の本文"
    assert json.loads(metadata) == {"tags": ["body"]}
    assert _vectors(env, "m2") == [DEFERRED_VECTOR]
    assert result.counts["messages"].updated == 1
    assert result.embeddings_deferred == 1
    assert result.return_merge["choices"] == {"message": {"lite": 1, "body": 0}}


def test_edited_message_keeps_body_when_chosen(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    zip_path = _edited_message_pack(pack)
    items, _ = _compare(env, zip_path)
    result = _apply(env, zip_path, _select(items, "body"))
    assert _message(env, "m2")[0] == "B の返事"
    assert result.counts["messages"].updated == 0


def test_message_deleted_in_body_is_restored_only_when_chosen(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    conn = _mem(env)
    conn.execute("DELETE FROM messages WHERE id = 'm2'")
    conn.commit()
    conn.close()
    zip_path = _edited_message_pack(pack)
    items, _ = _compare(env, zip_path)
    assert items[0].payload["body"]["exists"] is False
    _apply(env, zip_path, _select(items, "body"))
    assert _message(env, "m2") is None
    items, _ = _compare(env, zip_path)
    result = _apply(env, zip_path, _select(items, "lite"))
    assert _message(env, "m2")[0] == "Lite の本文"
    assert result.counts["messages"].added == 1
    assert result.counts["messages"].updated == 0


def test_deleted_message_is_removed_with_its_vectors(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    conn = _mem(env)
    conn.execute("INSERT INTO message_embeddings(message_id, chunk_index, vector) VALUES ('m2', 0, '[0.5]')")
    conn.commit()
    conn.close()
    changes = _empty_changes()
    changes["messages"]["deleted"] = ["m2", "m3"]
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", changes, drop_messages=["m2", "m3"])
    items, _ = _compare(env, zip_path)
    result = _apply(env, zip_path, _select(items, "lite", **{"message:m3": "body"}))
    assert _message(env, "m2") is None
    assert _vectors(env, "m2") == []
    assert _message(env, "m3") is not None
    assert result.return_merge["messages_deleted"] == 1
    assert result.return_merge["choices"]["message"] == {"lite": 1, "body": 1}


def test_edited_page_takes_lite_and_keeps_the_old_text_in_history(pack: Dict[str, Any]) -> None:
    env, apple = pack["env"], pack["ids"]["apple"]
    changes = _empty_changes()
    changes["pages"]["edited"] = [apple]
    # Lite の updated_at が本体より古くても、選んだ方が残る
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes,
        pages={apple: {"title": "林檎", "summary": "Lite の概要", "content": "Lite のりんご",
                       "updated_at": "2000-01-01T00:00:00+00:00"}},
    )
    conn = _mem(env)
    conn.execute("INSERT INTO memopedia_embeddings(page_id, vector) VALUES (?, '[0.5]')", (apple,))
    conn.commit()
    conn.close()
    items, _ = _compare(env, zip_path)
    assert items[0].payload["lite"]["title"] == "林檎"
    result = _apply(env, zip_path, _select(items, "lite"))
    assert _page(env, apple)[:4] == ("林檎", "Lite の概要", "Lite のりんご", 0)
    assert result.counts["pages"].updated == 1
    conn = _mem(env)
    history = conn.execute(
        "SELECT edit_type, edit_source, before_title, before_content FROM memopedia_page_edit_history "
        "WHERE page_id = ? ORDER BY edited_at DESC, rowid DESC LIMIT 1", (apple,),
    ).fetchone()
    embedded = conn.execute("SELECT COUNT(*) FROM memopedia_embeddings WHERE page_id = ?", (apple,)).fetchone()
    conn.close()
    assert history == ("update", "persona_pack_import", "りんご", "赤い果物")
    assert embedded == (0,)


def test_page_deleted_in_body_comes_back_when_lite_is_chosen(pack: Dict[str, Any]) -> None:
    env, apple = pack["env"], pack["ids"]["apple"]
    conn = _mem(env)
    conn.execute("UPDATE memopedia_pages SET is_deleted = 1 WHERE id = ?", (apple,))
    conn.commit()
    conn.close()
    changes = _empty_changes()
    changes["pages"]["edited"] = [apple]
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", changes)  # 本文は同じでも一覧に出る
    items, _ = _compare(env, zip_path)
    assert items[0].payload["body"]["deleted"] is True
    _apply(env, zip_path, _select(items, "lite"))
    assert _page(env, apple)[3] == 0


def test_deleted_page_and_core_memory(pack: Dict[str, Any]) -> None:
    env, banana = pack["env"], pack["ids"]["banana"]
    conn = _mem(env)
    core_page = conn.execute(
        "SELECT id FROM memopedia_pages WHERE category = 'core' AND is_trunk = 0 LIMIT 1"
    ).fetchone()[0]
    conn.close()
    changes = _empty_changes()
    changes["pages"]["deleted"] = [banana, core_page]
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", changes, drop_pages=[banana, core_page])
    items, _ = _compare(env, zip_path)
    result = _apply(env, zip_path, _select(items, "lite"))
    assert _page(env, banana)[3] == 1
    core = _page(env, core_page)
    assert core[3] == 1
    assert isinstance(json.loads(core[4])["deleted_at"], int)
    assert result.return_merge["pages_deleted"] == 2
    conn = _mem(env)
    edit_types = conn.execute(
        "SELECT edit_type FROM memopedia_page_edit_history WHERE page_id = ? AND edit_source = ?",
        (banana, "persona_pack_import"),
    ).fetchall()
    conn.close()
    assert edit_types == [("delete",)]


def test_core_memory_deleted_in_body_loses_deleted_at_when_revived(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    conn = _mem(env)
    core_page = conn.execute(
        "SELECT id FROM memopedia_pages WHERE category = 'core' AND is_trunk = 0 LIMIT 1"
    ).fetchone()[0]
    meta = json.loads(conn.execute(
        "SELECT metadata FROM memopedia_pages WHERE id = ?", (core_page,)
    ).fetchone()[0])
    meta["deleted_at"] = T0
    conn.execute(
        "UPDATE memopedia_pages SET is_deleted = 1, metadata = ? WHERE id = ?",
        (json.dumps(meta), core_page),
    )
    conn.commit()
    conn.close()
    changes = _empty_changes()
    changes["pages"]["edited"] = [core_page]
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", changes)
    items, _ = _compare(env, zip_path)
    _apply(env, zip_path, _select(items, "lite"))
    page = _page(env, core_page)
    assert page[3] == 0
    assert "deleted_at" not in json.loads(page[4])


def _world_ai(env: Dict[str, Any], column: str) -> Any:
    conn = sqlite3.connect(env["world_db"])
    try:
        return conn.execute(f"SELECT {column} FROM ai WHERE AIID = ?", (env["persona_id"],)).fetchone()[0]
    finally:
        conn.close()


def test_profile_writes_only_the_chosen_items(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    changes = _empty_changes()
    changes["profile"]["edited"] = ["name", "system_prompt"]
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes,
        profile={"AINAME": "Lite 名", "SYSTEMPROMPT": "Lite の指示", "DESCRIPTION": "Lite の説明",
                 "DEFAULT_MODEL": "lite-model"},
    )
    items, _ = _compare(env, zip_path)
    assert {i.key for i in items} == {"profile:AINAME", "profile:SYSTEMPROMPT"}
    result = _apply(env, zip_path, _select(items, "body", **{"profile:AINAME": "lite"}))
    assert _world_ai(env, "AINAME") == "Lite 名"
    assert _world_ai(env, "SYSTEMPROMPT") != "Lite の指示"
    assert _world_ai(env, "DESCRIPTION") != "Lite の説明"
    assert _world_ai(env, "DEFAULT_MODEL") != "lite-model"
    assert result.profile_columns_overwritten == ["AINAME"]


def test_overwrite_profile_is_not_used_for_a_lite_pack(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", _empty_changes(), profile={"AINAME": "Lite 名"},
    )
    result = _import(
        env, zip_path, "merge", overwrite_profile=True,
        return_selection=ReturnSelection(expected={}, decisions={}),
    )
    assert _world_ai(env, "AINAME") != "Lite 名"
    assert any("profile overwrite" in w for w in result.warnings)


def test_active_thread_changes_only_active_thread_id(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    state_path = env["persona_dir"] / "active_state.json"
    state_path.write_text(json.dumps({"active_thread_id": "room_a", "pulse_scoped_parent": "x"}), encoding="utf-8")
    # 全体の ID で書かれたファイルと、Lite で生まれたスレッド
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", _empty_changes(),
        active={"active_thread_id": f"{PID}:room_b"},
    )
    items, _ = _compare(env, zip_path)
    assert [i.key for i in items] == [KIND_ACTIVE_THREAD]
    _apply(env, zip_path, _select(items, "body"))
    assert json.loads(state_path.read_text(encoding="utf-8"))["active_thread_id"] == "room_a"
    items, _ = _compare(env, zip_path)
    _apply(env, zip_path, _select(items, "lite"))
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "active_thread_id": "room_b", "pulse_scoped_parent": "x",
    }


def test_same_active_thread_is_not_listed(pack: Dict[str, Any]) -> None:
    zip_path = _lite_pack(pack["zip"], pack["dir"] / "lite.zip", _empty_changes())
    assert _compare(pack["env"], zip_path) == ([], [])


# ---------------------------------------------------------------------------
# Lite で触っていない行と、足す行
# ---------------------------------------------------------------------------

def _message_row(mid: str, thread: str, content: str, created: int) -> Dict[str, Any]:
    return {
        "id": mid, "thread_id": thread, "role": "assistant", "content": content,
        "resource_id": PID, "created_at": created, "metadata": {"tags": ["conversation"]},
    }


def test_untouched_rows_keep_the_body_value(pack: Dict[str, Any]) -> None:
    env, apple = pack["env"], pack["ids"]["apple"]
    conn = _mem(env)
    conn.execute("UPDATE messages SET content = '本体の移行で変わった' WHERE id = 'm3'")
    conn.execute("DELETE FROM messages WHERE id = 'm4'")
    conn.execute("DELETE FROM messages WHERE thread_id = ?", (f"{PID}:stelis_1",))
    conn.execute("DELETE FROM stelis_threads")
    conn.execute("DELETE FROM threads WHERE id = ?", (f"{PID}:stelis_1",))
    conn.execute("DELETE FROM activities")
    conn.execute("DELETE FROM clips")
    conn.commit()
    conn.close()
    world = sqlite3.connect(env["world_db"])
    world.execute("DELETE FROM task_book")
    world.commit()
    world.close()
    (env["persona_dir"] / "day_reports" / "2026-10-01.md").unlink()
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", _empty_changes(),
        pages={apple: {"content": "Lite で触っていないが新しい", "updated_at": "2099-01-01T00:00:00+00:00"}},
    )
    assert _compare(env, zip_path) == ([], [])
    result = _apply(env, zip_path, ReturnSelection(expected={}, decisions={}))
    assert _message(env, "m3")[0] == "本体の移行で変わった"
    assert _message(env, "m4") is None
    assert _message(env, "m5") is None
    assert _page(env, apple)[2] == "赤い果物"
    conn = _mem(env)
    threads = {r[0] for r in conn.execute("SELECT id FROM threads")}
    n_activities = conn.execute("SELECT COUNT(*) FROM activities").fetchone()
    n_clips = conn.execute("SELECT COUNT(*) FROM clips").fetchone()
    n_stelis = conn.execute("SELECT COUNT(*) FROM stelis_threads").fetchone()
    conn.close()
    assert f"{PID}:stelis_1" not in threads
    assert n_activities == (0,)
    assert n_clips == (0,)
    assert n_stelis == (0,)
    assert not (env["persona_dir"] / "day_reports" / "2026-10-01.md").exists()
    world = sqlite3.connect(env["world_db"])
    assert world.execute("SELECT COUNT(*) FROM task_book").fetchone() == (0,)
    world.close()
    assert result.counts["activities"].skipped == 1
    assert result.counts["task_book"].skipped == 1


def test_created_rows_are_added(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    changes = _empty_changes()
    changes["messages"]["created"] = ["lite-m1"]
    changes["pages"]["created"] = ["lite-p1"]
    new_page = {
        "id": "lite-p1", "parent_id": "root_terms", "title": "Lite の項目", "summary": "", "content": "本文",
        "category": "terms", "created_at": T0 + 50, "updated_at": T0 + 50, "keywords": [],
        "vividness": "rough", "is_trunk": 0, "is_important": 0, "is_deleted": 0, "metadata": None,
    }
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes,
        add_messages=[
            _message_row("lite-m1", f"{PID}:lite_thread", "Lite で話した", T0 + 40),
            _message_row("not-listed", f"{PID}:lite_thread", "一覧に無い", T0 + 41),
        ],
        add_pages=[new_page],
        active={"active_thread_id": "lite_thread"},
    )
    items, _ = _compare(env, zip_path)
    assert [i.key for i in items] == [KIND_ACTIVE_THREAD]
    assert items[0].payload["lite"]["content"] == "一覧に無い"
    result = _apply(env, zip_path, _select(items, "lite"))
    assert _message(env, "lite-m1")[0] == "Lite で話した"
    assert _message(env, "not-listed") is None
    assert _page(env, "lite-p1")[0] == "Lite の項目"
    assert result.counts["pages"].added == 1
    conn = _mem(env)
    thread = conn.execute("SELECT id FROM threads WHERE id = ?", (f"{PID}:lite_thread",)).fetchone()
    conn.close()
    assert thread == (f"{PID}:lite_thread",)
    state = json.loads((env["persona_dir"] / "active_state.json").read_text(encoding="utf-8"))
    assert state["active_thread_id"] == "lite_thread"


def _fragment(fid: str, entity: str, chronicle: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": fid, "content": f"{fid} の内容", "entity_id": entity, "chronicle_entry_id": chronicle,
        "vividness": "vivid", "source_date": "2026-10-01", "created_at": T0 + 60,
    }


def test_created_fragments_are_added_with_their_links_checked(pack: Dict[str, Any]) -> None:
    env, apple = pack["env"], pack["ids"]["apple"]
    conn = _mem(env)
    conn.execute(
        "INSERT INTO memopedia_fragments(id, content, entity_id, created_at) VALUES ('kept', '本体の断片', ?, ?)",
        (apple, T0),
    )
    conn.commit()
    conn.close()
    changes = _empty_changes()
    changes["fragments"]["created"] = ["f-ok", "f-chronicle", "f-no-page", "kept"]
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes,
        fragments=[
            _fragment("f-ok", apple, pack["ids"]["chronicle"]),
            _fragment("f-chronicle", apple, "chronicle-not-here"),
            _fragment("f-no-page", "page-not-here"),
            _fragment("kept", apple),
            _fragment("f-not-listed", apple),
        ],
    )
    result = _apply(env, zip_path, ReturnSelection(expected={}, decisions={}))
    conn = _mem(env)
    rows = dict(conn.execute("SELECT id, chronicle_entry_id FROM memopedia_fragments").fetchall())
    kept = conn.execute("SELECT content FROM memopedia_fragments WHERE id = 'kept'").fetchone()
    conn.close()
    assert rows == {"f-ok": pack["ids"]["chronicle"], "f-chronicle": None, "kept": None}
    assert kept == ("本体の断片",)
    assert result.return_merge["fragments_added"] == 2
    assert result.return_merge["fragments_without_page"] == 1
    counts = result.counts["extensions/memory_db/memopedia_fragments"]
    assert (counts.added, counts.skipped) == (2, 3)


def test_applying_again_changes_nothing(pack: Dict[str, Any]) -> None:
    env, apple, banana = pack["env"], pack["ids"]["apple"], pack["ids"]["banana"]
    changes = _empty_changes()
    changes["messages"]["edited"] = ["m2"]
    changes["messages"]["deleted"] = ["m3"]
    changes["messages"]["created"] = ["lite-m1"]
    changes["pages"]["edited"] = [apple]
    changes["pages"]["deleted"] = [banana]
    changes["fragments"]["created"] = ["f-ok"]
    changes["profile"]["edited"] = ["name"]
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes,
        messages={"m2": "Lite の本文"},
        drop_messages=["m3"],
        add_messages=[_message_row("lite-m1", f"{PID}:room_a", "Lite で話した", T0 + 40)],
        pages={apple: {"content": "Lite のりんご"}},
        drop_pages=[banana],
        fragments=[_fragment("f-ok", apple)],
        profile={"AINAME": "Lite 名"},
    )
    items, _ = _compare(env, zip_path)
    _apply(env, zip_path, _select(items, "lite"))
    items, _ = _compare(env, zip_path)
    assert items == []
    conn = _mem(env)
    n_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    history_before = conn.execute("SELECT COUNT(*) FROM memopedia_page_edit_history").fetchone()
    conn.close()
    result = _apply(env, zip_path, ReturnSelection(expected={}, decisions={}))
    conn = _mem(env)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == n_before
    assert conn.execute("SELECT COUNT(*) FROM memopedia_page_edit_history").fetchone() == history_before
    assert conn.execute("SELECT COUNT(*) FROM memopedia_fragments").fetchone() == (1,)
    conn.close()
    assert result.counts["messages"].added == 0
    assert result.counts["messages"].updated == 0


# ---------------------------------------------------------------------------
# 適用の安全
# ---------------------------------------------------------------------------

def test_change_of_a_listed_item_stops_the_apply(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    zip_path = _edited_message_pack(pack)
    items, _ = _compare(env, zip_path)
    conn = _mem(env)
    conn.execute("UPDATE messages SET content = '見比べのあとで変わった' WHERE id = 'm2'")
    conn.commit()
    conn.close()
    with pytest.raises(ReturnMergeStale) as caught:
        _apply(env, zip_path, _select(items, "lite"))
    assert caught.value.keys == ["message:m2"]
    assert _message(env, "m2")[0] == "見比べのあとで変わった"
    assert not (env["home"] / "backups" / "persona_pack_merge").exists()


@pytest.mark.parametrize("change", ["page", "profile", "thread", "gone"])
def test_each_kind_of_listed_item_is_checked(pack: Dict[str, Any], change: str) -> None:
    env, apple = pack["env"], pack["ids"]["apple"]
    changes = _empty_changes()
    changes["pages"]["edited"] = [apple]
    changes["messages"]["deleted"] = ["m3"]
    changes["profile"]["edited"] = ["name"]
    zip_path = _lite_pack(
        pack["zip"], pack["dir"] / "lite.zip", changes,
        pages={apple: {"content": "Lite"}}, drop_messages=["m3"],
        profile={"AINAME": "Lite 名"}, active={"active_thread_id": "room_b"},
    )
    items, _ = _compare(env, zip_path)
    if change == "page":
        conn = _mem(env)
        conn.execute("UPDATE memopedia_pages SET summary = 'x' WHERE id = ?", (apple,))
        conn.commit()
        conn.close()
    elif change == "gone":
        conn = _mem(env)
        conn.execute("DELETE FROM messages WHERE id = 'm3'")
        conn.commit()
        conn.close()
    elif change == "profile":
        world = sqlite3.connect(env["world_db"])
        world.execute("UPDATE ai SET AINAME = 'x'")
        world.commit()
        world.close()
    else:
        (env["persona_dir"] / "active_state.json").write_text('{"active_thread_id": "other"}', encoding="utf-8")
    with pytest.raises(ReturnMergeStale):
        _apply(env, zip_path, _select(items, "lite"))


def test_change_of_an_unlisted_row_does_not_stop_the_apply(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    zip_path = _edited_message_pack(pack)
    items, _ = _compare(env, zip_path)
    conn = _mem(env)
    conn.execute("UPDATE messages SET content = '別の行' WHERE id = 'm3'")
    conn.commit()
    conn.close()
    _apply(env, zip_path, _select(items, "lite"))
    assert _message(env, "m2")[0] == "Lite の本文"
    assert _message(env, "m3")[0] == "別の行"


def test_backup_is_made_before_writing_and_only_three_remain(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    zip_path = _edited_message_pack(pack)
    folder = env["home"] / "backups" / "persona_pack_merge" / PID
    for _round in range(BACKUP_KEEP + 2):
        items, _ = _compare(env, zip_path)
        if not items:
            conn = _mem(env)
            conn.execute("UPDATE messages SET content = 'B の返事' WHERE id = 'm2'")
            conn.commit()
            conn.close()
            items, _ = _compare(env, zip_path)
        result = _apply(env, zip_path, _select(items, "lite"))
    backups = sorted(folder.glob("memory_*.db"))
    assert len(backups) == BACKUP_KEEP
    assert result.return_merge["backup_path"] == str(backups[-1])
    conn = sqlite3.connect(backups[-1])
    assert conn.execute("SELECT content FROM messages WHERE id = 'm2'").fetchone() == ("B の返事",)
    conn.close()


def test_failed_backup_stops_the_apply(pack: Dict[str, Any]) -> None:
    env = pack["env"]
    zip_path = _edited_message_pack(pack)
    items, _ = _compare(env, zip_path)
    # backups という名前のファイルがあるとフォルダを作れない
    (env["home"] / "backups").write_text("not a folder", encoding="utf-8")
    with pytest.raises(ReturnMergeBackupError):
        _apply(env, zip_path, _select(items, "lite"))
    assert _message(env, "m2")[0] == "B の返事"


# ---------------------------------------------------------------------------
# 選択の検査
# ---------------------------------------------------------------------------

def test_one_item_can_differ_from_the_bulk_choice() -> None:
    decided = resolve_choices(["a", "b", "c"], "lite", {"b": "body"})
    assert decided == {"a": "lite", "b": "body", "c": "lite"}


def test_every_item_needs_a_choice() -> None:
    with pytest.raises(ValueError, match="1 item"):
        resolve_choices(["a", "b"], None, {"a": "lite"})


@pytest.mark.parametrize("bulk, choices", [
    ("both", {}),
    (None, {"a": "both"}),
    ("lite", {"unknown": "lite"}),
])
def test_bad_choices_are_refused(bulk: Optional[str], choices: Dict[str, str]) -> None:
    with pytest.raises(ValueError):
        resolve_choices(["a"], bulk, choices)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """合成のペルソナが一体いる環境の TestClient と、Lite のパック。"""
    from api.routes import people
    from api.routes.people import persona_pack as route

    monkeypatch.setattr(route, "_start_embedding", lambda persona_id, count: True)
    home = tmp_path / "home"
    home.mkdir()
    manager = FakeManager(home)
    manager.add_persona(PID, "Tester")
    monkeypatch.setenv("SAIVERSE_HOME", str(home))
    conn = sqlite3.connect(home / "personas" / PID / "memory.db")
    _add_message(conn, PID, "m1", "room_a", "assistant", "hello", T0, {"tags": ["conversation"]})
    _add_message(conn, PID, "m2", "room_a", "assistant", "again", T0 + 1, {"tags": ["conversation"]})
    conn.commit()
    conn.close()
    app = FastAPI()
    app.include_router(people.router, prefix="/api/people")
    app.dependency_overrides[get_manager] = lambda: manager
    client = TestClient(app)
    plain = client.get(f"/api/people/{PID}/persona-pack")
    assert plain.status_code == 200, plain.text
    plain_zip = tmp_path / "plain.zip"
    plain_zip.write_bytes(plain.content)
    changes = _empty_changes()
    changes["messages"]["edited"] = ["m1", "m2"]
    lite_zip = _lite_pack(
        plain_zip, tmp_path / "lite.zip", changes, messages={"m1": "Lite 1", "m2": "Lite 2"},
    )
    return {"client": client, "home": home, "manager": manager, "plain": plain_zip, "lite": lite_zip}


def _post_compare(api: Dict[str, Any], zip_path: Path) -> Any:
    return api["client"].post(
        f"/api/people/{PID}/persona-pack/compare",
        files={"file": ("pack.zip", zip_path.read_bytes(), "application/zip")},
    )


def _post_apply(api: Dict[str, Any], **body: Any) -> Any:
    return api["client"].post(f"/api/people/{PID}/persona-pack/apply", json=body)


def _api_message(api: Dict[str, Any], mid: str) -> Optional[str]:
    conn = sqlite3.connect(api["home"] / "personas" / PID / "memory.db")
    try:
        row = conn.execute("SELECT content FROM messages WHERE id = ?", (mid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _compare_folders(api: Dict[str, Any]) -> List[Path]:
    root = api["home"] / "tmp" / "persona_pack_compare"
    return [p for parent in root.iterdir() for p in parent.iterdir()] if root.exists() else []


def test_api_compare_lists_pages_and_apply_takes_the_choices(api: Dict[str, Any]) -> None:
    res = _post_compare(api, api["lite"])
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["has_changes"] is True
    assert data["total"] == 2
    assert data["counts"]["message"] == 2
    assert _api_message(api, "m1") == "hello"

    url = f"/api/people/{PID}/persona-pack/compare/{data['token']}/items"
    first = api["client"].get(url, params={"offset": 0, "limit": 1}).json()["items"]
    second = api["client"].get(url, params={"offset": 1, "limit": 1}).json()["items"]
    assert [first[0]["key"], second[0]["key"]] == ["message:m1", "message:m2"]

    refused = _post_apply(api, token=data["token"], choices={"message:m1": "lite"})
    assert refused.status_code == 400
    assert "1 item" in refused.json()["detail"]
    assert _api_message(api, "m1") == "hello"

    applied = _post_apply(api, token=data["token"], bulk="lite", choices={"message:m2": "body"})
    assert applied.status_code == 200, applied.text
    payload = applied.json()
    assert payload["return_merge"]["choices"]["message"] == {"lite": 1, "body": 1}
    assert _api_message(api, "m1") == "Lite 1"
    assert _api_message(api, "m2") == "again"
    assert _compare_folders(api) == []

    again = _post_apply(api, token=data["token"], bulk="lite")
    assert again.status_code == 410
    assert again.json()["detail"]["code"] == "compare_expired"


def test_api_result_carries_the_items_left_out_of_the_list(api: Dict[str, Any], tmp_path: Path) -> None:
    changes = _empty_changes()
    changes["pages"]["deleted"] = ["root_terms"]
    zip_path = _lite_pack(api["plain"], tmp_path / "root.zip", changes)
    data = _post_compare(api, zip_path).json()
    assert data["total"] == 0
    res = _post_apply(api, token=data["token"])
    assert res.status_code == 200, res.text
    assert any("root_terms" in w for w in res.json()["warnings"])


def test_api_pack_without_changes_goes_through_apply(api: Dict[str, Any]) -> None:
    data = _post_compare(api, api["plain"]).json()
    assert data["has_changes"] is False
    assert data["total"] == 0
    res = _post_apply(api, token=data["token"], overwrite_profile=False)
    assert res.status_code == 200, res.text
    assert res.json()["return_merge"] is None


def test_api_import_refuses_a_lite_pack(api: Dict[str, Any]) -> None:
    res = api["client"].post(
        f"/api/people/{PID}/persona-pack/import",
        files={"file": ("pack.zip", api["lite"].read_bytes(), "application/zip")},
    )
    assert res.status_code == 400
    assert "compare" in res.json()["detail"]
    assert _api_message(api, "m1") == "hello"


def test_api_stale_comparison_answers_409(api: Dict[str, Any]) -> None:
    token = _post_compare(api, api["lite"]).json()["token"]
    conn = sqlite3.connect(api["home"] / "personas" / PID / "memory.db")
    conn.execute("UPDATE messages SET content = '別の内容' WHERE id = 'm1'")
    conn.commit()
    conn.close()
    res = _post_apply(api, token=token, bulk="lite")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "compare_stale"
    assert _api_message(api, "m1") == "別の内容"
    assert _api_message(api, "m2") == "again"


def test_api_new_comparison_replaces_the_old_zip(api: Dict[str, Any]) -> None:
    first = _post_compare(api, api["lite"]).json()["token"]
    second = _post_compare(api, api["lite"]).json()["token"]
    assert [p.name for p in _compare_folders(api)] == [second]
    assert _post_apply(api, token=first, bulk="body").status_code == 410


def test_api_comparison_over_a_day_old_is_removed(api: Dict[str, Any]) -> None:
    token = _post_compare(api, api["lite"]).json()["token"]
    old = _compare_folders(api)[0]
    day_ago = time.time() - 25 * 60 * 60
    os.utime(old, (day_ago, day_ago))
    from api.routes.people import persona_pack as route

    route._sweep_old_compares(api["manager"])
    assert _compare_folders(api) == []
    res = api["client"].get(f"/api/people/{PID}/persona-pack/compare/{token}/items")
    assert res.status_code == 410


def test_api_broken_token_is_gone(api: Dict[str, Any]) -> None:
    assert _post_apply(api, token="../../secret", bulk="lite").status_code == 410


def test_api_broken_pack_keeps_nothing(api: Dict[str, Any], tmp_path: Path) -> None:
    changes = _empty_changes()
    changes["messages"]["edited"] = ["nobody"]
    broken = _lite_pack(api["plain"], tmp_path / "broken.zip", changes)
    res = _post_compare(api, broken)
    assert res.status_code == 400
    assert "changes.json" in res.json()["detail"]
    assert _compare_folders(api) == []


def test_next_config_keeps_the_relay_idle_limit_for_long_requests() -> None:
    """見比べと適用は /api/ の下の POST で、返事まで黙る。中継の無活動の上限が 30 秒に戻らないこと。"""
    config = Path(__file__).resolve().parents[1] / "frontend" / "next.config.ts"
    assert "proxyTimeout: 3_600_000" in config.read_text(encoding="utf-8")
