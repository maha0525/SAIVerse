"""resolve_uri の「提示済みの除外」 (saiverse/uri_resolver.py)。

ペルソナがスペルとして resolve_uri を唱えたとき、スペルループは tools.context に
「これから送る messages の列」と PulseContext を置く。resolver はそこから
「いまプロンプトに載っている行の id」と「唱えた Pulse の id」を読み、見えている
ものを結果から外す — 見えている行をそのまま返しても想起にならない
(2026-11-07 まはー指摘)。ここで固定するのは:

- ``msg/recent`` は、見えている行とこの Pulse の行を除いた中から新しい順に
  depth 件 (表示は従来どおり古い→新しい)
- スペル文脈が無い経路 (API / CLI) では従来どおり素の直近
- ``msg?contain`` は同じ除外を当たりに掛け、``order=old`` / ``limit`` で
  向きと件数を選べる (既定は従来互換の新しい順・1 件)
- ``chronicle/recent`` は「見えているあらすじ」(_presented_chronicle_entry_ids)
  に出ていない Lv1 エントリを新しい順に depth 件
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sai_memory.db_locks import lock_for_path
from sai_memory.memory.storage import init_db
from saiverse.uri_resolver import UriResolver
from saiverse_memory.adapter import SAIMemoryAdapter
from tools.context import persona_context


def _insert(conn, mid, role, content, created_at, pulse_id=None, scope=None):
    if scope is None:
        # messages.scope は NOT NULL (既定値持ち) なので、指定しないときは列ごと省く
        conn.execute(
            "INSERT INTO messages (id, thread_id, role, content, resource_id, "
            "created_at, metadata, pulse_id) VALUES (?, 't1', ?, ?, 'r', ?, NULL, ?)",
            (mid, role, content, created_at, pulse_id),
        )
        return
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, resource_id, "
        "created_at, metadata, pulse_id, scope) VALUES (?, 't1', ?, ?, 'r', ?, NULL, ?, ?)",
        (mid, role, content, created_at, pulse_id, scope),
    )


_ROWS = [
    # (id, role, content, created_at, pulse_id[, scope])
    ("m1", "user", "昔の話: muse_import の最初の相談", 1000, None),
    ("m2", "assistant", "ふつうの会話A", 1001, None),
    ("m3", "assistant", "muse_import を設計した", 1002, None),
    ("m4", "user", "ふつうの会話B", 1003, None),
    ("m5", "user", "見えている発言", 1004, None),
    # スペルを唱えた周の発言 (見えていて、かつ同じ Pulse)
    ("m6", "assistant", "/spell resolve_uri muse_import を探す", 1005, "pulse-X"),
    # 同じ Pulse で記録に書かれたが、列にはまだ載っていない行
    ("m7", "system", "[Spell Result] muse_import の結果", 1006, "pulse-X"),
    # メタ判断の discardable な行 — recent の一覧に出ない (従来の get_messages_last と同じ)
    ("m8", "assistant", "捨てる行", 1007, None, "discardable"),
]


@pytest.fixture()
def resolver_and_adapter(tmp_path):
    db_path = tmp_path / "personas" / "p" / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db_path), check_same_thread=False)
    for row in _ROWS:
        _insert(conn, *row)
    conn.commit()

    adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
    adapter.conn = conn
    adapter._db_lock = lock_for_path(str(db_path))

    resolver = UriResolver(manager=None)
    resolver._get_adapter = lambda _pid: adapter
    resolver._get_memory_conn = lambda _pid: adapter.conn
    resolver._get_active_thread_id = lambda _adapter: "t1"
    yield resolver, adapter, tmp_path
    conn.close()


def _spell_context(tmp_path):
    """スペルループが置くのと同じ形の実行文脈 (m5 と m6 が見えている)。"""
    return persona_context(
        "p", tmp_path, None,
        llm_messages=[
            {"role": "user", "content": "見えている発言", "id": "m5"},
            {"role": "assistant", "content": "/spell ...", "id": "m6"},
        ],
        pulse_context=SimpleNamespace(pulse_id="pulse-X"),
    )


# ---- msg/recent ----

def test_recent_excludes_presented_rows_and_current_pulse(resolver_and_adapter):
    resolver, _adapter, tmp_path = resolver_and_adapter
    with _spell_context(tmp_path):
        resolved = resolver.resolve(
            "saiverse://self/message/msg/recent?depth=3", persona_id="p",
        )
    # m8 (discardable)・m7 (同じ Pulse)・m6 (見えている + 同じ Pulse)・
    # m5 (見えている) を飛ばし、その手前の m4, m3, m2 を古い順で返す
    assert resolved.metadata["count"] == 3
    assert resolved.metadata["excluded_presented"] == 2
    assert "見えている発言" not in resolved.content
    assert "[Spell Result]" not in resolved.content
    assert "捨てる行" not in resolved.content
    pos = [resolved.content.index(s) for s in ("ふつうの会話A", "muse_import を設計した", "ふつうの会話B")]
    assert pos == sorted(pos)


def test_recent_without_spell_context_returns_plain_recent(resolver_and_adapter):
    resolver, _adapter, _tmp = resolver_and_adapter
    resolved = resolver.resolve(
        "saiverse://self/message/msg/recent?depth=2", persona_id="p",
    )
    # 文脈が無ければ除外は掛からない (API / CLI 互換)
    assert resolved.metadata["count"] == 2
    assert "[Spell Result] muse_import の結果" in resolved.content


def test_recent_says_everything_is_already_presented(resolver_and_adapter):
    resolver, _adapter, tmp_path = resolver_and_adapter
    all_ids = [{"role": "user", "content": "x", "id": r[0]} for r in _ROWS]
    with persona_context("p", tmp_path, None, llm_messages=all_ids,
                         pulse_context=SimpleNamespace(pulse_id="pulse-X")):
        resolved = resolver.resolve(
            "saiverse://self/message/msg/recent?depth=3", persona_id="p",
        )
    assert resolved.metadata["count"] == 0
    assert "すべて現在のコンテキスト" in resolved.content


# ---- msg?contain ----

def test_contain_skips_casting_pulse_and_presented_rows(resolver_and_adapter):
    resolver, _adapter, tmp_path = resolver_and_adapter
    with _spell_context(tmp_path):
        resolved = resolver.resolve(
            "saiverse://self/message/msg?contain=muse_import", persona_id="p",
        )
    # 検索語を含む一番新しい行は m7/m6 だが、同じ Pulse なので当たりは m3
    assert resolved.metadata["message_id"] == "m3"
    assert resolved.metadata["count"] == 1


def test_contain_order_old_finds_first_occurrence(resolver_and_adapter):
    resolver, _adapter, tmp_path = resolver_and_adapter
    with _spell_context(tmp_path):
        resolved = resolver.resolve(
            "saiverse://self/message/msg?contain=muse_import&order=old", persona_id="p",
        )
    assert resolved.metadata["message_id"] == "m1"
    assert resolved.metadata["order"] == "old"


def test_contain_limit_returns_multiple_hits(resolver_and_adapter):
    resolver, _adapter, tmp_path = resolver_and_adapter
    with _spell_context(tmp_path):
        resolved = resolver.resolve(
            "saiverse://self/message/msg?contain=muse_import&limit=3", persona_id="p",
        )
    assert resolved.metadata["message_ids"] == ["m3", "m1"]
    assert resolved.content_type == "message_log"


def test_contain_reports_when_all_hits_are_presented(resolver_and_adapter):
    resolver, _adapter, tmp_path = resolver_and_adapter
    visible = [{"role": "user", "content": "x", "id": mid} for mid in ("m1", "m3")]
    with persona_context("p", tmp_path, None, llm_messages=visible,
                         pulse_context=SimpleNamespace(pulse_id="pulse-X")):
        resolved = resolver.resolve(
            "saiverse://self/message/msg?contain=muse_import", persona_id="p",
        )
    assert resolved.content_type == "error"
    assert "見えているメッセージは検索の対象外" in resolved.content


def test_contain_rejects_unknown_order(resolver_and_adapter):
    resolver, _adapter, _tmp = resolver_and_adapter
    resolved = resolver.resolve(
        "saiverse://self/message/msg?contain=muse_import&order=sideways", persona_id="p",
    )
    assert resolved.content_type == "error"


def test_limit_and_depth_are_clamped(resolver_and_adapter):
    resolver, _adapter, _tmp = resolver_and_adapter
    # limit=0 は 1 に繰り上げ (0 件という答えを作らない)
    resolved = resolver.resolve(
        "saiverse://self/message/msg?contain=muse_import&limit=0", persona_id="p",
    )
    assert resolved.metadata["count"] == 1
    # depth=0 も 1 に繰り上げ — 行があるのに「すべて載っています」と答えない
    resolved = resolver.resolve(
        "saiverse://self/message/msg/recent?depth=0", persona_id="p",
    )
    assert resolved.metadata["count"] == 1


# ---- chronicle/recent ----

@pytest.fixture()
def chronicle_resolver(resolver_and_adapter):
    from sai_memory.arasuji.storage import create_entry, init_arasuji_tables

    resolver, adapter, tmp_path = resolver_and_adapter
    init_arasuji_tables(adapter.conn)
    entries = []
    for i, text in enumerate(["あらすじ1", "あらすじ2", "あらすじ3"]):
        entries.append(create_entry(
            adapter.conn, level=1, content=text, source_ids=[f"s{i}"],
            start_time=100 * (i + 1), end_time=100 * (i + 1) + 50,
            source_count=1, message_count=1,
        ))
    adapter.conn.commit()
    return resolver, entries, tmp_path


def test_chronicle_recent_without_context_lists_newest_lv1(chronicle_resolver):
    resolver, _entries, _tmp = chronicle_resolver
    resolved = resolver.resolve(
        "saiverse://self/chronicle/recent?depth=2", persona_id="p",
    )
    # manager が無い経路では提示集合は空 — 新しい 2 件を古い順で返す
    assert resolved.metadata["entries"] == 2
    assert "あらすじ1" not in resolved.content
    assert resolved.content.index("あらすじ2") < resolved.content.index("あらすじ3")


def test_chronicle_recent_skips_presented_entries(chronicle_resolver):
    resolver, entries, _tmp = chronicle_resolver
    newest = entries[2]
    resolver._presented_chronicle_entry_ids = lambda *_a, **_k: {newest.id}
    resolved = resolver.resolve(
        "saiverse://self/chronicle/recent?depth=2", persona_id="p",
    )
    assert "あらすじ3" not in resolved.content
    assert "あらすじ1" in resolved.content and "あらすじ2" in resolved.content


def test_chronicle_recent_says_everything_is_presented(chronicle_resolver):
    resolver, entries, _tmp = chronicle_resolver
    resolver._presented_chronicle_entry_ids = lambda *_a, **_k: {e.id for e in entries}
    resolved = resolver.resolve(
        "saiverse://self/chronicle/recent?depth=3", persona_id="p",
    )
    assert resolved.metadata["entries"] == 0
    assert "あらすじ枠に載っています" in resolved.content


# ---- chronicle/recent: 提示集合の本経路 (スタブ manager で実走) ----

class _StubQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **_kw):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _StubDb:
    def __init__(self, rows_by_model):
        self._rows = rows_by_model

    def query(self, model):
        return _StubQuery(self._rows.get(model.__name__, []))

    def close(self):
        pass


class _StubManager:
    def __init__(self, rows_by_model):
        self._rows = rows_by_model

    def SessionLocal(self):
        return _StubDb(self._rows)


def test_chronicle_presented_path_with_weave_marks_everything_presented(chronicle_resolver):
    """weave 有効 + fold なし: head の選定の再現が全 Lv1 を提示済みにする。

    _presented_chronicle_entry_ids の本物を通す — get_episode_context の
    source_id と get_entries_by_level の id が同じ名前空間であることも
    これで固定される (ずれていたら除外が効かず entries > 0 になる)。
    """
    resolver, _entries, _tmp = chronicle_resolver
    ai_row = SimpleNamespace(MEMORY_WEAVE_CONTEXT=True, CHRONICLE_CHAR_BUDGET=None)
    resolver.manager = _StubManager({"AI": [ai_row], "SessionAnchor": []})
    resolved = resolver.resolve(
        "saiverse://self/chronicle/recent?depth=3", persona_id="p",
    )
    assert resolved.metadata["entries"] == 0
    assert resolved.metadata["excluded_presented"] == 3
    assert "あらすじ枠に載っています" in resolved.content


def test_chronicle_presented_path_reads_folds_when_weave_is_off(chronicle_resolver):
    """weave 無効 + fold が最新 entry を digest 提示中: その entry だけ除外される。"""
    import json

    resolver, entries, _tmp = chronicle_resolver
    newest = entries[2]
    ai_row = SimpleNamespace(MEMORY_WEAVE_CONTEXT=False, CHRONICLE_CHAR_BUDGET=None)
    anchor_row = SimpleNamespace(FOLDED_RANGES_JSON=json.dumps([{
        "message_ids": ["x1"],
        "chronicle_entry_ids": [newest.id],
    }]))
    resolver.manager = _StubManager({"AI": [ai_row], "SessionAnchor": [anchor_row]})
    resolved = resolver.resolve(
        "saiverse://self/chronicle/recent?depth=2", persona_id="p",
    )
    assert "あらすじ3" not in resolved.content
    assert "あらすじ1" in resolved.content and "あらすじ2" in resolved.content
    assert resolved.metadata["excluded_presented"] == 1
