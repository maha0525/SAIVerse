"""起点が memory.db に実在しない発言を指すときの解決
(docs/issues/dangling_session_anchor_refuses_every_pulse.md)。

固定する不変条件:

**起点は memory.db に実在する発言を指す。実在しないと確かに分かった起点は、
その行を消して、そのモデルで初めて話すときと同じ道 (編纂の最前線 / 他モデルの
起点の借用 / 最小ロード) で窓を始める。**

- 確かめる照会そのものが失敗したら消さない (厳格は例外、既定は行をそのまま)。
- 行を消すのは本番の読み (``persist_advance=True``) だけ。プレビューは同じ
  結果を返すが行は触らない。
- 消すのは起点がまだ同じ値のときだけ (並行して新しい起点を書いた書き手を
  潰さない)。
- 実在の判定はスレッド不問 (別スレッドにある発言は実在する)。

本物の SAIMemoryAdapter (memory.db) と、メモリ上の saiverse.db (session_anchor)
で組む。LLM は呼ばない。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from sea.eviction_plan import Watermarks
from sea.session_lifecycle import SessionLifecycle

PERSONA_ID = "alice"
MODEL = "model-a"
GONE = "cde81e67-1574-406f-a241-ad85fbf7d4d4"  # memory.db に無い発言 id
WM = Watermarks(target=5000, high=10_000)
LOGGER_NAME = "sea.session_lifecycle"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    """本物の SAIMemoryAdapter (隔離した tmp の memory.db)。埋め込みはダミー。"""
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path / "home"))

    class _Embedder:
        def __init__(self, model=None, **kwargs):
            self.model_name = model

        def embed(self, texts, **kwargs):
            return [[0.0] * 3 for _ in texts]

    with patch("saiverse_memory.adapter.Embedder", _Embedder):
        from saiverse_memory import SAIMemoryAdapter
        persona_dir = tmp_path / "personas" / PERSONA_ID
        persona_dir.mkdir(parents=True)
        a = SAIMemoryAdapter(PERSONA_ID, persona_dir=persona_dir, resource_id=PERSONA_ID)
        try:
            yield a
        finally:
            a.close()


def _lifecycle(session_factory):
    manager = SimpleNamespace(
        SessionLocal=session_factory, event_scheduler=None,
        meta_layer=SimpleNamespace(_load_judgment_config=lambda persona: {}),
        personas={},
    )
    return SessionLifecycle(
        SimpleNamespace(run_cache_keepalive=lambda *a, **k: None), manager,
    )


def _messages(adapter, n=5, *, thread_suffix=None, start=0):
    """発言を n 件書き、id を時系列順で返す。"""
    base = datetime(2026, 9, 1, 12, 0, 0)
    ids = []
    for i in range(n):
        mid = adapter.append_persona_message(
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"発言 {start + i}",
                "timestamp": (base + timedelta(minutes=start + i)).isoformat(),
            },
            thread_suffix=thread_suffix,
        )
        assert mid is not None
        ids.append(mid)
    return ids


def _hot_row(lc, model_key, anchor_id):
    """温かい (TTL 内の) 行を立てる — 温かい自行は解決がそのまま返す形。"""
    assert lc.upsert_anchor_entry(PERSONA_ID, model_key, {
        "anchor_id": anchor_id,
        "updated_at": datetime.now().replace(microsecond=0).isoformat(),
        "ttl_seconds": 3600,
    })


def _persona(adapter, history_manager=None):
    return SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, sai_memory=adapter,
        history_manager=history_manager,
    )


def _with_frontier(adapter, compiled_ids):
    """一次あらすじで ``compiled_ids`` を畳み、最前線をその次の発言に立てる。"""
    from sai_memory.arasuji.storage import create_entry, get_frontier_anchor_id
    create_entry(
        adapter.conn, level=1, content="digest", source_ids=list(compiled_ids),
        source_count=len(compiled_ids), message_count=len(compiled_ids),
    )
    return get_frontier_anchor_id(adapter.conn)


def _row_anchor(lc, model_key=MODEL):
    entry = lc.load_anchor_entry(PERSONA_ID, model_key)
    return entry.get("anchor_id") if entry else None


def _dangling_warnings(caplog):
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "missing from memory.db" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# 自行が実在しない発言を指す
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frontier_case", [True, False])
def test_dangling_self_row_resolves_like_no_row_and_is_dropped(
    session_factory, adapter, caplog, frontier_case,
):
    """自行の起点が実在しない → 「行なし」と同じ結果 (最前線 / 最小ロード)、
    行は消え、WARNING が残る。"""
    ids = _messages(adapter)
    frontier = _with_frontier(adapter, ids[:2]) if frontier_case else None
    if frontier_case:
        assert frontier == ids[2]
    persona = _persona(adapter)

    # 基準: 行が無いときの解決
    baseline = _lifecycle(session_factory).resolve_metabolism_anchor(
        persona, model_key=MODEL,
    )
    assert baseline == ((ids[2], "frontier") if frontier_case else (None, "minimal"))

    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert lc.resolve_metabolism_anchor(persona, model_key=MODEL) == baseline
    assert lc.load_anchor_entry(PERSONA_ID, MODEL) is None  # 行は消えた
    warnings = _dangling_warnings(caplog)
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert GONE in msg and PERSONA_ID in msg and MODEL in msg and "dropped" in msg


def test_dangling_self_row_is_dropped_on_the_strict_path(session_factory, adapter):
    """最終防衛ライン・読み戻しが使う厳格な解決でも同じ規則 (例外にしない)。"""
    ids = _messages(adapter)
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    assert lc.resolve_metabolism_anchor(
        _persona(adapter), model_key=MODEL, strict=True,
    ) == (None, "minimal")
    assert lc.load_anchor_entry(PERSONA_ID, MODEL) is None
    assert ids  # 記憶は触らない


def test_message_deleted_through_the_memory_ui_path_is_handled(session_factory, adapter):
    """記憶の画面の削除 (SAIMemoryAdapter.delete_message) は起点を直さない —
    その起点も解決の入口で拾う。"""
    ids = _messages(adapter)
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, ids[3])
    assert lc.resolve_metabolism_anchor(_persona(adapter), model_key=MODEL) == (ids[3], "self")
    assert adapter.delete_message(ids[3])
    assert lc.resolve_metabolism_anchor(_persona(adapter), model_key=MODEL) == (None, "minimal")
    assert lc.load_anchor_entry(PERSONA_ID, MODEL) is None


def test_preview_returns_the_same_result_without_touching_the_row(
    session_factory, adapter, caplog,
):
    """persist_advance=False (プレビュー) は同じ結果を返すが行を消さない。
    警告は (persona, model, anchor) ごとに 1 度 (ポーリングで溢れさせない)。"""
    ids = _messages(adapter)
    _with_frontier(adapter, ids[:2])
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    persona = _persona(adapter)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert lc.resolve_metabolism_anchor(
            persona, model_key=MODEL, persist_advance=False,
        ) == (ids[2], "frontier")
        assert lc.resolve_metabolism_anchor(
            persona, model_key=MODEL, persist_advance=False,
        ) == (ids[2], "frontier")
    assert _row_anchor(lc) == GONE  # 行はそのまま
    warnings = _dangling_warnings(caplog)
    assert len(warnings) == 1 and "preview" in warnings[0].getMessage()
    # 続く本番の読みが消す
    assert lc.resolve_metabolism_anchor(persona, model_key=MODEL) == (ids[2], "frontier")
    assert lc.load_anchor_entry(PERSONA_ID, MODEL) is None


# ---------------------------------------------------------------------------
# 確かめられなかったときは消さない
# ---------------------------------------------------------------------------


def test_existence_query_failure_never_drops(session_factory, adapter):
    """照会そのものの失敗: 厳格は例外、既定は行の起点をそのまま使う。どちらも
    行は消さない。"""
    _messages(adapter)
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    persona = _persona(adapter)
    with patch("sai_memory.memory.storage.get_existing_message_ids",
               side_effect=sqlite3.OperationalError("disk I/O error")):
        with pytest.raises(sqlite3.OperationalError):
            lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True)
        assert lc.resolve_metabolism_anchor(persona, model_key=MODEL) == (GONE, "self")
    assert _row_anchor(lc) == GONE


def test_broken_store_raises_on_strict_and_keeps_rows_by_default(session_factory):
    """器が broken (有効なのに接続が無い): 厳格は例外、既定は確かめずに行を使う。"""
    from persona.history_manager import HistoryStoreUnavailableError

    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    persona = _persona(SimpleNamespace(is_ready=lambda: False))
    with pytest.raises(HistoryStoreUnavailableError):
        lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True)
    assert lc.resolve_metabolism_anchor(persona, model_key=MODEL) == (GONE, "self")
    assert _row_anchor(lc) == GONE


def test_absent_store_skips_the_check(session_factory):
    """器が absent (従来のメモリ上モード) なら確かめない — 行はそのまま。"""
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    for store in (None, SimpleNamespace(
        is_ready=lambda: False, settings=SimpleNamespace(memory_enabled=False),
    )):
        persona = _persona(store)
        assert lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True) == (GONE, "self")
        assert lc.resolve_metabolism_anchor(persona, model_key=MODEL) == (GONE, "self")
    assert _row_anchor(lc) == GONE


def test_existence_check_runs_under_the_adapter_lock(session_factory, adapter):
    """一括照会は adapter の錠前の内側で 1 回だけ (行ごとに照会しない)。"""
    ids = _messages(adapter)
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, ids[4])
    _hot_row(lc, "model-b", ids[3])
    _hot_row(lc, "model-c", GONE)
    from sai_memory.memory import storage as storage_mod
    real = storage_mod.get_existing_message_ids
    calls = []

    def _spy(conn, message_ids):
        # RLock の保持者判定 (_is_owned は CPython の RLock 実装の内部 API)
        calls.append((sorted(message_ids), adapter._db_lock._is_owned()))
        return real(conn, message_ids)

    with patch("sai_memory.memory.storage.get_existing_message_ids", side_effect=_spy):
        assert lc.resolve_metabolism_anchor(
            _persona(adapter), model_key=MODEL,
        ) == (ids[4], "self")
    assert calls == [(sorted([ids[4], ids[3], GONE]), True)]
    assert lc.load_anchor_entry(PERSONA_ID, "model-c") is None
    assert _row_anchor(lc, "model-b") == ids[3]


# ---------------------------------------------------------------------------
# 他モデルの行 (借用候補)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("persist", [True, False])
def test_dangling_row_on_another_model_is_not_borrowed(
    session_factory, adapter, persist,
):
    """借用候補の起点が実在しなければ借りない — 次に新しい実在の行を借りる。
    本番の読みではその行を消し、プレビューでは残す。"""
    ids = _messages(adapter)
    lc = _lifecycle(session_factory)
    older = (datetime.now() - timedelta(hours=2)).replace(microsecond=0).isoformat()
    lc.upsert_anchor_entry(PERSONA_ID, "model-c", {"anchor_id": ids[1], "updated_at": older})
    _hot_row(lc, "model-b", GONE)  # 最も新しい = 以前なら借りられていた
    persona = _persona(adapter)
    assert lc.resolve_metabolism_anchor(
        persona, model_key=MODEL, persist_advance=persist,
    ) == (ids[1], "other")
    if persist:
        assert lc.load_anchor_entry(PERSONA_ID, "model-b") is None
    else:
        assert _row_anchor(lc, "model-b") == GONE
    assert _row_anchor(lc, "model-c") == ids[1]


def test_only_dangling_rows_to_borrow_fall_to_minimal(session_factory, adapter):
    """借りられる行が実在しない起点だけなら、行が無いのと同じ (最小ロード)。"""
    _messages(adapter)
    lc = _lifecycle(session_factory)
    _hot_row(lc, "model-b", GONE)
    assert lc.resolve_metabolism_anchor(_persona(adapter), model_key=MODEL) == (None, "minimal")
    assert lc.load_anchor_entries(PERSONA_ID) == {}


# ---------------------------------------------------------------------------
# 実在する発言 (スレッド不問) は消さない
# ---------------------------------------------------------------------------


def test_anchor_in_a_different_thread_is_kept(session_factory, adapter):
    """起点の発言が別スレッドにあっても実在する — 行は消さない。"""
    _messages(adapter, 3)
    other_thread = _messages(adapter, 2, thread_suffix="side", start=10)
    thread_ids = {
        row[0] for row in adapter.conn.execute(
            "SELECT DISTINCT thread_id FROM messages",
        ).fetchall()
    }
    assert len(thread_ids) == 2  # 本当に別スレッド
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, other_thread[0])
    for strict in (False, True):
        assert lc.resolve_metabolism_anchor(
            _persona(adapter), model_key=MODEL, strict=strict,
        ) == (other_thread[0], "self")
    assert _row_anchor(lc) == other_thread[0]


# ---------------------------------------------------------------------------
# 返事の前の最終防衛ライン / 読み戻し
# ---------------------------------------------------------------------------


def _real_history(adapter):
    from persona.history_manager import HistoryManager
    hm = HistoryManager.__new__(HistoryManager)
    hm.persona_id = PERSONA_ID
    hm.messages = []
    hm.memory_adapter = adapter
    return hm


def test_floor_is_not_unmet_with_a_dangling_self_row(session_factory, adapter):
    """kotori さんの報告の経路: 起点が実在しない発言を指す自行があっても、
    最終防衛ラインは "unmet" (= 返事の見送り) にならない。読み戻しも例外を
    出さない。行は消え、次の成功した返事の touch が立て直す。"""
    _messages(adapter)
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    persona = _persona(adapter, _real_history(adapter))
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM):
        assert lc.maybe_run_window_refill(persona, "room", model_key=MODEL) == "skip"
        assert lc.ensure_window_floor(persona, "room", model_key=MODEL) != "unmet"
    assert lc.load_anchor_entry(PERSONA_ID, MODEL) is None


def test_floor_with_a_dangling_self_row_starts_from_the_frontier(
    session_factory, adapter,
):
    """最前線があれば、窓はそこから始まる (そのモデルの初回と同じ道)。窓が
    残す量に足りないので、床は最前線より古い会話を読み足して新しい行を立てる。"""
    ids = _messages(adapter)
    _with_frontier(adapter, ids[:2])
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    persona = _persona(adapter, _real_history(adapter))
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "perception_blocks_for", return_value=[]), \
            patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                  lambda *a, **k: None):
        assert lc.ensure_window_floor(persona, "room", model_key=MODEL) == "ok"
    # 最前線 ids[2] から、その手前の ids[0..1] を読み足した位置に立て直された
    assert _row_anchor(lc) == ids[0]


# ---------------------------------------------------------------------------
# 並行する書き手の保護 (条件付き削除)
# ---------------------------------------------------------------------------


def test_concurrent_writer_between_check_and_delete_survives(
    session_factory, adapter, caplog,
):
    """確認と削除の間に別の書き手が実在する新しい起点を書いたら、行は残る。"""
    ids = _messages(adapter)
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, GONE)
    from sai_memory.memory import storage as storage_mod
    real = storage_mod.get_existing_message_ids

    def _check_then_concurrent_write(conn, message_ids):
        found = real(conn, message_ids)
        # 照会の後・削除の前に、別の書き手 (成功した返事の touch 等) が新しい起点を書く
        assert lc.upsert_anchor_entry(PERSONA_ID, MODEL, {
            "anchor_id": ids[4],
            "updated_at": datetime.now().replace(microsecond=0).isoformat(),
        })
        return found

    with patch("sai_memory.memory.storage.get_existing_message_ids",
               side_effect=_check_then_concurrent_write), \
            caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        lc.resolve_metabolism_anchor(_persona(adapter), model_key=MODEL)
    assert _row_anchor(lc) == ids[4]  # 新しい起点は潰されない
    assert any("changed concurrently" in r.getMessage() for r in _dangling_warnings(caplog))


def test_delete_anchor_entry_if_matches_is_a_compare_and_delete(session_factory):
    lc = _lifecycle(session_factory)
    _hot_row(lc, MODEL, "m-new")
    assert lc.delete_anchor_entry_if_matches(PERSONA_ID, MODEL, GONE) is False
    assert _row_anchor(lc) == "m-new"
    assert lc.delete_anchor_entry_if_matches(PERSONA_ID, MODEL, "m-new") is True
    assert lc.load_anchor_entry(PERSONA_ID, MODEL) is None
    assert lc.delete_anchor_entry_if_matches(PERSONA_ID, MODEL, "m-new") is False


def test_get_existing_message_ids_is_thread_agnostic(adapter):
    main = _messages(adapter, 2)
    side = _messages(adapter, 1, thread_suffix="side", start=10)
    from sai_memory.memory.storage import get_existing_message_ids
    assert get_existing_message_ids(adapter.conn, [main[0], side[0], GONE]) == {main[0], side[0]}
    assert get_existing_message_ids(adapter.conn, []) == set()
