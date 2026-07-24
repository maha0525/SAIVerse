"""Metabolism 二層分離 (統合工事 §6-5、SEA 監査 S2/M1) のテスト。

docs/intent/beat_execution_context.md §3.2:

- 編纂 (Chronicle 生成) は persona に一度 — 実行台帳の冪等 claim
  (kind="metabolism.run"、idempotency_key = persona:窓末尾ID) で全入口が
  同じ排他を通る (M1)。
- 退役 (anchor 前進) は model ごと — 編纂が済んだ ("ok") か編纂を持たない
  設計 ("disabled") のときだけ、渡された model の session_anchor 行を進める
  (S2。failed / deferred は据え置き → 次回 maybe_run で自然再試行)。
- 可視化は model の節目 — anchor を進めた model の (persona, model) snapshot
  だけを再 capture する。
- anchor touch は call-local (state["_prefix_anchor_id"] → anchor_id 引数)。
  None なら touch しない (persona 属性フォールバックの廃止 = 記憶監査第 4 片)。
- chronicle_index の件数ラベル diff は退役 (可視化は節目の構造交換が担保)。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from saiverse.execution_ledger import ExecutionLedger
from sea.eviction_plan import Watermarks
from sea.session_lifecycle import SessionLifecycle
from sea.session_window import SessionWindow

PERSONA_ID = "tester"


def _msg(mid, created_at, *, chars=1_000, episode_ref=None, pulse_id=None):
    """テスト用のメッセージ payload (水位は文字数基準なので実長を持たせる)。"""
    payload = {"id": mid, "content": "x" * chars, "created_at": created_at}
    if episode_ref:
        payload["metadata"] = {"origin_episode": episode_ref}
    if pulse_id:
        payload["pulse_id"] = pulse_id
    return payload


def _stub_chronicle_refs(_persona, folds):
    """編纂が済んで級 1 エントリが引けた状態を模す。

    実装は「あらすじを持たない fold は退場させない」(下限の手続き強制) なので、
    退役ゲートや退場の形を見るテストでは refs が付いた状態を前提にする。
    """
    for i, fold in enumerate(folds):
        fold.chronicle_entry_ids = [f"entry-{i}"]
        fold.chronicle_short_ids = [i + 1]


def _window(messages, *, anchor_id=None, folds=None):
    """穴なしの窓 (raw == presented)。"""
    return SessionWindow(
        anchor_id=anchor_id or (messages[0]["id"] if messages else None),
        raw=list(messages),
        presented=list(messages),
        folds=list(folds or []),
    )


def _history_manager(messages):
    """窓を返すだけの history_manager スタブ。

    Metabolism は Beat ロックの内側で窓を撮り直す (ロック外の値で穴を上書き
    保存すると先行の穴が消えるため) ので、anchor 行があるテストでは実際に
    ここが呼ばれる。
    """
    return SimpleNamespace(
        get_history_from_anchor=(
            lambda anchor, required_line_roles=None, required_scopes=None,
            pulse_id=None: list(messages)
        )
    )


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


# ---------------------------------------------------------------------------
# ① 編纂の冪等 claim + ⑦ 台帳なし degrade (実 SAIMemory + 実台帳)
# ---------------------------------------------------------------------------


class FakeExecutor:
    """execute_plan の呼び出しを記録するだけの実行器代替 (W4 新経路)。

    クラス属性 ``calls`` / ``raise_error`` / ``report_cancelled`` をテストが
    操作する。(arasuji_entries には書かないため、plan は毎回同じ窓を見る —
    claim の dedup だけで二重実行が止まることを検証できる。)
    """

    calls: list = []
    raise_error: bool = False
    report_cancelled: bool = False

    @classmethod
    def execute_plan(cls, plan, client, conn, **kwargs):
        cls.calls.append(sum(len(c.messages) for c in plan.chunks))
        if cls.raise_error:
            raise RuntimeError("llm down")
        from sai_memory.arasuji.executor import ExecutionResult
        return ExecutionResult(cancelled=cls.report_cancelled)


class ChronicleClaimTest(unittest.TestCase):
    """generate_chronicle の冪等 claim (M1) と status。実 SAIMemory temp DB 使用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        persona_path = Path(self._tmp.name) / "personas" / PERSONA_ID
        persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        os.environ["MEMORY_WEAVE_BATCH_SIZE"] = "3"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_path, resource_id=PERSONA_ID,
        )
        self.addCleanup(self._close_adapter)

        # 編纂対象: batch_size (3) 以上の会話メッセージ
        for i in range(4):
            self.adapter.append_persona_message({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"メッセージ {i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        self.session_factory, self._engine = _make_session_factory()
        self.ledger = ExecutionLedger(self.session_factory)
        FakeExecutor.calls = []
        FakeExecutor.raise_error = False
        FakeExecutor.report_cancelled = False

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        os.environ.pop("SAIMEMORY_MEMORY", None)
        os.environ.pop("MEMORY_WEAVE_BATCH_SIZE", None)
        try:
            self._engine.dispose()
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def _make_lifecycle(self, *, with_ledger=True):
        manager = SimpleNamespace(SessionLocal=self.session_factory)
        if with_ledger:
            manager.execution_ledger = self.ledger
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        return lifecycle

    def _persona(self):
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )

    def _message_ids(self):
        """編纂候補メッセージの id (created_at 昇順)。"""
        from sai_memory.memory.storage import get_messages_for_chronicle
        return [m.id for m in get_messages_for_chronicle(self.adapter.conn)]

    def _generate(self, lifecycle):
        with patch("saiverse.model_configs.find_model_config",
                   return_value=("mock-model", {"provider": "mock", "context_length": 1000})), \
                patch("llm_clients.factory.get_llm_client", return_value=SimpleNamespace()), \
                patch("sai_memory.arasuji.executor.execute_plan", FakeExecutor.execute_plan), \
                patch("sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0), \
                patch("sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0), \
                patch("sai_memory.memory.entity_extractor.make_batch_callback",
                      side_effect=RuntimeError("skip entity extraction")):
            return lifecycle.generate_chronicle(self._persona(), force=True)

    def test_same_window_second_begin_is_skipped(self):
        """① 同じ窓の二重実行は claim (created=False) でスキップされる。"""
        lifecycle = self._make_lifecycle()
        first = self._generate(lifecycle)
        self.assertEqual(first, "ok")
        self.assertEqual(len(FakeExecutor.calls), 1)

        # FakeExecutor は arasuji_entries に書かないため窓は同一のまま。
        # 台帳の dedup だけが二重編纂 (二重 LLM コスト) を止める。
        second = self._generate(lifecycle)
        self.assertEqual(second, "deferred")
        self.assertEqual(len(FakeExecutor.calls), 1)  # 生成は走らない

    def test_failed_generation_returns_failed_and_same_window_retries(self):
        """② (編纂側) 生成失敗 → "failed"。failed claim はキー退避されるため
        **同じ窓でも**再試行できる (claim_execution の意味論 — failed は
        副作用ゼロ保証で再実行安全。Codex W4 二巡 #6)。"""
        lifecycle = self._make_lifecycle()
        FakeExecutor.raise_error = True
        self.assertEqual(self._generate(lifecycle), "failed")
        self.assertEqual(len(FakeExecutor.calls), 1)

        # 同じ窓: failed 行はキー退避 → 新しい claim で再試行成功
        FakeExecutor.raise_error = False
        self.assertEqual(self._generate(lifecycle), "ok")
        self.assertEqual(len(FakeExecutor.calls), 2)

        # 完了後の同じ窓: completed 行がブロック → deferred (二重編纂なし)
        self.assertEqual(self._generate(lifecycle), "deferred")
        self.assertEqual(len(FakeExecutor.calls), 2)

    def test_degrades_without_ledger(self):
        """⑦ 台帳が無い環境では claim なしで従来どおり実行される。"""
        lifecycle = self._make_lifecycle(with_ledger=False)
        self.assertEqual(self._generate(lifecycle), "ok")
        # dedup が無いので毎回実行される (従来挙動)
        self.assertEqual(self._generate(lifecycle), "ok")
        self.assertEqual(len(FakeExecutor.calls), 2)

    def test_cancelled_returns_deferred_and_claim_not_completed(self):
        """⑨ キャンセル = 部分適用を completed で封印しない (Codex W4 #8)。
        status は "deferred" (anchor 据え置き)、claim は failed 終端 →
        キー退避で**同じ窓のまま**すぐ再実行できる (二巡 #6)。"""
        lifecycle = self._make_lifecycle()
        FakeExecutor.report_cancelled = True
        self.assertEqual(self._generate(lifecycle), "deferred")

        # claim は completed でなく failed — 窓が伸びなくても再試行できる
        FakeExecutor.report_cancelled = False
        self.assertEqual(self._generate(lifecycle), "ok")

    def test_band_backlog_counts_into_confirmation_gate(self):
        """⑩ 帯あふれ backlog の統合 LLM 予測が確認ゲートの LLM 数に乗り、
        plan が空でも帯統合だけの実行に進む (Codex W4 #3/#4)。"""
        # 実 arasuji entries で帯 1 のあふれを作る (U=100/B=2 → cap 200)
        from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
        init_arasuji_tables(self.adapter.conn)
        for i in range(4):
            create_entry(
                self.adapter.conn, level=1, content=f"lv1-{i}",
                source_ids=[f"m{i}-src"],
                start_time=100 * (i + 1), end_time=100 * (i + 1) + 99,
                source_count=1, message_count=1,
                extra_metadata={"digest_origin": "batch", "coverage_chars": 100},
            )
        # 全メッセージを processed 扱いにして plan を空にする
        all_ids = [
            r[0] for r in self.adapter.conn.execute("SELECT id FROM messages")
        ]
        create_entry(
            self.adapter.conn, level=1, content="全編纂済み", source_ids=all_ids,
            start_time=1, end_time=2, source_count=len(all_ids),
            message_count=len(all_ids),
            extra_metadata={"digest_origin": "batch", "coverage_chars": 100},
        )

        order = []
        with patch.dict(os.environ, {
            "SAIVERSE_CHRONICLE_BAND_BUDGET": "100",
            "SAIVERSE_CHRONICLE_BAND_BASE": "2",
        }), \
                patch("saiverse.model_configs.find_model_config",
                      return_value=("mock-model", {"provider": "mock", "context_length": 1000})), \
                patch("llm_clients.factory.get_llm_client", return_value=SimpleNamespace()), \
                patch("sai_memory.arasuji.executor.execute_plan", FakeExecutor.execute_plan), \
                patch("sai_memory.arasuji.bands.backfill_coverage",
                      lambda conn: order.append("backfill") or 0), \
                patch("sai_memory.arasuji.bands.run_band_overflow",
                      lambda *a, **k: order.append("band") or 1), \
                patch("sai_memory.memory.entity_extractor.make_batch_callback",
                      side_effect=RuntimeError("skip entity extraction")):
            lifecycle = self._make_lifecycle(with_ledger=False)
            status = lifecycle.generate_chronicle(self._persona(), force=True)
        self.assertEqual(status, "ok")
        # plan は空 (全 processed) — それでも帯統合が実行された。
        # backfill は dry 予測より前 (Codex W4 三巡 #3 — 近似 dry と実測
        # backfill の食い違いで早期 return が永久化する穴の閉塞)。
        self.assertEqual(order, ["backfill", "band"])
        # executor は plan 空 (0 メッセージ) で呼ばれるか、呼ばれても空
        if FakeExecutor.calls:
            self.assertEqual(FakeExecutor.calls[-1], 0)

    def test_compile_groups_limit_compile_range(self):
        """⑧ 退場時圧縮 (chronicle_eviction.md §2): 編纂対象は「今回退場させる
        範囲そのもの」。退場しないメッセージは編纂に入らない。"""
        lifecycle = self._make_lifecycle(with_ledger=False)
        all_ids = self._message_ids()
        self.assertEqual(len(all_ids), 4)

        with patch("saiverse.model_configs.find_model_config",
                   return_value=("mock-model", {"provider": "mock", "context_length": 1000})), \
                patch("llm_clients.factory.get_llm_client", return_value=SimpleNamespace()), \
                patch("sai_memory.arasuji.executor.execute_plan", FakeExecutor.execute_plan), \
                patch("sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0), \
                patch("sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0), \
                patch("sai_memory.memory.entity_extractor.make_batch_callback",
                      side_effect=RuntimeError("skip entity extraction")):
            # 退場するのは先頭 2 件だけ → 計画に載るのも 2 件
            status = lifecycle.generate_chronicle(
                self._persona(), force=True, compile_groups=[all_ids[:2]],
            )
            self.assertEqual(status, "ok")
            self.assertEqual(FakeExecutor.calls[-1], 2)

            # 退場範囲が空 → 編纂対象なし = "ok" no-op
            calls_before = len(FakeExecutor.calls)
            status = lifecycle.generate_chronicle(
                self._persona(), force=True, compile_groups=[],
            )
            self.assertEqual(status, "ok")
            self.assertEqual(len(FakeExecutor.calls), calls_before)  # 実行されない

    def test_compile_groups_do_not_bundle_across_holes(self):
        """離れた範囲 (窓の途中を畳んだ結果) は 1 つのあらすじに束ねない (§4-5)。"""
        lifecycle = self._make_lifecycle(with_ledger=False)
        all_ids = self._message_ids()

        with patch("saiverse.model_configs.find_model_config",
                   return_value=("mock-model", {"provider": "mock", "context_length": 1000})), \
                patch("llm_clients.factory.get_llm_client", return_value=SimpleNamespace()), \
                patch("sai_memory.arasuji.executor.execute_plan", FakeExecutor.execute_plan), \
                patch("sai_memory.arasuji.bands.backfill_coverage", lambda conn: 0), \
                patch("sai_memory.arasuji.bands.run_band_overflow", lambda *a, **k: 0), \
                patch("sai_memory.memory.entity_extractor.make_batch_callback",
                      side_effect=RuntimeError("skip entity extraction")):
            captured = {}

            def _capture(plan, *a, **k):
                captured["chunks"] = [list(c.message_ids) for c in plan.chunks]
                return FakeExecutor.execute_plan(plan, *a, **k)

            with patch("sai_memory.arasuji.executor.execute_plan", _capture):
                status = lifecycle.generate_chronicle(
                    self._persona(), force=True,
                    compile_groups=[[all_ids[0]], [all_ids[2]]],
                )
            self.assertEqual(status, "ok")
            # 2 群は別チャンク (連続していないので束ねない)
            self.assertEqual(captured["chunks"], [[all_ids[0]], [all_ids[2]]])


# ---------------------------------------------------------------------------
# ② S2 ガード + ③ 退役の model 行独立 (実 session_anchor 行)
# ---------------------------------------------------------------------------


class RetirementGateTest(unittest.TestCase):
    """anchor 前進の S2 ガードと model 行独立性。"""

    def setUp(self):
        self.session_factory, self._engine = _make_session_factory()
        self.addCleanup(self._engine.dispose)

    def _make_lifecycle(self, chronicle_status):
        manager = SimpleNamespace(SessionLocal=self.session_factory)
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        if isinstance(chronicle_status, Exception):
            def _raise(p, cb=None, **kw):
                raise chronicle_status
            lifecycle.generate_chronicle = _raise
        else:
            lifecycle.generate_chronicle = lambda p, cb=None, **kw: chronicle_status
        lifecycle.ensure_recall_embeddings = lambda p: None
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs
        return lifecycle

    def _persona(self, messages=()):
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=_history_manager(messages),
        )

    def _run(self, lifecycle, model_key=None):
        # 低水位 2,000字 = 末尾 2 通を保護 / 目標 2,000字 / U=2,500字。
        # → 退場候補帯 m0..m2 が 1 束 (3,000字 ≥ U) になり、先頭から連続なので
        #   anchor が飲み込んで m3 へ進む。
        messages = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(5)]
        window = _window(messages)
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            lifecycle.run_metabolism(
                self._persona(messages), "b", window,
                Watermarks(low=2_000, target=2_000, high=4_000), None,
                model_key=model_key,
            )

    def test_failed_holds_anchor_and_next_run_retries(self):
        """② 編纂 "failed" → anchor 据え置き。編纂が直れば次の実行で前進する。"""
        lifecycle = self._make_lifecycle("failed")
        self._run(lifecycle, model_key="std-model")
        self.assertIsNone(lifecycle.load_anchor_entry(PERSONA_ID, "std-model"))

        # 例外経路 (generate_chronicle が raise) も同じく据え置き
        lifecycle_ex = self._make_lifecycle(RuntimeError("boom"))
        self._run(lifecycle_ex, model_key="std-model")
        self.assertIsNone(lifecycle_ex.load_anchor_entry(PERSONA_ID, "std-model"))

        # 編纂が直る → 次の maybe_run 相当の再実行で前進 (自然再試行)
        lifecycle_ok = self._make_lifecycle("ok")
        self._run(lifecycle_ok, model_key="std-model")
        entry = lifecycle_ok.load_anchor_entry(PERSONA_ID, "std-model")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["anchor_id"], "m3")

    def test_deferred_holds_anchor(self):
        """② claim 競合 / 確認拒否 ("deferred") も据え置き。"""
        lifecycle = self._make_lifecycle("deferred")
        self._run(lifecycle, model_key="std-model")
        self.assertIsNone(lifecycle.load_anchor_entry(PERSONA_ID, "std-model"))

    def test_ok_and_disabled_advance(self):
        """② "ok" と "disabled" (トグル OFF) は前進する。"""
        lifecycle = self._make_lifecycle("ok")
        self._run(lifecycle, model_key="std-model")
        self.assertEqual(
            lifecycle.load_anchor_entry(PERSONA_ID, "std-model")["anchor_id"], "m3",
        )

        # disabled: Chronicle トグル OFF → generate_chronicle 自体呼ばれず前進
        lifecycle_off = self._make_lifecycle("failed")  # 呼ばれたら failed になる仕込み
        lifecycle_off.is_chronicle_enabled_for_persona = lambda p: False
        self._run(lifecycle_off, model_key="other-model")
        self.assertEqual(
            lifecycle_off.load_anchor_entry(PERSONA_ID, "other-model")["anchor_id"], "m3",
        )

    def test_retirement_advances_only_specified_model_row(self):
        """③ 退役は渡された model の行だけを進め、他 model の行は不変。"""
        lifecycle = self._make_lifecycle("ok")
        # 事前に両 model の行を張る
        t0 = datetime.now().replace(microsecond=0)
        lifecycle.upsert_anchor_entry(PERSONA_ID, "std-model", {
            "anchor_id": "old-std", "updated_at": t0.isoformat(),
        })
        lifecycle.upsert_anchor_entry(PERSONA_ID, "light-model", {
            "anchor_id": "old-light", "updated_at": t0.isoformat(),
        })

        self._run(lifecycle, model_key="light-model")

        self.assertEqual(
            lifecycle.load_anchor_entry(PERSONA_ID, "light-model")["anchor_id"], "m3",
        )
        # std-model の行は不変
        std = lifecycle.load_anchor_entry(PERSONA_ID, "std-model")
        self.assertEqual(std["anchor_id"], "old-std")
        self.assertEqual(std["updated_at"], t0.isoformat())


# ---------------------------------------------------------------------------
# ⑤ 退役境界の episode スナップ (W4 D2 — §4-1 × §4-2)
# ---------------------------------------------------------------------------


class EpisodeUnitEvictionTest(unittest.TestCase):
    """退場は episode 単位 (docs/intent/chronicle_eviction.md §3/§5)。

    open episode は単独でしか畳まず、U に届かないうちは生のまま残る。届いた
    open は pulse 関節で刻んで部分退場する。closed 同士は episode をまたいで
    束ねてよい。
    """

    def setUp(self):
        self.session_factory, self._engine = _make_session_factory()
        self.addCleanup(self._engine.dispose)
        self.manager = SimpleNamespace(SessionLocal=self.session_factory)

    def _make_lifecycle(self, chronicle_status="ok"):
        lifecycle = SessionLifecycle(SimpleNamespace(), self.manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: chronicle_status
        lifecycle.ensure_recall_embeddings = lambda p: None
        # 編纂参照・子 episode 記帳は別テストの守備範囲 (ここでは退場の形だけ見る)
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs
        lifecycle._record_partial_episode = lambda p, f: None
        return lifecycle

    def _persona(self, messages=()):
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=_history_manager(messages),
        )

    def _open_episode(self):
        from saiverse.episodes import KIND_CONVERSATION, open_episode
        return open_episode(self.manager, PERSONA_ID, KIND_CONVERSATION)

    def _run(self, lifecycle, messages, watermarks, *, band_budget=2_000):
        window = _window(messages)
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}), \
                patch.dict(os.environ,
                           {"SAIVERSE_CHRONICLE_BAND_BUDGET": str(band_budget)}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            lifecycle.run_metabolism(
                self._persona(messages), "b", window, watermarks, None,
                model_key="std-model",
            )
        return window

    def _anchor(self, lifecycle):
        entry = lifecycle.load_anchor_entry(PERSONA_ID, "std-model")
        return entry["anchor_id"] if entry else None

    def test_small_open_episode_is_not_folded(self):
        """U 未満の open episode は畳めない = 生のまま残る (会話 A が守られる)。

        古い側に U 未満の open 会話、新しい側に保護帯。退場候補帯は open だけ
        なので、どのまとまりも U に届かず今回は何も畳まない。
        """
        ref = self._open_episode()["episode_ref"]
        msgs = [_msg(f"m{i}", 100 + i, chars=500, episode_ref=ref) for i in range(2)]
        msgs += [_msg(f"n{i}", 200 + i, chars=1_000) for i in range(3)]
        lifecycle = self._make_lifecycle("ok")
        self._run(lifecycle, msgs, Watermarks(low=3_000, target=1_000, high=5_000))
        # 強制クローズは事前試算で見送られる (§5-5 — 閉じても U(2,000) に届かない:
        # open 会話は 1,000字)。anchor も動かない。
        self.assertIsNone(self._anchor(lifecycle))

    def test_large_open_episode_folds_in_pulse_units(self):
        """U に達した open episode は pulse 関節で刻んで部分退場する (§6)。"""
        ref = self._open_episode()["episode_ref"]
        # pulse p1 = 2 通 (2,000字) で U 到達。p2 以降は保護帯側。
        msgs = [
            _msg("a0", 100, chars=1_000, episode_ref=ref, pulse_id="p1"),
            _msg("a1", 101, chars=1_000, episode_ref=ref, pulse_id="p1"),
            _msg("a2", 102, chars=1_000, episode_ref=ref, pulse_id="p2"),
            _msg("a3", 103, chars=1_000, episode_ref=ref, pulse_id="p2"),
        ]
        lifecycle = self._make_lifecycle("ok")
        folded = []
        lifecycle._record_partial_episode = lambda p, f: folded.append(f.message_ids)
        self._run(lifecycle, msgs, Watermarks(low=2_000, target=2_000, high=3_000))
        # p1 が丸ごと退場し、anchor は a2 へ。p2 は保護帯なので残る。
        self.assertEqual(self._anchor(lifecycle), "a2")
        self.assertEqual(folded, [["a0", "a1"]])

    def test_closed_episodes_bundle_across_boundaries(self):
        """closed 同士は episode をまたいで束ねて U に届かせる (§3)。"""
        from saiverse.episodes import close_episode
        ep1 = self._open_episode()
        close_episode(self.manager, PERSONA_ID, ep1["episode_id"])
        ep2 = self._open_episode()
        close_episode(self.manager, PERSONA_ID, ep2["episode_id"])
        msgs = [
            _msg("c0", 100, chars=1_000, episode_ref=ep1["episode_ref"]),
            _msg("c1", 101, chars=1_000, episode_ref=ep2["episode_ref"]),
            _msg("c2", 102, chars=1_000),
            _msg("c3", 103, chars=1_000),
        ]
        lifecycle = self._make_lifecycle("ok")
        self._run(lifecycle, msgs, Watermarks(low=2_000, target=2_000, high=3_000))
        # c0 (ep1) + c1 (ep2) で U=2,000 到達 → 束ねて退場、anchor は c2。
        self.assertEqual(self._anchor(lifecycle), "c2")

    def test_protected_band_is_never_evicted(self):
        """低水位ぶんの直近は退場させない (§5-1 — 作業の直近が守られる)。"""
        msgs = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(6)]
        lifecycle = self._make_lifecycle("ok")
        self._run(lifecycle, msgs, Watermarks(low=4_000, target=1_000, high=5_000))
        # 末尾 4,000字 (m2..m5) は保護。候補は m0/m1 の 2,000字 = U ちょうど。
        self.assertEqual(self._anchor(lifecycle), "m2")

    def test_fold_without_chronicle_entry_is_not_evicted(self):
        """あらすじを持たない範囲は退場させない (下限の手続き強制、§2)。

        穴は「生ログの代わりに digest を見せる」記録なので、digest が無い穴は
        その範囲を黙って消すだけになる。退場そのものを見送って生で残す。
        """
        msgs = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(6)]
        lifecycle = self._make_lifecycle("ok")
        lifecycle._attach_chronicle_refs = lambda p, folds: None  # 引き当て失敗
        saved = []
        lifecycle.save_folded_ranges = lambda pid, mk, folds: saved.append(folds)
        self._run(lifecycle, msgs, Watermarks(low=2_000, target=1_000, high=5_000))
        self.assertIsNone(self._anchor(lifecycle))
        self.assertEqual(saved, [[]])

    def test_same_range_is_not_folded_twice(self):
        """既に穴になっている範囲を二重に記録しない。

        あらすじが一時的に引けないと提示に生ログが戻る (fail-open) ため、計画は
        同じ範囲をもう一度畳もうとする。ここで弾かないと同一範囲の穴が毎回 1 本
        ずつ積み上がり、JSON と照会コストが単調に増える。
        """
        from sea.session_window import FoldedRange
        open_ref = self._open_episode()["episode_ref"]
        msgs = [
            # 先頭は U 未満の open → 畳めない = anchor が動けない (穴が残る形)
            _msg("a0", 100, chars=500, episode_ref=open_ref),
            _msg("m1", 101, chars=1_000), _msg("m2", 102, chars=1_000),
            _msg("m3", 103, chars=1_000), _msg("m4", 104, chars=1_000),
            _msg("k0", 200, chars=1_000), _msg("k1", 201, chars=1_000),
        ]
        existing = FoldedRange(
            message_ids=["m1", "m2"], start_at=101, end_at=102,
            chronicle_entry_ids=["old"], chronicle_short_ids=[1],
        )
        window = SessionWindow(
            anchor_id="a0", raw=list(msgs), presented=list(msgs), folds=[existing],
        )
        lifecycle = self._make_lifecycle("ok")
        saved = []
        lifecycle.save_folded_ranges = lambda pid, mk, folds: saved.append(folds)
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2000"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            lifecycle.run_metabolism(
                self._persona(msgs), "b", window,
                Watermarks(low=2_000, target=1_000, high=5_000), None,
                model_key="std-model",
            )
        # m1/m2 は再度記録されず、新しく畳まれるのは m3/m4 だけ
        self.assertEqual(len(saved), 1)
        self.assertEqual(
            [f.message_ids for f in saved[0]], [["m1", "m2"], ["m3", "m4"]],
        )

    def test_partial_fold_records_closed_child_episode_with_digest(self):
        """open episode の部分退場は、閉じた子 episode + 再訪の鍵として刻まれる。

        experience_structure §6 (pulse 関節細分)。open/close/継承エッジは単一
        トランザクション — close が落ちて「開きっぱなしの子」が残ると、それが
        `get_open_episode` の「最後に開いた open」を奪い、以後の会話が合成
        episode に付いてしまう。
        """
        from saiverse.episodes import get_by_ref, get_open_episode
        from saiverse.experience_inheritance import get_parents
        parent = self._open_episode()
        ref = parent["episode_ref"]
        msgs = [
            _msg("a0", 100, chars=1_000, episode_ref=ref, pulse_id="p1"),
            _msg("a1", 101, chars=1_000, episode_ref=ref, pulse_id="p1"),
            _msg("a2", 102, chars=1_000, episode_ref=ref, pulse_id="p2"),
            _msg("a3", 103, chars=1_000, episode_ref=ref, pulse_id="p2"),
        ]
        lifecycle = self._make_lifecycle("ok")
        # 子 episode の記帳だけは本物を通す (この経路が検証対象)
        del lifecycle._record_partial_episode
        self._run(lifecycle, msgs, Watermarks(low=2_000, target=2_000, high=3_000))

        # 親は開いたまま (長さを理由に出来事を分割しない §6)
        self.assertEqual(get_by_ref(self.manager, PERSONA_ID, ref)["status"], "open")
        # 子は closed + digest 参照つき
        child = get_by_ref(self.manager, PERSONA_ID, "episode:2")
        self.assertEqual(child["status"], "closed")
        self.assertTrue(child["digest_ref"].startswith("chronicle:"))
        self.assertEqual(child["meta"]["partial_of"], ref)
        # 開きっぱなしの子は残らない (最後に開いた open は親のまま)
        self.assertEqual(
            get_open_episode(self.manager, PERSONA_ID)["episode_ref"], ref,
        )
        # 親 → 子 の継承エッジ (digest 層) が張られている
        parents = get_parents(self.manager, PERSONA_ID, ref)
        self.assertEqual([p["layer"] for p in parents], ["digest"])

    def test_anchor_moved_outside_eviction_clears_holes(self):
        """退場経路以外で anchor が差し替わったら穴を捨てる。

        TTL 失効後の最小ロードで新しい起点が立ち、LLM 成功後の touch がそれを
        永続化する経路がある。古い穴を残すと、その範囲は窓に出ない (anchor の
        外) のに head の Chronicle 枠からは除外され続け、**窓にも head にも
        現れない**体験になる。
        """
        from sea.session_window import FoldedRange
        lifecycle = self._make_lifecycle("ok")
        t0 = datetime.now().replace(microsecond=0)
        lifecycle.upsert_anchor_entry(PERSONA_ID, "std-model", {
            "anchor_id": "m0", "updated_at": t0.isoformat(),
        })
        lifecycle.save_folded_ranges(PERSONA_ID, "std-model", [
            FoldedRange(message_ids=["m1"], chronicle_entry_ids=["e1"]),
        ])
        self.assertTrue(lifecycle.load_folded_ranges(PERSONA_ID, "std-model"))

        # anchor だけを別経路で差し替える (touch_anchor_after_llm_call 相当)
        lifecycle.update_anchor_for_model(persona=self._persona(), model_key="std-model",
                                          anchor_id="z9")
        self.assertEqual(lifecycle.load_folded_ranges(PERSONA_ID, "std-model"), [])

        # 同じ anchor への再 touch では穴を消さない
        lifecycle.save_folded_ranges(PERSONA_ID, "std-model", [
            FoldedRange(message_ids=["z9"], chronicle_entry_ids=["e2"]),
        ])
        lifecycle.update_anchor_for_model(persona=self._persona(), model_key="std-model",
                                          anchor_id="z9")
        self.assertEqual(
            [f.message_ids for f in lifecycle.load_folded_ranges(PERSONA_ID, "std-model")],
            [["z9"]],
        )

    def test_middle_fold_keeps_anchor_and_records_hole(self):
        """窓の途中を畳んだときは anchor を動かさず穴として記録する (§6)。"""
        open_ref = self._open_episode()["episode_ref"]
        msgs = [
            # 先頭は U 未満の open 会話 A → 畳めない = anchor は動けない
            _msg("a0", 100, chars=500, episode_ref=open_ref),
            # 続く無帰属 (closed 扱い) が U に到達 → 窓の途中が畳まれる
            _msg("b0", 101, chars=1_000),
            _msg("b1", 102, chars=1_000),
            _msg("k0", 200, chars=1_000),
            _msg("k1", 201, chars=1_000),
        ]
        lifecycle = self._make_lifecycle("ok")
        saved = []
        lifecycle.save_folded_ranges = lambda pid, mk, folds: saved.append(folds)
        self._run(lifecycle, msgs, Watermarks(low=2_000, target=1_000, high=5_000))
        self.assertIsNone(self._anchor(lifecycle))
        self.assertEqual(len(saved), 1)
        self.assertEqual([f.message_ids for f in saved[0]], [["b0", "b1"]])


# ---------------------------------------------------------------------------
# ④ touch_anchor_after_llm_call の call-local anchor_id
# ---------------------------------------------------------------------------


class CallLocalAnchorTouchTest(unittest.TestCase):
    def setUp(self):
        self.session_factory, self._engine = _make_session_factory()
        self.addCleanup(self._engine.dispose)

    def _make_lifecycle(self):
        manager = SimpleNamespace(SessionLocal=self.session_factory)
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        lifecycle.get_anchor_validity_seconds = lambda mk, pid=None: 1200
        lifecycle.schedule_cache_ttl_pulse = lambda persona, mk, ct: None
        lifecycle.check_token_threshold = lambda persona, mk, usage: None
        return lifecycle

    @staticmethod
    def _usage(model="light-model"):
        return SimpleNamespace(
            model=model, input_tokens=100, output_tokens=5,
            cached_tokens=0, cache_write_tokens=0, cache_ttl="",
        )

    @patch("saiverse.model_configs.get_cache_config", return_value={"type": "implicit"})
    def test_anchor_id_none_does_not_touch(self, _mock_cache):
        """④ anchor_id=None (prefix に anchor が無い呼び出し) は touch しない。
        旧: persona 属性フォールバックで旧 anchor を touch していた (第 4 片)。"""
        lifecycle = self._make_lifecycle()
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, model="std-model",
            # 旧実装が読んでいた属性を模した残留値があっても読まれないこと
            history_manager=SimpleNamespace(metabolism_anchor_message_id="stale"),
        )
        lifecycle.touch_anchor_after_llm_call(persona, self._usage(), anchor_id=None)
        self.assertEqual(lifecycle.load_anchor_entries(PERSONA_ID), {})

    @patch("saiverse.model_configs.get_cache_config", return_value={"type": "implicit"})
    def test_anchor_id_is_used_call_locally(self, _mock_cache):
        """④ 渡した anchor_id が usage.model の行に記帳される (state 経由の値)。"""
        lifecycle = self._make_lifecycle()
        persona = SimpleNamespace(persona_id=PERSONA_ID, model="std-model")
        state = {"_prefix_anchor_id": "a-42"}  # runner が積む形
        lifecycle.touch_anchor_after_llm_call(
            persona, self._usage(model="light-model"),
            anchor_id=state.get("_prefix_anchor_id"),
        )
        entry = lifecycle.load_anchor_entry(PERSONA_ID, "light-model")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["anchor_id"], "a-42")
        self.assertIsNone(lifecycle.load_anchor_entry(PERSONA_ID, "std-model"))


# ---------------------------------------------------------------------------
# ⑤ 可視化 dispatch が当該 model へ向く
# ---------------------------------------------------------------------------


class MetabolismVisualizationDispatchTest(unittest.TestCase):
    def test_on_metabolism_passes_model_key_to_head_input(self):
        from saiverse.dynamic_state import DynamicStateManager

        persona = SimpleNamespace(persona_id=PERSONA_ID, current_building_id="b")
        manager = SimpleNamespace()

        fake_ctx = SimpleNamespace(persona_id=PERSONA_ID, model_key="light-model")
        build = MagicMock(return_value=fake_ctx)
        pipeline = MagicMock()
        pipeline.registry.all_sections.return_value = ["section"]

        with patch("sea.head_pipeline.build_line_head_input", build), \
                patch("sea.head_pipeline.get_default_pipeline", return_value=pipeline):
            DynamicStateManager.on_metabolism(persona, manager, model_key="light-model")

        build.assert_called_once_with(persona, manager, "b", model_key="light-model")
        pipeline.dispatch_event.assert_called_once()
        (ctx_arg, _event), _ = pipeline.dispatch_event.call_args
        self.assertIs(ctx_arg, fake_ctx)

    def test_run_metabolism_dispatches_with_advancing_model(self):
        """_run_metabolism_locked → on_metabolism に「anchor を進めた model」が渡る。"""
        session_factory, engine = _make_session_factory()
        self.addCleanup(engine.dispose)
        manager = SimpleNamespace(SessionLocal=session_factory)
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False  # → "disabled"
        lifecycle.ensure_recall_embeddings = lambda p: None
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs

        messages = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(5)]
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=_history_manager(messages),
        )
        dispatched = []
        window = _window(messages)
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda p, m, model_key=None: dispatched.append(model_key)):
            lifecycle.run_metabolism(
                persona, "b", window,
                Watermarks(low=2_000, target=2_000, high=4_000), None,
                model_key="light-model",
            )
        self.assertEqual(dispatched, ["light-model"])


# ---------------------------------------------------------------------------
# ⑥ chronicle_index の件数ラベル diff は退役
# ---------------------------------------------------------------------------


class ChronicleIndexDiffRetiredTest(unittest.TestCase):
    def test_diff_returns_empty_even_with_new_entries(self):
        from sea.head_pipeline.sections.chronicle_index import (
            ChronicleEntryItem,
            ChronicleIndexSection,
            ChronicleIndexSnapshot,
        )

        section = ChronicleIndexSection()
        now = time.time()
        old = ChronicleIndexSnapshot(captured_at=now - 100, entries=())
        new = ChronicleIndexSnapshot(
            captured_at=now,
            entries=(
                ChronicleEntryItem(entry_id="e1", level=1, created_at=int(now) - 10),
                ChronicleEntryItem(entry_id="e2", level=2, created_at=int(now) - 5),
            ),
        )
        # 旧実装なら「Chronicleに新しいエントリが追加されました」ラベルが出た局面
        self.assertEqual(section.diff_to_notifications(old, new), [])


if __name__ == "__main__":
    unittest.main()
