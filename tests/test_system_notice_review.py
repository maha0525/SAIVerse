"""確認待ちの行を所有者が画面で確定する (saiverse/system_notice_review.py)。

docs/intent/system_notice_delivery.md「追補」の「確認待ちの行の確定 (利用者の画面)」。
ここで固定するのは:

- 確認待ちの印を持つ行だけを全ペルソナから集め、同じ型 (role・書き出し・タグ・
  理由・選べる操作) で束ねる。各行は全文・添付・ハッシュ (version) を持つ
- 行ごとの「選べる操作」: 「通常のユーザー発話に変換」は role=user の行にだけ
  出す (包みが本文全体を囲んでいなければ、本文を変えずに発話へ戻す)。発話者の
  添付を持つ行には「正式に変換」を出さず、API でも拒否する
- 4 つの操作の結果 (role・本文・通知の印・確認待ちの印・確定の記録)。発話への
  変換は実会話から外す印 (機構のタグ・line_role) も外して記録し、包みの内側の
  本文を一字も変えない
- 選べない操作・見つからない行・確定済みの行・一覧の後に中身が変わった行は
  書き換えずに拒否する
- 建物の履歴から転記された行の削除は、建物の側の取り込み済みの印を先に確実にし、
  取り込み直しで行が復活しない
- 書き手が決まっていない行 (確認待ち・「そのままにする」) は、あらすじと
  Memopedia の材料に発話として渡らない (SQL と Python の判定は同じ行を選ぶ)
- 画面の警告は件数と読めなかった記憶に応じて置き直し、どちらも無くなったら外す

記憶 DB は一時ディレクトリに作り、書き込みは本物の ``SAIMemoryAdapter.rewrite_message``
(同じ書き込みの取引) を通す。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sai_memory.db_locks import lock_for_path
from sai_memory.memory.storage import (
    SYSTEM_NOTICE_KEY,
    SYSTEM_NOTICE_REVIEW_KEY,
    SYSTEM_NOTICE_REVIEWED_KEY,
    Message,
    _author_unsettled_sql,
    filter_real_conversation_ids,
    init_db,
    is_author_unsettled,
    is_system_notice,
)
from saiverse import system_notice_review as review
from saiverse_memory.adapter import SAIMemoryAdapter

UNCLASSIFIED = "系統を判定できなかった"
AMBIGUOUS = "タグの対応が曖昧"
UNKNOWN_SYSTEM = "系統を推定できなかった role=system"


def _insert(conn, mid, role, content, metadata, created_at, line_role=None):
    raw = metadata if isinstance(metadata, str) else (
        json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    )
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, metadata, line_role) "
        "VALUES (?, 't1', ?, ?, 'r', ?, ?, ?)",
        (mid, role, content, created_at, raw, line_role),
    )


def _make_persona(home: Path, pid: str, rows) -> Path:
    db_path = home / "personas" / pid / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db_path))
    for i, row in enumerate(rows):
        mid, role, content, metadata = row[:4]
        line_role = row[4] if len(row) > 4 else None
        _insert(conn, mid, role, content, metadata, 1000 + i, line_role)
    conn.commit()
    conn.close()
    return db_path


def _adapter(db_path: Path) -> SAIMemoryAdapter:
    adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
    adapter.conn = init_db(str(db_path), check_same_thread=False)
    adapter._db_lock = lock_for_path(str(db_path))
    return adapter


def _raw_row(db_path: Path, mid: str):
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT role, content, metadata, line_role FROM messages WHERE id=?", (mid,),
        ).fetchone()


def _row(db_path: Path, mid: str):
    row = _raw_row(db_path, mid)
    if row is None:
        return None
    return row[0], row[1], json.loads(row[2]) if row[2] else None


def _version(db_path: Path, mid: str) -> str:
    """一覧が返すのと同じハッシュ (今の行から作る)。行が無ければ仮の値。"""
    row = _raw_row(db_path, mid)
    if row is None:
        return "missing"
    try:
        meta = json.loads(row[2]) if row[2] else None
    except ValueError:
        meta = None
    return review.row_version(row[0], row[1], meta if isinstance(meta, dict) else None)


@pytest.fixture()
def world(tmp_path):
    a_rows = [
        ("a-tool", "user", "<system>Email sent.</system>",
         {"tags": ["conversation", "tool", "pulse:1"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
        ("a-assistant", "assistant", "<system>(memo)</system>",
         {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
        ("a-ambiguous", "user", "<system>A</system> B",
         {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: AMBIGUOUS}),
        ("a-system", "system", "spell result",
         {"tags": ["mystery"], SYSTEM_NOTICE_REVIEW_KEY: UNKNOWN_SYSTEM}),
        ("a-plain", "user", "hello", {"tags": ["conversation"]}),
        ("a-settled", "user", "<system>old</system>",
         {"tags": ["conversation"], SYSTEM_NOTICE_REVIEWED_KEY: "keep"}),
        # 印の文字列を含むが JSON として読めない行は一覧に出さない (読み飛ばす)
        ("a-broken", "user", "x", '{"__system_notice_review__": '),
    ]
    b_rows = [
        ("b-tool", "user", "<system>Email sent.</system>",
         {"tags": ["conversation", "tool", "pulse:9"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
    ]
    a_db = _make_persona(tmp_path, "alice", a_rows)
    b_db = _make_persona(tmp_path, "bob", b_rows)
    # 本体 DB に登録の無いペルソナの記憶 (確定の操作が届かないので数えない)
    _make_persona(tmp_path, "orphan", [
        ("o-1", "user", "<system>x</system>",
         {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
    ])
    adapters = {"alice": _adapter(a_db), "bob": _adapter(b_db)}
    manager = SimpleNamespace(
        personas={
            pid: SimpleNamespace(persona_name=pid.title(), sai_memory=ad)
            for pid, ad in adapters.items()
        },
        startup_alerts=[{"id": "other", "level": "info", "title": "t", "message": "m"}],
    )
    db_of = {"alice": a_db, "bob": b_db}
    yield SimpleNamespace(
        home=tmp_path, a_db=a_db, b_db=b_db, adapters=adapters, manager=manager, db_of=db_of,
        dbs=review.known_persona_dbs(manager, tmp_path / "personas"),
    )
    for ad in adapters.values():
        ad.conn.close()


def _open(world):
    @contextlib.contextmanager
    def opener(pid):
        if pid not in world.adapters:
            raise LookupError(pid)
        yield world.adapters[pid]
    return opener


def _apply(world, action, pairs, release=None):
    """画面と同じく、今の行のハッシュを添えて当てる。"""
    items = [
        (pid, mid, _version(world.db_of[pid], mid) if pid in world.db_of else "x")
        for pid, mid in pairs
    ]
    return review.apply_review_action(action, items, _open(world), release)


# ---------------------------------------------------------------------------
# 一覧と束ね方
# ---------------------------------------------------------------------------


def test_known_persona_dbs_skip_memory_without_a_persona(world):
    assert [(pid, name) for pid, name, _ in world.dbs] == [("alice", "Alice"), ("bob", "Bob")]


def test_listing_collects_only_pending_rows_and_groups_by_shape(world):
    listing = review.build_review_listing(world.dbs)
    assert listing["total"] == 5
    assert listing["errors"] == []
    ids = {r["message_id"] for g in listing["groups"] for r in g["rows"]}
    assert ids == {"a-tool", "a-assistant", "a-ambiguous", "a-system", "b-tool"}

    # 同じ型の行は、ペルソナをまたいで一つの束になる (pulse: のタグは鍵に含めない)
    top = listing["groups"][0]
    assert top["count"] == 2
    assert top["head"] == "Email sent."
    assert top["tags"] == ["conversation", "tool"]
    assert top["reason"] == UNCLASSIFIED
    assert {p["persona_id"]: p["count"] for p in top["personas"]} == {"alice": 1, "bob": 1}
    assert top["actions"] == ["keep", "system", "user", "delete"]
    # 本文は表示用で、保存された本文そのまま (剥がしていない)
    assert top["rows"][0]["content"] == "<system>Email sent.</system>"
    assert top["rows"][0]["version"] == _version(world.a_db, "a-tool")
    assert top["rows"][0]["attachments"] == []
    assert top["rows"][0]["attachment_counts"] == {}
    assert len(listing["groups"]) == 4


def test_listing_returns_the_full_text(tmp_path):
    """各行の全文を返す (所有者が束ごとに確定する前に、全行の全文を読めるように)。"""
    long_body = "<system>" + "x" * 5000 + "</system>"
    db = _make_persona(tmp_path, "p", [
        ("m", "user", long_body, {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
    ])
    listing = review.build_review_listing([("p", "P", db)])
    row = listing["groups"][0]["rows"][0]
    assert row["content"] == long_body
    assert "content_truncated" not in row


def test_allowed_actions_offer_user_only_for_user_rows():
    pending = {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}
    full = ["keep", "system", "user", "delete"]
    no_user = ["keep", "system", "delete"]
    assert review.allowed_actions("user", "<system>a</system>", pending) == full
    # ペルソナや機構の文をユーザーの名義にしない
    assert review.allowed_actions("assistant", "<system>a</system>", pending) == no_user
    assert review.allowed_actions("system", "plain", pending) == no_user
    # ユーザー本人が本文の途中に <system> を打った発言など、包みが本文全体を
    # 囲んでいない role=user の行にも出す (本文を変えずに発話へ戻す)
    assert review.allowed_actions("user", "<system>a</system> b", pending) == full
    assert review.allowed_actions("user", "plain", pending) == full
    # 確認待ちでない行には何も出さない
    assert review.allowed_actions("user", "<system>a</system>", {}) == []
    assert review.allowed_actions("user", "<system>a</system>", None) == []


# ---------------------------------------------------------------------------
# 添付 (指摘 3)
# ---------------------------------------------------------------------------


AUDIO = {"type": "audio", "uri": "saiverse://audio/voice.ogg", "mime_type": "audio/ogg"}
IMAGE = {"uri": "saiverse://image/pic.png"}
DOC = {"path": "docs/note.txt"}


def test_rows_with_speaker_attachments_cannot_become_official_notices():
    pending = {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}
    for field_name, item in (("media", AUDIO), ("images", IMAGE), ("documents", DOC)):
        meta = {**pending, field_name: [item]}
        assert review.allowed_actions("user", "<system>a</system>", meta) == ["keep", "user", "delete"]
        assert review.allowed_actions("assistant", "x", meta) == ["keep", "delete"]
    # ファイルを指す欄の無い項目も、行ごと role を変えれば一緒に持ち主が変わる
    assert review.allowed_actions("user", "x", {**pending, "media": [{"type": "audio"}]}) == [
        "keep", "user", "delete",
    ]
    # 機構が書いた role=system の行の添付は機構のもの
    assert review.allowed_actions("system", "x", {**pending, "media": [AUDIO]}) == [
        "keep", "system", "delete",
    ]


def test_listing_returns_attachment_kinds_and_counts(tmp_path):
    db = _make_persona(tmp_path, "p", [
        ("m", "user", "<system>音声入力を受信しました。</system>", {
            SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED,
            "media": [AUDIO, {"type": "image", "uri": "saiverse://image/a.png"}],
            "images": [IMAGE],
            "documents": [DOC],
        }),
    ])
    row = review.build_review_listing([("p", "P", db)])["groups"][0]["rows"][0]
    assert row["attachment_counts"] == {"audio": 1, "image": 2, "document": 1}
    assert row["attachments"] == [
        {"kind": "audio", "ref": "saiverse://audio/voice.ogg"},
        {"kind": "image", "ref": "saiverse://image/a.png"},
        {"kind": "image", "ref": "saiverse://image/pic.png"},
        {"kind": "document", "ref": "docs/note.txt"},
    ]
    assert row["actions"] == ["keep", "user", "delete"]


def test_api_rejects_official_conversion_of_a_row_with_attachments(tmp_path):
    db = _make_persona(tmp_path, "p", [
        ("m", "user", "<system>a</system>",
         {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED, "media": [AUDIO]}),
    ])
    adapter = _adapter(db)
    try:
        before = _raw_row(db, "m")

        @contextlib.contextmanager
        def opener(_pid):
            yield adapter

        result = review.apply_review_action("system", [("p", "m", _version(db, "m"))], opener)
        assert result["applied"] == []
        assert result["rejected"] == [{"persona_id": "p", "message_id": "m", "reason": "not_allowed"}]
        assert _raw_row(db, "m") == before
    finally:
        adapter.conn.close()


# ---------------------------------------------------------------------------
# 4 つの操作
# ---------------------------------------------------------------------------


def test_keep_records_the_decision_and_stays_out_of_real_conversation(world):
    result = _apply(world, "keep", [("alice", "a-tool")])
    assert result["rejected"] == []
    role, content, meta = _row(world.a_db, "a-tool")
    assert (role, content) == ("user", "<system>Email sent.</system>")
    assert SYSTEM_NOTICE_REVIEW_KEY not in meta
    assert meta[SYSTEM_NOTICE_REVIEWED_KEY] == "keep"
    assert meta["tags"] == ["conversation", "tool", "pulse:1"]
    # 「そのままにする」の行は偽タグに見える本文のままなので、実会話から外れたまま
    conn = world.adapters["alice"].conn
    assert filter_real_conversation_ids(conn, ["a-tool", "a-plain"]) == {"a-plain"}


def test_system_converts_to_owner_confirmed_notice(world):
    _apply(world, "system", [("alice", "a-tool"), ("alice", "a-system")])
    role, content, meta = _row(world.a_db, "a-tool")
    assert (role, content) == ("system", "Email sent.")
    assert meta[SYSTEM_NOTICE_KEY] == review.OWNER_CONFIRMED_KIND
    assert meta[SYSTEM_NOTICE_REVIEWED_KEY] == "system"
    assert SYSTEM_NOTICE_REVIEW_KEY not in meta
    assert is_system_notice({"role": role, "content": content, "metadata": meta})
    # 包みの無い role=system 行は本文を変えずに印だけ付ける
    role, content, meta = _row(world.a_db, "a-system")
    assert (role, content) == ("system", "spell result")
    assert meta[SYSTEM_NOTICE_KEY] == review.OWNER_CONFIRMED_KIND


def test_system_keeps_body_when_the_wrapper_cannot_be_stripped(world):
    _apply(world, "system", [("alice", "a-ambiguous")])
    role, content, meta = _row(world.a_db, "a-ambiguous")
    assert (role, content) == ("system", "<system>A</system> B")
    assert meta[SYSTEM_NOTICE_REVIEWED_KEY] == "system"


def test_user_strips_the_wrapper_and_keeps_role(world):
    _apply(world, "user", [("bob", "b-tool")])
    role, content, meta = _row(world.b_db, "b-tool")
    assert (role, content) == ("user", "Email sent.")
    assert meta[SYSTEM_NOTICE_REVIEWED_KEY] == "user"
    assert SYSTEM_NOTICE_REVIEW_KEY not in meta
    assert SYSTEM_NOTICE_KEY not in meta
    # 機構のタグも line_role も持っていなかったので、外した記録は書かない
    assert review.REVIEW_CLEARED_KEY not in meta
    # 発話として確定したので実会話に戻る
    conn = world.adapters["bob"].conn
    assert filter_real_conversation_ids(conn, ["b-tool"]) == {"b-tool"}


def test_user_keeps_the_body_verbatim_when_the_wrapper_cannot_be_stripped(world):
    # 本人が本文の途中に <system> を打った発言は、打ったとおりの本文で発話へ戻す
    _apply(world, "user", [("alice", "a-ambiguous")])
    role, content, meta = _row(world.a_db, "a-ambiguous")
    assert (role, content) == ("user", "<system>A</system> B")
    assert meta[SYSTEM_NOTICE_REVIEWED_KEY] == "user"
    assert SYSTEM_NOTICE_REVIEW_KEY not in meta
    conn = world.adapters["alice"].conn
    assert filter_real_conversation_ids(conn, ["a-ambiguous"]) == {"a-ambiguous"}


def test_user_removes_only_the_tags_and_keeps_inner_whitespace(tmp_path):
    """発話へ戻すときは開きと閉じだけを外す (包みの内側の前後の空白・改行も本文)。"""
    body = "<system>\n  こんにちは\n\n  </system>\n"
    db = _make_persona(tmp_path, "p", [
        ("m", "user", body, {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
        ("n", "user", "<system> 地の文 <system>引用</system> </system>",
         {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
        # 剥がした結果がまだ包みで始まる行は、包みか引用かを決められないので本文を変えない
        ("o", "user", "<system> <system>引用</system> 地の文 </system>",
         {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
    ])
    adapter = _adapter(db)
    try:
        @contextlib.contextmanager
        def opener(_pid):
            yield adapter

        review.apply_review_action(
            "user", [("p", mid, _version(db, mid)) for mid in ("m", "n", "o")], opener,
        )
        assert _row(db, "m")[1] == "\n  こんにちは\n\n  \n"
        # 内側で対になった引用のタグは本文の一部として残す
        assert _row(db, "n")[1] == " 地の文 <system>引用</system> "
        assert _row(db, "o")[1] == "<system> <system>引用</system> 地の文 </system>"
    finally:
        adapter.conn.close()


def test_user_lifts_the_marks_that_keep_the_row_out_of_real_conversation(tmp_path):
    """発話へ戻した行は、機構のタグと line_role の除外からも外れる (外したものは残す)。"""
    db = _make_persona(tmp_path, "p", [
        ("m", "user", "<system>hi</system>",
         {"tags": ["conversation", "event_message", "spell", "internal"],
          SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED},
         "meta_judgment"),
    ])
    adapter = _adapter(db)
    try:
        @contextlib.contextmanager
        def opener(_pid):
            yield adapter

        assert filter_real_conversation_ids(adapter.conn, ["m"]) == set()
        result = review.apply_review_action("user", [("p", "m", _version(db, "m"))], opener)
        assert result["rejected"] == []
        role, content, meta, line_role = _raw_row(db, "m")
        meta = json.loads(meta)
        assert (role, content) == ("user", "hi")
        assert meta["tags"] == ["conversation", "internal"]
        assert line_role == review.USER_LINE_ROLE
        assert meta[review.REVIEW_CLEARED_KEY] == {
            "tags": ["event_message", "spell"], "line_role": "meta_judgment",
        }
        assert filter_real_conversation_ids(adapter.conn, ["m"]) == {"m"}
    finally:
        adapter.conn.close()


def test_delete_removes_row_and_embeddings(world):
    conn = world.adapters["alice"].conn
    conn.execute(
        "INSERT INTO message_embeddings (message_id, chunk_index, vector) VALUES ('a-tool', 0, '[]')",
    )
    conn.commit()
    result = _apply(world, "delete", [("alice", "a-tool")])
    assert result["applied"] == [
        {"persona_id": "alice", "message_id": "a-tool", "result": "deleted"},
    ]
    assert _row(world.a_db, "a-tool") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM message_embeddings WHERE message_id='a-tool'",
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 削除と建物の履歴からの取り込み直し (指摘 8)
# ---------------------------------------------------------------------------


@pytest.fixture()
def building_world(tmp_path):
    """建物の履歴の 1 行が記憶へ転記済みで、建物の側の取り込み済みの印だけが無い状態。

    転記は「記憶へ追記 → 建物の印」の順で、印の書き込みに失敗すると次の転記が
    記憶の側の ``building_msg_ref`` を照会して重複を避ける
    (builtin_data/tools/get_building_messages.py)。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.building_messages import insert_building_message
    from database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    building_id = "room:a"  # 区切り文字を含む建物の id でも参照を解ける
    saved = insert_building_message(session_factory, building_id, {
        "role": "user", "content": "<system>hello</system>", "heard_by": ["p"],
    })
    ref = f"{building_id}:{saved['message_id']}"
    db = _make_persona(tmp_path, "p", [
        ("m", "user", "<system>hello</system>",
         {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED, "building_msg_ref": ref}),
    ])
    adapter = _adapter(db)

    @contextlib.contextmanager
    def opener(_pid):
        yield adapter

    yield SimpleNamespace(
        db=db, adapter=adapter, opener=opener, session_factory=session_factory,
        building_id=building_id, message_id=saved["message_id"], ref=ref,
    )
    adapter.conn.close()
    engine.dispose()


def _ingested_by(world_):
    from database.building_messages import fetch_building_messages

    rows = fetch_building_messages(world_.session_factory, world_.building_id)
    return [r for r in rows if r["message_id"] == world_.message_id][0]["ingested_by"]


def test_delete_marks_the_building_row_ingested_so_it_is_not_transcribed_again(building_world):
    from builtin_data.tools import get_building_messages as ingest

    w = building_world
    assert _ingested_by(w) == []
    result = review.apply_review_action(
        "delete", [("p", "m", _version(w.db, "m"))], w.opener,
        lambda pid, ref: review.mark_building_ref_ingested(w.session_factory, ref, pid),
    )
    assert result["applied"] == [{"persona_id": "p", "message_id": "m", "result": "deleted"}]
    assert _row(w.db, "m") is None
    assert _ingested_by(w) == ["p"]

    # 次の転記はこの建物の行を取り込み済みとして飛ばす (記憶に行が戻らない)
    from database.building_messages import fetch_building_messages

    building_row = [
        r for r in fetch_building_messages(w.session_factory, w.building_id)
        if r["message_id"] == w.message_id
    ][0]
    appended = []
    history = SimpleNamespace(add_to_persona_only=lambda entry, **_: appended.append(entry) or ("ok", "x"))
    outcome = ingest._transcribe_message(
        building_row,
        persona=SimpleNamespace(sai_memory=w.adapter),
        manager=SimpleNamespace(SessionLocal=w.session_factory),
        persona_id="p", building_id=w.building_id, history_manager=history,
        id_to_name_map={}, check_provenance=True,
    )
    assert outcome[0] == "consumed"
    assert appended == []


def test_delete_is_refused_when_the_building_mark_cannot_be_made(building_world):
    w = building_world
    before = _raw_row(w.db, "m")
    # 本体 DB へ届かない (印を確実にできない) なら消さない
    result = review.apply_review_action(
        "delete", [("p", "m", _version(w.db, "m"))], w.opener,
        lambda pid, ref: review.mark_building_ref_ingested(None, ref, pid),
    )
    assert result["applied"] == []
    assert result["rejected"] == [{"persona_id": "p", "message_id": "m", "reason": "error"}]
    assert _raw_row(w.db, "m") == before
    # 印を付ける手段を渡されていなければ、転記された行は消さない
    result = review.apply_review_action("delete", [("p", "m", _version(w.db, "m"))], w.opener)
    assert result["rejected"] == [{"persona_id": "p", "message_id": "m", "reason": "error"}]
    assert _raw_row(w.db, "m") == before


# ---------------------------------------------------------------------------
# 拒否
# ---------------------------------------------------------------------------


def test_disallowed_actions_are_rejected_without_writing(world):
    before = {mid: _row(world.a_db, mid) for mid in ("a-assistant", "a-system", "a-plain", "a-settled")}
    result = _apply(world, "user", [
        ("alice", "a-assistant"),  # role=assistant
        ("alice", "a-system"),  # role=system
        ("alice", "a-plain"),  # 確認待ちでない
        ("alice", "a-settled"),  # 確定済み
        ("alice", "missing"),
        ("nobody", "x"),
    ])
    assert result["applied"] == []
    reasons = {(r["persona_id"], r["message_id"]): r["reason"] for r in result["rejected"]}
    assert reasons == {
        ("alice", "a-assistant"): "not_allowed",
        ("alice", "a-system"): "not_allowed",
        ("alice", "a-plain"): "not_allowed",
        ("alice", "a-settled"): "not_allowed",
        ("alice", "missing"): "not_found",
        ("nobody", "x"): "unavailable",
    }
    for mid, row in before.items():
        assert _row(world.a_db, mid) == row


def test_second_apply_on_the_same_row_is_rejected(world):
    _apply(world, "keep", [("alice", "a-tool")])
    result = _apply(world, "delete", [("alice", "a-tool")])
    assert result["rejected"] == [
        {"persona_id": "alice", "message_id": "a-tool", "reason": "not_allowed"},
    ]
    assert _row(world.a_db, "a-tool") is not None


def test_a_row_changed_after_listing_is_rejected_without_writing(world):
    """一覧の後に別の書き手が本文や metadata を変えた行には、所有者の選択を当てない。"""
    listing = review.build_review_listing(world.dbs)
    seen = {
        (r["persona_id"], r["message_id"]): r["version"]
        for g in listing["groups"] for r in g["rows"]
    }
    # 記憶ブラウザの編集が本文を書き換えた (確認待ちの印は残る)
    conn = world.adapters["alice"].conn
    conn.execute("UPDATE messages SET content='<system>別の本文</system>' WHERE id='a-tool'")
    # 別の行は metadata だけが変わった
    meta = _row(world.b_db, "b-tool")[2]
    meta["tags"].append("edited")
    world.adapters["bob"].conn.execute(
        "UPDATE messages SET metadata=? WHERE id='b-tool'", (json.dumps(meta, ensure_ascii=False),),
    )
    conn.commit()
    world.adapters["bob"].conn.commit()
    before = (_raw_row(world.a_db, "a-tool"), _raw_row(world.b_db, "b-tool"))
    items = [
        ("alice", "a-tool", seen[("alice", "a-tool")]),
        ("bob", "b-tool", seen[("bob", "b-tool")]),
        ("alice", "a-ambiguous", seen[("alice", "a-ambiguous")]),
    ]
    result = review.apply_review_action("delete", items, _open(world))
    assert {(r["message_id"], r["reason"]) for r in result["rejected"]} == {
        ("a-tool", "changed"), ("b-tool", "changed"),
    }
    assert [a["message_id"] for a in result["applied"]] == ["a-ambiguous"]
    assert (_raw_row(world.a_db, "a-tool"), _raw_row(world.b_db, "b-tool")) == before


def test_unknown_action_raises(world):
    with pytest.raises(ValueError):
        _apply(world, "rename", [("alice", "a-tool")])


# ---------------------------------------------------------------------------
# あらすじ・Memopedia の材料 (指摘 5)
# ---------------------------------------------------------------------------


UNSETTLED_CASES = [
    ({SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}, True),
    ({SYSTEM_NOTICE_REVIEW_KEY: ""}, True),
    ({SYSTEM_NOTICE_REVIEWED_KEY: "keep"}, True),
    ({SYSTEM_NOTICE_REVIEWED_KEY: "user"}, False),
    ({SYSTEM_NOTICE_REVIEWED_KEY: "system"}, False),
    ({SYSTEM_NOTICE_REVIEW_KEY: None}, False),
    ({"tags": ["conversation"]}, False),
    (None, False),
]


def test_python_and_sql_forms_of_the_unsettled_author_judgement_agree(tmp_path):
    db = _make_persona(tmp_path, "p", [
        (f"m{i}", "user", "x", meta) for i, (meta, _expected) in enumerate(UNSETTLED_CASES)
    ])
    with contextlib.closing(sqlite3.connect(db)) as conn:
        by_sql = {
            r[0] for r in conn.execute(f"SELECT id FROM messages WHERE {_author_unsettled_sql()}")
        }
    by_python = {f"m{i}" for i, (meta, _e) in enumerate(UNSETTLED_CASES) if is_author_unsettled(meta)}
    expected = {f"m{i}" for i, (_m, e) in enumerate(UNSETTLED_CASES) if e}
    assert by_sql == by_python == expected


def _message(mid, role, content, metadata):
    return Message(
        id=mid, thread_id="t1", role=role, content=content, resource_id="r",
        created_at=1000, metadata=metadata,
    )


def test_unsettled_rows_are_not_given_to_chronicle_as_user_speech():
    from sai_memory.arasuji.generator import (
        _format_messages_for_prompt,
        material_chars,
        material_len,
        material_text,
    )

    pending = _message("p", "user", "<system>機構の文</system>",
                       {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED})
    kept = _message("k", "user", "<system>残した文</system>",
                    {"tags": ["conversation"], SYSTEM_NOTICE_REVIEWED_KEY: "keep"})
    settled = _message("u", "user", "本人の発話",
                       {"tags": ["conversation"], SYSTEM_NOTICE_REVIEWED_KEY: "user"})
    for msg in (pending, kept):
        assert material_text(msg) == ""
        assert material_chars(msg) == 0
        # 退場計画の payload の勘定も同じ (U の物差しと実際の材料がずれない)
        assert material_len(msg.content, ("conversation",), msg.metadata) == 0
    text = _format_messages_for_prompt([pending, kept, settled])
    assert "機構の文" not in text and "残した文" not in text
    assert "[user]: 本人の発話" in text


def test_eviction_plan_counts_the_same_material_as_chronicle():
    from sea.eviction_plan import material_message_chars

    payload = [
        {"role": "user", "content": "<system>機構の文</system>",
         "metadata": {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}},
        {"role": "user", "content": "abc", "metadata": {"tags": ["conversation"]}},
    ]
    assert material_message_chars(payload) == 3


def test_unsettled_rows_are_not_given_to_memopedia_as_user_speech():
    from sai_memory.memopedia.generator import format_messages_for_extraction

    text = format_messages_for_extraction([
        _message("p", "user", "<system>機構の文</system>", {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
        _message("k", "user", "残した文", {SYSTEM_NOTICE_REVIEWED_KEY: "keep"}),
        _message("u", "user", "本人の発話", {}),
    ])
    assert "機構の文" not in text and "残した文" not in text
    assert "本人の発話" in text


def test_unsettled_rows_stay_in_chronicle_coverage(tmp_path):
    """材料からは外すが、編纂の対象 (被覆) からは外さない (退場の勘定を変えない)。"""
    from sai_memory.memory.storage import filter_chronicle_eligible_ids

    db = _make_persona(tmp_path, "p", [
        ("m", "user", "<system>x</system>", {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
    ])
    with contextlib.closing(init_db(str(db))) as conn:
        assert filter_chronicle_eligible_ids(conn, ["m"]) == {"m"}


# ---------------------------------------------------------------------------
# 画面の警告 (指摘 10)
# ---------------------------------------------------------------------------


def test_alert_follows_the_pending_count(world):
    manager = world.manager
    assert review.refresh_review_alert(manager, world.dbs) == 5
    ids = [a["id"] for a in manager.startup_alerts]
    assert ids == ["other", review.REVIEW_ALERT_ID]
    alert = manager.startup_alerts[1]
    assert alert["level"] == "warning"
    assert alert["title"] == {"$ui": "api.systemNoticeReview.alertTitle", "params": {"count": 5}}

    # 件数が変わっても同じ位置で差し替える
    manager.startup_alerts.append({"id": "later"})
    _apply(world, "keep", [("alice", "a-tool"), ("bob", "b-tool")])
    assert review.refresh_review_alert(manager, world.dbs) == 3
    assert [a["id"] for a in manager.startup_alerts] == ["other", review.REVIEW_ALERT_ID, "later"]
    assert manager.startup_alerts[1]["title"]["params"]["count"] == 3

    # 全部確定したら外す (他の警告は残す)
    _apply(world, "delete", [("alice", "a-assistant"), ("alice", "a-ambiguous"), ("alice", "a-system")])
    assert review.refresh_review_alert(manager, world.dbs) == 0
    assert [a["id"] for a in manager.startup_alerts] == ["other", "later"]


def test_alert_stays_while_a_memory_cannot_be_read(tmp_path):
    """読めなかった記憶 DB があるあいだは、0 件でも警告を外さず、読めなかったことを書く。"""
    good = _make_persona(tmp_path, "good", [("m", "user", "hello", {})])
    broken = tmp_path / "personas" / "broken" / "memory.db"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"this is not a sqlite database" * 100)
    manager = SimpleNamespace(startup_alerts=[{"id": review.REVIEW_ALERT_ID}])
    dbs = [("good", "Good", good), ("broken", "Broken", broken)]

    count, errors = review.count_review_rows(dbs)
    assert count == 0
    assert [(e["persona_id"], e["persona_name"]) for e in errors] == [("broken", "Broken")]

    assert review.refresh_review_alert(manager, dbs) == 0
    [alert] = manager.startup_alerts
    assert alert["id"] == review.REVIEW_ALERT_ID
    assert alert["title"] == {"$ui": "api.systemNoticeReview.alertUnreadableTitle", "params": {}}
    assert alert["message"] == {
        "$ui": "api.systemNoticeReview.alertMessageUnreadable", "params": {"names": "Broken"},
    }
    # 読めるようになって 0 件なら外す
    assert review.refresh_review_alert(manager, [("good", "Good", good)]) == 0
    assert manager.startup_alerts == []


def test_alert_wording_lives_in_the_frontend_message_table():
    """警告の文言は frontend の文言表にあり、両言語で同じ引数を持つ。"""
    table = json.loads(
        (Path(__file__).resolve().parent.parent / "frontend" / "src" / "i18n" / "messages.json")
        .read_text(encoding="utf-8"),
    )
    for alert in (review.review_alert(3), review.review_alert(0, ["A", "B"])):
        for field_name in ("title", "message"):
            key = alert[field_name]["$ui"]
            assert key.startswith("api.")
            assert table[key]["ja"] and table[key]["en"]
            for locale in ("ja", "en"):
                for param in alert[field_name]["params"]:
                    assert "{" + param + "}" in table[key][locale]


def test_no_alert_when_nothing_is_pending(tmp_path):
    db = _make_persona(tmp_path, "p", [("m", "user", "hello", {"tags": ["conversation"]})])
    manager = SimpleNamespace(startup_alerts=[])
    assert review.refresh_review_alert(manager, [("p", "P", db)]) == 0
    assert manager.startup_alerts == []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_lists_and_applies_through_get_adapter(world, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import system as system_routes
    from saiverse import app_state

    monkeypatch.setenv("SAIVERSE_HOME", str(world.home))
    monkeypatch.setattr(app_state, "manager", world.manager)
    app = FastAPI()
    app.include_router(system_routes.router, prefix="/api/system")
    client = TestClient(app)

    listing = client.get("/api/system/notice-review").json()
    assert listing["total"] == 5
    version = {
        r["message_id"]: r["version"] for g in listing["groups"] for r in g["rows"]
    }

    bad = client.post("/api/system/notice-review/apply", json={"action": "rename", "items": []})
    assert bad.status_code == 400
    # ハッシュの無い要求は受け付けない (画面が見た内容に当てる約束を API でも守る)
    no_version = client.post("/api/system/notice-review/apply", json={
        "action": "keep", "items": [{"persona_id": "alice", "message_id": "a-tool"}],
    })
    assert no_version.status_code == 422

    res = client.post("/api/system/notice-review/apply", json={
        "action": "user",
        "items": [
            {"persona_id": "alice", "message_id": "a-tool", "version": version["a-tool"]},
            {"persona_id": "alice", "message_id": "a-assistant", "version": version["a-assistant"]},
            {"persona_id": "bob", "message_id": "b-tool", "version": "stale"},
        ],
    }).json()
    assert [a["message_id"] for a in res["applied"]] == ["a-tool"]
    assert [(r["message_id"], r["reason"]) for r in res["rejected"]] == [
        ("a-assistant", "not_allowed"), ("b-tool", "changed"),
    ]
    assert res["remaining"] == 4
    assert _row(world.a_db, "a-tool")[:2] == ("user", "Email sent.")
    assert any(a["id"] == review.REVIEW_ALERT_ID for a in world.manager.startup_alerts)
