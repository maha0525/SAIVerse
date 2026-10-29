"""確認待ちの行を所有者が画面で確定する (saiverse/system_notice_review.py)。

docs/intent/system_notice_delivery.md「追補」の「確認待ちの行の確定 (利用者の画面)」。
ここで固定するのは:

- 確認待ちの印を持つ行だけを全ペルソナから集め、同じ型 (role・書き出し・タグ・
  理由・選べる操作) で束ねる。各行は全文・添付・ハッシュ (version) を持つ
- 行ごとの「選べる操作」: 「通常のユーザー発話に変換」は role=user の行にだけ
  出す (包みが本文全体を囲んでいなければ、本文を変えずに発話へ戻す)。発話者の
  添付を持つ行には「正式に変換」を出さず、API でも拒否する。例外は移行の規則 5 の
  形の行で、「正式に変換」は移行と同じ二行分割 (通知の行と、本文の無い音声の行) に
  なる。音声の行を通知の直後に並べられなければ出さない
- 4 つの操作の結果 (role・本文・通知の印・確認待ちの印・確定の記録)。発話への
  変換は実会話から外す印 (機構のタグ・line_role) も外して記録し、包みの内側の
  本文を一字も変えない
- 選べない操作・見つからない行・確定済みの行・一覧の後に中身 (本文・metadata・
  created_at・line_role) が変わった行は書き換えずに拒否する
- 建物の履歴から転記された行の削除は、建物の側の取り込み済みの印を先に確実にし、
  取り込み直しで行が復活しない。転記が建物の行を読んだ後に削除が割り込んでも、
  転記は記憶へ足す直前に印を読み直して足さない。複数のペルソナの印が同じ建物の行に
  並行して付いても、どちらも失われない
- 書き手が決まっていない行 (確認待ち・「そのままにする」) は、あらすじと
  Memopedia の材料に発話として渡らない (SQL と Python の判定は同じ行を選ぶ)。
  記憶の検索・想起 (キーワード検索・意味検索・周辺の行の広げ・鮮明想起) の
  候補にも入らない
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
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        created_at = conn.execute("SELECT created_at FROM messages WHERE id=?", (mid,)).fetchone()[0]
    return review.row_version(
        row[0], row[1], meta if isinstance(meta, dict) else None, created_at, row[3],
    )


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


def test_relisting_replaces_a_stale_alert_without_any_apply(world, monkeypatch):
    """一時的に読めなかった記憶が読めるようになった後、一覧を取り直すだけで警告が直る。

    確定する行が無いと確定の操作は起きないので、取り直しの経路でも置き直す
    (PR #434 のレビューの指摘)。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import system as system_routes
    from saiverse import app_state

    monkeypatch.setenv("SAIVERSE_HOME", str(world.home))
    monkeypatch.setattr(app_state, "manager", world.manager)
    app = FastAPI()
    app.include_router(system_routes.router, prefix="/api/system")
    client = TestClient(app)

    # 起動時に記憶を読めなかったときの警告 (確認待ち 0 件・読めないペルソナあり)。
    # ほかの警告は位置ごとそのまま残る
    other = {"id": "other_alert", "level": "warning", "title": "t", "message": "m", "details": {}}
    world.manager.startup_alerts = [other, review.review_alert(0, ["alice"])]
    listing = client.get("/api/system/notice-review").json()
    assert listing["total"] == 5
    assert world.manager.startup_alerts == [other, review.review_alert(5, [])]

    # 確認待ちが全部片づいていれば、取り直しで警告が外れる
    for db in (world.a_db, world.b_db):
        with contextlib.closing(sqlite3.connect(str(db))) as conn:
            conn.execute(
                "UPDATE messages SET metadata = json_remove(metadata, ?) WHERE json_valid(metadata)",
                (f'$."{SYSTEM_NOTICE_REVIEW_KEY}"',),
            )
            conn.commit()
    world.manager.startup_alerts = [other, review.review_alert(0, ["alice"])]
    assert client.get("/api/system/notice-review").json()["total"] == 0
    assert world.manager.startup_alerts == [other]


# ---------------------------------------------------------------------------
# 記憶を検索・想起してモデルに渡す経路 (Codex 2 巡目)
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """問い合わせを常に ``[1, 0]`` にする埋め込み (行の側は DB に直接書く)。"""

    def __init__(self, *_args, **_kwargs):
        pass

    def embed(self, texts, *, is_query=False):
        return [[1.0, 0.0] for _ in texts]


#: (id, role, 本文, metadata, 埋め込み)。問い合わせ [1, 0] に近いのは書き手が
#: 決まっていない行 (u1・k1・u2) で、決まっている行で一番近いのは s1。u2 は s1 の
#: 直後に並ぶので、周辺の行を広げると s1 の隣に来る。
_RECALL_ROWS = [
    ("n1", "user", "前の発話 りんご", {"tags": ["conversation"]}, [0.0, 1.0]),
    ("s1", "user", "本人の発話 りんご", {"tags": ["conversation"]}, [0.9, 0.1]),
    ("u2", "user", "<system>機構の文 りんご</system>",
     {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}, [1.0, 0.05]),
    ("n2", "assistant", "後ろの応答 りんご", {"tags": ["conversation"]}, [0.0, 1.0]),
    ("u1", "user", "<system>機構の文 りんご</system>",
     {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}, [1.0, 0.0]),
    ("k1", "user", "<system>残した文 りんご</system>",
     {"tags": ["conversation"], SYSTEM_NOTICE_REVIEWED_KEY: "keep"}, [1.0, 0.0]),
]
_SETTLED_IDS = {"n1", "s1", "n2"}


@pytest.fixture()
def recall_db(tmp_path):
    db = _make_persona(tmp_path, "p", [row[:4] for row in _RECALL_ROWS])
    conn = init_db(str(db), check_same_thread=False)
    for mid, _role, _content, _meta, vec in _RECALL_ROWS:
        conn.execute(
            "INSERT INTO message_embeddings (message_id, chunk_index, vector) VALUES (?, 0, ?)",
            (mid, json.dumps(vec)),
        )
    conn.commit()
    yield SimpleNamespace(db=db, conn=conn)
    conn.close()


def _assert_no_unsettled_text(text):
    assert "機構の文" not in text and "残した文" not in text


def test_search_candidates_leave_out_rows_whose_author_is_unsettled(recall_db):
    from sai_memory.memory.storage import get_all_messages_for_search, get_embeddings_for_scope

    conn = recall_db.conn
    found = {m.id for m in get_all_messages_for_search(conn, required_tags=["conversation"])}
    assert found == _SETTLED_IDS
    assert {m.id for m in get_all_messages_for_search(conn)} == _SETTLED_IDS
    corpus = get_embeddings_for_scope(conn, thread_id="t1", required_tags=["conversation"])
    assert {m.id for m, _v, _c in corpus} == _SETTLED_IDS


def test_semantic_recall_leaves_out_unsettled_rows_before_ranking_and_after_expanding(recall_db):
    from sai_memory.memory.recall import semantic_recall, semantic_recall_groups

    kwargs = dict(
        thread_id="t1", resource_id=None, topk=1, range_before=1, range_after=1,
        scope="thread", required_tags=["conversation"],
    )
    msgs = semantic_recall(recall_db.conn, _FakeEmbedder(), "q", **kwargs)
    # 順位付けの前に u1・k1 が外れて s1 が選ばれ、広げた周辺 (前後 1 件ずつ =
    # n1 と u2) からも u2 が外れる。外した u2 の代わりにその先の n2 を取らない
    # (Codex 3 巡目 — 2 巡目のこの期待値は、外してから数える誤った順序を固定していた)
    assert [m.id for m in msgs] == ["n1", "s1"]
    [(seed, bundle, _score)] = semantic_recall_groups(recall_db.conn, _FakeEmbedder(), "q", **kwargs)
    assert seed.id == "s1"
    assert [m.id for m in bundle] == ["n1", "s1"]


def test_memopedia_search_hits_leave_out_unsettled_rows(recall_db, monkeypatch):
    from sai_memory.memory import recall as recall_module
    from sai_memory.memopedia.generator import format_search_results, recall_with_snippets

    monkeypatch.setattr(recall_module, "Embedder", _FakeEmbedder)
    hits = recall_with_snippets(recall_db.conn, "q", topk=10, keywords=["りんご"])
    assert hits
    assert {h.message_id for h in hits} <= _SETTLED_IDS
    _assert_no_unsettled_text(format_search_results(hits, set()))


def _recall_adapter(recall_db):
    adapter = _adapter(recall_db.db)
    adapter.embedder = _FakeEmbedder()
    adapter.settings = SimpleNamespace(
        topk=1, range_before=1, range_after=1, last_messages=0, scope="thread",
    )
    adapter._thread_id = lambda *_a, **_k: "t1"
    return adapter


def test_hybrid_recall_leaves_out_unsettled_rows_and_their_neighbours(recall_db):
    adapter = _recall_adapter(recall_db)
    try:
        text = adapter.recall_hybrid("q", ["りんご"], max_chars=4000)
        snippet = adapter.recall_snippet(None, "q", max_chars=4000)
    finally:
        adapter.conn.close()
    assert "本人の発話" in text
    _assert_no_unsettled_text(text)
    assert "本人の発話" in snippet
    _assert_no_unsettled_text(snippet)


def test_memory_search_brief_leaves_out_unsettled_rows(recall_db, monkeypatch):
    from builtin_data.tools import memory_search_brief as brief

    adapter = _recall_adapter(recall_db)

    @contextlib.contextmanager
    def opener():
        yield adapter

    monkeypatch.setattr(brief, "get_active_persona_id", lambda: "p")
    monkeypatch.setattr(brief, "open_persona_memory", opener)
    try:
        text = brief.memory_search_brief(query="q", keywords=["りんご"], topk=10)
    finally:
        adapter.conn.close()
    assert "本人の発話" in text
    _assert_no_unsettled_text(text)
    for mid in ("u1", "u2", "k1"):
        assert f"({mid})" not in text


def test_message_uri_text_search_leaves_out_unsettled_rows(recall_db):
    from saiverse.uri_resolver import UriResolver

    adapter = _recall_adapter(recall_db)
    resolver = UriResolver(manager=None)
    resolver._get_adapter = lambda _pid: adapter
    resolver._get_active_thread_id = lambda _adapter: "t1"
    try:
        resolved = resolver.resolve("saiverse://self/message/msg?contain=りんご&window=1", persona_id="p")
    finally:
        adapter.conn.close()
    # 本文に「りんご」を含む一番新しい行は k1 (残した文) だが、当たりは n2
    assert resolved.metadata["message_id"] == "n2"
    _assert_no_unsettled_text(resolved.content)


def test_vivid_recall_does_not_quote_unsettled_rows(recall_db):
    from sea.auto_recall import _LedgerItem, _source_message_ids, _vivid_fallback_messages

    conn = recall_db.conn
    conn.execute("CREATE TABLE arasuji_entries (id TEXT PRIMARY KEY, source_ids_json TEXT)")
    conn.execute(
        "INSERT INTO arasuji_entries VALUES ('e1', ?)",
        (json.dumps(["s1", "u1", "u2", "k1"]),),
    )
    conn.commit()
    assert _source_message_ids(conn, "e1") == ["s1"]
    item = _LedgerItem(
        source_type="fragment", source_id="f1", title="りんご", content="", uri="",
        best_score=1.0, chronicle_entry_id="e1",
    )
    quoted = _vivid_fallback_messages(conn, item, max_messages=5)
    assert [q[1] for q in quoted] and all("本人の発話" in q[1] for q in quoted)


# ---------------------------------------------------------------------------
# 削除と転記の競合 (Codex 2 巡目)
# ---------------------------------------------------------------------------


def test_parallel_marks_on_the_same_building_row_keep_both(building_world, monkeypatch):
    """二人のペルソナの印が同じ建物の行に重なっても、どちらも失われない。"""
    from database import building_messages as bm

    w = building_world
    real_loads = json.loads
    state = {"fired": False}

    def loads(text, *args, **kwargs):
        if not state["fired"]:
            state["fired"] = True
            # 一人目が行を読んだ直後に、二人目の印が先に書かれる
            bm.mark_ingested(w.session_factory, w.building_id, w.message_id, "q")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(bm, "json", SimpleNamespace(
        loads=loads, dumps=json.dumps, JSONDecodeError=json.JSONDecodeError,
    ))
    assert bm.mark_ingested(w.session_factory, w.building_id, w.message_id, "p") is True
    assert state["fired"]
    monkeypatch.undo()
    assert sorted(_ingested_by(w)) == ["p", "q"]


@pytest.mark.parametrize("check_provenance", [True, False])
def test_delete_between_reading_the_building_row_and_appending_does_not_restore_the_row(
    building_world, check_provenance,
):
    """転記が建物の行を読んだ後、記憶へ足す前に削除が割り込んでも、行は戻らない。"""
    from builtin_data.tools import get_building_messages as ingest
    from database.building_messages import fetch_building_messages

    w = building_world
    # 転記が建物の履歴を読む (まだ取り込み済みの印が無い)
    building_row = [
        r for r in fetch_building_messages(w.session_factory, w.building_id)
        if r["message_id"] == w.message_id
    ][0]
    building_row["_building_id"] = w.building_id  # _ingest_round と同じ
    assert building_row["ingested_by"] == []
    # その後、記憶の照会の前に確認の画面の削除が割り込む
    result = review.apply_review_action(
        "delete", [("p", "m", _version(w.db, "m"))], w.opener,
        lambda pid, ref: review.mark_building_ref_ingested(w.session_factory, ref, pid),
    )
    assert [a["message_id"] for a in result["applied"]] == ["m"]

    appended = []
    history = SimpleNamespace(add_to_persona_only=lambda entry, **_: appended.append(entry) or ("ok", "x"))
    outcome = ingest._transcribe_message(
        building_row,
        persona=SimpleNamespace(sai_memory=w.adapter),
        manager=SimpleNamespace(SessionLocal=w.session_factory),
        persona_id="p", building_id=w.building_id, history_manager=history,
        id_to_name_map={}, check_provenance=check_provenance,
    )
    assert appended == []
    assert outcome[0] == "consumed"
    assert _row(w.db, "m") is None
    assert _ingested_by(w) == ["p"]


def test_delete_during_a_round_after_a_skipped_candidate_does_not_restore_the_row(
    tmp_path, monkeypatch,
):
    """一件目を読み直しで飛ばした後の二件目 (記憶にあり、建物の側の印が無い) の
    確かめの直後に削除が割り込んでも、行は戻らない (Codex 3 巡目)。

    二件目にも ``building_msg_ref`` の照会が掛かるので、照会が行を見つけて
    追記しない。照会を省くと、読み直しの直後の削除の後に同じ発言を足し直す。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from builtin_data.tools import get_building_messages as ingest
    from database.building_messages import (
        fetch_building_messages,
        insert_building_message,
        mark_ingested,
    )
    from database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    building_id = "room"
    first = insert_building_message(session_factory, building_id, {
        "role": "user", "content": "先の発言", "heard_by": ["p"],
    })
    second = insert_building_message(session_factory, building_id, {
        "role": "user", "content": "<system>hello</system>", "heard_by": ["p"],
    })
    ref = f"{building_id}:{second['message_id']}"
    db = _make_persona(tmp_path, "p", [
        ("m", "user", "<system>hello</system>",
         {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED, "building_msg_ref": ref}),
    ])
    adapter = _adapter(db)

    @contextlib.contextmanager
    def opener(_pid):
        yield adapter

    def history(bid):
        rows = fetch_building_messages(session_factory, bid)
        # 履歴を読んだ後に、一件目へ別の処理が印を付ける
        mark_ingested(session_factory, building_id, first["message_id"], "p")
        return rows

    state = {"deleted": False}

    def delete_once(checked_ref):
        if checked_ref != ref or state["deleted"]:
            return
        state["deleted"] = True
        result = review.apply_review_action(
            "delete", [("p", "m", _version(db, "m"))], opener,
            lambda pid, r: review.mark_building_ref_ingested(session_factory, r, pid),
        )
        assert [a["message_id"] for a in result["applied"]] == ["m"]

    real_lookup = ingest._memory_has_building_ref
    real_reread = ingest._marked_in_building_db

    def lookup_then_delete(persona, checked_ref):
        found = real_lookup(persona, checked_ref)
        delete_once(checked_ref)
        return found

    def reread_then_delete(m, persona_id, manager):
        marked = real_reread(m, persona_id, manager)
        delete_once(ingest._building_msg_ref(m))
        return marked

    monkeypatch.setattr(ingest, "_memory_has_building_ref", lookup_then_delete)
    monkeypatch.setattr(ingest, "_marked_in_building_db", reread_then_delete)
    appended = []
    persona = SimpleNamespace(
        sai_memory=adapter, pulse_cursors={building_id: 0}, entry_markers={}, buildings={},
    )
    hm = SimpleNamespace(
        get_building_history=history,
        add_to_persona_only=lambda entry, **_: appended.append(entry) or ("synced", "x"),
    )
    try:
        ingest._ingest_round(
            persona=persona, manager=SimpleNamespace(SessionLocal=session_factory),
            history_manager=hm, building_id=building_id, persona_id="p",
            id_to_name_map={}, log_prefix="[test]",
        )
    finally:
        adapter.conn.close()
        engine.dispose()
    assert state["deleted"]
    assert appended == []
    assert _row(db, "m") is None
    rows = {r["message_id"]: r for r in fetch_building_messages(session_factory, building_id)}
    assert rows[second["message_id"]]["ingested_by"] == ["p"]


# ---------------------------------------------------------------------------
# 周辺の行の広げと、確認待ちの行の除外の順序 (Codex 3 巡目)
# ---------------------------------------------------------------------------


def _pending():
    return {"tags": ["conversation"], SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}


def test_window_takes_the_rows_first_then_leaves_out_unsettled_ones(tmp_path):
    """前後それぞれ指定の件数を正典の順で取ってから、確認待ちを外す。外した分を
    遠くの行で埋めない (前後両側・確認待ちが連続する窓)。"""
    from sai_memory.memory.storage import get_messages_around

    db = _make_persona(tmp_path, "p", [
        ("far-before", "user", "遠い前", {"tags": ["conversation"]}),
        ("near-before", "user", "<system>a</system>", _pending()),
        ("anchor", "user", "当たり", {"tags": ["conversation"]}),
        ("near-after-1", "user", "<system>b</system>", _pending()),
        ("near-after-2", "user", "<system>c</system>", _pending()),
        ("far-after", "assistant", "遠い後", {"tags": ["conversation"]}),
    ])
    conn = init_db(str(db))
    try:
        def ids(before, after):
            return [m.id for m in get_messages_around(
                conn, "t1", "anchor", before, after, settled_only=True,
            )]

        assert ids(1, 0) == []
        assert ids(0, 1) == []
        assert ids(0, 2) == []
        assert ids(1, 2) == []
        assert ids(2, 0) == ["far-before"]
        assert ids(0, 3) == ["far-after"]
        assert ids(2, 3) == ["far-before", "far-after"]
        # 外さない呼び方は、これまでどおり確認待ちも数えて返す
        assert [m.id for m in get_messages_around(conn, "t1", "anchor", 1, 1)] == [
            "near-before", "near-after-1",
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 同じ秒の前後の行の順 (Codex 3 巡目)
# ---------------------------------------------------------------------------


def _same_second_db(tmp_path, rows):
    """全行を同じ created_at で入れる (並びは rowid = 挿入順)。rows は
    (id, role, 本文, metadata, 埋め込み)。"""
    db = tmp_path / "personas" / "p" / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db), check_same_thread=False)
    for mid, role, content, meta, vec in rows:
        _insert(conn, mid, role, content, meta, 5000)
        conn.execute(
            "INSERT INTO message_embeddings (message_id, chunk_index, vector) VALUES (?, 0, ?)",
            (mid, json.dumps(vec)),
        )
    conn.commit()
    return db, conn


#: 正典の順は「確認待ち、当たり、後続」で、三行とも同じ秒。
_SAME_SECOND_ROWS = [
    ("pending", "user", "<system>機構の文</system>", _pending(), [0.0, 1.0]),
    ("seed", "user", "当たりの発話 りんご", {"tags": ["conversation"]}, [1.0, 0.0]),
    ("next", "assistant", "後続の応答", {"tags": ["conversation"]}, [0.0, 1.0]),
]


def test_semantic_recall_keeps_same_second_neighbours_in_canonical_order(tmp_path):
    from sai_memory.memory.recall import semantic_recall, semantic_recall_groups

    db, conn = _same_second_db(tmp_path, _SAME_SECOND_ROWS)
    kwargs = dict(
        thread_id="t1", resource_id=None, topk=1, range_before=1, range_after=1,
        scope="thread", required_tags=["conversation"],
    )
    try:
        msgs = semantic_recall(conn, _FakeEmbedder(), "q", **kwargs)
        [(seed, bundle, _score)] = semantic_recall_groups(conn, _FakeEmbedder(), "q", **kwargs)
    finally:
        conn.close()
    assert [m.id for m in msgs] == ["seed", "next"]
    assert seed.id == "seed"
    assert [m.id for m in bundle] == ["seed", "next"]


def test_semantic_recall_merges_bundles_of_the_same_second_in_canonical_order(tmp_path):
    """当たりが複数あるとき、束を並べ直す順も正典の順 (同じ秒なら rowid)。"""
    from sai_memory.memory.recall import semantic_recall

    db, conn = _same_second_db(tmp_path, [
        ("earlier", "user", "前の当たり", {"tags": ["conversation"]}, [0.8, 0.6]),
        ("later", "user", "後の当たり", {"tags": ["conversation"]}, [1.0, 0.0]),
    ])
    try:
        msgs = semantic_recall(
            conn, _FakeEmbedder(), "q", thread_id="t1", resource_id=None, topk=2,
            range_before=0, range_after=0, scope="thread", required_tags=["conversation"],
        )
    finally:
        conn.close()
    # 点数は後の行が高いが、返す順は正典の順
    assert [m.id for m in msgs] == ["earlier", "later"]


def test_hybrid_recall_keeps_same_second_neighbours_in_canonical_order(tmp_path):
    db, conn = _same_second_db(tmp_path, _SAME_SECOND_ROWS)
    conn.close()
    adapter = _recall_adapter(SimpleNamespace(db=db))
    try:
        text = adapter.recall_hybrid("q", ["りんご"], max_chars=4000)
    finally:
        adapter.conn.close()
    assert "機構の文" not in text
    assert "当たりの発話" in text and "後続の応答" in text
    assert text.index("当たりの発話") < text.index("後続の応答")


def test_reads_around_an_id_keep_same_second_neighbours_in_canonical_order(
    tmp_path, monkeypatch,
):
    """ID を指定した周辺の読み出し (``memory_read_around`` と
    ``saiverse://self/message/<id>?window=``) も、同じ秒の後ろの行を当たりの前に
    置かない。ID を指定した読み出しは確認待ちを外さない。"""
    from builtin_data.tools import memory_read_around as around
    from saiverse.uri_resolver import UriResolver

    db, conn = _same_second_db(tmp_path, _SAME_SECOND_ROWS)
    conn.close()
    adapter = _recall_adapter(SimpleNamespace(db=db))

    @contextlib.contextmanager
    def opener():
        yield adapter

    monkeypatch.setattr(around, "get_active_persona_id", lambda: "p")
    monkeypatch.setattr(around, "open_persona_memory", opener)
    resolver = UriResolver(manager=None)
    resolver._get_adapter = lambda _pid: adapter
    try:
        text = around.memory_read_around("seed", window=1)
        resolved = resolver.resolve("saiverse://self/message/seed?window=1", persona_id="p")
    finally:
        adapter.conn.close()
    for out in (text, resolved.content):
        assert out.index("機構の文") < out.index("当たりの発話") < out.index("後続の応答")


def test_message_uri_text_search_window_includes_the_hit(recall_db):
    """本文検索の周辺は、当たりの行そのものを挟んで返す (印 ``<<<`` は当たりに付く)。"""
    from saiverse.uri_resolver import UriResolver

    adapter = _recall_adapter(recall_db)
    resolver = UriResolver(manager=None)
    resolver._get_adapter = lambda _pid: adapter
    resolver._get_active_thread_id = lambda _adapter: "t1"
    try:
        resolved = resolver.resolve(
            "saiverse://self/message/msg?contain=本人の発話&window=1", persona_id="p",
        )
    finally:
        adapter.conn.close()
    lines = resolved.content.split("\n\n")
    # s1 の前は n1、後ろの u2 は確認待ちなので外れて、その先の n2 は取らない
    assert len(lines) == 2
    assert "前の発話" in lines[0]
    assert "本人の発話" in lines[1] and lines[1].endswith("<<<")


# ---------------------------------------------------------------------------
# 行のハッシュの材料 (Codex 2 巡目)
# ---------------------------------------------------------------------------


def test_a_row_whose_time_or_line_role_changed_after_listing_is_rejected(world):
    listing = review.build_review_listing(world.dbs)
    seen = {
        (r["persona_id"], r["message_id"]): r["version"]
        for g in listing["groups"] for r in g["rows"]
    }
    a_conn = world.adapters["alice"].conn
    b_conn = world.adapters["bob"].conn
    # 記憶ブラウザで日時だけが変わった行と、line_role だけが変わった行
    a_conn.execute("UPDATE messages SET created_at=created_at+3600 WHERE id='a-tool'")
    b_conn.execute("UPDATE messages SET line_role='sub_line' WHERE id='b-tool'")
    a_conn.commit()
    b_conn.commit()
    before = (_raw_row(world.a_db, "a-tool"), _raw_row(world.b_db, "b-tool"))
    result = review.apply_review_action("keep", [
        ("alice", "a-tool", seen[("alice", "a-tool")]),
        ("bob", "b-tool", seen[("bob", "b-tool")]),
    ], _open(world))
    assert result["applied"] == []
    assert {(r["message_id"], r["reason"]) for r in result["rejected"]} == {
        ("a-tool", "changed"), ("b-tool", "changed"),
    }
    assert (_raw_row(world.a_db, "a-tool"), _raw_row(world.b_db, "b-tool")) == before


# ---------------------------------------------------------------------------
# 境より後の音声入力の案内文 (Codex 7 巡目) — 「正式に変換」は二行分割
# ---------------------------------------------------------------------------


VOICE_TEXT = "ｺﾞﾄﾞｰから音声入力を受信しました。添付の音声を聴いて応答してください。"
VOICE_CONTENT = f"<system>{VOICE_TEXT}</system>"
VOICE_CUTOVER = 1_800_000_000


def _voice_meta(name, ref):
    return {
        "tags": ["conversation", "event_message"], "with": ["user"], "source": "godot_voice",
        "gateway_session_id": "g1",
        "media": [{"type": "audio", "uri": f"saiverse://audio/{name}", "mime_type": "audio/ogg"}],
        "building_msg_ref": ref,
    }


def _voice_main_db(path: Path, metas) -> Path:
    """本体 DB の building_messages。``metas`` は (seq, metadata) — どれも client_message_id なし。"""
    from sqlalchemy import create_engine

    from database.models import BuildingMessage

    engine = create_engine(f"sqlite:///{path}")
    BuildingMessage.__table__.create(engine)
    engine.dispose()
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        for seq, meta in metas:
            conn.execute(
                "INSERT INTO building_messages (building_id, seq, role, content, timestamp, "
                "heard_by, ingested_by, metadata_json, message_id, client_message_id) "
                "VALUES ('b1', ?, 'user', ?, '2026-09-01T00:00:00+00:00', '[]', '[]', ?, ?, NULL)",
                (seq, VOICE_CONTENT, json.dumps(meta, ensure_ascii=False), f"b1:{seq}"),
            )
        conn.commit()
    return path


@pytest.fixture()
def voice_world(tmp_path):
    """境より前と後に一つずつ、音声入力を中継するアドオンの案内文の行がある記憶。"""
    from database.system_notice_migration import BuildingSourceRows, migrate_persona_db

    pre = _voice_meta("pre.ogg", "b1:b1:1")
    post = _voice_meta("post.ogg", "b1:b1:2")
    main = _voice_main_db(tmp_path / "saiverse.db", [(1, pre), (2, post)])
    db_path = tmp_path / "personas" / "p" / "memory.db"
    db_path.parent.mkdir(parents=True)
    conn = init_db(str(db_path))
    _insert(conn, "voice-pre", "user", VOICE_CONTENT, pre, VOICE_CUTOVER - 100, "meta_judgment")
    _insert(conn, "reply-pre", "assistant", "pre reply", {"tags": ["conversation"]}, VOICE_CUTOVER - 90)
    _insert(conn, "voice-post", "user", VOICE_CONTENT, post, VOICE_CUTOVER + 100, "meta_judgment")
    _insert(conn, "reply-post", "assistant", "post reply", {"tags": ["conversation"]}, VOICE_CUTOVER + 110)
    conn.commit()
    conn.close()
    with BuildingSourceRows(main) as source_rows:
        migrate_persona_db(
            db_path, persona_id="p", output_dir=tmp_path / "out", stamp="20261029_000000",
            playbook_names=frozenset(), source_rows=source_rows, cutover=VOICE_CUTOVER,
        )
    adapter = _adapter(db_path)

    @contextlib.contextmanager
    def opener(_pid):
        yield adapter

    yield SimpleNamespace(db=db_path, adapter=adapter, opener=opener, pre=pre, post=post)
    adapter.conn.close()


def _ordered(db_path: Path):
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        return [
            (r[0], r[1], r[2], json.loads(r[3]) if r[3] else None, r[4], r[5])
            for r in conn.execute(
                "SELECT id, role, content, metadata, created_at, line_role FROM messages "
                "ORDER BY created_at, rowid"
            ).fetchall()
        ]


def test_a_voice_notice_after_the_cutover_can_be_split_from_the_review_screen(voice_world):
    from database.system_notice_migration import AFTER_CUTOVER_REASON, VOICE_INPUT_NOTICE_KIND

    db = voice_world.db
    # 境より前の行は移行が二行に分け、後の行は確認待ちに残る
    assert _row(db, "voice-post")[2][SYSTEM_NOTICE_REVIEW_KEY] == AFTER_CUTOVER_REASON
    listing = review.build_review_listing([("p", "P", db)])
    assert listing["total"] == 1
    group = listing["groups"][0]
    assert group["actions"] == ["keep", "system", "user", "delete"]
    assert group["system_split"] is True
    row = group["rows"][0]
    assert row["message_id"] == "voice-post"

    result = review.apply_review_action(
        "system", [("p", "voice-post", row["version"])], voice_world.opener,
    )
    assert result["rejected"] == []
    assert result["applied"] == [{"persona_id": "p", "message_id": "voice-post", "result": "updated"}]

    rows = _ordered(db)
    ids = [r[0] for r in rows]
    # 本文の無い音声の行は、通知の行の直後 (同じ created_at、後ろの rowid) に並ぶ
    assert len(ids) == 6
    pre_voice = rows[ids.index("voice-pre") + 1]
    post_voice = rows[ids.index("voice-post") + 1]
    assert ids[ids.index("voice-post") + 2] == "reply-post"
    pre_notice = rows[ids.index("voice-pre")]
    post_notice = rows[ids.index("voice-post")]

    # 通知の行: 移行の分割と同じ形 (違いは所有者の確定の記録だけ)
    assert post_notice[1:3] == ("system", VOICE_TEXT)
    assert post_notice[3][SYSTEM_NOTICE_KEY] == VOICE_INPUT_NOTICE_KIND
    assert post_notice[3][SYSTEM_NOTICE_REVIEWED_KEY] == "system"
    assert SYSTEM_NOTICE_REVIEW_KEY not in post_notice[3]
    assert {k: v for k, v in post_notice[3].items() if k != SYSTEM_NOTICE_REVIEWED_KEY} == pre_notice[3]
    assert post_notice[5] == pre_notice[5] == "meta_judgment"
    assert is_system_notice({"role": "system", "content": post_notice[2], "metadata": post_notice[3]})

    # 音声の行: 本文なし・role=user・添付と来歴を持ち、移行の分割と同じ形
    assert post_voice[1:3] == ("user", "")
    assert post_voice[4] == VOICE_CUTOVER + 100
    assert post_voice[3]["media"] == voice_world.post["media"]
    assert post_voice[3]["building_msg_ref"] == "b1:b1:2"
    same = {"media": None, "building_msg_ref": None}
    assert {**post_voice[3], **same} == {**pre_voice[3], **same}
    assert post_voice[3]["tags"] == ["conversation"]
    assert post_voice[3][review.REVIEW_CLEARED_KEY] == {
        "tags": ["event_message"], "line_role": "meta_judgment",
    }
    assert post_voice[5] == pre_voice[5] == review.USER_LINE_ROLE
    assert SYSTEM_NOTICE_REVIEW_KEY not in post_voice[3]
    # 音声の行は実会話に入り、確認待ちはもう残らない
    conn = voice_world.adapter.conn
    assert filter_real_conversation_ids(conn, [post_voice[0], "voice-post"]) == {post_voice[0]}
    assert review.build_review_listing([("p", "P", db)])["total"] == 0


def test_a_voice_split_is_not_offered_when_the_voice_row_cannot_follow_the_notice(voice_world):
    """同じ時刻の後ろに別の行があると、音声の行を通知の直後に置けない (移行と同じ約束)。"""
    conn = voice_world.adapter.conn
    # 一覧の後に同じ時刻の行が足されても、確定の操作が書き込みの取引の中で判定し直す
    seen = review.build_review_listing([("p", "P", voice_world.db)])["groups"][0]["rows"][0]
    assert seen["actions"] == ["keep", "system", "user", "delete"]
    _insert(conn, "same-second", "assistant", "x", {"tags": ["conversation"]}, VOICE_CUTOVER + 100)
    conn.commit()
    listing = review.build_review_listing([("p", "P", voice_world.db)])
    row = listing["groups"][0]["rows"][0]
    assert row["actions"] == ["keep", "user", "delete"]
    assert listing["groups"][0]["system_split"] is False
    assert row["version"] == seen["version"]
    before = _ordered(voice_world.db)
    result = review.apply_review_action(
        "system", [("p", "voice-post", row["version"])], voice_world.opener,
    )
    assert result["applied"] == []
    assert result["rejected"] == [{"persona_id": "p", "message_id": "voice-post", "reason": "not_allowed"}]
    assert _ordered(voice_world.db) == before


def test_voice_split_needs_the_whole_voice_input_shape():
    """規則 5 の形でない添付つきの行には、今までどおり「正式に変換」を出さない。"""
    pending = {SYSTEM_NOTICE_REVIEW_KEY: "x"}
    voice = {**pending, **_voice_meta("a.ogg", "b1:b1:1")}
    assert review.allowed_actions("user", VOICE_CONTENT, voice) == ["keep", "system", "user", "delete"]
    image = {**voice, "media": [{"type": "image", "uri": "saiverse://image/a.png"}]}
    not_voice_source = {**voice, "source": "screen"}
    for role, content, meta in (
        ("user", VOICE_CONTENT, image),
        ("user", VOICE_CONTENT, not_voice_source),
        ("user", "<system>別の文</system>", voice),
        ("user", VOICE_TEXT, voice),  # 包みの無い本文
    ):
        assert review.allowed_actions(role, content, meta) == ["keep", "user", "delete"], (content, meta)
    assert review.allowed_actions("assistant", VOICE_CONTENT, voice) == ["keep", "delete"]


def test_voice_rows_and_other_attachment_rows_are_separate_groups(tmp_path):
    pending = {SYSTEM_NOTICE_REVIEW_KEY: "x"}
    voice = {**pending, **_voice_meta("a.ogg", "b1:b1:1")}
    db = _make_persona(tmp_path, "p", [
        ("voice", "user", VOICE_CONTENT, voice),
        ("not-voice", "user", VOICE_CONTENT, {**voice, "source": "screen"}),
    ])
    groups = review.build_review_listing([("p", "P", db)])["groups"]
    by_row = {g["rows"][0]["message_id"]: g for g in groups}
    assert len(groups) == 2
    assert by_row["voice"]["system_split"] is True
    assert by_row["not-voice"]["system_split"] is False
    assert by_row["not-voice"]["actions"] == ["keep", "user", "delete"]
