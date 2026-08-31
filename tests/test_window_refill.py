"""読み戻し (arasuji_levels.md §15) のテスト — 編纂を「揃える」双方向の操作にする。

固定する不変条件:

- 開き直しは帳簿の付け替えだけ (LLM なし)。圧縮区間の記録は消えず、
  「生で見せる」印 (presented_raw) が付くだけ — head の除外名簿は効き続ける。
- 引き戻しの梯子はあらすじの段だけ。あらすじの無い過去 (編纂なしで忘れた
  範囲) は掘り起こさない。段が壊れていたら (source が提示対象から消えていたら)
  そこで止まる。
- 天井は残す量。区間は丸ごと単位。
- 再畳み (印戻し) は既存あらすじを再利用し、編纂を走らせない。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from sea.eviction_plan import ESTIMATED_FOLD_PLACEHOLDER_CHARS, Watermarks
from sea.session_lifecycle import SessionLifecycle
from sea.session_window import FoldedRange, SessionWindow, deserialize_folds
from sea.window_refill import plan_reopen, plan_rewind

PERSONA_ID = "alice"


# ---------------------------------------------------------------------------
# 計画の純関数
# ---------------------------------------------------------------------------


def _msg(mid, at, chars):
    return {"id": mid, "content": "x" * chars, "created_at": at}


def _entry(eid, source_ids, short_id=None):
    return SimpleNamespace(id=eid, source_ids=list(source_ids), short_id=short_id)


def _ph(first_live_mid, chars=ESTIMATED_FOLD_PLACEHOLDER_CHARS, at=100):
    """提示中の置き換えメッセージ (sea/session_window.py の _placeholder の形)。"""
    return {
        "id": f"folded:{first_live_mid}", "content": "p" * chars,
        "created_at": at, "metadata": {"__folded_range__": True},
    }


class TestPlanReopen:
    def test_opens_newest_first_up_to_target(self):
        """digest 区間を新しい方から、残す量を超えない範囲で開く。"""
        raw = [_msg(f"m{i}", 100 + i, 2000) for i in range(6)]
        old = FoldedRange(message_ids=["m0", "m1"])   # 生 4000 字
        new = FoldedRange(message_ids=["m2", "m3"])   # 生 4000 字
        presented = [_ph("m0"), _ph("m2"), _msg("m4", 104, 2000), _msg("m5", 105, 2000)]
        # 現提示 4000 字、残す量 8000 → 開けるのは 1 区間だけ
        gain = 4000 - ESTIMATED_FOLD_PLACEHOLDER_CHARS
        chosen, projected = plan_reopen(
            [old, new], raw, presented, 4000, 4000 + gain + 100,
        )
        assert chosen == [new]  # 新しい方が先に選ばれる
        assert projected == 4000 + gain

    def test_opens_both_when_budget_allows(self):
        raw = [_msg(f"m{i}", 100 + i, 2000) for i in range(6)]
        old = FoldedRange(message_ids=["m0", "m1"])
        new = FoldedRange(message_ids=["m2", "m3"])
        presented = [_ph("m0"), _ph("m2"), _msg("m4", 104, 2000), _msg("m5", 105, 2000)]
        chosen, _ = plan_reopen([old, new], raw, presented, 4000, 100_000)
        assert chosen == [new, old]

    def test_skips_oversized_but_tries_smaller_older(self):
        """入らない区間は飛ばし、より小さい古い区間を試す。"""
        raw = [
            _msg("m0", 100, 1500),           # 古くて小さい区間の中身
            _msg("m1", 101, 50_000),         # 新しくて大きい区間の中身
            _msg("m2", 102, 1000),
        ]
        small_old = FoldedRange(message_ids=["m0"])
        big_new = FoldedRange(message_ids=["m1"])
        presented = [_ph("m0"), _ph("m1"), _msg("m2", 102, 1000)]
        chosen, _ = plan_reopen([small_old, big_new], raw, presented, 1000, 3000)
        assert chosen == [small_old]

    def test_already_raw_and_tiny_folds_are_skipped(self):
        raw = [_msg("m0", 100, 500), _msg("m1", 101, 2000)]
        already = FoldedRange(message_ids=["m1"], presented_raw=True)
        tiny = FoldedRange(message_ids=["m0"])  # 生 500 字 < 置き換えの実文字数
        presented = [_ph("m0"), _msg("m1", 101, 2000)]
        chosen, projected = plan_reopen([already, tiny], raw, presented, 1000, 100_000)
        assert chosen == []
        assert projected == 1000

    def test_partially_live_fold_is_never_opened(self):
        """一部が anchor の手前に出ている区間は開けない (Codex 2026-07-30)。

        開くと digest が消えて head の除外も効き続けるため、手前に出ている
        分の体験が提示からもあらすじからも消える。"""
        raw = [_msg("m0", 100, 5000), _msg("m1", 101, 100)]
        partial = FoldedRange(
            message_ids=["gone0", "m0"], chronicle_entry_ids=["e1"],
        )
        presented = [_ph("m0"), _msg("m1", 101, 100)]
        chosen, projected = plan_reopen([partial], raw, presented, 1000, 100_000)
        assert chosen == []
        assert projected == 1000

    def test_fail_open_fold_without_placeholder_is_skipped(self):
        """digest が引けず fail-open で既に生提示の区間は開かない (Codex
        2026-07-30)。開いても提示は増えないのに架空の利得を計上すると、
        引き戻しの予算が削られて窓が残す量未満のまま固定される。"""
        raw = [_msg("m0", 100, 5000), _msg("m1", 101, 1000)]
        fold = FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"])
        presented = list(raw)  # fail-open: 置き換え無し・生のまま
        chosen, projected = plan_reopen([fold], raw, presented, 6000, 100_000)
        assert chosen == []
        assert projected == 6000

    def test_gain_uses_actual_placeholder_length(self):
        """利得は提示中の置き換えの**実文字数**で数える (Codex 2026-07-30)。

        実際の置き換えが固定見込み (1200) より短い区間で、見込みなら入るが
        実測では天井を超える場合、開いてはいけない。"""
        raw = [_msg("m0", 100, 3000), _msg("m1", 101, 1000)]
        fold = FoldedRange(message_ids=["m0"], chronicle_entry_ids=["e1"])
        placeholder = {
            "id": "folded:m0", "content": "x" * 100, "created_at": 100,
            "metadata": {"__folded_range__": True},
        }
        presented = [placeholder, _msg("m1", 101, 1000)]
        # 現提示 1100。実利得 = 3000−100 = 2900 → 4000 > target 3000。
        # 固定見込みなら 3000−1200 = 1800 → 2900 ≤ 3000 で通ってしまう値。
        chosen, projected = plan_reopen([fold], raw, presented, 1100, 3000)
        assert chosen == []
        assert projected == 1100
        # 天井が実利得ぶんあるなら開ける
        chosen, projected = plan_reopen([fold], raw, presented, 1100, 4000)
        assert chosen == [fold]
        assert projected == 4000


class TestPlanRewind:
    def test_rewinds_to_entry_boundary_within_budget(self):
        """段 (一次エントリの被覆) 単位で、予算内の最古の段まで引き戻す。"""
        before = [_msg(f"m{i}", 100 + i, 1000) for i in range(6)]  # m0..m5
        entries = [
            _entry("e1", ["m0", "m1"], short_id=1),
            _entry("e2", ["m2", "m3"], short_id=2),
            _entry("e3", ["m4", "m5"], short_id=3),
        ]
        plan = plan_rewind(before, entries, [], set(), set(), set(), budget_chars=4500)
        assert plan is not None
        # 予算 4500 → e3 (2000) + e2 (2000) は入るが e1 まで戻ると 6000 で超過
        assert plan.new_anchor_id == "m2"
        assert plan.restored_chars == 4000
        assert [f.chronicle_entry_ids for f in plan.folds] == [["e2"], ["e3"]]
        assert all(f.presented_raw for f in plan.folds)
        assert plan.folds[0].chronicle_short_ids == [2]

    def test_no_entries_means_no_rewind(self):
        """あらすじの無い過去は掘り起こさない (編纂なしで忘れる合意の尊重)。"""
        before = [_msg(f"m{i}", 100 + i, 1000) for i in range(4)]
        assert plan_rewind(before, [], [], set(), set(), set(), budget_chars=10_000) is None

    def test_uncovered_messages_between_rungs_ride_along(self):
        """段の間に挟まる編纂対象外メッセージは境界の内側なら一緒に生へ戻る。
        境界そのものは常にエントリの被覆境界 (裸の未被覆メッセージでは止まらない)。"""
        before = [
            _msg("m0", 100, 1000),  # e1 被覆
            _msg("m1", 101, 1000),  # 未被覆 (除外タグ等)
            _msg("m2", 102, 1000),  # e2 被覆
        ]
        entries = [_entry("e1", ["m0"]), _entry("e2", ["m2"])]
        plan = plan_rewind(
            before, entries, [], set(), set(),
            {"m0", "m2"},  # m1 は編纂対象外 → 同乗してよい
            budget_chars=10_000,
        )
        assert plan.new_anchor_id == "m0"
        assert plan.restored_chars == 3000  # m1 も勘定に入る
        assert plan.restored_message_count == 3

    def test_eligible_uncovered_gap_stops_the_ladder(self):
        """編纂対象なのに被覆の無いメッセージ = 編纂なしで忘れた過去。
        跨いで戻すと忘却済みの内容が復活するので、梯子はその手前で止まる
        (Codex 2026-07-30 6巡目)。"""
        before = [
            _msg("m0", 100, 1000),  # e1 被覆
            _msg("m1", 101, 1000),  # 編纂対象なのに未被覆 (忘却済み)
            _msg("m2", 102, 1000),  # e2 被覆
        ]
        entries = [_entry("e1", ["m0"]), _entry("e2", ["m2"])]
        plan = plan_rewind(
            before, entries, [], set(), set(),
            {"m0", "m1", "m2"},  # 全部編纂対象
            budget_chars=10_000,
        )
        assert plan is not None
        assert plan.new_anchor_id == "m2"  # m1 を跨がず e2 で止まる
        assert [f.chronicle_entry_ids for f in plan.folds] == [["e2"]]

    def test_eligible_uncovered_gap_before_first_rung_blocks_rewind(self):
        """最初の段と anchor の間に忘却済みメッセージが居たら一段も戻れない。"""
        before = [
            _msg("m0", 100, 1000),  # e1 被覆
            _msg("m1", 101, 1000),  # 編纂対象なのに未被覆・anchor 直前
        ]
        entries = [_entry("e1", ["m0"])]
        assert plan_rewind(
            before, entries, [], set(), set(), {"m0", "m1"},
            budget_chars=10_000,
        ) is None

    def test_broken_rung_stops_the_ladder(self):
        """source が提示対象から消えた段は開けない — 梯子はそこで終わる。
        その先 (より古い健全な段) へ飛び越えない。"""
        before = [
            _msg("m0", 100, 1000),  # e1 被覆 (健全・古い)
            _msg("m2", 102, 1000),  # e2 被覆だが e2 は m_gone も指す
            _msg("m3", 103, 1000),  # e3 被覆 (健全・新しい)
        ]
        entries = [
            _entry("e1", ["m0"]),
            _entry("e2", ["m2", "m_gone"]),
            _entry("e3", ["m3"]),
        ]
        plan = plan_rewind(before, entries, [], set(), set(), set(), budget_chars=10_000)
        assert plan is not None
        assert plan.new_anchor_id == "m3"  # e3 だけ。e2 で止まり e1 へ降りない
        assert [f.chronicle_entry_ids for f in plan.folds] == [["e3"]]

    def test_sources_spilling_into_window_join_the_fold(self):
        """anchor 跨ぎのエントリ: 窓内側の source は欠けと数えず、合成する
        区間の範囲に**含める** (Codex 2026-07-30)。

        含めないと、印戻し後に digest がエントリ全体を要約する一方で窓側の
        source が生ログのまま残り、同じ体験が二重提示になる。"""
        before = [_msg("m0", 100, 1000)]
        entries = [_entry("e1", ["m0", "w1"])]
        plan = plan_rewind(before, entries, ["w1", "w2"], set(), set(), set(), budget_chars=5000)
        assert plan is not None
        assert plan.folds[0].message_ids == ["m0", "w1"]

    def test_already_folded_entries_are_left_alone(self):
        """既に窓内の圧縮区間が持つエントリ (anchor 跨ぎ) はここでは扱わない。"""
        before = [_msg("m0", 100, 1000)]
        entries = [_entry("e1", ["m0"])]
        assert plan_rewind(before, entries, [], {"e1"}, set(), set(), budget_chars=5000) is None

    def test_window_side_shared_source_merges_rungs(self):
        """窓側の source を共有するエントリも一つの段 = 一枚の区間に束ねる
        (Codex 2026-07-30)。before 側の位置だけで束ねると別段に分かれ、
        印戻し後に共有メッセージが二つの digest に属する。"""
        before = [_msg("b0", 100, 1000), _msg("b1", 101, 1000), _msg("b2", 102, 1000)]
        entries = [_entry("e1", ["b0", "w1"]), _entry("e2", ["b2", "w1"])]
        plan = plan_rewind(
            before, entries, ["w1"], set(), set(), set(), budget_chars=10_000,
        )
        assert plan is not None
        assert len(plan.folds) == 1
        fold = plan.folds[0]
        assert fold.message_ids == ["b0", "b2", "w1"]
        assert sorted(fold.chronicle_entry_ids) == ["e1", "e2"]

    def test_ladder_never_descends_past_existing_fold_territory(self):
        """既存の圧縮区間に属するメッセージより古くへは降りない (Codex
        2026-07-30)。踏み込むと、部分生存の印付き区間が「全体生存」に変わって
        digest 表示から生表示へ切り替わり、予算外の増分で天井が破れる。"""
        before = [
            _msg("b0", 100, 1000),  # e1 被覆 (古い・健全)
            _msg("gone0", 101, 1000),  # 既存の印付き区間 F の領土
            _msg("b2", 102, 1000),  # e2 被覆 (新しい)
        ]
        entries = [_entry("e1", ["b0"]), _entry("e2", ["b2"])]
        plan = plan_rewind(
            before, entries, [], set(), {"gone0"}, set(), budget_chars=10_000,
        )
        assert plan is not None
        assert plan.new_anchor_id == "b2"  # gone0 より古い e1 へは降りない
        assert [f.chronicle_entry_ids for f in plan.folds] == [["e2"]]

    def test_rung_intersecting_existing_fold_is_not_opened(self):
        """段の source が既存の圧縮区間のメッセージと交差したら開けない
        (Codex 2026-07-30)。同じメッセージが二つの区間に属すると digest の
        二重提示が起こる。梯子はそこで止まる。"""
        before = [_msg("b0", 100, 1000)]
        entries = [_entry("e_new", ["b0", "w1"])]
        assert plan_rewind(
            before, entries, ["w1"], set(), {"w1"}, set(), budget_chars=10_000,
        ) is None

    def test_overlapping_entries_form_one_rung_and_one_fold(self):
        """被覆が重なるエントリは一つの段 = **一枚の圧縮区間** (Codex
        2026-07-30)。別々の区間にすると、印戻し後に共有メッセージが二つの
        digest に属して同じ体験が二重提示される。"""
        before = [_msg(f"m{i}", 100 + i, 1000) for i in range(3)]
        entries = [_entry("e1", ["m0", "m1"]), _entry("e2", ["m1", "m2"])]
        # 予算が段全体 (3000) に足りない → 何も戻せない
        assert plan_rewind(before, entries, [], set(), set(), set(), budget_chars=2500) is None
        plan = plan_rewind(before, entries, [], set(), set(), set(), budget_chars=3000)
        assert plan is not None
        assert plan.new_anchor_id == "m0"
        assert len(plan.folds) == 1
        fold = plan.folds[0]
        assert fold.message_ids == ["m0", "m1", "m2"]  # 重複なしの正典順
        assert sorted(fold.chronicle_entry_ids) == ["e1", "e2"]


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
    wm = Watermarks(low=1000, target=2000, high=4000)
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
    wm = Watermarks(low=1000, target=20_000, high=40_000)
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
    """アップデート組の型: 窓が薄く、anchor の手前があらすじ被覆済み →
    anchor を段の境界まで引き戻し、印付きの圧縮区間を合成する。"""
    lc = _make_lifecycle(session_factory)
    before = [_msg(f"b{i}", 100 + i, 1000) for i in range(4)]  # b0..b3
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=SimpleNamespace(
            get_history_before_anchor=lambda *a, **k: list(before),
        ),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(low=1000, target=5000, high=10_000)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e1", ["b0", "b1"], short_id=1), _entry("e2", ["b2", "b3"], short_id=2)]
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor", return_value=("m0", "self")), \
            patch.object(lc, "get_presented_window", return_value=window), \
            patch("sai_memory.arasuji.storage.get_entries_covering_messages",
                  return_value=entries):
        assert lc.maybe_run_window_refill(persona, "room") == "ok"

    entry = lc.load_anchor_entry(PERSONA_ID, "model-a")
    # 予算 4000 (target 5000 − 現 1000) → e2 (2000) + e1 (2000) 両方戻る
    assert entry["anchor_id"] == "b0"
    saved = deserialize_folds(entry.get("folded_ranges"))
    assert [f.chronicle_entry_ids for f in saved] == [["e1"], ["e2"]]
    assert all(f.presented_raw for f in saved)


def test_preview_refilled_history_is_read_only(session_factory):
    """context preview 用の読み戻しシミュレーション (§15 追補): 返る内容は
    本番の読み戻しと同じ計算だが、anchor 行・圧縮区間・温度を一切書かない
    (§14-6-5 の「プレビューは行を触らない」)。"""
    lc = _make_lifecycle(session_factory)
    before = [_msg(f"b{i}", 100 + i, 1000) for i in range(4)]
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, model="model-a",
        history_manager=SimpleNamespace(
            get_history_before_anchor=lambda *a, **k: list(before),
        ),
        sai_memory=SimpleNamespace(conn=object(), is_ready=lambda: True),
    )
    stale = _now() - timedelta(days=3)
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": stale.isoformat(), "ttl_seconds": 300,
    })
    wm = Watermarks(low=1000, target=5000, high=10_000)
    window = _window("m0", [_msg("m0", 200, 1000)])
    entries = [_entry("e1", ["b0", "b1"]), _entry("e2", ["b2", "b3"])]
    with patch.object(lc, "get_metabolism_watermarks", return_value=wm), \
            patch.object(lc, "resolve_metabolism_anchor",
                         return_value=("m0", "self")) as resolve, \
            patch.object(lc, "get_presented_window", return_value=window), \
            patch("sai_memory.arasuji.storage.get_entries_covering_messages",
                  return_value=entries):
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


def test_preview_refilled_history_none_when_at_target(session_factory):
    """不足が無ければ None — 呼び出し側は従来どおり素の窓を組む。"""
    lc = _make_lifecycle(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID, model="model-a")
    lc.upsert_anchor_entry(PERSONA_ID, "model-a", {
        "anchor_id": "m0", "updated_at": _now().isoformat(), "ttl_seconds": 3600,
    })
    wm = Watermarks(low=1000, target=2000, high=4000)
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
    wm = Watermarks(low=1000, target=2000, high=4000)
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
    wm = Watermarks(low=1000, target=20_000, high=40_000)
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
    wm = Watermarks(low=1000, target=20_000, high=40_000)
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
    wm = Watermarks(low=1000, target=17_000, high=19_000)
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
    wm = Watermarks(low=1000, target=17_000, high=19_000)
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
    wm = Watermarks(low=1000, target=2000, high=4000)
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
    wm = Watermarks(low=1000, target=2000, high=4000)
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
