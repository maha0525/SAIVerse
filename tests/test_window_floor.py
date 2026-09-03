"""最終防衛ライン (docs/issues/window_floor_and_refill_redesign.md 設計 0) のテスト。

固定する不変条件:

**ペルソナは、窓の会話が残す量を下回った状態で発話しない。埋める材料 (起点より
古い会話) があるかぎり。**

読み戻し (§15) がどんな理由で埋め切れなくても、発話の直前に起点より古い会話を
不足分だけ生で読み足す。書き込みは読み戻しと同じ CAS、head の再 capture も同じ。
発火は上流の失敗の印 (WARNING + context-status の ``window_floor_applied_at``)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from sea.eviction_plan import ESTIMATED_FOLD_PLACEHOLDER_CHARS, Watermarks, stored_message_chars
from sea.session_lifecycle import SessionLifecycle
from sea.session_window import FoldedRange, SessionWindow, deserialize_folds

PERSONA_ID = "alice"
MODEL = "model-a"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _make_lifecycle(session_factory):
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        event_scheduler=None,
        meta_layer=SimpleNamespace(
            _load_judgment_config=lambda persona: {
                "keep_cache_alive": True,
                "cache_threshold_ratio": 0.3,
            }
        ),
        personas={},
    )
    runtime = SimpleNamespace(run_cache_keepalive=lambda pid, mk=None: None)
    lc = SessionLifecycle(runtime, manager)
    lc.upsert_anchor_entry(PERSONA_ID, MODEL, {
        "anchor_id": "m0",
        "updated_at": datetime.now().replace(microsecond=0).isoformat(),
        "ttl_seconds": 3600,
    })
    return lc


def _msg(mid, at, chars):
    return {"id": mid, "content": "x" * chars, "created_at": at}


def _entry(eid, source_ids, short_id=None):
    return SimpleNamespace(id=eid, source_ids=list(source_ids), short_id=short_id)


def _ph(first_live_mid, chars=ESTIMATED_FOLD_PLACEHOLDER_CHARS, at=100):
    return {
        "id": f"folded:{first_live_mid}", "content": "p" * chars,
        "created_at": at, "metadata": {"__folded_range__": True},
    }


def _window(anchor_id, presented, raw=None, folds=None):
    return SessionWindow(
        anchor_id=anchor_id,
        raw=list(raw if raw is not None else presented),
        presented=list(presented),
        folds=list(folds or []),
    )


def _persona(before, *, ready=False):
    """起点 m0 の手前に ``before`` (時系列昇順) がある履歴。

    ``get_history_before_anchor`` は要求された起点より古い分だけを返す
    (起点が ``before`` の外なら全部)。``get_history_from_anchor`` は指定の行から
    窓の末尾 (m0) まで。"""
    ids = [m["id"] for m in before]
    rows = list(before) + [_msg("m0", 200, 1000)]

    def _before_anchor(anchor_id, **_k):
        if anchor_id in ids:
            return list(before[: ids.index(anchor_id)])
        return list(before)

    def _from_anchor(start_id, **_k):
        idx = next((i for i, m in enumerate(rows) if m["id"] == start_id), None)
        return rows[idx:] if idx is not None else []

    return SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        history_manager=SimpleNamespace(
            get_history_before_anchor=_before_anchor,
            get_history_from_anchor=_from_anchor,
        ),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: ready),
    )


WM = Watermarks(low=1000, target=5000, high=10_000)
BEFORE = [_msg(f"b{i}", 100 + i, 1000) for i in range(4)]  # b0..b3 = 4,000 字


def _run(lc, persona, window, wm=WM):
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window", return_value=window):
        return lc.ensure_window_floor(persona, "room")


def _rows_after(lc, persona, window):
    """フック後の窓 (書かれた起点から) の保存行の字数を、書かれた記録で組んで測る。"""
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    folds = deserialize_folds(entry.get("folded_ranges"))
    before_ids = [m["id"] for m in BEFORE]
    start = before_ids.index(entry["anchor_id"]) if entry["anchor_id"] in before_ids else None
    restored = BEFORE[start:] if start is not None else []
    presented = lc._present_with_folds(persona, list(restored) + list(window.raw), folds)
    return stored_message_chars(presented)


def test_floor_fills_rows_up_to_target_from_older_conversation(session_factory, caplog):
    """(a) 起点より古い会話が残す量以上あるとき、フック後の窓の保存行 ≥ 残す量。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    window = _window("m0", [_msg("m0", 200, 1000)])  # 行 1,000 < 残す量 5,000
    with caplog.at_level(logging.WARNING, logger="sea.session_lifecycle"):
        assert _run(lc, persona, window) == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    assert entry["anchor_id"] == "b0"  # 読み足した最古の行
    assert _rows_after(lc, persona, window) >= WM.target
    assert not deserialize_folds(entry.get("folded_ranges"))  # 覆うあらすじ無し = 記録なし
    assert lc.window_floor_applied_at(PERSONA_ID, MODEL) is not None
    assert any("window floor applied" in r.getMessage() for r in caplog.records)


def test_floor_holds_when_refill_planned_nothing_across_a_straddling_fold(
    session_factory,
):
    """(b) 読み戻しが None を返す形 (起点をまたぐ圧縮区間) でも (a) が成立する。

    またぐ区間は生に戻る (presented_raw)。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    straddling = FoldedRange(message_ids=["b3", "m0"], chronicle_entry_ids=["e_f"])
    window = _window(
        "m0", [_ph("m0", chars=100)], raw=[_msg("m0", 200, 1000)], folds=[straddling],
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window", return_value=window), \
            patch.object(lc, "_plan_window_refill", return_value=None):
        assert lc.maybe_run_window_refill(persona, "room") == "skip"
        assert lc.ensure_window_floor(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e_f"]]
    assert saved[0].presented_raw is True
    assert _rows_after(lc, persona, window) >= WM.target


def test_floor_writes_nothing_when_rows_meet_target(session_factory):
    """(c) 保存行が残す量以上なら何も書かない。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    window = _window("m0", [_msg("m0", 200, 5000)])
    with patch.object(lc, "_write_refill") as write:
        assert _run(lc, persona, window) == "skip"
    write.assert_not_called()
    assert lc.window_floor_applied_at(PERSONA_ID, MODEL) is None


def test_floor_writes_nothing_without_older_conversation(session_factory):
    """(d) 起点より古い会話が無ければ何も書かない (埋める材料が無い)。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona([])
    window = _window("m0", [_msg("m0", 200, 1000)])
    with patch.object(lc, "_write_refill") as write:
        assert _run(lc, persona, window) == "skip"
    write.assert_not_called()
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "m0"


def test_floor_records_covering_entries_as_presented_raw_folds(session_factory):
    """(e) 覆うあらすじがある範囲は presented_raw の圧縮区間として記録される
    (head の除外名簿に載って二重提示を防ぐ)。新しい起点をまたぐエントリは
    記録しない (部分生存の区間は digest に倒れて読み足した行が縮むため)。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE, ready=True)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [
        _entry("e0", ["older", "b0"]),  # 新しい起点 b0 をまたぐ → 見送り
        _entry("e1", ["b0", "b1"], short_id=1),
        _entry("e2", ["b2", "b3"], short_id=2),
    ]
    with patch("sai_memory.arasuji.storage.get_entries_covering_messages",
               return_value=entries):
        assert _run(lc, persona, window) == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e1"], ["e2"]]
    assert [f.message_ids for f in saved] == [["b0", "b1"], ["b2", "b3"]]
    assert all(f.presented_raw for f in saved)
    assert [f.chronicle_short_ids for f in saved] == [[1], [2]]
    # 印付きなので提示は生のまま — 行は残す量に届く
    assert _rows_after(lc, persona, window) >= WM.target


def test_floor_skips_models_without_watermarks(session_factory):
    """水位を持たない model (軽量 model の窓など) は従来どおり skip。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    with patch.object(lc, "get_metabolism_watermarks", return_value=None), \
            patch.object(lc, "resolve_metabolism_anchor") as resolve, \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room", model_key="lite") == "skip"
    resolve.assert_not_called()
    write.assert_not_called()


def test_run_meta_user_applies_the_floor_for_every_pulse_type():
    """フックは非常畳み → 読み戻しの直後、全 pulse_type に置く。読み戻しは
    user Pulse だけ (§15-4) だが、最終防衛ラインは自律の発話にも効く。"""
    from sea.runtime import SEARuntime

    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(
        persona_name="p", persona_id="pid", model="m", llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()), execution_state={},
    )
    playbook = SimpleNamespace(
        name="meta_user/exec", start_node="exec", context_requirements=None,
    )
    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])
    runtime._compile_with_langgraph = Mock(return_value=["ok"])
    runtime.session_lifecycle.maybe_run_metabolism = Mock()
    runtime.session_lifecycle.maybe_run_emergency_precompaction = Mock(return_value="skip")
    runtime.session_lifecycle.maybe_run_window_refill = Mock(return_value="skip")
    runtime.session_lifecycle.ensure_window_floor = Mock(return_value="skip")

    runtime.run_meta_user(persona=persona, user_input="hello", building_id="b1")
    runtime.session_lifecycle.maybe_run_window_refill.assert_called_once()
    runtime.session_lifecycle.ensure_window_floor.assert_called_once()

    runtime.session_lifecycle.maybe_run_window_refill.reset_mock()
    runtime.session_lifecycle.ensure_window_floor.reset_mock()
    runtime.run_meta_user(
        persona=persona, user_input="", building_id="b1", pulse_type="auto",
    )
    runtime.session_lifecycle.maybe_run_window_refill.assert_not_called()
    runtime.session_lifecycle.ensure_window_floor.assert_called_once()
    assert runtime.session_lifecycle.ensure_window_floor.call_args.kwargs["model_key"]


def _straddle_persona(older, *, calls=None):
    """起点 m0 をまたぐ区間を持つ履歴。

    ``get_history_from_anchor`` は区間の先頭から窓の末尾まで (BEFORE + m0)、
    ``get_history_before_anchor`` はさらに古い材料 ``older`` を返す。"""
    rows = list(BEFORE) + [_msg("m0", 200, 1000)]

    def _from_anchor(start_id, **_k):
        idx = next(i for i, m in enumerate(rows) if m["id"] == start_id)
        return rows[idx:]

    def _before_anchor(anchor_id, **kwargs):
        if calls is not None:
            calls.append((anchor_id, kwargs.get("max_chars")))
        return list(older)

    return SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        history_manager=SimpleNamespace(
            get_history_from_anchor=_from_anchor,
            get_history_before_anchor=_before_anchor,
        ),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: False),
    )


def test_floor_restores_the_whole_straddling_fold_even_when_larger_than_the_shortfall(
    session_factory,
):
    """起点がまたぐ区間の左側 (4,000 字) が不足分 (1,900 字) より大きくても、
    区間は丸ごと窓に入り、行は残す量に届く。不足分だけ読むと区間が新しい
    起点をまたいだまま digest に倒れ、行が残す量を下回ったままになる。"""
    lc = _make_lifecycle(session_factory)
    calls = []
    persona = _straddle_persona([], calls=calls)
    straddling = FoldedRange(
        message_ids=["b0", "b1", "b2", "b3", "m0"], chronicle_entry_ids=["e_f"],
    )
    window = _window(
        "m0", [_ph("m0", chars=100)], raw=[_msg("m0", 200, 1000)], folds=[straddling],
    )
    wm = Watermarks(low=500, target=2000, high=10_000)
    with patch.object(lc, "_resolve_fold_digest", lambda persona, f: "d" * 100):
        assert _run(lc, persona, window, wm=wm) == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    assert entry["anchor_id"] == "b0"  # 区間の最古の行 (不足分 1,900 字より左)
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e_f"]]
    assert saved[0].presented_raw is True
    assert _rows_after(lc, persona, window) == 5000 >= wm.target
    assert calls == []  # 区間だけで足りたので、さらに古い方は読まない


def test_floor_extends_past_the_straddling_fold_when_still_short(session_factory):
    """またぐ区間 (b2, b3) を丸ごと戻しても足りなければ、区間の先頭から
    さらに古い方へ不足分だけ読み足す。"""
    lc = _make_lifecycle(session_factory)
    calls = []
    persona = _straddle_persona(BEFORE[:2], calls=calls)
    straddling = FoldedRange(message_ids=["b2", "b3", "m0"], chronicle_entry_ids=["e_f"])
    window = _window(
        "m0", [_ph("m0", chars=100)], raw=[_msg("m0", 200, 1000)], folds=[straddling],
    )
    with patch.object(lc, "_resolve_fold_digest", lambda persona, f: "d" * 100):
        assert _run(lc, persona, window) == "ok"  # 残す量 5,000
    # 区間で 3,000 → 残り 2,000 を区間の先頭 b2 から古い方へ
    assert calls == [("b2", 2000)]
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e_f"]]
    assert saved[0].presented_raw is True
    assert _rows_after(lc, persona, window) == 5000 >= WM.target


def test_floor_retries_once_after_a_cas_mismatch(session_factory):
    """書き込みが CAS で棄却されたら、新しい起点から一度だけ計画し直す →
    二度目が書ければ "ok" (Codex 一巡目 #1)。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    window = _window("m0", [_msg("m0", 200, 1000)])
    real = lc._write_refill
    calls = []

    def _flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return False  # 1 回目: 別入口が起点を動かした (CAS 不一致)
        return real(*args, **kwargs)

    with patch.object(lc, "_write_refill", side_effect=_flaky):
        assert _run(lc, persona, window) == "ok"
    assert len(calls) == 2
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "b0"


def test_floor_reports_unmet_when_the_write_keeps_failing(session_factory):
    """二度書けなければ "unmet" — 材料があるのに行が残す量に届かないまま。
    呼び出し側 (run_meta_user) がこれで発話を見送る。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    window = _window("m0", [_msg("m0", 200, 1000)])
    with patch.object(lc, "_write_refill", return_value=False) as write:
        assert _run(lc, persona, window) == "unmet"
    assert write.call_count == 2
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "m0"
    assert lc.window_floor_applied_at(PERSONA_ID, MODEL) is None


def test_floor_reports_unmet_on_exception(session_factory):
    """例外も "unmet" — 黙って fail-open にしない。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window", side_effect=RuntimeError("db down")):
        assert lc.ensure_window_floor(persona, "room") == "unmet"


def _runtime_for_floor_tests():
    from sea.runtime import SEARuntime

    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(
        persona_name="p", persona_id="pid", model="m", llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()), execution_state={},
    )
    playbook = SimpleNamespace(
        name="meta_user/exec", start_node="exec", context_requirements=None,
    )
    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])
    runtime._compile_with_langgraph = Mock(return_value=["ok"])
    runtime.session_lifecycle.maybe_run_metabolism = Mock()
    runtime.session_lifecycle.maybe_run_emergency_precompaction = Mock(return_value="skip")
    runtime.session_lifecycle.maybe_run_window_refill = Mock(return_value="skip")
    return runtime, persona


def test_run_meta_user_raises_when_the_floor_is_unmet():
    """"unmet" (または床の例外) なら Playbook を走らせず、型付き例外
    WindowFloorUnmetError を送出する — 不変条件が発話より優先。ユーザー Pulse
    には error イベントを**一度だけ** (type/error_code/content)、自律 Pulse は
    ログだけ。[] を返さない — PulseController が "completed" と記帳して
    schedule の occurrence が消費されるため (Codex 二巡目 #2)。"""
    from sea.runtime_context import WindowFloorUnmetError

    runtime, persona = _runtime_for_floor_tests()
    runtime.session_lifecycle.ensure_window_floor = Mock(return_value="unmet")
    events = []
    with pytest.raises(WindowFloorUnmetError):
        runtime.run_meta_user(
            persona=persona, user_input="hello", building_id="b1",
            event_callback=events.append,
        )
    runtime._compile_with_langgraph.assert_not_called()
    errors = [e for e in events if e.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["error_code"] == "window_floor_unmet"
    assert errors[0]["content"] == (
        "記憶の窓を用意できなかったため、この応答を見送りました。次に話しかけると再試行します。"
    )

    events.clear()
    with pytest.raises(WindowFloorUnmetError):
        runtime.run_meta_user(
            persona=persona, user_input="", building_id="b1", pulse_type="auto",
            event_callback=events.append,
        )
    runtime._compile_with_langgraph.assert_not_called()
    assert events == []  # 自律 Pulse には通知しない

    runtime.session_lifecycle.ensure_window_floor = Mock(side_effect=RuntimeError("boom"))
    with pytest.raises(WindowFloorUnmetError):
        runtime.run_meta_user(
            persona=persona, user_input="hello", building_id="b1",
            event_callback=events.append,
        )
    runtime._compile_with_langgraph.assert_not_called()
    assert len([e for e in events if e.get("error_code") == "window_floor_unmet"]) == 1

    # "ok" / "skip" は従来どおり走る
    runtime.session_lifecycle.ensure_window_floor = Mock(return_value="ok")
    assert runtime.run_meta_user(
        persona=persona, user_input="hello", building_id="b1",
    ) == ["ok"]


class _History:
    """正典順の行を持つ history_manager の最小フェイク (窓の読み出し 2 本)。"""

    def __init__(self, rows):
        self.rows = list(rows)

    def _index(self, mid):
        return next((i for i, m in enumerate(self.rows) if m["id"] == mid), None)

    def get_history_from_anchor(self, anchor_id, **_k):
        i = self._index(anchor_id)
        return list(self.rows[i:]) if i is not None else []

    def get_history_before_anchor(self, anchor_id, *, max_chars, **_k):
        i = self._index(anchor_id)
        if i is None or max_chars <= 0:
            return []
        out, acc = [], 0
        for m in reversed(self.rows[:i]):
            out.append(m)
            acc += len(m["content"])
            if acc >= max_chars:
                break
        return list(reversed(out))


def test_preflight_converges_when_a_straddling_fold_alone_exceeds_high(session_factory):
    """Codex 一巡目 #4: またぐ区間だけで上限を超える窓から、応答前の三段
    (非常畳み → 読み戻し → 最終防衛ライン) を三回続けて回しても、二回目と
    三回目の起点・圧縮区間が同じ (fold → 生 → fold の往復にならない)。

    一回目: 読み戻しの前段が区間を生に戻し (行 11,000 > 上限 8,000、WARNING)、
    同じ Pulse の非常畳み (知覚の消費の後) の印戻し (§15-3) が区間を digest に
    戻して編纂ゼロで完了。二回目: 区間は窓の中に全部入っているので、読み戻しは
    開けず (開くと残す量超え)、床も材料なしで skip、非常畳みも発火しない。
    三回目も同じ姿 — 収束する。"""
    import os as _os
    lc = _make_lifecycle(session_factory)
    rows = [_msg(f"b{i}", 100 + i, 1000) for i in range(10)] + [_msg("m0", 200, 1000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, current_building_id="room",
        history_manager=_History(rows),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    straddling = FoldedRange(
        message_ids=[f"b{i}" for i in range(10)] + ["m0"], chronicle_entry_ids=["e_f"],
    )
    lc.save_folded_ranges(PERSONA_ID, MODEL, [straddling])
    wm = Watermarks(low=1000, target=5000, high=8000)
    lc.is_chronicle_enabled_for_persona = lambda p: True
    lc.ensure_recall_embeddings = lambda p: None
    lc._retry_extraction_backlog = lambda p, **kw: None
    lc._drop_dead_folds = lambda p, mk, w: w

    def _snapshot():
        entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
        return entry["anchor_id"], [
            (tuple(f.message_ids), tuple(f.chronicle_entry_ids), f.presented_raw)
            for f in deserialize_folds(entry.get("folded_ranges"))
        ]

    def _preflight():
        # run_meta_user の順 (Codex 四巡目 #4): 読み戻し → 床 (永続化より前) →
        # 知覚の消費 → 非常畳み → LLM
        lc.maybe_run_window_refill(persona, "room", model_key=MODEL)
        assert lc.ensure_window_floor(persona, "room", model_key=MODEL) != "unmet"
        after_floor = _snapshot()
        lc.maybe_run_emergency_precompaction(persona, "room", None, model_key=MODEL)
        return after_floor, _snapshot()

    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "_resolve_fold_digest", lambda persona, f: "d" * 100), \
            patch.object(lc, "perception_blocks_for", return_value=[]), \
            patch("sai_memory.arasuji.storage.get_entries_covering_messages",
                  return_value=[]), \
            patch("sai_memory.memory.storage.filter_chronicle_eligible_ids",
                  return_value=[]), \
            patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                  lambda *a, **k: None), \
            patch.dict(_os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}):
        first = _preflight()
        second = _preflight()
        third = _preflight()

    raw_state = ("b0", [(tuple(straddling.message_ids), ("e_f",), True)])
    digest_state = ("b0", [(tuple(straddling.message_ids), ("e_f",), False)])
    # 一回目: 読み戻しの前段がまたぐ区間を丸ごと生で戻し (行 11,000 > 上限 —
    # 前段は外さない)、同じ Pulse の非常畳みが印戻しで digest に戻す。
    assert first == (raw_state, digest_state)
    # 二回目以降: 区間は窓の中に全部入っていて開けず (開くと残す量超え)、床も
    # 材料なし、非常畳みも発火しない — そのまま安定 (往復しない)
    assert second == (digest_state, digest_state)
    assert third == second


def test_floor_reads_all_older_conversation_when_it_is_smaller_than_the_shortfall(
    session_factory,
):
    """古い会話はあるが不足分より少ない → 全部読んで書き、"ok" (行は残す量に
    届かないが、不変条件は「埋める材料があるかぎり」)。"ok" なので Playbook は
    走る (run_meta_user は "unmet" だけを見送る)。"""
    lc = _make_lifecycle(session_factory)
    older = BEFORE[:2]  # 2,000 字 < 不足分 4,000 字
    persona = _persona(older)
    window = _window("m0", [_msg("m0", 200, 1000)])
    assert _run(lc, persona, window) == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    assert entry["anchor_id"] == "b0"  # いちばん古い行まで動いた
    presented = lc._present_with_folds(
        persona, list(older) + list(window.raw),
        deserialize_folds(entry.get("folded_ranges")),
    )
    assert stored_message_chars(presented) == 3000 < WM.target
    assert lc.window_floor_applied_at(PERSONA_ID, MODEL) is not None


def test_floor_read_failure_is_unmet_not_skip(session_factory):
    """Codex 二巡目 #1: 履歴の読み失敗は「古い会話が無い (skip)」ではなく
    "unmet"。床の読みは厳格モード (raise_on_error=True) で呼ぶ。"""
    lc = _make_lifecycle(session_factory)
    seen = []

    def _before_anchor(anchor_id, **kwargs):
        seen.append(kwargs.get("raise_on_error"))
        if kwargs.get("raise_on_error"):
            raise RuntimeError("database is locked")
        return []  # 既定の縮退 (他の呼び出し側の従来挙動)

    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        history_manager=SimpleNamespace(get_history_before_anchor=_before_anchor),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: False),
    )
    window = _window("m0", [_msg("m0", 200, 1000)])
    with patch.object(lc, "_write_refill") as write:
        assert _run(lc, persona, window) == "unmet"
    assert seen == [True]
    write.assert_not_called()
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "m0"


def test_floor_straddle_read_failure_is_unmet(session_factory):
    """またぐ区間の読み (get_history_from_anchor) の失敗も "unmet"。"""
    lc = _make_lifecycle(session_factory)

    def _from_anchor(start_id, **kwargs):
        if kwargs.get("raise_on_error"):
            raise RuntimeError("database is locked")
        return []

    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        history_manager=SimpleNamespace(
            get_history_from_anchor=_from_anchor,
            get_history_before_anchor=lambda *a, **k: [],
        ),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: False),
    )
    straddling = FoldedRange(message_ids=["b3", "m0"], chronicle_entry_ids=["e_f"])
    window = _window(
        "m0", [_ph("m0", chars=100)], raw=[_msg("m0", 200, 1000)], folds=[straddling],
    )
    assert _run(lc, persona, window) == "unmet"


def test_adapter_strict_read_raises_and_default_degrades(tmp_path, monkeypatch):
    """SAIMemoryAdapter.persona_messages_before_anchor / from_anchor:
    raise_on_error=True は DB 例外を伝え、既定は従来どおり空へ縮退する。"""
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")

    class _Embedder:
        def __init__(self, model=None, **kwargs):
            self.model_name = model

        def embed(self, texts, **kwargs):
            return [[0.0] * 3 for _ in texts]

    with patch("saiverse_memory.adapter.Embedder", _Embedder):
        from saiverse_memory import SAIMemoryAdapter
        persona_dir = tmp_path / "personas" / PERSONA_ID
        persona_dir.mkdir(parents=True)
        adapter = SAIMemoryAdapter(PERSONA_ID, persona_dir=persona_dir, resource_id=PERSONA_ID)
        try:
            mid = adapter.append_persona_message({
                "role": "user", "content": "hello", "timestamp": datetime.now().isoformat(),
            })
            assert mid is not None
            with patch("sai_memory.memory.storage.get_messages_before_id",
                       side_effect=RuntimeError("database is locked")):
                assert adapter.persona_messages_before_anchor(mid, max_chars=100) == []
                with pytest.raises(RuntimeError):
                    adapter.persona_messages_before_anchor(
                        mid, max_chars=100, raise_on_error=True,
                    )
            with patch("sai_memory.memory.storage.get_messages_from_id",
                       side_effect=RuntimeError("database is locked")):
                assert adapter.persona_messages_from_anchor(mid) == []
                with pytest.raises(RuntimeError):
                    adapter.persona_messages_from_anchor(mid, raise_on_error=True)
        finally:
            adapter.close()


def test_history_manager_threads_strict_mode_to_the_adapter():
    """HistoryManager の 2 本は raise_on_error を adapter へそのまま渡す。"""
    from persona.history_manager import HistoryManager

    calls = []

    class _Adapter:
        def is_ready(self):
            return True

        def persona_messages_from_anchor(self, anchor_id, **kwargs):
            calls.append(("from", kwargs.get("raise_on_error")))
            return []

        def persona_messages_before_anchor(self, anchor_id, **kwargs):
            calls.append(("before", kwargs.get("raise_on_error")))
            return []

    hm = HistoryManager.__new__(HistoryManager)
    hm.memory_adapter = _Adapter()
    hm.persona_id = PERSONA_ID
    hm.messages = []
    hm.get_history_from_anchor("m0", raise_on_error=True)
    hm.get_history_before_anchor("m0", max_chars=10, raise_on_error=True)
    hm.get_history_from_anchor("m0")
    hm.get_history_before_anchor("m0", max_chars=10)
    assert calls == [("from", True), ("before", True), ("from", False), ("before", False)]


def test_floor_unmet_leaves_no_persistence_behind_on_schedule_fires():
    """Codex 三巡目 #1: 三段は永続化より前。floor_unmet で見送られた schedule 発火
    は schedule プロンプトを記録せず知覚も消費しない。三度目 (成功) で一度だけ。"""
    from sea.runtime_context import WindowFloorUnmetError

    runtime, persona = _runtime_for_floor_tests()
    persona.history_manager.add_to_persona_only = Mock()
    persona.sai_memory = SimpleNamespace(
        flush_perception_buffer=Mock(), get_current_thread=lambda: "",
    )
    runtime.session_lifecycle.ensure_window_floor = Mock(
        side_effect=["unmet", "unmet", "skip"],
    )
    for _ in range(2):
        with pytest.raises(WindowFloorUnmetError):
            runtime.run_meta_user(
                persona=persona, user_input="<system>予定の時刻です</system>",
                building_id="b1", pulse_type="schedule",
            )
    persona.history_manager.add_to_persona_only.assert_not_called()
    persona.sai_memory.flush_perception_buffer.assert_not_called()
    runtime._compile_with_langgraph.assert_not_called()

    assert runtime.run_meta_user(
        persona=persona, user_input="<system>予定の時刻です</system>",
        building_id="b1", pulse_type="schedule",
    ) == ["ok"]
    assert persona.history_manager.add_to_persona_only.call_count == 1
    assert persona.sai_memory.flush_perception_buffer.call_count == 1
    runtime._compile_with_langgraph.assert_called_once()


def test_floor_anchor_read_failure_is_unmet(session_factory):
    """Codex 三巡目 #2: session_anchor 行の読み失敗は「起点なし = skip」ではなく
    "unmet"。床は resolve_metabolism_anchor(strict=True) を使い、縮退版の
    load_anchor_entries は呼ばない。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE)
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "load_anchor_entries_strict",
                         side_effect=RuntimeError("db down")), \
            patch.object(lc, "load_anchor_entries", return_value={}) as lenient, \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    lenient.assert_not_called()
    write.assert_not_called()


def test_floor_skips_when_there_is_genuinely_no_anchor(session_factory):
    """行も最前線も本当に無い (ブートストラップ前) は従来どおり "skip"。"""
    manager = SimpleNamespace(
        SessionLocal=session_factory, event_scheduler=None,
        meta_layer=SimpleNamespace(_load_judgment_config=lambda persona: {}),
        personas={},
    )
    lc = SessionLifecycle(SimpleNamespace(run_cache_keepalive=lambda *a, **k: None), manager)
    import threading
    # 器はあるが編纂の器 (arasuji_entries) が無い = 最前線が存在しない
    conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: None))
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        history_manager=_persona(BEFORE).history_manager,
        sai_memory=SimpleNamespace(
            conn=conn, is_ready=lambda: True, _db_lock=threading.RLock(),
        ),
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room") == "skip"
    write.assert_not_called()


def test_strict_anchor_resolution_raises_on_frontier_failure(session_factory):
    """strict=True は最前線の導出失敗を例外で伝える。既定は縮退 (前進しない)。"""
    # 冷えた行 → 最前線を引きに行く (同じ anchor への upsert は温度を据え置く
    # ので、温かい行を作る _make_lifecycle は使わず素の lifecycle に立てる)
    manager = SimpleNamespace(
        SessionLocal=session_factory, event_scheduler=None,
        meta_layer=SimpleNamespace(_load_judgment_config=lambda persona: {}),
        personas={},
    )
    lc = SessionLifecycle(SimpleNamespace(run_cache_keepalive=lambda *a, **k: None), manager)
    lc.upsert_anchor_entry(PERSONA_ID, MODEL, {
        "anchor_id": "m0",
        "updated_at": (datetime.now() - timedelta(days=3)).isoformat(),
        "ttl_seconds": 300,
    })
    assert not lc._anchor_entry_is_hot(lc.load_anchor_entry(PERSONA_ID, MODEL), MODEL, PERSONA_ID)
    # 器 (arasuji_entries) はある、と答える conn — 照会そのものが失敗する形
    import threading
    conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: (1,)))
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        sai_memory=SimpleNamespace(
            conn=conn, is_ready=lambda: True, _db_lock=threading.RLock(),
        ),
    )
    with patch("sai_memory.arasuji.storage.get_frontier_anchor_id",
               side_effect=RuntimeError("db down")):
        assert lc.resolve_metabolism_anchor(persona, model_key=MODEL) == ("m0", "self")
        with pytest.raises(RuntimeError):
            lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True)


def test_history_manager_strict_read_raises_only_when_the_store_is_broken():
    """Codex 三巡目 #3 / 五巡目 #2: 厳格モードで例外にするのは器が **broken**
    (有効なのに接続が無い) のときだけ。adapter なし・設定で無効 (従来のメモリ上
    モード) は厳格モードでも写しを読む。既定はどれも縮退。"""
    from persona.history_manager import (
        HistoryManager, HistoryStoreUnavailableError, memory_store_state,
    )

    rows = list(BEFORE) + [_msg("m0", 200, 1000)]
    hm = HistoryManager.__new__(HistoryManager)
    hm.persona_id = PERSONA_ID
    hm.messages = rows
    disabled = SimpleNamespace(
        is_ready=lambda: False, settings=SimpleNamespace(memory_enabled=False),
    )
    for adapter in (None, disabled):
        hm.memory_adapter = adapter
        assert memory_store_state(adapter) == "absent"
        assert hm.get_history_from_anchor("m0", raise_on_error=True) == [rows[-1]]
        assert hm.get_history_before_anchor("m0", max_chars=10, raise_on_error=True) == [BEFORE[-1]]
    broken = SimpleNamespace(is_ready=lambda: False)
    hm.memory_adapter = broken
    assert memory_store_state(broken) == "broken"
    assert hm.get_history_before_anchor("m0", max_chars=10) == [BEFORE[-1]]  # 既定は縮退
    with pytest.raises(HistoryStoreUnavailableError):
        hm.get_history_before_anchor("m0", max_chars=10, raise_on_error=True)
    with pytest.raises(HistoryStoreUnavailableError):
        hm.get_history_from_anchor("m0", raise_on_error=True)


def test_floor_store_unavailable_is_unmet(session_factory):
    """記憶の器が未準備のまま床を通ると "unmet" (skip ではない)。"""
    from persona.history_manager import HistoryManager

    lc = _make_lifecycle(session_factory)
    hm = HistoryManager.__new__(HistoryManager)
    hm.persona_id = PERSONA_ID
    hm.messages = list(BEFORE)  # 写しには材料があるが、厳格モードは使わない
    hm.memory_adapter = SimpleNamespace(is_ready=lambda: False)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm,
        sai_memory=SimpleNamespace(is_ready=lambda: False),
    )
    window = _window("m0", [_msg("m0", 200, 1000)])
    with patch.object(lc, "_write_refill") as write:
        assert _run(lc, persona, window) == "unmet"
    write.assert_not_called()


def test_emergency_precompaction_runs_after_the_flush_and_the_floor_before_it():
    """Codex 四巡目 #4: 読み戻し・床は永続化 (知覚の消費・定時の文面) より前、
    非常畳みは知覚の消費の後・Playbook の前。"""
    runtime, persona = _runtime_for_floor_tests()
    seq = []
    persona.sai_memory = SimpleNamespace(
        flush_perception_buffer=lambda **k: seq.append("flush"),
        get_current_thread=lambda: "",
    )
    persona.history_manager.add_to_persona_only = lambda msg: seq.append("schedule_prompt")
    runtime.session_lifecycle.maybe_run_window_refill = Mock(
        side_effect=lambda *a, **k: (seq.append("refill"), "skip")[1],
    )
    runtime.session_lifecycle.ensure_window_floor = Mock(
        side_effect=lambda *a, **k: (seq.append("floor"), "skip")[1],
    )
    runtime.session_lifecycle.maybe_run_emergency_precompaction = Mock(
        side_effect=lambda *a, **k: (seq.append("emergency"), "skip")[1],
    )
    runtime._compile_with_langgraph = Mock(
        side_effect=lambda *a, **k: (seq.append("playbook"), ["ok"])[1],
    )
    assert runtime.run_meta_user(persona=persona, user_input="hello", building_id="b1") == ["ok"]
    assert seq == ["refill", "floor", "flush", "emergency", "playbook"]
    seq.clear()
    assert runtime.run_meta_user(
        persona=persona, user_input="<system>予定の時刻です</system>",
        building_id="b1", pulse_type="schedule",
    ) == ["ok"]
    assert seq == ["floor", "flush", "schedule_prompt", "emergency", "playbook"]


def _strict_history(rows, *, ready):
    """本物の HistoryManager に、メモリ上の写しと (準備済み/未準備の) adapter を持たせる。"""
    from persona.history_manager import HistoryManager

    hm = HistoryManager.__new__(HistoryManager)
    hm.persona_id = PERSONA_ID
    hm.messages = list(rows)

    import threading

    class _Adapter:
        conn = object()
        _db_lock = threading.RLock()

        def is_ready(self):
            return ready

        def persona_messages_from_anchor(self, anchor_id, **kwargs):
            idx = next((i for i, m in enumerate(rows) if m["id"] == anchor_id), None)
            return list(rows[idx:]) if idx is not None else []

        def persona_messages_before_anchor(self, anchor_id, **kwargs):
            idx = next((i for i, m in enumerate(rows) if m["id"] == anchor_id), None)
            return list(rows[:idx]) if idx else []

    hm.memory_adapter = _Adapter()
    return hm


def test_floor_window_read_is_strict_even_when_the_memory_copy_is_large(session_factory):
    """Codex 四巡目 #1: adapter 未準備のとき、メモリ上の写しが厚くても床は
    その窓を信じない — 厳格な読みが例外を出して "unmet"。既定の読みは写しへ
    縮退する (対比)。"""
    lc = _make_lifecycle(session_factory)
    big = [_msg(f"m{i}", 100 + i, 3000) for i in range(3)]  # 写しでは 9,000 字 ≥ 残す量
    hm = _strict_history(big, ready=False)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm,
        sai_memory=hm.memory_adapter,
    )
    # 既定の読みは写しへ縮退して「足りている」窓を返す
    assert stored_message_chars(lc.get_presented_window(persona, MODEL, "m0").presented) >= WM.target
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    write.assert_not_called()


def test_floor_missing_anchor_message_is_unmet(session_factory):
    """Codex 五巡目 #1: 提示対象の読みが空で、起点の行が messages に**物理的に
    無い** (scope 不問の照会で None) → 帳簿の破損 = "unmet"。"""
    lc = _make_lifecycle(session_factory)
    hm = _strict_history([_msg("other", 100, 1000)], ready=True)  # m0 が無い
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm,
        sai_memory=hm.memory_adapter,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch("sai_memory.memory.storage.get_message_position", return_value=None), \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    write.assert_not_called()


def test_floor_fills_when_the_anchor_is_discardable_with_no_presentable_rows_after_it(
    session_factory,
):
    """Codex 五巡目 #1: 起点の行が提示対象外 (discardable) で、その後ろにまだ
    提示対象の行が無い窓は正当 (行 0)。起点は物理的に在るので例外にせず、床が
    古い方から埋めて "ok"、起点は最古の読み足した行へ。"""
    import threading

    from persona.history_manager import HistoryManager

    hm = HistoryManager.__new__(HistoryManager)
    hm.persona_id = PERSONA_ID
    hm.messages = []

    class _Adapter:
        conn = object()
        _db_lock = threading.RLock()

        def is_ready(self):
            return True

        def persona_messages_from_anchor(self, anchor_id, **kwargs):
            return []  # 起点 m0 は discardable、後ろに提示対象の行はまだ無い

        def persona_messages_before_anchor(self, anchor_id, **kwargs):
            return list(BEFORE)

    hm.memory_adapter = _Adapter()
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm,
        sai_memory=hm.memory_adapter,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch("sai_memory.memory.storage.get_message_position", return_value=(100, 7)), \
            patch("sai_memory.arasuji.storage.get_entries_covering_messages",
                  return_value=[]):
        assert lc.ensure_window_floor(persona, "room") == "ok"
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "b0"


def _folds_json(lc):
    from database.models import SessionAnchor
    db = lc.manager.SessionLocal()
    try:
        row = db.query(SessionAnchor).filter_by(PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL).first()
        return row.FOLDED_RANGES_JSON
    finally:
        db.close()


def test_floor_fold_read_failure_is_unmet_and_keeps_the_folds(session_factory):
    """Codex 四巡目 #3: 圧縮区間の記録が読めなければ "unmet" で書かない —
    空の記録で上書きして既存の区間を消さない。"""
    lc = _make_lifecycle(session_factory)
    existing = FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e_keep"])
    lc.save_folded_ranges(PERSONA_ID, MODEL, [existing])
    before_json = _folds_json(lc)
    assert before_json
    hm = _strict_history(list(BEFORE) + [_msg("m0", 200, 1000)], ready=True)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm,
        sai_memory=SimpleNamespace(is_ready=lambda: False),
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "load_anchor_entry_strict", side_effect=RuntimeError("db down")):
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    assert _folds_json(lc) == before_json

    # 壊れた JSON も同じ — 既定の読みは空へ縮退するが、床は上書きしない
    from database.models import SessionAnchor
    db = lc.manager.SessionLocal()
    try:
        db.query(SessionAnchor).filter_by(PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL).update(
            {SessionAnchor.FOLDED_RANGES_JSON: "{broken"}, synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
    assert lc.load_folded_ranges(PERSONA_ID, MODEL) == []  # 既定は縮退
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")):
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    assert _folds_json(lc) == "{broken"


@pytest.fixture
def real_adapter(tmp_path, monkeypatch):
    """本物の SAIMemoryAdapter (memory.db 付き)。埋め込みはダミー。"""
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")

    class _Embedder:
        def __init__(self, model=None, **kwargs):
            self.model_name = model

        def embed(self, texts, **kwargs):
            return [[0.0] * 3 for _ in texts]

    with patch("saiverse_memory.adapter.Embedder", _Embedder):
        from saiverse_memory import SAIMemoryAdapter
        persona_dir = tmp_path / "personas" / PERSONA_ID
        persona_dir.mkdir(parents=True)
        adapter = SAIMemoryAdapter(PERSONA_ID, persona_dir=persona_dir, resource_id=PERSONA_ID)
        try:
            yield adapter
        finally:
            adapter.close()


def _bare_lifecycle(session_factory):
    manager = SimpleNamespace(
        SessionLocal=session_factory, event_scheduler=None,
        meta_layer=SimpleNamespace(_load_judgment_config=lambda persona: {}),
        personas={},
    )
    return SessionLifecycle(SimpleNamespace(run_cache_keepalive=lambda *a, **k: None), manager)


def _drop_arasuji_tables(conn):
    """編纂の器を消して「一度も編纂していない」memory.db にする (arasuji_entries は
    Memopedia 統合後はビュー)。"""
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name LIKE 'arasuji%'"
    ).fetchall()
    for name, kind in rows:
        conn.execute(f"DROP {'VIEW' if kind == 'view' else 'TABLE'} IF EXISTS {name}")
    conn.commit()


def test_floor_skips_for_a_brand_new_persona_without_arasuji_tables(
    session_factory, real_adapter,
):
    """Codex 四巡目 #2 (a): 編纂の器 (arasuji_entries) が無い新規ペルソナ — 最前線は
    「存在しない」(照会の失敗ではない) → 起点なし → "skip"。Playbook は走る
    ("skip" は run_meta_user が通す)。"""
    _drop_arasuji_tables(real_adapter.conn)
    assert not SessionLifecycle._arasuji_tables_exist(real_adapter.conn)
    # 器が無い状態では最前線の照会そのものが "no such table" で落ちる — それを
    # 厳格経路が「存在しない = None」と読めることの検算
    from sai_memory.arasuji.storage import get_frontier_anchor_id
    with pytest.raises(Exception):
        get_frontier_anchor_id(real_adapter.conn)
    real_adapter.append_persona_message({
        "role": "user", "content": "はじめまして", "timestamp": datetime.now().isoformat(),
    })
    lc = _bare_lifecycle(session_factory)  # 行なし
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, sai_memory=real_adapter,
        history_manager=_persona(BEFORE).history_manager,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "_write_refill") as write:
        assert lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True) == (None, "minimal")
        assert lc.ensure_window_floor(persona, "room") == "skip"
    write.assert_not_called()


def test_floor_skips_for_a_chronicle_disabled_persona(session_factory, real_adapter):
    """Codex 四巡目 #2 (b): Chronicle を使わないペルソナ (器はあっても一次エントリ
    が無い) — 最前線は None (正常) → "skip"。"""
    from sai_memory.arasuji.storage import init_arasuji_tables
    init_arasuji_tables(real_adapter.conn)
    real_adapter.append_persona_message({
        "role": "user", "content": "はじめまして", "timestamp": datetime.now().isoformat(),
    })
    lc = _bare_lifecycle(session_factory)
    lc.is_chronicle_enabled_for_persona = lambda p: False
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, sai_memory=real_adapter,
        history_manager=_persona(BEFORE).history_manager,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room") == "skip"
    write.assert_not_called()


def test_floor_frontier_io_failure_is_unmet(session_factory, real_adapter):
    """Codex 四巡目 #2 (c): 器はあるのに最前線の照会が I/O で失敗 → "unmet"。"""
    import sqlite3
    from sai_memory.arasuji.storage import init_arasuji_tables
    init_arasuji_tables(real_adapter.conn)
    real_adapter.append_persona_message({
        "role": "user", "content": "はじめまして", "timestamp": datetime.now().isoformat(),
    })
    lc = _bare_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, sai_memory=real_adapter,
        history_manager=_persona(BEFORE).history_manager,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch("sai_memory.arasuji.storage.get_frontier_anchor_id",
                  side_effect=sqlite3.OperationalError("disk I/O error")), \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    write.assert_not_called()


def test_floor_skips_when_sai_memory_is_absent(session_factory, caplog):
    """Codex 六巡目 #3: 床の保証は永続の器 (SAIMemory) に対して定義する。器が
    absent (adapter なし / 設定で無効 = 従来のメモリ上の履歴) なら床は履歴に
    触れずに "skip" (INFO はペルソナごと 1 度)。写しの窓が薄くても同じ —
    Playbook は走る ("skip" は run_meta_user が通す)。五巡目の「写しから埋める」
    契約はこの裁定で反転した。"""
    import logging as _logging

    def _touched(*a, **k):
        raise AssertionError("the floor must not read the in-memory history")

    hm = SimpleNamespace(
        get_history_from_anchor=_touched, get_history_before_anchor=_touched,
    )
    lc = _make_lifecycle(session_factory)
    disabled = SimpleNamespace(
        is_ready=lambda: False, settings=SimpleNamespace(memory_enabled=False),
    )
    for store in (None, disabled):
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, model=MODEL, history_manager=hm, sai_memory=store,
        )
        with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
                patch.object(lc, "resolve_metabolism_anchor") as resolve, \
                patch.object(lc, "_write_refill") as write, \
                caplog.at_level(_logging.INFO, logger="sea.session_lifecycle"):
            assert lc.ensure_window_floor(persona, "room") == "skip"
            assert lc.ensure_window_floor(persona, "room") == "skip"
        resolve.assert_not_called()
        write.assert_not_called()
    notices = [r for r in caplog.records if "window floor disabled: SAIMemory absent" in r.getMessage()]
    assert len(notices) == 1  # ペルソナごと 1 度
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "m0"


def test_frontier_probe_and_query_run_under_the_adapter_lock(session_factory):
    """Codex 五巡目 #3: 器の検査と最前線の照会は adapter の錠前の内側で一続き。"""
    lc = _bare_lifecycle(session_factory)
    lc.upsert_anchor_entry(PERSONA_ID, MODEL, {
        "anchor_id": "m0",
        "updated_at": (datetime.now() - timedelta(days=3)).isoformat(),
        "ttl_seconds": 300,  # 冷えた行 → 最前線を引く
    })

    class _Lock:
        held = False

        def __enter__(self):
            self.held = True

        def __exit__(self, *exc):
            self.held = False

    lock = _Lock()
    seen = []

    def _execute(*a, **k):
        seen.append(("probe", lock.held))
        return SimpleNamespace(fetchone=lambda: (1,))

    adapter = SimpleNamespace(
        conn=SimpleNamespace(execute=_execute), is_ready=lambda: True, _db_lock=lock,
    )
    persona = SimpleNamespace(persona_id=PERSONA_ID, model=MODEL, sai_memory=adapter)

    def _frontier(conn):
        seen.append(("query", lock.held))
        return None

    with patch("sai_memory.arasuji.storage.get_frontier_anchor_id", side_effect=_frontier):
        assert lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True) == ("m0", "self")
    assert seen == [("probe", True), ("query", True)]
    assert lock.held is False


def test_strict_fold_records_reject_semantically_broken_shapes(session_factory):
    """Codex 五巡目 #4: 厳格モードは形の壊れた記録 (``{}`` / 文字列の message_ids /
    型違いの id 列) を例外にする。既定は寛容なまま。床は "unmet" で書かない。"""
    from sea.session_window import deserialize_folds

    broken = [
        '[{}]',
        '[{"message_ids": "abc"}]',
        '[{"message_ids": ["m1"], "chronicle_entry_ids": [1]}]',
        '[{"message_ids": ["m1"], "chronicle_short_ids": ["7"]}]',
        '[{"message_ids": [""]}]',
    ]
    for raw in broken:
        deserialize_folds(raw)  # 既定: 例外にしない
        with pytest.raises(ValueError):
            deserialize_folds(raw, strict=True)
    good = '[{"message_ids": ["m1"], "chronicle_entry_ids": ["e1"], "chronicle_short_ids": [7], "presented_raw": true}]'
    assert len(deserialize_folds(good, strict=True)) == 1

    lc = _make_lifecycle(session_factory)
    from database.models import SessionAnchor
    db = lc.manager.SessionLocal()
    try:
        db.query(SessionAnchor).filter_by(PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL).update(
            {SessionAnchor.FOLDED_RANGES_JSON: '[{"message_ids": "abc"}]'},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
    hm = _strict_history(list(BEFORE) + [_msg("m0", 200, 1000)], ready=True)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm,
        sai_memory=hm.memory_adapter,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")):
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    assert _folds_json(lc) == '[{"message_ids": "abc"}]'


def test_floor_broken_store_without_anchor_row_is_unmet(session_factory):
    """Codex 六巡目 #1: 器が broken (有効なのに接続が無い) で行も無いとき、
    最前線の解決は None ではなく例外 → "unmet" (「行も最前線も無い = 起点なし
    = skip」に潰さない)。"""
    lc = _bare_lifecycle(session_factory)  # 行なし
    broken = SimpleNamespace(is_ready=lambda: False)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, sai_memory=broken,
        history_manager=_persona(BEFORE).history_manager,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "_write_refill") as write:
        with pytest.raises(RuntimeError):
            lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True)
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    write.assert_not_called()


def test_strict_resolve_does_not_advance_over_corrupted_folds(session_factory):
    """Codex 六巡目 #2: 冷えた行が最前線の手前にあり、圧縮区間の記録が壊れて
    いるとき、厳格な解決は**何も書かずに**例外 → FOLDED_RANGES_JSON も起点も
    そのまま (寛容に読んで書き戻すと劣化した列で上書きされる)。"""
    import threading

    lc = _bare_lifecycle(session_factory)
    lc.upsert_anchor_entry(PERSONA_ID, MODEL, {
        "anchor_id": "m0",
        "updated_at": (datetime.now() - timedelta(days=3)).isoformat(),
        "ttl_seconds": 300,  # 冷えた行
    })
    from database.models import SessionAnchor
    broken_json = '[{"message_ids": "abc"}]'
    db = lc.manager.SessionLocal()
    try:
        db.query(SessionAnchor).filter_by(PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL).update(
            {SessionAnchor.FOLDED_RANGES_JSON: broken_json}, synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
    conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: (1,)))
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        sai_memory=SimpleNamespace(conn=conn, is_ready=lambda: True, _db_lock=threading.RLock()),
        history_manager=_persona(BEFORE).history_manager,
    )
    with patch("sai_memory.arasuji.storage.get_frontier_anchor_id", return_value="m3"), \
            patch("sai_memory.arasuji.storage.compare_message_positions", return_value=1), \
            patch.object(lc, "_cap_advance_at_pan_marker", return_value="m3"):
        with pytest.raises(ValueError):
            lc.resolve_metabolism_anchor(persona, model_key=MODEL, strict=True)
        with patch.object(lc, "get_metabolism_watermarks", return_value=WM):
            assert lc.ensure_window_floor(persona, "room") == "unmet"
    entry = lc.load_anchor_entry(PERSONA_ID, MODEL)
    assert entry["anchor_id"] == "m0"  # 前進していない
    assert _folds_json(lc) == broken_json  # 上書きされていない


def test_floor_skips_with_absent_store_even_when_the_in_memory_window_is_short(
    session_factory,
):
    """Codex 六巡目 #3: absent + 写しの窓が薄い (行 < 残す量) でも床は "skip"。
    run_meta_user は "skip" を通すので Playbook は走る。"""
    from persona.history_manager import HistoryManager

    hm = HistoryManager.__new__(HistoryManager)
    hm.persona_id = PERSONA_ID
    hm.messages = [_msg("m0", 200, 100)]  # 写しの窓は 100 字
    hm.memory_adapter = None
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm, sai_memory=None,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM):
        assert lc.ensure_window_floor(persona, "room") == "skip"
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "m0"


def test_preview_refill_strict_mode_raises_on_a_broken_store(session_factory):
    """Codex 六巡目 #4: context-status の厳格モード (raise_on_error=True) は
    壊れた器を「読み戻しの適用なし」として返さず例外 (measurement_failed へ)。
    既定は従来どおり None。"""
    from persona.history_manager import HistoryStoreUnavailableError

    lc = _make_lifecycle(session_factory)  # 温かい行 m0
    hm = _strict_history([_msg("m0", 200, 1000)], ready=False)  # broken
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL, history_manager=hm,
        sai_memory=hm.memory_adapter,
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM):
        assert lc.preview_refilled_history(persona, MODEL) is None
        with pytest.raises(HistoryStoreUnavailableError):
            lc.preview_refilled_history(persona, MODEL, raise_on_error=True)


def test_floor_missing_history_manager_with_ready_store_is_unmet(session_factory):
    """Codex 七巡目 #2: 器は在る (ready) のに履歴の読み手が無い → skip ではなく
    "unmet" (器を検証しないまま喋らせない)。"""
    import threading

    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        sai_memory=SimpleNamespace(
            conn=object(), is_ready=lambda: True, _db_lock=threading.RLock(),
        ),
    )  # history_manager 属性なし
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "_write_refill") as write:
        assert lc.ensure_window_floor(persona, "room") == "unmet"
    write.assert_not_called()


def test_preview_refill_strict_distinguishes_read_failure_from_no_material(
    session_factory,
):
    """Codex 七巡目 #4: context-status の厳格モードは、古い材料の読み失敗を
    「材料なし (適用なし)」と読まず例外にする。既定は None。"""
    import threading

    lc = _make_lifecycle(session_factory)
    m0 = _msg("m0", 200, 1000)

    def _before(anchor_id, **kwargs):
        if kwargs.get("raise_on_error"):
            raise RuntimeError("database is locked")
        return []

    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        history_manager=SimpleNamespace(
            get_history_from_anchor=lambda anchor_id, **k: [m0],
            get_history_before_anchor=_before,
        ),
        sai_memory=SimpleNamespace(
            conn=object(), is_ready=lambda: True, _db_lock=threading.RLock(),
        ),
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "perception_blocks_for", return_value=[]):
        assert lc.preview_refilled_history(persona, MODEL) is None
        with pytest.raises(RuntimeError):
            lc.preview_refilled_history(persona, MODEL, raise_on_error=True)


def test_refill_does_not_write_over_unreadable_folds(session_factory):
    """掃討: 本走行の読み戻しは器を厳格に読む — 圧縮区間の記録が壊れていたら
    例外 (呼び出し側が記録して床へ) で、何も書かない。"""
    import threading

    lc = _make_lifecycle(session_factory)
    from database.models import SessionAnchor
    broken = '[{"message_ids": "abc"}]'
    db = lc.manager.SessionLocal()
    try:
        db.query(SessionAnchor).filter_by(PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL).update(
            {SessionAnchor.FOLDED_RANGES_JSON: broken}, synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
    m0 = _msg("m0", 200, 1000)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model=MODEL,
        history_manager=SimpleNamespace(
            get_history_from_anchor=lambda anchor_id, **k: [m0],
            get_history_before_anchor=lambda anchor_id, **k: list(BEFORE),
        ),
        sai_memory=SimpleNamespace(
            conn=object(), is_ready=lambda: True, _db_lock=threading.RLock(),
        ),
    )
    with patch.object(lc, "get_metabolism_watermarks", return_value=WM), \
            patch.object(lc, "_write_refill") as write:
        with pytest.raises(ValueError):
            lc.maybe_run_window_refill(persona, "room", model_key=MODEL)
    write.assert_not_called()
    assert _folds_json(lc) == broken


def test_run_meta_user_refuses_the_pulse_when_the_execution_model_cannot_be_resolved():
    """Codex 八巡目 #1: 応答前の実行 model の解決が失敗したら床未達として見送る
    (None のまま persona.model の窓を検証して別の model で喋らない)。ユーザー
    Pulse には同じ error イベントが一度だけ。"""
    from sea.runtime_context import WindowFloorUnmetError

    runtime, persona = _runtime_for_floor_tests()
    runtime.session_lifecycle.ensure_window_floor = Mock(return_value="skip")
    events = []
    with patch("sea.runtime.resolve_execution_context", side_effect=RuntimeError("boom")):
        with pytest.raises(WindowFloorUnmetError):
            runtime.run_meta_user(
                persona=persona, user_input="hello", building_id="b1",
                event_callback=events.append,
            )
    runtime._compile_with_langgraph.assert_not_called()
    runtime.session_lifecycle.ensure_window_floor.assert_not_called()
    errors = [e for e in events if e.get("type") == "error"]
    assert len(errors) == 1 and errors[0]["error_code"] == "window_floor_unmet"
    # model 属性の無いペルソナ (標準 tier の解決が "unknown") も同じ
    bare = SimpleNamespace(
        persona_name="p", persona_id="pid", llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()), execution_state={},
    )
    with pytest.raises(WindowFloorUnmetError):
        runtime.run_meta_user(persona=bare, user_input="hello", building_id="b1")
    runtime._compile_with_langgraph.assert_not_called()


def test_floor_covering_entry_lookup_failure_is_unmet(session_factory):
    """Codex 八巡目 #2: 覆うあらすじの照会が失敗したら、空の記録で書かず "unmet"。"""
    lc = _make_lifecycle(session_factory)
    persona = _persona(BEFORE, ready=True)
    window = _window("m0", [_msg("m0", 200, 1000)])
    with patch("sai_memory.arasuji.storage.get_entries_covering_messages",
               side_effect=RuntimeError("db down")), \
            patch.object(lc, "_write_refill") as write:
        assert _run(lc, persona, window) == "unmet"
    write.assert_not_called()
    assert lc.load_anchor_entry(PERSONA_ID, MODEL)["anchor_id"] == "m0"


def test_remove_folds_referencing_entry_leaves_unreadable_rows_untouched(session_factory):
    """Codex 八巡目 #5: エントリ削除の道連れは記録を厳格に読み、形の壊れた行は
    触らない (寛容に読んで書き戻すと属性が黙って落ちる)。"""
    from sea.session_lifecycle import remove_folds_referencing_entry

    lc = _make_lifecycle(session_factory)
    broken = '[{"message_ids": "abc", "chronicle_entry_ids": ["e1"]}]'
    from database.models import SessionAnchor
    db = lc.manager.SessionLocal()
    try:
        db.query(SessionAnchor).filter_by(PERSONA_ID=PERSONA_ID, MODEL_KEY=MODEL).update(
            {SessionAnchor.FOLDED_RANGES_JSON: broken}, synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
    assert remove_folds_referencing_entry(lc.manager, PERSONA_ID, "e1") == 0
    assert _folds_json(lc) == broken
    # 読める行は従来どおり外れる
    lc.save_folded_ranges(PERSONA_ID, MODEL, [
        FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"]),
    ])
    assert remove_folds_referencing_entry(lc.manager, PERSONA_ID, "e1") == 1
    assert not lc.load_folded_ranges(PERSONA_ID, MODEL)
