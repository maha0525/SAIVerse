"""システム通知の移行の規則の版 4 (database/system_notice_migration.py)。

docs/intent/system_notice_delivery.md「追補」の移行の規則の版 4。v0.3.27 で書き手を
直したときに漏れていた二つの書き手 — ``switch_active_thread`` (builtin_data/tools/
thread_switch.py) と ChatGPT の会話の取り込み (scripts/import_chatgpt_conversations.py)
の見出し — が、印の無い role=system のまま書いた行に、系統の印を付ける。合成した一時
DB だけを使う。固定すること:

- ``other_thread_messages`` を metadata に持つ行と、本文が
  ``[thread switch] moved from `` で始まる行は ``thread_switch``。
- 本文がその見出しの形の全体と一致する行は ``chatgpt_import``。形の違う行は
  推定できないまま確認待ちに残す。
- 本文が空で metadata の ``type`` が ``stelis_anchor`` の行 (``lg_stelis_start_node``) は
  ``stelis_anchor``。削除済みの書き手の行 — 別の City への移動の失敗の案内は
  ``city_dispatch_failure``、``get_since_last_user_conversation`` の要約の行は
  ``since_user_conversation_summary``。どれも書き手の形から外れた行は推定しない。
- 版 3 の移行が確認待ちにした行は、版 4 の走査し直しで判定し直し、印が外れて通知に
  なる。版 4 の規則に当たらない確認待ちの行は、走査し直しでも数え直さない。
- 新しい形式に切り替わった時刻より後に作られた行でも、role=system の行は版 4 の規則で
  判定する (印を足すだけで、名義も本文も変えない)。境より後のユーザー名義の行は確認待ちの
  まま (理由は ``AFTER_CUTOVER_REASON``)。
- 所有者が確定した行は触らない。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from database.system_notice_migration import (
    AFTER_CUTOVER_REASON,
    CHATGPT_IMPORT_KIND,
    CITY_DISPATCH_FAILURE_KIND,
    MIGRATION_MARKER_KEY,
    MIGRATION_RULES_VERSION,
    SINCE_USER_CONVERSATION_SUMMARY_KIND,
    STELIS_ANCHOR_KIND,
    THREAD_SWITCH_KIND,
    UNKNOWN_SYSTEM_REASON,
    classify_unmarked_system_message,
    marker_rules_version,
    migrate_persona_db,
)
from sai_memory.memory.storage import (
    SYSTEM_NOTICE_KEY,
    SYSTEM_NOTICE_REVIEW_KEY,
    SYSTEM_NOTICE_REVIEWED_KEY,
    init_db,
    is_system_notice,
)

STAMP = "20261030_000000"

_LINK = [{"thread_id": "p1:sns", "message_id": "m0", "range_before": 8, "range_after": 0}]
_HEADER = (
    '[Imported ChatGPT conversation "アイフィ" (68972926-24d8-8329-a1fc-871d5d3664bc) '
    "created 2025-08-09T10:55:34Z]"
)

#: (id, 本文, metadata, 期待する系統名 / None は推定できない)
_ROWS = [
    ("switch_default", "[thread switch] moved from p1:sns",
     {"tags": ["conversation"], "other_thread_messages": _LINK}, THREAD_SWITCH_KIND),
    # summary を渡した行は本文が自由なので、metadata の参照で見分ける
    ("switch_summary", "ユーザーの指示により、スレッドを切り替えます。",
     {"tags": ["conversation"], "other_thread_messages": _LINK}, THREAD_SWITCH_KIND),
    # 移動元に行が無かった行は参照を持たないが、既定の本文で見分ける
    ("switch_no_link", "[thread switch] moved from p1:empty", {}, THREAD_SWITCH_KIND),
    ("header", _HEADER, {"tags": ["conversation"]}, CHATGPT_IMPORT_KIND),
    # 会話の id の代わりに別の文字列が入った世代
    ("header_other_id",
     '[Imported ChatGPT conversation "スーパーアイフィ" (google.com) created 2025-01-27T00:30:00Z]',
     {"tags": ["conversation"]}, CHATGPT_IMPORT_KIND),
    # 形の違う行は推定しない
    ("switch_bare", "[thread switch]", {"tags": ["conversation"]}, None),
    ("header_with_tail", _HEADER + " 追記", {"tags": ["conversation"]}, None),
    ("header_other_time",
     '[Imported ChatGPT conversation "x" (y) created 2025-08-09 10:55]', {}, None),
    # lg_stelis_start_node が書いた Stelis の開始の行 (本文は空)
    ("stelis_anchor", "",
     {"type": "stelis_anchor", "stelis_thread_id": "p1:stelis_40456e53",
      "stelis_label": "小説執筆セッション", "created_at": 1769364238}, STELIS_ANCHOR_KIND),
    # 本文を持つ行・Stelis のスレッドの id が無い行は、その書き手の形ではない
    ("stelis_with_body", "何か",
     {"type": "stelis_anchor", "stelis_thread_id": "p1:stelis_40456e53"}, None),
    ("stelis_without_thread", "", {"type": "stelis_anchor"}, None),
    # 旧 _handle_movement が書いた別の City への移動の失敗の案内
    ("city_failure",
     "別のCityへの移動に失敗しました。移動失敗: City 'city_a' はネットワーク上に見つかりませんでした。"
     "相手のCityが起動しているか確認してください。",
     {"tags": ["conversation"]}, CITY_DISPATCH_FAILURE_KIND),
    ("city_unavailable", "別のCityへ移動する機能が設定されていません。",
     {"tags": ["conversation"]}, CITY_DISPATCH_FAILURE_KIND),
    ("city_unavailable_tail", "別のCityへ移動する機能が設定されていません。追記",
     {"tags": ["conversation"]}, None),
    # 旧 get_since_last_user_conversation が書いた要約の行
    ("since_summary", "[要約 c71abb3c] 待機0回、その他のアクティビティ1件がありました。",
     {"tags": ["summary", "since_user_conversation"],
      "summary_uuid": "c71abb3c-ddd4-4761-9a5d-7849d1eaa435", "source_message_count": 1},
     SINCE_USER_CONVERSATION_SUMMARY_KIND),
    # 本文の先頭の 8 文字が summary_uuid と合わない行・tags の無い行は推定しない
    ("since_summary_other_uuid", "[要約 00000000] 待機0回",
     {"tags": ["summary", "since_user_conversation"],
      "summary_uuid": "c71abb3c-ddd4-4761-9a5d-7849d1eaa435"}, None),
    ("since_summary_without_tag", "[要約 c71abb3c] 待機0回",
     {"summary_uuid": "c71abb3c-ddd4-4761-9a5d-7849d1eaa435"}, None),
]


def _insert(conn, row_id, role, content, metadata, *, created_at=0):
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?, 't', ?, ?, 'p1', ?, ?)",
        (row_id, role, content, created_at, json.dumps(metadata, ensure_ascii=False)),
    )


def _rows(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        return {
            r[0]: (r[1], r[2], json.loads(r[3]) if r[3] else {})
            for r in conn.execute("SELECT id, role, content, metadata FROM messages").fetchall()
        }


def _marker(path: Path):
    with closing(sqlite3.connect(str(path))) as conn:
        row = conn.execute(
            "SELECT value FROM embed_metadata WHERE key = ?", (MIGRATION_MARKER_KEY,),
        ).fetchone()
    return row[0] if row else None


def _set_marker(path: Path, value: str) -> None:
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embed_metadata (key, value, updated_at) VALUES (?, ?, ?)",
            (MIGRATION_MARKER_KEY, value, "2026-10-29T00:00:00+00:00"),
        )
        conn.commit()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "personas" / "p1" / "memory.db"
    path.parent.mkdir(parents=True)
    init_db(str(path)).close()
    return path


def _migrate(db: Path, tmp_path: Path, *, cutover=None):
    return migrate_persona_db(
        db, persona_id="p1", output_dir=tmp_path / "out", stamp=STAMP,
        playbook_names=frozenset(), cutover=cutover,
    )


@pytest.mark.parametrize("row_id, content, meta, kind", _ROWS, ids=[r[0] for r in _ROWS])
def test_classify_unmarked_system_message(row_id, content, meta, kind):
    assert classify_unmarked_system_message(meta, content) == kind


def test_a_full_scan_marks_the_rows_and_leaves_body_and_role(db, tmp_path):
    with closing(sqlite3.connect(str(db))) as conn:
        for i, (row_id, content, meta, _kind) in enumerate(_ROWS):
            _insert(conn, row_id, "system", content, meta, created_at=i)
        conn.commit()

    result = _migrate(db, tmp_path)

    rows = _rows(db)
    for row_id, content, meta, kind in _ROWS:
        role, new_content, new_meta = rows[row_id]
        assert (role, new_content) == ("system", content), row_id
        if kind is None:
            assert new_meta == {**meta, SYSTEM_NOTICE_REVIEW_KEY: UNKNOWN_SYSTEM_REASON}, row_id
            continue
        assert new_meta == {**meta, SYSTEM_NOTICE_KEY: kind}, row_id
        assert is_system_notice({"role": role, "metadata": new_meta}), row_id
    assert result.unknown_system_count == sum(1 for r in _ROWS if r[3] is None)
    assert marker_rules_version(_marker(db)) == MIGRATION_RULES_VERSION


def _make_v3_db(db: Path) -> None:
    """版 3 で移行を済ませた記憶の形 (版 4 の規則に当たる行が確認待ちで残っている)。"""
    with closing(sqlite3.connect(str(db))) as conn:
        for i, (row_id, content, meta, _kind) in enumerate(_ROWS):
            _insert(conn, row_id, "system", content,
                    {**meta, SYSTEM_NOTICE_REVIEW_KEY: UNKNOWN_SYSTEM_REASON}, created_at=i)
        conn.commit()
    _set_marker(db, "v3 2026-10-29T00:00:00+00:00")


def test_rows_left_for_review_by_version_3_become_notices_on_the_rescan(db, tmp_path):
    _make_v3_db(db)
    before = _rows(db)

    result = _migrate(db, tmp_path)

    assert result.rescan_from_version == 3
    assert result.backup_path is not None
    rows = _rows(db)
    for row_id, content, meta, kind in _ROWS:
        role, new_content, new_meta = rows[row_id]
        assert (role, new_content) == ("system", content), row_id
        if kind is None:
            # 版 4 の規則に当たらない確認待ちの行は、版 3 の判断のまま
            assert rows[row_id] == before[row_id], row_id
            continue
        # 確認待ちの印が外れて通知になる
        assert new_meta == {**meta, SYSTEM_NOTICE_KEY: kind}, row_id
    # 走査し直しでは、前の版で報告済みの残した行を数え直さない
    assert not result.skipped and not result.unknown_system_count
    assert {c.row_id for c in result.changes} == {r[0] for r in _ROWS if r[3] is not None}
    assert marker_rules_version(_marker(db)) == MIGRATION_RULES_VERSION

    # 今の版の印が立ったので、次の起動では何もしない
    again = _migrate(db, tmp_path)
    assert again.already_done and not again.changes
    assert _rows(db) == rows


def test_system_rows_created_after_the_cutover_are_still_judged(db, tmp_path):
    """境は「ユーザーが真似て送った文を勝手に変換しない」ためのもの。role=system の行には
    印を足すだけで名義も本文も変えないので、境より後に作られても版 4 の規則で判定する
    (記憶ブラウザで所有者が足した行に印が付くことは受け入れた — 2026-10-30)。境より後の
    ユーザー名義の ``<system>`` の行は、従来どおり確認待ちに残る。"""
    with closing(sqlite3.connect(str(db))) as conn:
        _insert(conn, "old_switch", "system", "[thread switch] moved from p1:sns",
                {"other_thread_messages": _LINK, SYSTEM_NOTICE_REVIEW_KEY: UNKNOWN_SYSTEM_REASON},
                created_at=100)
        _insert(conn, "new_switch", "system", "[thread switch] moved from p1:sns",
                {"other_thread_messages": _LINK, SYSTEM_NOTICE_REVIEW_KEY: UNKNOWN_SYSTEM_REASON},
                created_at=300)
        _insert(conn, "new_header", "system", _HEADER, {"tags": ["conversation"]},
                created_at=400)
        # Stelis の開始の行は書き手を直す前の版でも境より後に書かれうる
        _insert(conn, "new_stelis", "system", "",
                {"type": "stelis_anchor", "stelis_thread_id": "p1:stelis_x"}, created_at=500)
        # ユーザー名義で、旧形式のツールの結果を真似た行
        _insert(conn, "new_user", "user", "<system>Email sent.</system>",
                {"tags": ["conversation", "tool"]}, created_at=600)
        conn.commit()
    _set_marker(db, "v3 2026-10-29T00:00:00+00:00")

    result = _migrate(db, tmp_path, cutover=200)

    rows = _rows(db)
    assert rows["old_switch"][2] == {"other_thread_messages": _LINK, SYSTEM_NOTICE_KEY: THREAD_SWITCH_KIND}
    assert rows["new_switch"][2] == {"other_thread_messages": _LINK, SYSTEM_NOTICE_KEY: THREAD_SWITCH_KIND}
    assert rows["new_header"][2] == {"tags": ["conversation"], SYSTEM_NOTICE_KEY: CHATGPT_IMPORT_KIND}
    assert rows["new_stelis"][2] == {
        "type": "stelis_anchor", "stelis_thread_id": "p1:stelis_x",
        SYSTEM_NOTICE_KEY: STELIS_ANCHOR_KIND,
    }
    assert rows["new_user"] == ("user", "<system>Email sent.</system>", {
        "tags": ["conversation", "tool"], SYSTEM_NOTICE_REVIEW_KEY: AFTER_CUTOVER_REASON,
    })
    assert {s.row_id: s.reason for s in result.skipped} == {"new_user": AFTER_CUTOVER_REASON}


def test_system_rows_held_only_for_the_cutover_are_judged_on_rescan(db, tmp_path):
    """前の版が時刻の境だけを理由に確認待ちにした role=system の行は、規則の版が上がった
    走査し直しで、前の版の規則 (ここでは版 1 の spell) でも判定し直す。ユーザー名義の
    行は前の版の判断のまま。"""
    with closing(sqlite3.connect(str(db))) as conn:
        _insert(conn, "held_spell", "system", "[Spell Result]\nok",
                {"tags": ["spell"], SYSTEM_NOTICE_REVIEW_KEY: AFTER_CUTOVER_REASON}, created_at=300)
        _insert(conn, "held_user", "user", "<system>Email sent.</system>",
                {"tags": ["conversation", "tool"], SYSTEM_NOTICE_REVIEW_KEY: AFTER_CUTOVER_REASON},
                created_at=300)
        conn.commit()
    _set_marker(db, "v3 2026-10-29T00:00:00+00:00")
    before = _rows(db)

    _migrate(db, tmp_path, cutover=200)

    rows = _rows(db)
    assert rows["held_spell"] == ("system", "[Spell Result]\nok", {
        "tags": ["spell"], SYSTEM_NOTICE_KEY: "spell_result",
    })
    assert rows["held_user"] == before["held_user"]


def test_rows_reviewed_by_the_owner_and_rows_already_marked_are_left(db, tmp_path):
    reviewed = {"other_thread_messages": _LINK, SYSTEM_NOTICE_REVIEWED_KEY: "keep"}
    marked = {"tags": ["conversation"], SYSTEM_NOTICE_KEY: CHATGPT_IMPORT_KIND}
    with closing(sqlite3.connect(str(db))) as conn:
        _insert(conn, "reviewed", "system", "[thread switch] moved from p1:sns", reviewed)
        _insert(conn, "marked", "system", _HEADER, marked, created_at=1)
        conn.commit()
    _set_marker(db, "v3 2026-10-29T00:00:00+00:00")
    before = _rows(db)

    result = _migrate(db, tmp_path)

    assert _rows(db) == before
    assert not result.changes and not result.skipped and not result.unknown_system_count


# ---------------------------------------------------------------------------
# 規則の版 5: 中断の再開文 (v0.3.27 より前の PulseController._build_resumption_prompt)
# ---------------------------------------------------------------------------

_RESUMED_PROMPT = "出かけて、「ニュースアベニュー」に来ました。"
_RESUMED_HEAD = "[前回の処理が中断されました]\n中断理由: 優先度の高いリクエストを処理しました\n前回のプロンプト: "


def _resumed(quoted: str, tail: str) -> str:
    """旧形式の再開文の行の本文 (頭を包んだ組 + 空行 + 元のプロンプト)。"""
    return f"<system>\n{_RESUMED_HEAD}{quoted}\n</system>\n\n{tail}"


_WRAPPED = f"<system>\n{_RESUMED_PROMPT}\n</system>"
_SLOT_META = {
    "with": ["system"], "source": "day_plan_slot", "slot_kind": "出かける",
    "slot_id": "c98e7b5aa5cc", "tags": ["conversation"],
}


def test_old_resumption_rows_become_schedule_notices(db, tmp_path):
    """本番で確認待ちに残っていた形: 包みの組が二つ並ぶので版 3 までは剥がせず「タグの対応が
    曖昧」で残った (版 4 の走査の後も)。版 5 の走査し直しで、今の書き手が書く形の本文の
    schedule の通知にする。"""
    pending = {**_SLOT_META, SYSTEM_NOTICE_REVIEW_KEY: "タグの対応が曖昧"}
    with closing(sqlite3.connect(str(db))) as conn:
        _insert(conn, "resumed", "user", _resumed(_WRAPPED, _WRAPPED), pending, created_at=100)
        conn.commit()
    _set_marker(db, "v4 2026-10-30T00:00:00+00:00")

    result = _migrate(db, tmp_path, cutover=200)

    role, content, meta = _rows(db)["resumed"]
    assert role == "system"
    assert content == f"{_RESUMED_HEAD}{_RESUMED_PROMPT}\n\n{_RESUMED_PROMPT}"
    assert meta == {**_SLOT_META, SYSTEM_NOTICE_KEY: "schedule"}
    assert [c.row_id for c in result.changes] == ["resumed"]


@pytest.mark.parametrize("row_id, role, content, extra", [
    # 引用と末尾のプロンプトが違う (誰かが形を真似て書き換えた行でありうる)
    ("mismatch", "user", _resumed(_WRAPPED, "<system>\n別の文\n</system>"), {}),
    # 末尾のプロンプトが包まれていない (元のプロンプトが機構の文と言えない)
    ("bare_tail", "user", _resumed(_RESUMED_PROMPT, _RESUMED_PROMPT), {}),
    # 建物履歴から転記された行 (ユーザーの発言の来歴)
    ("from_building", "user", _resumed(_WRAPPED, _WRAPPED), {"building_msg_ref": "b1:b1:3"}),
    # ペルソナの名義の行
    ("assistant", "assistant", _resumed(_WRAPPED, _WRAPPED), {}),
    # 書き手の来歴 (schedule_id / slot_id) が無い — 会話の取り込みが本人の発言として
    # 元の時刻で保存した行は、同じ文面を貼り付けた発言でありうる
    ("imported", "user", _resumed(_WRAPPED, _WRAPPED), None),
])
def test_rows_off_the_resumption_shape_are_not_converted(db, tmp_path, row_id, role, content, extra):
    meta = {"tags": ["conversation"]} if extra is None else {**_SLOT_META, **extra}
    with closing(sqlite3.connect(str(db))) as conn:
        _insert(conn, row_id, role, content, meta, created_at=100)
        conn.commit()

    _migrate(db, tmp_path, cutover=200)

    new_role, new_content, meta = _rows(db)[row_id]
    assert (new_role, new_content) == (role, content)
    assert SYSTEM_NOTICE_KEY not in meta


def test_resumption_of_a_schedule_is_converted(db, tmp_path):
    """スケジュールの発火を再開した行 (``schedule_id`` を持つ。要求の metadata が ``with`` を
    ``user`` で上書きしていた世代もある) も同じく変換する。"""
    schedule = "<system>\nスケジュールの実行時刻です。\n朝の散歩\n</system>"
    meta = {"with": ["user"], "schedule_id": 2, "schedule_type": "periodic", "tags": ["conversation"]}
    with closing(sqlite3.connect(str(db))) as conn:
        _insert(conn, "sched", "user", _resumed(schedule, schedule), dict(meta), created_at=100)
        conn.commit()

    _migrate(db, tmp_path, cutover=200)

    inner = "スケジュールの実行時刻です。\n朝の散歩"
    assert _rows(db)["sched"] == (
        "system", f"{_RESUMED_HEAD}{inner}\n\n{inner}", {**meta, SYSTEM_NOTICE_KEY: "schedule"},
    )


def test_resumption_rows_after_the_cutover_stay_for_review(db, tmp_path):
    """ユーザー名義の行なので、時刻の境より後なら形が合っても確認待ち。"""
    with closing(sqlite3.connect(str(db))) as conn:
        _insert(conn, "late", "user", _resumed(_WRAPPED, _WRAPPED), dict(_SLOT_META), created_at=300)
        conn.commit()

    _migrate(db, tmp_path, cutover=200)

    role, content, meta = _rows(db)["late"]
    assert (role, content) == ("user", _resumed(_WRAPPED, _WRAPPED))
    assert meta[SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON
