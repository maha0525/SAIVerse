"""確認待ちの行を所有者が画面で確定する (saiverse/system_notice_review.py)。

docs/intent/system_notice_delivery.md「追補」の「確認待ちの行の確定 (利用者の画面)」。
ここで固定するのは:

- 確認待ちの印を持つ行だけを全ペルソナから集め、同じ型 (role・書き出し・タグ・
  理由・選べる操作) で束ねる
- 行ごとの「選べる操作」: 「通常のユーザー発話に変換」は role=user の行にだけ
  出す (包みが本文全体を囲んでいなければ、本文を変えずに発話へ戻す)
- 4 つの操作の結果 (role・本文・通知の印・確認待ちの印・確定の記録)
- 選べない操作・見つからない行・確定済みの行は書き換えずに拒否する
- 画面の警告は件数に応じて置き直し、0 件で外す (他の警告は触らない)

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
    filter_real_conversation_ids,
    init_db,
    is_system_notice,
)
from saiverse import system_notice_review as review
from saiverse_memory.adapter import SAIMemoryAdapter

UNCLASSIFIED = "系統を判定できなかった"
AMBIGUOUS = "タグの対応が曖昧"
UNKNOWN_SYSTEM = "系統を推定できなかった role=system"


def _insert(conn, mid, role, content, metadata, created_at):
    raw = metadata if isinstance(metadata, str) else (
        json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    )
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, metadata) "
        "VALUES (?, 't1', ?, ?, 'r', ?, ?)",
        (mid, role, content, created_at, raw),
    )


def _make_persona(home: Path, pid: str, rows) -> Path:
    db_path = home / "personas" / pid / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db_path))
    for i, (mid, role, content, metadata) in enumerate(rows):
        _insert(conn, mid, role, content, metadata, 1000 + i)
    conn.commit()
    conn.close()
    return db_path


def _adapter(db_path: Path) -> SAIMemoryAdapter:
    adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
    adapter.conn = init_db(str(db_path), check_same_thread=False)
    adapter._db_lock = lock_for_path(str(db_path))
    return adapter


def _row(db_path: Path, mid: str):
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT role, content, metadata FROM messages WHERE id=?", (mid,),
        ).fetchone()
    if row is None:
        return None
    return row[0], row[1], json.loads(row[2]) if row[2] else None


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
    yield SimpleNamespace(
        home=tmp_path, a_db=a_db, b_db=b_db, adapters=adapters, manager=manager,
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


def _apply(world, action, items):
    return review.apply_review_action(action, items, _open(world))


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
    assert len(listing["groups"]) == 4


def test_listing_truncates_only_the_preview(tmp_path):
    long_body = "<system>" + "x" * 1000 + "</system>"
    db = _make_persona(tmp_path, "p", [
        ("m", "user", long_body, {SYSTEM_NOTICE_REVIEW_KEY: UNCLASSIFIED}),
    ])
    listing = review.build_review_listing([("p", "P", db)])
    row = listing["groups"][0]["rows"][0]
    assert row["content_truncated"] is True
    assert len(row["content"]) == review.PREVIEW_CHARS
    assert _row(db, "m")[1] == long_body


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


def test_unknown_action_raises(world):
    with pytest.raises(ValueError):
        _apply(world, "rename", [("alice", "a-tool")])


# ---------------------------------------------------------------------------
# 画面の警告
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


def test_alert_wording_lives_in_the_frontend_message_table():
    """警告の文言は frontend の文言表にあり、両言語で同じ引数を持つ。"""
    table = json.loads(
        (Path(__file__).resolve().parent.parent / "frontend" / "src" / "i18n" / "messages.json")
        .read_text(encoding="utf-8"),
    )
    alert = review.review_alert(3)
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

    bad = client.post("/api/system/notice-review/apply", json={"action": "rename", "items": []})
    assert bad.status_code == 400

    res = client.post("/api/system/notice-review/apply", json={
        "action": "user",
        "items": [
            {"persona_id": "alice", "message_id": "a-tool"},
            {"persona_id": "alice", "message_id": "a-assistant"},
        ],
    }).json()
    assert [a["message_id"] for a in res["applied"]] == ["a-tool"]
    assert [(r["message_id"], r["reason"]) for r in res["rejected"]] == [("a-assistant", "not_allowed")]
    assert res["remaining"] == 4
    assert _row(world.a_db, "a-tool")[:2] == ("user", "Email sent.")
    assert any(a["id"] == review.REVIEW_ALERT_ID for a in world.manager.startup_alerts)
