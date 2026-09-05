"""読み戻し (arasuji_levels.md §15) のテスト — 編纂を「揃える」双方向の操作にする。

固定する不変条件 (2026-09-05 まはー裁定 —
docs/issues/refill_reads_by_budget_instead_of_arasuji_unit.md §裁定の確定):

- 開き直しは帳簿の付け替えだけ (LLM なし)。圧縮区間の記録は消えず、
  「生で見せる」印 (presented_raw) が付くだけ — head の除外名簿は効き続ける。
- 会話文が目標量 (残す量) を下回っていたら、いちばん新しいあらすじから順に
  **丸ごと**開く — 窓内 digest・起点をまたぐ区間・起点より古い側の別なく。
  会話文が目標量に達したら終了。超過してよい (目標量は下限であって上限では
  ない)。読む範囲を字数で切る「予算」は無い。
- 材料に読めない行があるあらすじでも止まらない — 読める行だけで開く。
- 合計が上限を超えても開いた結果は保つ (WARNING のみ)。
- 再畳み (印戻し) は既存あらすじを再利用し、編纂を走らせない。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from sea.eviction_plan import (
    CONSUMED_PERCEPTION_KEY,
    ESTIMATED_FOLD_PLACEHOLDER_CHARS,
    Watermarks,
)
from sea.session_lifecycle import SessionLifecycle
from sea.session_window import FoldedRange, SessionWindow, deserialize_folds
from sea.window_refill import merge_refill_fold, openable_folds_newest_first

PERSONA_ID = "alice"


# ---------------------------------------------------------------------------
# 計画の純関数
# ---------------------------------------------------------------------------


def _msg(mid, at, chars):
    return {"id": mid, "content": "x" * chars, "created_at": at}


def _entry(eid, source_ids, short_id=None, end_time=None):
    """テスト用の一次あらすじ。``end_time`` は実物の同名列に相当する「覆う範囲の
    終わりの時刻」。未指定 (None) なら照会の偽物 (_patch_storage) が材料の最後の
    実在行の時刻で代用する — 実物でも end_time は材料の最後の行の時刻なので、
    既定では材料の位置の順と一致し、既存テストの意味を変えない。"""
    return SimpleNamespace(
        id=eid, source_ids=list(source_ids), short_id=short_id,
        end_time=end_time,
    )


def _ph(first_live_mid, chars=ESTIMATED_FOLD_PLACEHOLDER_CHARS, at=100):
    """提示中の置き換えメッセージ (sea/session_window.py の _placeholder の形)。"""
    return {
        "id": f"folded:{first_live_mid}", "content": "p" * chars,
        "created_at": at, "metadata": {"__folded_range__": True},
    }


class TestOpenableFoldsNewestFirst:
    def test_orders_digest_folds_newest_first(self):
        """窓内の digest 区間は「覆う提示中いちばん新しい行」の新しい順に並ぶ。"""
        raw = [_msg(f"m{i}", 100 + i, 1000) for i in range(6)]
        old = FoldedRange(message_ids=["m0", "m1"])
        new = FoldedRange(message_ids=["m2", "m3"])
        presented = [_ph("m0"), _ph("m2"), _msg("m4", 104, 1000), _msg("m5", 105, 1000)]
        assert openable_folds_newest_first([old, new], raw, presented) == [new, old]

    def test_straddling_folds_are_openable_regardless_of_flags(self):
        """起点をまたぐ区間は presented_raw でも対象 — 提示は digest に倒れて
        いて (apply_folds は部分生存の印を尊重しない)、またいだ側の行は起点を
        戻さないと提示に現れない。"""
        raw = [_msg("m0", 100, 1000), _msg("m1", 101, 1000)]
        straddling = FoldedRange(message_ids=["b9", "m0"], presented_raw=True)
        presented = [_ph("m0"), _msg("m1", 101, 1000)]
        assert openable_folds_newest_first([straddling], raw, presented) == [straddling]

    def test_straddling_sorts_by_its_newest_window_row(self):
        """またぐ区間も特別扱いせず、覆う提示中の行の新しさだけで並ぶ (裁定 2)。"""
        raw = [_msg(f"m{i}", 100 + i, 1000) for i in range(4)]
        straddling = FoldedRange(message_ids=["b9", "m0"])  # 窓側の最新 = m0
        inner = FoldedRange(message_ids=["m2"])
        presented = [_ph("m0"), _msg("m1", 101, 1000), _ph("m2"), _msg("m3", 103, 1000)]
        assert openable_folds_newest_first(
            [straddling, inner], raw, presented,
        ) == [inner, straddling]

    def test_raw_view_and_fail_open_folds_are_not_openable(self):
        """presented_raw の区間 (既に生) と、置き換えが提示に無い区間 (あらすじが
        引けず fail-open で生提示) は、開いても提示が 1 字も増えない = 対象外。"""
        raw = [_msg("m0", 100, 1000), _msg("m1", 101, 1000)]
        raw_view = FoldedRange(message_ids=["m0"], presented_raw=True)
        fail_open = FoldedRange(message_ids=["m1"])  # 置き換え無し = 生提示
        presented = list(raw)
        assert openable_folds_newest_first([raw_view, fail_open], raw, presented) == []

    def test_empty_fold_is_skipped(self):
        assert openable_folds_newest_first(
            [FoldedRange(message_ids=[])], [], [],
        ) == []


class TestMergeRefillFold:
    def test_bundles_all_covering_entries_into_one_fold(self):
        """開いた範囲に材料を持つあらすじは一枚の区間に束ねる — 同じ行が二つの
        区間に属すると、印戻し後に digest が二重提示になる。"""
        rows = [_msg(f"m{i}", 100 + i, 10) for i in range(4)]
        fold, absorbed = merge_refill_fold(
            [
                _entry("e1", ["m0", "m1"], short_id=1),
                _entry("e2", ["m1", "m2"], short_id=2),
            ],
            rows, [],
        )
        assert absorbed == []
        assert fold.message_ids == ["m0", "m1", "m2"]  # 重複なしの正典順
        assert fold.chronicle_entry_ids == ["e1", "e2"]
        assert fold.chronicle_short_ids == [1, 2]
        assert fold.presented_raw is True
        assert fold.start_at == 100 and fold.end_at == 102

    def test_unreadable_source_rows_are_left_out_of_the_fold(self):
        """材料のうち提示対象に無い id は区間の行に載せない — 載せると部分生存の
        区間と見なされ digest 提示に倒れて、開いたはずの行が縮む (裁定 5 の
        「読める行だけで開く」の帳簿側)。あらすじ id は載る (head の除外名簿)。"""
        rows = [_msg("m0", 100, 10)]
        fold, _absorbed = merge_refill_fold([_entry("e1", ["ghost", "m0"])], rows, [])
        assert fold.message_ids == ["m0"]
        assert fold.chronicle_entry_ids == ["e1"]

    def test_absorbs_existing_folds_sharing_rows_or_entries(self):
        """行かあらすじ id を共有する既存区間は併合して absorbed で返す —
        呼び出し側は窓の記録からそれを外し、組んだ一枚に置き換える。既存区間の
        あらすじ id が先 (記録の連続性)。"""
        rows = [_msg("b0", 100, 10), _msg("w1", 101, 10)]
        existing = FoldedRange(
            message_ids=["w1"], chronicle_entry_ids=["e_old"],
            chronicle_short_ids=[7], presented_raw=True,
        )
        unrelated = FoldedRange(message_ids=["z9"], chronicle_entry_ids=["e_z"])
        fold, absorbed = merge_refill_fold(
            [_entry("e_new", ["b0", "w1"], short_id=9)], rows,
            [existing, unrelated],
        )
        assert absorbed == [existing]
        assert fold.message_ids == ["b0", "w1"]
        assert fold.chronicle_entry_ids == ["e_old", "e_new"]
        assert fold.chronicle_short_ids == [7, 9]
        assert fold.presented_raw is True

    def test_returns_none_when_no_row_is_readable(self):
        fold, absorbed = merge_refill_fold([_entry("e1", ["ghost"])], [], [])
        assert fold is None
        assert absorbed == []


# ---------------------------------------------------------------------------
# あらすじ照会の素朴なメモリ実装 (lifecycle 統合テスト用)
# ---------------------------------------------------------------------------


def _patch_storage(entries, universe):
    """読み戻しが使う arasuji.storage の照会 4 つを、メモリ上の素朴な実装で差し替える。

    ``entries`` は一次あらすじ (_entry のリスト)、``universe`` は提示対象の
    全行 (時系列昇順) で正典順の位置の真実。「messages に実在する」は
    「universe に居る」と読み替える。「いちばん新しい一次あらすじ」の選択は
    実物の照会 (sai_memory/arasuji/storage.py の
    get_latest_primary_entry_before_message) と同じ ``end_time`` の降順 —
    材料の位置の順で選ぶと、両者が食い違う入力で実物と別のあらすじを返す。
    """
    pos = {str(m["id"]): i for i, m in enumerate(universe)}
    at = {str(m["id"]): m["created_at"] for m in universe}

    def _positions(e):
        return [pos[str(s)] for s in e.source_ids if str(s) in pos]

    def _end_time(e):
        """実物の end_time 列に相当する新しさ。未指定 (None) のエントリは
        材料の最後の実在行の時刻で代用する (_entry の docstring 参照)。"""
        if e.end_time is not None:
            return e.end_time
        return max(at[str(s)] for s in e.source_ids if str(s) in at)

    def _latest_before(conn, message_id, *, exclude_entry_ids=()):
        if str(message_id) not in pos:
            return None
        boundary = pos[str(message_id)]
        excluded = {str(x) for x in exclude_entry_ids}
        best = None
        for e in entries:
            if str(e.id) in excluded:
                continue
            ps = _positions(e)
            if not ps or not any(p < boundary for p in ps):
                continue
            if best is None or _end_time(e) > _end_time(best):
                best = e
        return best

    def _oldest_present(conn, ids):
        ps = sorted((pos[str(i)], str(i)) for i in ids if str(i) in pos)
        return ps[0][1] if ps else None

    def _covering(conn, ids):
        wanted = {str(i) for i in ids}
        hits = [
            e for e in entries if {str(s) for s in e.source_ids} & wanted
        ]
        hits.sort(key=lambda e: min(_positions(e)) if _positions(e) else 0)
        return hits

    def _compare(conn, id_a, id_b):
        if str(id_a) not in pos or str(id_b) not in pos:
            return None
        if pos[str(id_a)] == pos[str(id_b)]:
            return 0
        return 1 if pos[str(id_a)] > pos[str(id_b)] else -1

    return [
        patch(
            "sai_memory.arasuji.storage.get_latest_primary_entry_before_message",
            side_effect=_latest_before,
        ),
        patch(
            "sai_memory.arasuji.storage.get_oldest_present_message_id",
            side_effect=_oldest_present,
        ),
        patch(
            "sai_memory.arasuji.storage.get_entries_covering_messages",
            side_effect=_covering,
        ),
        patch(
            "sai_memory.arasuji.storage.compare_message_positions",
            side_effect=_compare,
        ),
    ]


def _refill_history(universe):
    """指定の行 (時系列昇順) を持つ履歴の読み手 — 読み戻しが読む形だけを実装。"""
    def _from_anchor(start_id, **_k):
        idx = next(
            (i for i, m in enumerate(universe) if m["id"] == start_id), None,
        )
        return list(universe[idx:]) if idx is not None else []

    return SimpleNamespace(get_history_from_anchor=_from_anchor)


# ---------------------------------------------------------------------------
# lifecycle 統合 (発火・書き込み・印戻し)
# ---------------------------------------------------------------------------


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
    return SessionLifecycle(runtime, manager)


def _now():
    return datetime.now().replace(microsecond=0)


def _window(anchor_id, presented, raw=None, folds=None):
    return SessionWindow(
        anchor_id=anchor_id,
        raw=list(raw if raw is not None else presented),
        presented=list(presented),
        folds=list(folds or []),
    )


def test_refill_skips_at_or_above_target(session_factory):
    """残す量以上なら何もしない。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m1", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=2000, high=4000)
    msgs = [_msg("m1", 100, 2500)]
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "get_presented_window", return_value=_window("m1", msgs)), \
            patch.object(lc, "_write_refill") as write:
        assert lc.maybe_run_window_refill(persona, "room") == "skip"
    write.assert_not_called()


def test_refill_reopens_in_window_folds_and_stamps_warm(session_factory):
    """窓内の digest 区間に印を付けて保存し、行の温度を now にする。
    anchor は動かない。あらすじ本体には触らない。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=SimpleNamespace(
            get_history_before_anchor=lambda *a, **k: [],
        ),
        sai_memory=None,
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    raw = [_msg("m0", 100, 3000), _msg("m1", 101, 3000), _msg("m2", 102, 3000)]
    fold = FoldedRange(message_ids=["m0", "m1"], chronicle_entry_ids=["e1"])
    presented = [_ph("m0", chars=500), _msg("m2", 102, 3000)]
    wm = Watermarks(target=20_000, high=40_000)
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m0", presented, raw=raw, folds=[fold])):
        assert lc.maybe_run_window_refill(persona, "room") == "ok"

    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "m0"  # 引き戻し無し — 印だけ
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert len(saved) == 1
    assert saved[0].presented_raw is True
    assert saved[0].chronicle_entry_ids == ["e1"]  # 除外名簿は無傷
    # 行は温かい (直後の resolve が §14-2 前進で読み戻しを飲み込まない)
    assert lc._anchor_entry_is_hot(entry, "model-a", PERSONA_ID)


def test_refill_rewinds_anchor_with_synthesized_folds(session_factory):
    """アップデート組の型: 窓が薄く、起点の手前があらすじ被覆済み → いちばん
    新しいあらすじから順に丸ごと開き、起点を戻して印付きの圧縮区間を合成する。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    before = [_msg(f"b{i}", 100 + i, 1000) for i in range(4)]  # b0..b3
    universe = before + [_msg("m0", 200, 1000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=5000, high=10_000)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e1", ["b0", "b1"], short_id=1), _entry("e2", ["b2", "b3"], short_id=2)]
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        for p in _patch_storage(entries, universe):
            stack.enter_context(p)
        assert lc.maybe_run_window_refill(persona, "room") == "ok"

    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    # e2 (2000) を開いても 3000 < 5000 → e1 (2000) も開いて 5000 で終了
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e1"], ["e2"]]
    assert all(f.presented_raw for f in saved)


def test_preview_refilled_history_is_read_only(session_factory):
    """context preview 用の読み戻しシミュレーション (§15 追補): 返る内容は
    本番の読み戻しと同じ計算だが、anchor 行・圧縮区間・温度を一切書かない
    (§14-6-5 の「プレビューは行を触らない」)。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    before = [_msg(f"b{i}", 100 + i, 1000) for i in range(4)]
    universe = before + [_msg("m0", 200, 1000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    wm = Watermarks(target=5000, high=10_000)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e1", ["b0", "b1"]), _entry("e2", ["b2", "b3"])]
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        resolve = stack.enter_context(
            patch.object(lc, "resolve_metabolism_anchor",
                         return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        for p in _patch_storage(entries, universe):
            stack.enter_context(p)
        plan = lc.preview_refilled_history(persona, "model-a")

    # 提示は読み戻し後の姿 (b0..b3 が生で先頭に戻っている)
    assert plan is not None
    assert [m["id"] for m in plan["presented"]] == ["b0", "b1", "b2", "b3", "m0"]
    # weave 組み直し用の材料 (引き戻し後の始点 + 除外名簿) も返る
    assert plan["new_anchor_id"] == "b0"
    assert sorted(plan["fold_entry_ids"]) == ["e1", "e2"]
    # resolve は preview 型 (persist_advance=False) で呼ばれる
    assert resolve.call_args.kwargs.get("persist_advance") is False
    # 行は無傷 — anchor も圧縮区間も温度も書かれていない
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "m0"
    assert not deserialize_folds(entry.get("folded_ranges"))
    assert not lc._anchor_entry_is_hot(entry, "model-a", PERSONA_ID)


def test_refill_without_watermarks_never_touches_rows(session_factory):
    """水位が定義できない model では resolve (§14-2 前進の永続化を含む) を
    呼ばずに引き返す (Codex 2026-07-30: 計画切り出しで順序が逆転し、no-op の
    はずの経路が anchor を書いていた)。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    with patch.object(lc, "get_metabolism_watermarks", return_value=None), \
            patch.object(lc, "resolve_metabolism_anchor") as resolve:
        assert lc.maybe_run_window_refill(persona, "room") == "skip"
        assert lc.preview_refilled_history(persona, "model-a") is None
    resolve.assert_not_called()


def test_refill_measures_rows_only_against_target(session_factory, caplog):
    """不足も終了も**会話の行だけ**を目標量と比べる (2026-09-03 / 09-05 裁定)。

    残す量の主語は「会話の行の量」。知覚ブロックを足した合計で測ると、巨大な
    部屋の様子が乗った窓は会話が 3,000 字しか無くても「足りている」と読まれ、
    畳みすぎた窓が二度と埋め戻らない — 本番でペルソナが直近の記憶を失った
    事故の後半 (docs/issues/protection_quota_consumed_by_perception_blocks.md)。
    旧判定 (合計) なら 3,000 + 15,000 ≥ 18,000 で None を返していた。

    開いた結果が目標量を超えるのは問題ない (下限であって上限ではない)。知覚を
    足した合計が上限を超えても、開いた結果は保って WARNING だけ出す (裁定 7)。
    """
    import logging as _logging
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    before = [_msg(f"b{i}", 100 + i, 4000) for i in range(4)]  # 16,000 字
    universe = before + [_msg("m0", 200, 3000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=18_000, high=26_000)
    window = _window("m0", [_msg("m0", 200, 3000)])  # 保存行は 3,000 字
    entries = [_entry("e1", ["b0", "b1"]), _entry("e2", ["b2", "b3"])]
    block = {
        "role": "user", "content": "p" * 15_000, "created_at": 199,
        "metadata": {"tags": ["internal", "event_message", "perception"],
                     CONSUMED_PERCEPTION_KEY: True},
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        stack.enter_context(
            patch.object(lc, "perception_blocks_for", return_value=[block]))
        for p in _patch_storage(entries, universe):
            stack.enter_context(p)
        with caplog.at_level(_logging.WARNING, logger="sea.session_lifecycle"):
            plan = lc.preview_refilled_history(persona, "model-a")
            assert plan is not None
            # e2 (8,000) で 11,000 < 18,000 → e1 (8,000) も開いて 19,000 で終了。
            # 会話文は目標量を超えてよい。
            from sea.eviction_plan import message_chars
            assert message_chars(plan["presented"]) == 19_000
            assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "b0"
    # 合計 19,000 + 知覚 15,000 = 34,000 > 上限 26,000 → WARNING だけで結果は保つ
    assert any(
        "exceeds the high watermark" in r.getMessage() for r in caplog.records
    )


def test_preview_refilled_history_none_when_at_target(session_factory):
    """不足が無ければ None — 呼び出し側は従来どおり素の窓を組む。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=2000, high=4000)
    window = _window("m0", [_msg("m0", 100, 2500)])
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window", return_value=window):
        assert lc.preview_refilled_history(persona, "model-a") is None


def test_preview_refill_raise_on_error_distinguishes_failure(session_factory):
    """既定は fail-open (内部失敗 → None) だが、strict では例外が届くこと。

    None が「適用なし (正常)」と「内部失敗」の両方を意味すると、context-status の
    ような読み手が障害を正常値として表示する (Codex 指摘 2026-07-30)。
    """
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=2000, high=4000)
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "_plan_window_refill", side_effect=RuntimeError("boom")):
        assert lc.preview_refilled_history(persona, "model-a") is None
        with pytest.raises(RuntimeError):
            lc.preview_refilled_history(persona, "model-a", raise_on_error=True)


def test_refill_head_recapture_failure_retries_and_warns(session_factory, caplog):
    """head 再 capture の失敗は 1 回だけ再試行し、駄目なら WARNING で進む
    (§14-3 fail-open と同じ裁定 — 応答は止めない)。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=SimpleNamespace(
            get_history_before_anchor=lambda *a, **k: [],
        ),
        sai_memory=None,
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    raw = [_msg("m0", 100, 3000), _msg("m1", 101, 3000), _msg("m2", 102, 3000)]
    fold = FoldedRange(message_ids=["m0", "m1"], chronicle_entry_ids=["e1"])
    presented = [_ph("m0", chars=500), _msg("m2", 102, 3000)]
    wm = Watermarks(target=20_000, high=40_000)
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m0", presented, raw=raw, folds=[fold])), \
            patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                  return_value=False) as recapture:
        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            assert lc.maybe_run_window_refill(persona, "room") == "ok"
    assert recapture.call_count == 2  # 1 回だけ再試行
    assert any("head re-capture failed" in r.message for r in caplog.records)


def test_refill_detects_stale_weave_reuse(session_factory, caplog):
    """dispatch が成功しても weave が作り直されていなければ失敗と数える
    (Codex 2026-07-30: capture_all は capture 例外時に既存オブジェクトを
    使い回す = 旧除外名簿の weave が残る)。identity 不変 → 再試行 → WARNING。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=SimpleNamespace(
            get_history_before_anchor=lambda *a, **k: [],
        ),
        sai_memory=None,
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    raw = [_msg("m0", 100, 3000), _msg("m1", 101, 3000), _msg("m2", 102, 3000)]
    fold = FoldedRange(message_ids=["m0", "m1"], chronicle_entry_ids=["e1"])
    presented = [_ph("m0", chars=500), _msg("m2", 102, 3000)]
    wm = Watermarks(target=20_000, high=40_000)
    stale_weave = object()  # capture されない = 同一オブジェクトが返り続ける
    fake_snap = SimpleNamespace(sections={"memory_weave": stale_weave})
    fake_pipeline = SimpleNamespace(get_snapshot=lambda pid, mk: fake_snap)
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window",
                         return_value=_window("m0", presented, raw=raw, folds=[fold])), \
            patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                  return_value=True) as recapture, \
            patch("sea.head_pipeline.get_default_pipeline",
                  return_value=fake_pipeline):
        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            assert lc.maybe_run_window_refill(persona, "room") == "ok"
    assert recapture.call_count == 2  # identity 不変 → 1 回だけ再試行
    assert any("head re-capture failed" in r.message for r in caplog.records)


def test_weave_context_raise_on_error_distinguishes_failure(tmp_path):
    """raise_on_error=True は組み立て失敗を例外で伝える。既定は [] へ変換
    (Codex 2026-07-30: 失敗を「成功した空」としてコミットさせない)。"""
    from builtin_data.tools.get_memory_weave_context import get_memory_weave_context

    # 実在する db パスまで進めてから sqlite3.connect を落とす
    (tmp_path / "memory.db").write_bytes(b"")
    with patch("builtin_data.tools.get_memory_weave_context.sqlite3") as fake_sqlite:
        fake_sqlite.connect.side_effect = RuntimeError("db down")
        assert get_memory_weave_context(
            persona_id="p1", persona_dir=str(tmp_path),
        ) == []
        with pytest.raises(RuntimeError):
            get_memory_weave_context(
                persona_id="p1", persona_dir=str(tmp_path),
                raise_on_error=True,
            )


def test_weave_context_strict_propagates_inner_failures(tmp_path):
    """strict は内側のヘルパー (Chronicle 組み立て) の読取失敗も例外で伝える
    (Codex 2026-07-30: 外側の try だけだと内側の握り潰しが「成功した空」に化ける)。"""
    from builtin_data.tools.get_memory_weave_context import get_memory_weave_context

    (tmp_path / "memory.db").write_bytes(b"")
    with patch(
        "sai_memory.arasuji.context.get_episode_context",
        side_effect=RuntimeError("query down"),
    ):
        assert get_memory_weave_context(
            persona_id="p1", persona_dir=str(tmp_path),
        ) == []
        with pytest.raises(RuntimeError):
            get_memory_weave_context(
                persona_id="p1", persona_dir=str(tmp_path),
                raise_on_error=True,
            )
    # 実行時 ImportError (依存の version skew / 遅延 import 失敗) も読取失敗 —
    # 「モジュール不在の正当な空」と混同しない (Codex 2026-07-30)
    with patch(
        "sai_memory.arasuji.context.get_episode_context",
        side_effect=ImportError("runtime dependency failed"),
    ):
        assert get_memory_weave_context(
            persona_id="p1", persona_dir=str(tmp_path),
        ) == []
        with pytest.raises(ImportError):
            get_memory_weave_context(
                persona_id="p1", persona_dir=str(tmp_path),
                raise_on_error=True,
            )


def test_weave_context_strict_rejects_broken_import(tmp_path):
    """export 欠落の ImportError は「モジュール不在の正当な空」ではなく読取失敗
    (Codex 2026-07-30)。strict では再送出、既定では空へ縮退。本当に無い
    (ModuleNotFoundError) ときだけ正当な空。"""
    import sys
    import types

    from builtin_data.tools.get_memory_weave_context import get_memory_weave_context

    (tmp_path / "memory.db").write_bytes(b"")
    broken = types.ModuleType("sai_memory.arasuji.context")  # export を持たない
    with patch.dict(sys.modules, {"sai_memory.arasuji.context": broken}):
        assert get_memory_weave_context(
            persona_id="p1", persona_dir=str(tmp_path),
        ) == []
        with pytest.raises(ImportError):
            get_memory_weave_context(
                persona_id="p1", persona_dir=str(tmp_path),
                raise_on_error=True,
            )


def test_preview_weave_swap_failure_falls_back(session_factory):
    """weave の組み直しに失敗したら False (messages 無変更) — 呼び出し側は
    読み戻しプレビューを見送って素の窓に落とす (二重表示より薄い方)。"""
    from sea.runtime_context import _swap_preview_weave_for_refill

    weave = {
        "role": "user", "content": "old-weave",
        "metadata": {"__memory_weave_context__": True, "__memory_weave_type__": "chronicle"},
    }
    messages = [{"role": "system", "content": "head"}, dict(weave)]
    runtime = SimpleNamespace(manager=None)
    persona = SimpleNamespace(persona_id=PERSONA_ID, sai_memory=None)
    plan = {"presented": [], "new_anchor_id": "b0", "fold_entry_ids": ["e1"]}
    with patch(
        "builtin_data.tools.get_memory_weave_context.get_memory_weave_context",
        side_effect=RuntimeError("weave down"),
    ):
        assert _swap_preview_weave_for_refill(runtime, persona, messages, plan) is False
    assert [m["content"] for m in messages] == ["head", "old-weave"]  # 無変更

    # weave がそもそも無ければ衝突相手が居ない = True
    no_weave = [{"role": "system", "content": "head"}]
    assert _swap_preview_weave_for_refill(runtime, persona, no_weave, plan) is True


def test_replace_weave_messages_swaps_in_place():
    """preview の weave 差し替え: 位置を保って全 weave を新しい列に置き換える。
    weave が無ければ何もしない (weave 無効設定を上書きしない)。"""
    from sea.runtime_context import _replace_weave_messages

    def _weave(mid, kind="chronicle"):
        return {
            "role": "user", "content": mid,
            "metadata": {"__memory_weave_context__": True, "__memory_weave_type__": kind},
        }

    messages = [
        {"role": "system", "content": "head"},
        _weave("old-1"), _weave("old-2", "memopedia"),
        {"role": "user", "content": "history"},
    ]
    _replace_weave_messages(messages, [_weave("new-1")])
    assert [m["content"] for m in messages] == ["head", "new-1", "history"]

    no_weave = [{"role": "system", "content": "head"}]
    _replace_weave_messages(no_weave, [_weave("new-1")])
    assert [m["content"] for m in no_weave] == ["head"]


def test_write_refill_cas_rejects_moved_anchor(session_factory):
    """発火判定と書き込みの間に anchor が動いていたら棄却する (古い窓の計画)。"""
    lc = _make_lifecycle(session_factory)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "moved", "updated_at": _now().isoformat(), "ttl_seconds": 300,
    })
    ok = lc._write_refill(
        PERSONA_ID, "model-a", "m0", "b0",
        [FoldedRange(message_ids=["b0"], presented_raw=True)],
    )
    assert ok is False
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "moved"
    assert not deserialize_folds(entry.get("folded_ranges"))
    # CAS 不一致は「意図した見送り」なので raise_on_error でも False のまま
    # (例外にするのは書き込み自体が失敗したときだけ — Codex 十一巡 P1)。
    assert lc._write_refill(
        PERSONA_ID, "model-a", "m0", "b0",
        [FoldedRange(message_ids=["b0"], presented_raw=True)],
        raise_on_error=True,
    ) is False


def test_write_refill_separates_db_failure_from_cas_rejection(session_factory):
    """[Codex 十一巡 P1] 書き込みの DB 失敗と CAS 不一致を分ける。

    既定 (raise_on_error=False) は従来どおり両方 False — §15 読み戻しは
    どちらも "skip" へ落とす fail-open のまま。補修の引き戻しだけが
    raise_on_error=True で呼び、DB 失敗を "failed" へ写像する。
    """
    lc = _make_lifecycle(session_factory)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 300,
    })
    real_factory = lc.manager.SessionLocal

    class _Query:
        def __init__(self, real):
            self._real = real

        def filter_by(self, **kwargs):
            return _Query(self._real.filter_by(**kwargs))

        def update(self, *args, **kwargs):
            raise RuntimeError("db write down")

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _Session:
        def __init__(self, real):
            self._real = real

        def query(self, *args, **kwargs):
            return _Query(self._real.query(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self._real, name)

    lc.manager.SessionLocal = lambda: _Session(real_factory())
    folds = [FoldedRange(message_ids=["b0"], presented_raw=True)]
    # 既定: 従来どおり False へ縮退 (§15 の呼び出し側の挙動は不変)
    assert lc._write_refill(PERSONA_ID, "model-a", "m0", "b0", folds) is False
    # 補修経路: 書き込みの失敗は例外で伝わる
    with pytest.raises(RuntimeError):
        lc._write_refill(
            PERSONA_ID, "model-a", "m0", "b0", folds, raise_on_error=True,
        )
    # manager 未接続 (書き込みを試みられない状態) も同じ扱い
    lc.manager = None
    assert lc._write_refill(PERSONA_ID, "model-a", "m0", "b0", folds) is False
    with pytest.raises(RuntimeError):
        lc._write_refill(
            PERSONA_ID, "model-a", "m0", "b0", folds, raise_on_error=True,
        )


def test_refold_flips_oldest_first_until_target(session_factory):
    """§15-3 印戻し: 古い方から、残す量に収まるまで。編纂は走らない。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    raw = [_msg(f"m{i}", 100 + i, 5000) for i in range(4)]
    old = FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"], presented_raw=True)
    new = FoldedRange(message_ids=["m1"], chronicle_entry_ids=["e2"], presented_raw=True)
    window = _window("m0", raw, raw=raw, folds=[old, new])
    # 現 20000 字。1 区間戻すと m0 (5000) が置き換え (digest 1200 + 注釈) に
    # なって 17000 を下回る → 実測の継続判定で 1 区間だけ戻る
    wm = Watermarks(target=17_000, high=19_000)
    from sea.session_window import apply_folds
    with patch.object(lc, "_present_with_folds",
                      side_effect=lambda p, msgs, folds: apply_folds(
                          msgs, folds, lambda f: "d" * 1200)):
        result = lc._refold_raw_view_folds(persona, "model-a", window, wm)
    assert result is not None
    assert old.presented_raw is False   # 古い方だけ戻る
    assert new.presented_raw is True
    saved = deserialize_folds(lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges"))
    flags = {f.chronicle_entry_ids[0]: f.presented_raw for f in saved}
    assert flags == {"e1": False, "e2": True}


def _perception_block(chars, at=99):
    """送信直前に差し込まれる知覚ブロック (保存行ではないので id が無い)。"""
    return {
        "role": "user", "content": "p" * chars, "created_at": at,
        "metadata": {"tags": ["internal", "event_message", "perception"],
                     CONSUMED_PERCEPTION_KEY: True},
    }


def test_refold_stops_on_rows_only_regardless_of_injected_perceptions(
    session_factory,
):
    """§15-3 印戻しの止め時は**会話の行だけ**を残す量と比べる (2026-09-03 裁定)。

    退場計画の保護範囲が会話の行だけで測られる (ProtectionSubjectTest) 以上、
    印戻しも同じ物差しで止めるのが一貫した規則: 行が残す量以下になった時点で
    残る印付き区間は保護範囲の内側にあり、退場計画に拾われない (再編纂の
    二本立ちは起きない)。合計で止め続けると、巨大な部屋の様子が乗った回に
    印付き区間を全部 digest へ戻して会話が痩せる。
    """
    from sea.session_window import apply_folds

    raw = [_msg(f"m{i}", 100 + i, 5000) for i in range(4)]  # 20,000 字
    wm = Watermarks(target=17_000, high=19_000)

    def _run(blocks):
        lc = _make_lifecycle(session_factory)
        persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
        lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
            "anchor_id": "m0", "updated_at": _now().isoformat(),
            "ttl_seconds": 3600,
        })
        old = FoldedRange(
            message_ids=["m0"], chronicle_entry_ids=["e1"], presented_raw=True,
        )
        new = FoldedRange(
            message_ids=["m1"], chronicle_entry_ids=["e2"], presented_raw=True,
        )
        window = _window("m0", raw, raw=raw, folds=[old, new])
        stack = [patch.object(
            lc, "_present_with_folds",
            side_effect=lambda p, msgs, folds: apply_folds(
                msgs, folds, lambda f: "d" * 1200),
        )]
        if blocks is not None:
            stack.append(
                patch.object(lc, "perception_blocks_for", return_value=blocks)
            )
        for ctx in stack:
            ctx.start()
        try:
            lc._refold_raw_view_folds(persona, "model-a", window, wm)
        finally:
            for ctx in reversed(stack):
                ctx.stop()
        return [old.presented_raw, new.presented_raw]

    # 会話の行: 1 区間戻せば 1,200 + 15,000 = 16,200 <= 17,000 で止まる。
    assert _run(None) == [False, True]
    # 知覚 1,500 字が乗っても止め時は同じ — ブロックは残す量を消費しない。
    assert _run([_perception_block(1500)]) == [False, True]


def test_refold_early_completion_counts_rows_only(session_factory):
    """印戻し後の早期完了も**会話の行だけ**で判定する (2026-09-03 裁定)。

    行が残す量以下なら退場計画は保護範囲で埋まって空になる — 計画へ進んでも
    "nothing" で終わるだけなので、印戻しの時点で完了を返してよい。合計で判定
    すると、知覚ぶんで超えた回に「整理しました」が出ず、計画も空で、ユーザー
    には何も起きなかったように見える。
    """
    from sea.session_window import apply_folds

    raw = [_msg(f"m{i}", 100 + i, 5000) for i in range(4)]  # 20,000 字
    wm = Watermarks(target=17_000, high=19_000)

    def _run(blocks):
        lc = _make_lifecycle(session_factory)
        lc.is_chronicle_enabled_for_persona = lambda p: True
        lc.ensure_recall_embeddings = lambda p: None
        lc._retry_extraction_backlog = lambda p, **kw: None
        lc._drop_dead_folds = lambda p, mk, w: w
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, model="model-a", sai_memory=None,
            current_building_id="room",
        )
        lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
            "anchor_id": "m0", "updated_at": _now().isoformat(),
            "ttl_seconds": 3600,
        })
        # 印戻しで digest へ戻せる区間は 1 つだけ (戻すと 16,200 字)。
        window = _window("m0", raw, raw=raw, folds=[FoldedRange(
            message_ids=["m0"], chronicle_entry_ids=["e1"], presented_raw=True,
        )])
        events = []
        stack = [
            patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}),
            patch.object(
                lc, "_present_with_folds",
                side_effect=lambda p, msgs, folds: apply_folds(
                    msgs, folds, lambda f: "d" * 1200),
            ),
            patch.object(lc, "get_presented_window", return_value=window),
            patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                  lambda *a, **k: None),
        ]
        if blocks is not None:
            stack.append(
                patch.object(lc, "perception_blocks_for", return_value=blocks)
            )
        for ctx in stack:
            ctx.start()
        try:
            status = lc.run_metabolism(
                persona, "room", window, wm, events.append, model_key="model-a",
            )
        finally:
            for ctx in reversed(stack):
                ctx.stop()
        return status, [
            e.get("content") for e in events
            if e.get("status") == "completed"
        ]

    # 会話の行: 印戻し後 16,200 <= 17,000 → 編纂ゼロで完了を返す。
    completed = (
        "ok", ["記憶を整理しました（開いていた範囲をあらすじ表示に戻しました）"],
    )
    assert _run(None) == completed
    # 知覚 1,500 字が乗っても判定は行だけ — 同じく早期完了する。
    assert _run([_perception_block(1500)]) == completed


def test_preview_planning_window_refolds_without_writing(session_factory):
    """context-status の下見 (preview_planning_window) は、本走行と同じ正規化
    (§15-3 印戻し) を**写しに**適用した窓を返し、行にも元の fold オブジェクト
    にも書かない (Codex 指摘 2026-08-29: 下見と本走行が同じ形の窓を planner に
    渡す)。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a", sai_memory=None)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    raw = [_msg(f"m{i}", 100 + i, 5000) for i in range(4)]
    old = FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"], presented_raw=True)
    new = FoldedRange(message_ids=["m1"], chronicle_entry_ids=["e2"], presented_raw=True)
    window = _window("m0", raw, raw=raw, folds=[old, new])
    wm = Watermarks(target=17_000, high=19_000)
    from sea.session_window import apply_folds
    with patch.object(lc, "_present_with_folds",
                      side_effect=lambda p, msgs, folds: apply_folds(
                          msgs, folds, lambda f: "d" * 1200)):
        normalized, refold_ranges = lc.preview_planning_window(
            persona, "model-a", window, wm,
        )
    # 本走行の印戻しと同じ答え: 古い方 1 区間だけ digest 表示へ戻る。
    assert refold_ranges == 1
    assert [m["id"] for m in normalized.presented][0].startswith("folded:")
    # 元の窓の fold は無傷 (写しに flip した)。
    assert old.presented_raw is True
    assert new.presented_raw is True
    # 行も無傷 — 何も書かれていない。
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert not deserialize_folds(entry.get("folded_ranges"))


def test_preview_planning_window_passthrough_without_raw_view(session_factory):
    """正規化で何も変わらない窓 (印付き fold も恒久欠落も無い) はそのままの
    提示を返し、refold_ranges=0。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a", sai_memory=None)
    window = _window("m0", [_msg("m0", 100, 2500)],
                     folds=[FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"])])
    wm = Watermarks(target=2000, high=4000)
    normalized, refold_ranges = lc.preview_planning_window(
        persona, "model-a", window, wm,
    )
    assert refold_ranges == 0
    assert [m["id"] for m in normalized.presented] == ["m0"]


def test_refold_noop_when_no_raw_view(session_factory):
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    window = _window("m0", [_msg("m0", 100, 100)],
                     folds=[FoldedRange(message_ids=["m0"])])
    wm = Watermarks(target=2000, high=4000)
    assert lc._refold_raw_view_folds(persona, "model-a", window, wm) is None


def test_digest_with_partially_missing_entries_fails_open(session_factory, tmp_path):
    """記録された chronicle_entry_ids の一部だけが引けた区間は、揃った顔をした
    digest を出さず恒久欠落 (fail-open 生提示 + 記録破棄対象) に倒す (Codex
    2026-07-30)。欠けたエントリだけが持つ体験が静かに消えるのを防ぐ。"""
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.storage import create_entry
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_arasuji_tables(conn)
    entry = create_entry(
        conn, level=1, content="digest", source_ids=["m0"],
        source_count=1, message_count=1,
    )
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, sai_memory=SimpleNamespace(
            conn=conn, is_ready=lambda: True,
        ),
    )
    # 全部引ける → digest が返る
    whole = FoldedRange(message_ids=["m0"], chronicle_entry_ids=[entry.id])
    digest, missing = lc._resolve_fold_digest_status(persona, whole)
    assert digest == "digest"
    assert missing is False
    # 一部が引けない → (None, 恒久欠落)
    partial = FoldedRange(
        message_ids=["m0", "m1"], chronicle_entry_ids=[entry.id, "ghost"],
    )
    digest, missing = lc._resolve_fold_digest_status(persona, partial)
    assert digest is None
    assert missing is True
    # id は全件引けるが本文が空のエントリ混じり → 同じく恒久欠落 (Codex
    # 2026-07-30: digest は id と本文が全件揃って初めて成立する)
    empty = create_entry(
        conn, level=1, content="", source_ids=["m1"],
        source_count=1, message_count=1,
    )
    mixed = FoldedRange(
        message_ids=["m0", "m1"], chronicle_entry_ids=[entry.id, empty.id],
    )
    digest, missing = lc._resolve_fold_digest_status(persona, mixed)
    assert digest is None
    assert missing is True
    # 記録 id が**全件**欠落 → 被覆保証の無い救済照会に落ちず恒久欠落
    # (Codex 2026-07-30 5巡目: m0 だけ覆う別エントリが居ても m1 の体験を
    # m0 の digest で置き換えてはいけない)
    all_ghost = FoldedRange(
        message_ids=["m0", "m1"], chronicle_entry_ids=["ghost-a"],
    )
    digest, missing = lc._resolve_fold_digest_status(persona, all_ghost)
    assert digest is None
    assert missing is True
    conn.close()


def test_legacy_fold_fallback_requires_full_coverage(session_factory, tmp_path):
    """id を記録しない旧形式の記録の救済照会は、編纂対象メッセージ全件の
    被覆を検算する (Codex 2026-07-30 5巡目)。部分被覆なら fail-open。"""
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.storage import create_entry
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_arasuji_tables(conn)
    for mid in ("m0", "m1"):
        conn.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at, "
            "metadata, line_role) VALUES (?, 't-main', 'user', 'hello', 100, "
            "NULL, 'main_line')",
            (mid,),
        )
    conn.commit()
    create_entry(
        conn, level=1, content="digest-m0", source_ids=["m0"],
        source_count=1, message_count=1,
    )
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, sai_memory=SimpleNamespace(
            conn=conn, is_ready=lambda: True,
        ),
    )
    # 部分被覆 (m1 が編纂対象なのにどのエントリにも無い) → 恒久欠落
    partial = FoldedRange(message_ids=["m0", "m1"], chronicle_entry_ids=[])
    digest, missing = lc._resolve_fold_digest_status(persona, partial)
    assert digest is None
    assert missing is True
    # 全被覆なら digest が返る
    full = FoldedRange(message_ids=["m0"], chronicle_entry_ids=[])
    digest, missing = lc._resolve_fold_digest_status(persona, full)
    assert digest == "digest-m0"
    assert missing is False
    conn.close()


def test_covering_query_chunks_large_id_lists(tmp_path):
    """覆いの照会 (get_entries_covering_messages) はチャンク境界をまたぐ id 列
    でも、単発照会と同じエントリ集合を同じ並び (start_time 昇順) で返す。

    読み戻しは「選んだあらすじの材料の最古の行から起点まで」を行数無制限で
    読み、その全行 id をこの照会へ渡すため、SQLite の変数上限を超える長さで
    壊れないことを固定する (docs/issues/refill_reads_by_budget_instead_of_
    arasuji_unit.md の Codex 指摘 7)。"""
    import sqlite3

    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.storage import (
        _ID_QUERY_CHUNK,
        create_entry,
        get_entries_covering_messages,
    )
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_arasuji_tables(conn)
    # 実物のチャンク定数で境界を踏む: チャンク 2 つ + 端数 = 照会 3 回分。
    n = _ID_QUERY_CHUNK * 2 + 200
    ids = [f"dummy-{i:05d}" for i in range(n)]
    # 実在の材料 id を散らす:
    # - 先頭 (チャンク 1) = いちばん新しいエントリの材料。チャンク到達順の
    #   まま返すと先頭に来てしまう = 最終ソートの検算。
    # - チャンク 1/2 の境界の両側 = 同一エントリの材料が二つのチャンクに
    #   またがって現れる = エントリ id での重複排除の検算。
    # - チャンク 2 の途中 = start_time が NULL のエントリの材料。到達順では
    #   3 番目だが、並びでは先頭に来なければならない (NULL 先頭の検算)。
    # - 末尾 (チャンク 3) = いちばん古いエントリの材料。
    ids[0] = "mat-late"
    ids[_ID_QUERY_CHUNK - 1] = "mat-mid-a"
    ids[_ID_QUERY_CHUNK] = "mat-mid-b"
    ids[_ID_QUERY_CHUNK + 100] = "mat-null"
    ids[n - 1] = "mat-early"
    e_early = create_entry(
        conn, level=1, content="early", source_ids=["mat-early"],
        start_time=100, end_time=110, source_count=1, message_count=1,
    )
    e_mid = create_entry(
        conn, level=1, content="mid", source_ids=["mat-mid-a", "mat-mid-b"],
        start_time=200, end_time=210, source_count=2, message_count=2,
    )
    e_late = create_entry(
        conn, level=1, content="late", source_ids=["mat-late"],
        start_time=300, end_time=310, source_count=1, message_count=1,
    )
    # start_time 無し (NULL)。旧単発照会の ORDER BY start_time ASC は SQLite
    # では NULL を先頭に置く — その契約をここで固定する。
    e_null = create_entry(
        conn, level=1, content="null-start", source_ids=["mat-null"],
        source_count=1, message_count=1,
    )
    # 変数上限をチャンク幅ちょうどまで絞る: 分割しない実装 (id 全件を 1 回の
    # SQL に載せる) は n 変数 > 上限で必ず "too many SQL variables" になる。
    # 既定の上限 (この環境では 250,000) のままだと n=1,200 の単発照会が
    # 通ってしまい、チャンク分割の回帰を検出できない。
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, _ID_QUERY_CHUNK)
    result = get_entries_covering_messages(conn, ids)
    assert [e.id for e in result] == [
        e_null.id, e_early.id, e_mid.id, e_late.id,
    ]
    assert result[0].start_time is None
    # 重複排除の検算: e_mid は両チャンクで引き当たるが 1 件だけ返る。
    assert len(result) == 4
    conn.close()


# ---------------------------------------------------------------------------
# 提示 (apply_folds) と直列化の印対応
# ---------------------------------------------------------------------------


def test_apply_folds_keeps_raw_for_presented_raw():
    """印付きの区間は digest に置き換えず生のまま通す (resolver も呼ばない)。"""
    from sea.session_window import apply_folds

    messages = [_msg(f"m{i}", 100 + i, 10) for i in range(4)]
    fold = FoldedRange(
        message_ids=["m1", "m2"], chronicle_entry_ids=["e1"], presented_raw=True,
    )
    calls = []

    def _resolver(f):
        calls.append(f)
        return "要約"

    out = apply_folds(messages, [fold], _resolver)
    assert [m["id"] for m in out] == ["m0", "m1", "m2", "m3"]
    assert calls == []


def test_apply_folds_partial_raw_fold_falls_back_to_digest():
    """anchor 前進で一部が窓の外に出た印付き区間は digest 提示に倒す
    (Codex 2026-07-30)。生扱いのままだと、外に出た分が digest にも生にも
    現れず体験が消える (head の除外は効き続けるため)。"""
    from sea.session_window import apply_folds

    messages = [_msg(f"m{i}", 100 + i, 10) for i in range(3)]
    fold = FoldedRange(
        message_ids=["gone0", "m0", "m1"],  # gone0 は anchor の手前
        chronicle_entry_ids=["e1"], presented_raw=True,
    )
    out = apply_folds(messages, [fold], lambda f: "全体の要約")
    assert [m["id"] for m in out] == ["folded:m0", "m2"]
    assert "全体の要約" in out[0]["content"]


def test_presented_raw_serialization_roundtrip_and_backcompat():
    from sea.session_window import serialize_folds

    folds = [FoldedRange(message_ids=["m1"], presented_raw=True)]
    restored = deserialize_folds(serialize_folds(folds))
    assert restored[0].presented_raw is True
    # 旧記録 (キー無し) は False = digest 提示に読める
    legacy = '[{"message_ids": ["m1"]}]'
    assert deserialize_folds(legacy)[0].presented_raw is False


# ---------------------------------------------------------------------------
# storage の遡り読み (正典順の対関数)
# ---------------------------------------------------------------------------


def test_get_messages_before_id(tmp_path):
    """境界より正典順で前の行を新しい側から返す (排他・昇順・ページング可能)。"""
    from sai_memory.memory.storage import get_messages_before_id, init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    for i in range(5):
        conn.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at, metadata) "
            "VALUES (?, 't-main', 'user', 'hello', ?, NULL)",
            (f"m{i}", 1000 + i),
        )
    conn.commit()

    rows = get_messages_before_id(conn, "t-main", "m3", limit=10)
    assert [r.id for r in rows] == ["m0", "m1", "m2"]

    # limit は新しい側を優先する
    rows = get_messages_before_id(conn, "t-main", "m3", limit=2)
    assert [r.id for r in rows] == ["m1", "m2"]
    # ページング: 前ページの最古行を次の排他境界に
    older = get_messages_before_id(conn, "t-main", rows[0].id, limit=2)
    assert [r.id for r in older] == ["m0"]

    # 境界行が存在しない → 空
    assert get_messages_before_id(conn, "t-main", "nope", limit=10) == []
    conn.close()


def test_latest_primary_entry_before_message_and_oldest_present_source(tmp_path):
    """新設計の照会 (2026-09-05): 「起点より前に材料を持ついちばん新しい一次
    あらすじ」と「材料の最古の実在行」。材料が全部消えたエントリと別スレッド
    (Stelis) のエントリは候補にならない。"""
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.storage import (
        create_entry,
        get_latest_primary_entry_before_message,
        get_oldest_present_message_id,
    )
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_arasuji_tables(conn)
    for i in range(6):
        conn.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at, metadata) "
            "VALUES (?, 't-main', 'user', 'hello', ?, NULL)",
            (f"m{i}", 1000 + i),
        )
    conn.commit()
    e_old = create_entry(
        conn, level=1, content="old", source_ids=["m0", "m1"],
        start_time=1000, end_time=1001, source_count=2, message_count=2,
    )
    e_new = create_entry(
        conn, level=1, content="new", source_ids=["m2", "m3"],
        start_time=1002, end_time=1003, source_count=2, message_count=2,
    )
    # 材料が全部消えたエントリは、end_time が最新でも候補にならない
    # (開く位置を決められない)
    create_entry(
        conn, level=1, content="all-gone", source_ids=["gone-1", "gone-2"],
        start_time=1004, end_time=1004, source_count=2, message_count=2,
    )
    # 別スレッド (Stelis) のエントリも候補にならない
    create_entry(
        conn, level=1, content="stelis", source_ids=["m4"],
        start_time=1004, end_time=1004, source_count=1, message_count=1,
        thread_id="th-stelis",
    )

    got = get_latest_primary_entry_before_message(conn, "m5")
    assert got is not None and got.id == e_new.id
    got = get_latest_primary_entry_before_message(
        conn, "m5", exclude_entry_ids=[e_new.id],
    )
    assert got is not None and got.id == e_old.id
    # 起点より前に材料が無い / 起点が実在しない → None
    assert get_latest_primary_entry_before_message(conn, "m0") is None
    assert get_latest_primary_entry_before_message(conn, "nope") is None
    # 材料の一部だけが起点より前でも候補になる (m2 < m3 の間に起点を置く)
    got = get_latest_primary_entry_before_message(
        conn, "m3", exclude_entry_ids=[e_old.id],
    )
    assert got is not None and got.id == e_new.id

    assert get_oldest_present_message_id(conn, ["gone", "m3", "m1"]) == "m1"
    assert get_oldest_present_message_id(conn, ["gone"]) is None
    assert get_oldest_present_message_id(conn, []) is None
    conn.close()


def test_latest_primary_entry_tiebreak_null_end_time_and_level(tmp_path):
    """照会の絞り込みと順序の残りの条件 (隣のテストが固定していない側):

    - ``end_time`` 同値の 2 エントリは ``created_at`` の降順で選ぶ
      (挿入順と逆の created_at を与え、行の並びではなく列で選ぶことを固定)
    - ``end_time`` が NULL のエントリは値持ちより後回し (SQLite の DESC で
      NULL は最後)。候補外ではないので、値持ちを除外すれば選ばれる
    - level=2 以上のエントリは ``end_time`` が最新でも選ばれない
      (一次あらすじだけが読み戻しの対象)

    別スレッド (thread_id 付き) が end_time 最新でも選ばれないことは隣の
    テストが固定済み (stelis の end_time=1004 > e_new の 1003)。
    """
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.storage import (
        create_entry,
        get_latest_primary_entry_before_message,
    )
    from sai_memory.memory.storage import init_db

    conn = init_db(str(tmp_path / "memory.db"), check_same_thread=False)
    init_arasuji_tables(conn)
    for i in range(5):
        conn.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at, metadata) "
            "VALUES (?, 't-main', 'user', 'hello', ?, NULL)",
            (f"m{i}", 1000 + i),
        )
    conn.commit()

    def _set_created_at(entry_id, value):
        # arasuji_entries は memopedia_pages 上の互換 VIEW。created_at は
        # ページの列 (create_entry は現在時刻を刻む) なので、物理側を直接
        # 書き換えて挿入順と独立に制御する。
        conn.execute(
            "UPDATE memopedia_pages SET created_at = ? WHERE id = ?",
            (value, entry_id),
        )

    # end_time 同値の 2 エントリ。先に挿入した方へ新しい created_at を与える —
    # created_at DESC で選ばれていれば e_first、挿入順 (行の並び) で選ばれて
    # いれば e_second が返るので、タイブレークの列を判別できる。
    e_first = create_entry(
        conn, level=1, content="tie-created-later", source_ids=["m0"],
        start_time=1000, end_time=1001, source_count=1, message_count=1,
    )
    e_second = create_entry(
        conn, level=1, content="tie-created-earlier", source_ids=["m1"],
        start_time=1001, end_time=1001, source_count=1, message_count=1,
    )
    _set_created_at(e_first.id, 200)
    _set_created_at(e_second.id, 100)
    conn.commit()
    got = get_latest_primary_entry_before_message(conn, "m4")
    assert got is not None and got.id == e_first.id

    # end_time NULL は created_at が最新でも値持ちの後回し
    e_null = create_entry(
        conn, level=1, content="null-end-time", source_ids=["m2"],
        start_time=1002, end_time=None, source_count=1, message_count=1,
    )
    _set_created_at(e_null.id, 999_999)
    conn.commit()
    got = get_latest_primary_entry_before_message(conn, "m4")
    assert got is not None and got.id == e_first.id
    # 候補外ではない: 値持ちを除外すれば NULL のエントリが選ばれる
    got = get_latest_primary_entry_before_message(
        conn, "m4", exclude_entry_ids=[e_first.id, e_second.id],
    )
    assert got is not None and got.id == e_null.id

    # level=2 のエントリは end_time が全エントリより新しくても選ばれない
    create_entry(
        conn, level=2, content="secondary", source_ids=["m3"],
        start_time=1003, end_time=9999, source_count=1, message_count=1,
    )
    got = get_latest_primary_entry_before_message(conn, "m4")
    assert got is not None and got.id == e_first.id
    conn.close()


# ---------------------------------------------------------------------------
# 読み戻しの再設計 (2026-09-05 裁定 —
# docs/issues/refill_reads_by_budget_instead_of_arasuji_unit.md)
# ---------------------------------------------------------------------------


def _straddling_setup(session_factory, *, outside_chars, before_older=None,
                      older_entries=None):
    """起点 m0 をまたぐ圧縮区間 F (b1, b2 が起点より左) を持つ窓。

    F は digest 提示 (b1, b2 が窓の外なので apply_folds が digest に倒す)。
    ``before_older`` は F の先頭 b1 よりさらに古い行 (古い側のあらすじの検証用)。
    """
    lc = _make_lifecycle(session_factory)
    b1, b2 = _msg("b1", 90, outside_chars), _msg("b2", 91, outside_chars)
    m0, m1 = _msg("m0", 100, 1000), _msg("m1", 101, 1000)
    universe = list(before_older or []) + [b1, b2, m0, m1]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    fold = FoldedRange(message_ids=["b1", "b2", "m0"], chronicle_entry_ids=["e_f"])
    window = _window("m0", [_ph("m0", chars=100), m1], raw=[m0, m1], folds=[fold])
    patches = [
        patch.object(lc, "get_presented_window", return_value=window),
        patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")),
        patch.object(lc, "_resolve_fold_digest", lambda persona, f: "d" * 100),
        *_patch_storage(list(older_entries or []), universe),
    ]
    return lc, persona, patches


def test_refill_opens_a_straddling_fold_whole_regardless_of_target(
    session_factory,
):
    """起点をまたぐ圧縮区間も「いちばん新しいあらすじ」として丸ごと開く。
    開いた結果 (10,000) が目標量 (5,000) を超えても問題ない — 途中で切らない
    (目標量は下限であって上限ではない)。旧設計はこの一歩目で読み戻しが止まり、
    窓が二度と埋まらなかった。"""
    lc, persona, patches = _straddling_setup(session_factory, outside_chars=4000)
    wm = Watermarks(target=5000, high=20_000)
    from contextlib import ExitStack
    with ExitStack() as stack, patch.object(lc, "get_metabolism_watermarks", return_value=wm):
        for p in patches:
            stack.enter_context(p)
        plan = lc.preview_refilled_history(persona, "model-a")
        assert plan is not None
        assert plan["new_anchor_id"] == "b1"
        from sea.eviction_plan import stored_message_chars
        assert stored_message_chars(plan["presented"]) == 10_000  # 丸ごと生で戻った
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "b1"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e_f"]]
    assert saved[0].presented_raw is True


def test_refill_continues_to_older_arasuji_after_the_straddling_fold(
    session_factory,
):
    """またぐ区間を開いても目標量に届かなければ、起点より古い側のあらすじへ
    続けて降りる (場所の区別なく新しい順)。"""
    older = [_msg("a0", 80, 1000), _msg("a1", 81, 1000)]
    lc, persona, patches = _straddling_setup(
        session_factory, outside_chars=500, before_older=older,
        older_entries=[_entry("e_a", ["a0", "a1"])],
    )
    # またぐ区間で 500+500+1000+1000 = 3,000 < 5,000 → e_a (2,000) も開いて 5,000。
    wm = Watermarks(target=5000, high=20_000)
    from contextlib import ExitStack
    with ExitStack() as stack, patch.object(lc, "get_metabolism_watermarks", return_value=wm):
        for p in patches:
            stack.enter_context(p)
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "a0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e_a"], ["e_f"]]
    assert all(f.presented_raw for f in saved)


def _older_arasuji_setup(session_factory, *, perception_chars):
    """あらすじ e1 (b0,b1) / e2 (b2,b3) が起点の手前を覆う窓 + 知覚ブロック。

    窓の行は m0 (1,000 字)。"""
    lc = _make_lifecycle(session_factory)
    before = [_msg(f"b{i}", 100 + i, 1000) for i in range(4)]
    universe = before + [_msg("m0", 200, 1000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e1", ["b0", "b1"]), _entry("e2", ["b2", "b3"])]
    blocks = [_perception_block(perception_chars)] if perception_chars else []
    patches = [
        patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")),
        patch.object(lc, "get_presented_window", return_value=window),
        patch.object(lc, "perception_blocks_for", return_value=blocks),
        *_patch_storage(entries, universe),
    ]
    return lc, persona, patches


def test_refill_keeps_the_opened_result_and_warns_when_over_high(
    session_factory, caplog,
):
    """⑤ 仕上げの検算は上限 (合計・知覚込み) と比べるが、超えても開いた結果は
    保って WARNING を出すだけ (裁定 7)。「古いものから外して測り直す」は無い —
    足りないから開いているのだから原理的に不要。

    行 1,000 + あらすじ二つ 4,000 = 5,000 (目標量ちょうど) に知覚 1,500 で
    合計 6,500 > 上限 6,000。旧設計はここで古い方のあらすじを外していた。"""
    import logging as _logging
    from contextlib import ExitStack
    lc, persona, patches = _older_arasuji_setup(session_factory, perception_chars=1500)
    wm = Watermarks(target=5000, high=6000)
    with ExitStack() as stack, patch.object(lc, "get_metabolism_watermarks", return_value=wm):
        for p in patches:
            stack.enter_context(p)
        with caplog.at_level(_logging.WARNING, logger="sea.session_lifecycle"):
            plan = lc.preview_refilled_history(persona, "model-a")
            assert plan is not None
            assert plan["new_anchor_id"] == "b0"  # 何も外れていない
            assert lc.maybe_run_window_refill(persona, "room") == "ok"
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "b0"
    assert any(
        "exceeds the high watermark" in r.getMessage() for r in caplog.records
    )


def test_refill_does_not_warn_at_or_below_high(session_factory, caplog):
    """合計が上限以下なら WARNING は出ない — 会話文が目標量ちょうどでも
    (検算が比べる相手は上限だけ)。行 5,000 + 知覚 900 = 5,900 ≤ 上限 6,000。"""
    import logging as _logging
    from contextlib import ExitStack
    lc, persona, patches = _older_arasuji_setup(session_factory, perception_chars=900)
    wm = Watermarks(target=5000, high=6000)
    with ExitStack() as stack, patch.object(lc, "get_metabolism_watermarks", return_value=wm):
        for p in patches:
            stack.enter_context(p)
        with caplog.at_level(_logging.WARNING, logger="sea.session_lifecycle"):
            assert lc.maybe_run_window_refill(persona, "room") == "ok"
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "b0"
    assert not any(
        "exceeds the high watermark" in r.getMessage() for r in caplog.records
    )


def test_refill_without_high_watermark_never_warns(session_factory, caplog):
    """上限を持たない model (high=None) は検算の比較相手が無い — 開くだけ。"""
    import logging as _logging
    from contextlib import ExitStack
    lc, persona, patches = _older_arasuji_setup(
        session_factory, perception_chars=50_000,
    )
    wm = Watermarks(target=5000, high=None)
    with ExitStack() as stack, patch.object(lc, "get_metabolism_watermarks", return_value=wm):
        for p in patches:
            stack.enter_context(p)
        with caplog.at_level(_logging.WARNING, logger="sea.session_lifecycle"):
            assert lc.maybe_run_window_refill(persona, "room") == "ok"
    assert lc.load_anchor_entry(PERSONA_ID, "model-a")["anchor_id"] == "b0"
    assert not any(
        "exceeds the high watermark" in r.getMessage() for r in caplog.records
    )


def test_refill_opens_the_newest_arasuji_whole_and_stops_at_target(
    session_factory,
):
    """①② いちばん新しいあらすじは途中で切らず丸ごと開き、会話文が目標量に
    達したら次のあらすじへは進まない。不足は 1,000 字でもあらすじ全体
    (4,000) が生に戻る — 超過してよい。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    before = [_msg(f"b{i}", 100 + i, 2000) for i in range(4)]  # b0..b3
    universe = before + [_msg("m0", 200, 1000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=2000, high=None)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e_a", ["b0", "b1"]), _entry("e_b", ["b2", "b3"])]
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        for p in _patch_storage(entries, universe):
            stack.enter_context(p)
        plan = lc.preview_refilled_history(persona, "model-a")
        assert plan is not None
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "b2"  # e_b の最古の行まで。e_a へは降りない
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e_b"]]
    assert saved[0].presented_raw is True


def test_refill_opens_older_arasuji_by_end_time_not_material_position(
    session_factory,
):
    """古い側のあらすじの「いちばん新しい」は照会が返す end_time の降順であって、
    材料の行の位置ではない (実物の get_latest_primary_entry_before_message の
    ORDER BY end_time DESC と同じ物差し — 偽物の照会もこれに合わせてある)。

    材料の位置の順と end_time の順を意図的に食い違わせる: A は材料 (a0, a1) が
    古い位置だが end_time が新しく、B は材料 (b0, b1) が新しい位置だが
    end_time が古い。end_time が新しい A が先に選ばれ、起点は A の最古の材料
    a0 まで戻る (a0 から起点までの範囲読みなので B の材料も一緒に生へ戻り、
    B は同じ一枚の区間に束ねられる)。照会一回で会話文が目標量に達して止まる。
    材料の位置で選ぶと B だけが開いて起点は b0 で止まり、a0, a1 は生に
    戻らない — 既存テストは位置順と end_time 順が一致する入力ばかりなので、
    この食い違いを固定するのは本テストだけ。
    """
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    a0, a1 = _msg("a0", 100, 1000), _msg("a1", 101, 1000)
    b0, b1 = _msg("b0", 102, 1000), _msg("b1", 103, 1000)
    universe = [a0, a1, b0, b1, _msg("m0", 200, 1000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=3000, high=None)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [
        _entry("e_a", ["a0", "a1"], end_time=250),  # 材料は古い位置・end_time は新
        _entry("e_b", ["b0", "b1"], end_time=150),  # 材料は新しい位置・end_time は旧
    ]
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        for p in _patch_storage(entries, universe):
            stack.enter_context(p)
        plan = lc.preview_refilled_history(persona, "model-a")
        assert plan is not None
        # A が先: 起点は A の最古の材料 a0 まで戻り、範囲読みで B の材料も生に立つ
        assert plan["new_anchor_id"] == "a0"
        assert [m["id"] for m in plan["presented"]] == ["a0", "a1", "b0", "b1", "m0"]
        # B は二つ目の選択ではなく、A の範囲読みに束ねられて記録に載る
        assert sorted(plan["fold_entry_ids"]) == ["e_a", "e_b"]
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "a0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    # 開いた範囲に材料を持つ B も一枚の区間に束ねて記録される
    assert [f.chronicle_entry_ids for f in saved] == [["e_a", "e_b"]]
    assert saved[0].presented_raw is True


def test_refill_opens_an_arasuji_with_unreadable_rows_using_the_readable_ones(
    session_factory,
):
    """③ 材料に読めない行があるあらすじでも止まらない — 読めない id は飛ばし、
    読める行を全部生で戻して開く。旧設計はここで壊れていると判定して読み戻し
    全体が止まり、本番の窓が二度と埋まらなかった (2026-09-04 エリスの実機)。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    b0, b1 = _msg("b0", 100, 1000), _msg("b1", 101, 1000)
    universe = [b0, b1, _msg("m0", 200, 1000)]  # ghost は実在しない
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=2500, high=None)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e1", ["ghost", "b0", "b1"])]
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        for p in _patch_storage(entries, universe):
            stack.enter_context(p)
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert saved[0].chronicle_entry_ids == ["e1"]
    assert saved[0].message_ids == ["b0", "b1"]  # ghost は載らない
    assert saved[0].presented_raw is True


def test_refill_restores_uncovered_rows_between_arasuji_raw(session_factory):
    """編纂対象なのにどのあらすじにも覆われていない行も、開く範囲に居れば
    一緒に生で戻る (裁定 9)。旧設計は「編纂なしで忘れた過去」と判定してその
    手前で止まっていた — なにも忘れないのが基本思想なので、読めないほうがバグ。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    b0 = _msg("b0", 100, 1000)    # e1 被覆
    gap = _msg("gap", 101, 1000)  # どのあらすじにも覆われていない
    b2 = _msg("b2", 102, 1000)    # e2 被覆
    universe = [b0, gap, b2, _msg("m0", 200, 1000)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(target=4000, high=None)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e1", ["b0"]), _entry("e2", ["b2"])]
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        for p in _patch_storage(entries, universe):
            stack.enter_context(p)
        plan = lc.preview_refilled_history(persona, "model-a")
        assert plan is not None
        # gap も生で提示に立つ (合計 4,000 に数えられて目標に届く)
        assert [m["id"] for m in plan["presented"]] == ["b0", "gap", "b2", "m0"]
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e1"], ["e2"]]
    # gap はどの区間にも属さない (覆いが無いのが正しい姿)
    assert all("gap" not in f.message_ids for f in saved)


def test_refill_opens_by_recency_regardless_of_location(session_factory):
    """④ 窓内 digest・またぎ区間・古い側のあらすじは、場所ではなく新しさの順で
    開く。目標量に達したらそこで終わり、残りは閉じたまま。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    b9 = _msg("b9", 90, 3000)   # またぎ区間 F の外側の行
    s1 = _msg("s1", 100, 1000)  # 窓の先頭 (F の窓側)
    m1 = _msg("m1", 101, 1000)
    m2 = _msg("m2", 102, 5000)  # 窓内 digest 区間 G の中身 (いちばん新しい)
    universe = [b9, s1, m1, m2]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "s1", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    fold_f = FoldedRange(message_ids=["b9", "s1"], chronicle_entry_ids=["e_f"])
    fold_g = FoldedRange(message_ids=["m2"], chronicle_entry_ids=["e_g"])
    raw = [s1, m1, m2]
    presented = [_ph("s1", chars=100), m1, _ph("m2", chars=100)]
    window = _window("s1", presented, raw=raw, folds=[fold_f, fold_g])
    lookups = []

    def _lookup(conn, message_id, *, exclude_entry_ids=()):
        lookups.append(str(message_id))
        return None

    def _run(target):
        fold_f.presented_raw = False
        fold_g.presented_raw = False
        lookups.clear()
        wm = Watermarks(target=target, high=None)
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                lc, "resolve_metabolism_anchor", return_value=("s1", "self")))
            stack.enter_context(patch.object(
                lc, "get_presented_window", return_value=window))
            stack.enter_context(patch.object(
                lc, "_resolve_fold_digest", lambda persona, f: "d" * 100))
            stack.enter_context(patch(
                "sai_memory.arasuji.storage."
                "get_latest_primary_entry_before_message",
                side_effect=_lookup))
            return lc._plan_window_refill(persona, "model-a", "s1", wm)

    # 目標 5,500: いちばん新しい G (窓内 digest) だけで足りる — またぎ区間 F は
    # 閉じたまま、起点も動かない。古い側のあらすじは照会すらされない。
    plan = _run(5500)
    assert plan is not None
    assert plan["new_anchor_id"] == "s1"
    assert fold_g.presented_raw is True
    assert fold_f.presented_raw is False
    assert plan["opened_in_window"] == 1
    assert plan["opened_straddling"] == 0
    assert lookups == []

    # 目標 8,000: G の次に新しい F (またぎ区間) も開く — それで届くので
    # 古い側のあらすじは照会すらされない。
    plan = _run(8000)
    assert plan is not None
    assert plan["new_anchor_id"] == "b9"
    assert fold_g.presented_raw is True
    assert fold_f.presented_raw is True
    assert plan["opened_straddling"] == 1
    assert lookups == []


def test_refill_logs_why_it_planned_nothing(session_factory, caplog):
    """見送りの各経路に INFO で理由が残る。"""
    import logging as _logging
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    wm = Watermarks(target=5000, high=10_000)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })

    def _persona_for(universe, ready=True):
        return SimpleNamespace(
            persona_id=PERSONA_ID, model="model-a",
            history_manager=_refill_history(universe),
            sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: ready),
        )

    def _messages(entries, universe, presented_chars=1000, ready=True,
                  extra_patches=()):
        caplog.clear()
        window = _window("m0", [_msg("m0", 200, presented_chars)])
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(lc, "get_metabolism_watermarks", return_value=wm))
            stack.enter_context(patch.object(
                lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
            stack.enter_context(
                patch.object(lc, "get_presented_window", return_value=window))
            for p in _patch_storage(entries, universe):
                stack.enter_context(p)
            for p in extra_patches:
                stack.enter_context(p)
            with caplog.at_level(_logging.INFO, logger="sea.session_lifecycle"):
                lc.preview_refilled_history(_persona_for(universe, ready), "model-a")
        return [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]

    m0 = _msg("m0", 200, 1000)
    # 不足なし
    msgs = _messages([], [m0], presented_chars=6000)
    assert any("refill not needed" in m for m in msgs)
    # 起点より古い側に開けるあらすじが無い
    msgs = _messages([], [_msg("b0", 100, 1000), m0])
    assert any(
        "planned nothing" in m and "no arasuji left to open" in m for m in msgs
    )
    # 器が無い (SAIMemory not ready)
    msgs = _messages([], [m0], ready=False)
    assert any("history or memory store unavailable" in m for m in msgs)
    # 材料が一行も実在しないあらすじは飛ばして次へ (次が無ければ見送り)
    ghost_entry = _entry("e1", ["ghost-a", "ghost-b"])

    def _latest_ghost(conn, message_id, *, exclude_entry_ids=()):
        if "e1" in {str(x) for x in exclude_entry_ids}:
            return None
        return ghost_entry

    msgs = _messages(
        [], [_msg("b0", 100, 1000), m0],
        extra_patches=[patch(
            "sai_memory.arasuji.storage."
            "get_latest_primary_entry_before_message",
            side_effect=_latest_ghost,
        )],
    )
    assert any("no surviving material rows" in m for m in msgs)
    assert any("planned nothing" in m for m in msgs)
    # 一部だけ開いて目標量に届かない → その旨を INFO (開いた結果は保つ)
    msgs = _messages([_entry("e1", ["b0"])], [_msg("b0", 100, 1000), m0])
    assert any("refill stopped below target" in m for m in msgs)


def test_refill_restores_a_straddling_fold_when_the_anchor_row_is_not_presentable(
    session_factory,
):
    """またぐ区間の開きは、起点の行自体が提示対象外 (scope=discardable 等) でも動く。

    本番の事故 (2026-09-03) では起点の行が discardable で、区間の先頭から読んだ
    列に起点が現れなかった。切る位置は「起点の行」または「既に提示にある行」の
    最初のもの — 起点だけを探すと、またぐ区間が存在する理由そのものの形で
    見送ってしまう。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    b1, b2, m1 = _msg("b1", 90, 2000), _msg("b2", 91, 2000), _msg("m1", 101, 1000)
    universe = [b1, b2, m1]  # 起点 m0 は提示対象外なので列に居ない
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    fold = FoldedRange(message_ids=["b1", "b2", "m1"], chronicle_entry_ids=["e_f"])
    window = _window("m0", [_ph("m1", chars=100)], raw=[m1], folds=[fold])
    wm = Watermarks(target=6000, high=20_000)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        stack.enter_context(patch.object(
            lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(patch.object(
            lc, "_resolve_fold_digest", lambda persona, f: "d" * 100))
        for p in _patch_storage([], universe):
            stack.enter_context(p)
        plan = lc.preview_refilled_history(persona, "model-a")
        assert plan is not None
        assert plan["new_anchor_id"] == "b1"
        from sea.eviction_plan import stored_message_chars
        assert stored_message_chars(plan["presented"]) == 5000
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "b1"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e_f"]]
    assert saved[0].presented_raw is True


def test_refill_merges_an_opened_arasuji_into_the_intersecting_window_fold(
    session_factory,
):
    """開いたあらすじの材料が窓の既存区間と交差したら、書かれる記録は併合済みの
    一枚で、行が二つの区間に属さない (印戻し後の digest の二重提示を防ぐ)。"""
    from contextlib import ExitStack
    lc = _make_lifecycle(session_factory)
    b0 = _msg("b0", 100, 1000)
    m0, w1 = _msg("m0", 200, 1000), _msg("w1", 201, 1000)
    universe = [b0, m0, w1]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=_refill_history(universe),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    raw = [m0, w1]
    existing = FoldedRange(
        message_ids=["w1"], chronicle_entry_ids=["e_old"], presented_raw=True,
    )
    window = _window("m0", raw, raw=raw, folds=[existing])
    wm = Watermarks(target=5000, high=10_000)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(lc, "get_metabolism_watermarks", return_value=wm))
        stack.enter_context(patch.object(
            lc, "resolve_metabolism_anchor", return_value=("m0", "self")))
        stack.enter_context(
            patch.object(lc, "get_presented_window", return_value=window))
        for p in _patch_storage([_entry("e_new", ["b0", "w1"])], universe):
            stack.enter_context(p)
        assert lc.maybe_run_window_refill(persona, "room") == "ok"
    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert len(saved) == 1
    assert saved[0].message_ids == ["b0", "w1"]
    assert saved[0].chronicle_entry_ids == ["e_old", "e_new"]
    assert saved[0].presented_raw is True


def test_write_refill_merges_overlapping_folds_before_writing(session_factory, caplog):
    """書き込み前の最終検査: 同じ行を持つ区間は併合して書く (拒まない) +
    WARNING。計画側が防ぐのが本筋で、ここは最後の網。"""
    import logging as _logging
    lc = _make_lifecycle(session_factory)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    a = FoldedRange(message_ids=["m1", "m2"], chronicle_entry_ids=["e1"])
    b = FoldedRange(message_ids=["m2", "m3"], chronicle_entry_ids=["e2"], presented_raw=True)
    c = FoldedRange(message_ids=["m9"], chronicle_entry_ids=["e9"])
    with caplog.at_level(_logging.WARNING, logger="sea.session_lifecycle"):
        assert lc._write_refill(PERSONA_ID, "model-a", "m0", "m0", [a, b, c]) is True
    saved = deserialize_folds(lc.load_anchor_entry(PERSONA_ID, "model-a").get("folded_ranges"))
    assert [f.message_ids for f in saved] == [["m1", "m2", "m3"], ["m9"]]
    assert saved[0].chronicle_entry_ids == ["e1", "e2"]
    assert saved[0].presented_raw is True  # どれか一つでも生なら生
    assert any("shared message ids" in r.getMessage() for r in caplog.records)
