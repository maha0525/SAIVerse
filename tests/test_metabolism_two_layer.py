"""Metabolism 二層分離 (統合工事 §6-5、SEA 監査 S2/M1) のテスト。

docs/intent/beat_execution_context.md §3.2:

- 編纂 (Chronicle 生成) は persona に一度 — 実行台帳の冪等 claim
  (kind="metabolism.run"、idempotency_key = persona:提示コンテキスト末尾ID) で全入口が
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
    """編纂が済んで一次あらすじ エントリが引けた状態を模す。

    実装は「あらすじを持たない fold は退場させない」(下限の手続き強制) なので、
    退役ゲートや退場の形を見るテストでは refs が付いた状態を前提にする。
    """
    for i, fold in enumerate(folds):
        fold.chronicle_entry_ids = [f"entry-{i}"]
        fold.chronicle_short_ids = [i + 1]


def _window(messages, *, anchor_id=None, folds=None):
    """圧縮区間なしの提示コンテキスト (raw == presented)。"""
    return SessionWindow(
        anchor_id=anchor_id or (messages[0]["id"] if messages else None),
        raw=list(messages),
        presented=list(messages),
        folds=list(folds or []),
    )


def _history_manager(messages):
    """提示コンテキストを返すだけの history_manager スタブ。

    Metabolism は Beat ロックの内側で提示コンテキストを撮り直す (ロック外の値で圧縮区間を上書き
    保存すると先行の圧縮区間が消えるため) ので、anchor 行があるテストでは実際に
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
    操作する。(arasuji_entries には書かないため、plan は毎回同じ提示コンテキストを見る —
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
        # 極小 run 吸収 (2026-08-31): 材料 0.5U 未満の run は全量計画で単独
        # 編纂されない。本クラスの関心は claim であって run の大きさではない
        # ので、U を小さくして 4 通の小さなメッセージが通常編纂に乗る前提を保つ。
        os.environ["SAIVERSE_CHRONICLE_BAND_BUDGET"] = "10"
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
        os.environ.pop("SAIVERSE_CHRONICLE_BAND_BUDGET", None)
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
        """① 同じ提示コンテキストの二重実行は claim (created=False) でスキップされる。"""
        lifecycle = self._make_lifecycle()
        first = self._generate(lifecycle)
        self.assertEqual(first, "ok")
        self.assertEqual(len(FakeExecutor.calls), 1)

        # FakeExecutor は arasuji_entries に書かないため提示コンテキストは同一のまま。
        # 台帳の dedup だけが二重編纂 (二重 LLM コスト) を止める。
        second = self._generate(lifecycle)
        self.assertEqual(second, "deferred")
        self.assertEqual(len(FakeExecutor.calls), 1)  # 生成は走らない

    def test_failed_generation_returns_failed_and_same_window_retries(self):
        """② (編纂側) 生成失敗 → "failed"。failed claim はキー退避されるため
        **同じ提示コンテキストでも**再試行できる (claim_execution の意味論 — failed は
        副作用ゼロ保証で再実行安全。Codex W4 二巡 #6)。"""
        lifecycle = self._make_lifecycle()
        FakeExecutor.raise_error = True
        self.assertEqual(self._generate(lifecycle), "failed")
        self.assertEqual(len(FakeExecutor.calls), 1)

        # 同じ提示コンテキスト: failed 行はキー退避 → 新しい claim で再試行成功
        FakeExecutor.raise_error = False
        self.assertEqual(self._generate(lifecycle), "ok")
        self.assertEqual(len(FakeExecutor.calls), 2)

        # 完了後の同じ提示コンテキスト: completed 行がブロック → deferred (二重編纂なし)
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
        キー退避で**同じ提示コンテキストのまま**すぐ再実行できる (二巡 #6)。"""
        lifecycle = self._make_lifecycle()
        FakeExecutor.report_cancelled = True
        self.assertEqual(self._generate(lifecycle), "deferred")

        # claim は completed でなく failed — 提示コンテキストが伸びなくても再試行できる
        FakeExecutor.report_cancelled = False
        self.assertEqual(self._generate(lifecycle), "ok")

    def test_band_backlog_counts_into_confirmation_gate(self):
        """⑩ 列のあふれ backlog の統合 LLM 予測が確認ゲートの LLM 数に乗り、
        plan が空でも列の統合だけの実行に進む (Codex W4 #3/#4)。"""
        # 実 arasuji entries でレベル1 の並びの予算超過を作る (上限 5,000 字:
        # 9 × 600 = 5,400 > 5,000 → 畳みが 1 件計画される)
        from sai_memory.arasuji.storage import create_entry, init_arasuji_tables
        init_arasuji_tables(self.adapter.conn)
        for i in range(9):
            create_entry(
                self.adapter.conn, level=1, content="x" * 600,
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
        with patch("saiverse.model_configs.find_model_config",
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
        # plan は空 (全 processed) — それでも列の統合が実行された。
        # backfill は dry 予測より前 (Codex W4 三巡 #3 — 近似 dry と実測
        # backfill の食い違いで早期 return が永久化する圧縮区間の閉塞)。
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
        """離れた範囲 (提示コンテキストの途中を畳んだ結果) は 1 つのあらすじに束ねない (§4-5)。"""
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

    def test_compile_groups_boundary_survives_excluded_head(self):
        """群の先頭が Chronicle 除外対象でも fold 境界は立つ (§4-5 回帰)。

        除外 line_role (sub_line / meta_judgment / nested) のメッセージは
        編纂対象に現れない (2026-08-29 裁定でタグ除外は解除されたので、
        除外の代表は line_role)。境界を「fold の先頭 id」で持っていたときは、
        先頭が落ちた fold の境界が一度も立たず、離れた fold が一つの
        あらすじに束ねられていた
        (docs/issues/archive/chronicle_run_boundary_lost_by_excluded_tag.md)。

        除外メッセージの時系列上の位置は問わない — フィルタで編纂対象から
        丸ごと消えるので、計画が見るのは「残ったメッセージの所属」だけ。
        """
        lifecycle = self._make_lifecycle(with_ledger=False)
        all_ids = self._message_ids()

        excluded_id = self.adapter.append_persona_message({
            "role": "assistant",
            "content": "サブラインの作業ログ",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "line_role": "sub_line",
        })
        self.assertIsNotNone(excluded_id)
        # 前提の確認: 除外 line_role は編纂対象から落ちている
        self.assertNotIn(excluded_id, self._message_ids())

        with patch("saiverse.model_configs.find_model_config",
                   return_value=("mock-model", {"provider": "mock", "context_length": 1000})), \
                patch("llm_clients.factory.get_llm_client", return_value=SimpleNamespace()), \
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
                    compile_groups=[
                        [all_ids[0], all_ids[1]],
                        # 先頭が編纂対象に居ない fold
                        [excluded_id, all_ids[2]],
                    ],
                )
            self.assertEqual(status, "ok")
            self.assertEqual(
                captured["chunks"], [[all_ids[0], all_ids[1]], [all_ids[2]]],
            )

    def test_filter_chronicle_eligible_ids_shares_the_compile_filter(self):
        """filter_chronicle_eligible_ids は編纂の入力フィルタと同じ集合を返す。

        退場適用側の「この fold にあらすじが生まれる可能性はあるか」判定
        (顔その1) の土台。除外 line_role のメッセージは対象外、通常
        メッセージと機構名義の行 (spell タグ等) は対象 (2026-08-29 裁定 —
        本人の提示に立った行はすべて編纂の材料に入る)。
        """
        from sai_memory.memory.storage import filter_chronicle_eligible_ids
        all_ids = self._message_ids()

        excluded_id = self.adapter.append_persona_message({
            "role": "assistant",
            "content": "サブラインの作業ログ",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "line_role": "sub_line",
        })
        self.assertIsNotNone(excluded_id)
        # 機構名義の行 (spell タグ) は 2026-08-29 裁定から編纂対象に**入る**。
        spell_id = self.adapter.append_persona_message({
            "role": "system",
            "content": "[Spell Result: web_search]\n検索結果本文",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {"tags": ["conversation", "spell"]},
        })
        self.assertIsNotNone(spell_id)

        eligible = filter_chronicle_eligible_ids(
            self.adapter.conn, all_ids + [excluded_id, spell_id],
        )
        self.assertEqual(eligible, set(all_ids) | {spell_id})
        # 全部が除外 line_role = 空集合 (あらすじは永久に生まれない fold の形)
        self.assertEqual(
            filter_chronicle_eligible_ids(self.adapter.conn, [excluded_id]), set(),
        )
        self.assertEqual(filter_chronicle_eligible_ids(self.adapter.conn, []), set())



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
        # → 退場候補範囲 m0..m2 が 1 束 (3,000字 ≥ U) になり、先頭から連続なので
        #   anchor が飲み込んで m3 へ進む。
        messages = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(5)]
        window = _window(messages)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}), \
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
    """退場の形 (docs/intent/arasuji_levels.md §3/§4)。

    残す量 (watermarks.target) より古い側を、古い順に U ずつ刻んで全部畳む。
    エピソードに畳みを止める権利は無い — 帰属も開閉も畳みに影響しない。

    ⚠ 部分エピソード記録 (``_record_partial_episode``) を見ていたケースは
    束 6c (2026-08-22、autonomous_behavior_v3.md §7) で削除した。「エピソード
    という専用の記録行は持たない」の裁定でメソッドごと退役し、畳んだ範囲の
    記録は Chronicle エントリが持つようになったので、「呼ばれない」ことを
    固定する相手が存在しない。畳み自体の挙動テストはそのまま残す。

    帰属タグ (``metadata.origin_episode``) の刻印も同じ裁定で退役したので、
    ここで渡す ``episode_ref`` は旧世代が書き残した行に付いている値の再現
    (畳みの判定には使われず、被覆元の錨として記録されるだけ)。
    """

    #: 旧世代のメッセージに残っている帰属タグ (episodes テーブルは読み取り専用の
    #: 残置なので、行そのものは作らない — 畳みは値を読むだけで引かない)。
    LEGACY_EPISODE_REF = "episode:1"

    def setUp(self):
        self.session_factory, self._engine = _make_session_factory()
        self.addCleanup(self._engine.dispose)
        self.manager = SimpleNamespace(SessionLocal=self.session_factory)

    def _make_lifecycle(self, chronicle_status="ok"):
        lifecycle = SessionLifecycle(SimpleNamespace(), self.manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: chronicle_status
        lifecycle.ensure_recall_embeddings = lambda p: None
        # 編纂参照の引き当ては別テストの守備範囲 (ここでは退場の形だけ見る)
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs
        return lifecycle

    def _persona(self, messages=()):
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=_history_manager(messages),
        )

    def _run(self, lifecycle, messages, watermarks, *, band_budget=2_000):
        window = _window(messages)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}), \
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

    def test_undersized_open_at_the_front_is_folded_so_the_anchor_advances(self):
        """先頭の U 未満 open も普通に畳まれ、anchor が進む (拒否権の廃止)。

        旧設計は open episode を守る二段構えを持ち、それが取り残しと恒久的な
        詰まりの温床だった。新設計では帰属も開閉も畳みに影響しない。
        """
        ref = self.LEGACY_EPISODE_REF
        msgs = [_msg(f"m{i}", 100 + i, chars=500, episode_ref=ref) for i in range(2)]
        msgs += [_msg(f"n{i}", 200 + i, chars=1_000) for i in range(3)]
        lifecycle = self._make_lifecycle("ok")
        # 残す量 1,000字 → 保護は n2 のみ。候補 m0,m1,n0,n1 (3,000字) のうち
        # U=2,000 で [m0,m1,n0] が畳まれ、端数 n1 は次回へ残る。
        self._run(lifecycle, msgs, Watermarks(low=0, target=1_000, high=5_000))
        self.assertEqual(self._anchor(lifecycle), "n1")

    def test_fold_cuts_at_pulse_joint(self):
        """畳みは pulse 関節で切れる (帰属が同じでも関節をまたがない)。

        ⚠ 元は test_open_episode_folds_at_pulse_joint_without_partial_record で、
        「部分エピソード記録 (open_episode_ref → 子 episode 化) が呼ばれない」
        ことも一緒に固定していた。その機構は束 6c (2026-08-22、v3 §7) で
        ``_record_partial_episode`` ごと退役したため、後半の主張は相手を失った
        (呼ばれないメソッドが存在しない)。切り位置の主張だけを残す。
        """
        ref = self.LEGACY_EPISODE_REF
        msgs = [
            _msg("a0", 100, chars=1_000, episode_ref=ref, pulse_id="p1"),
            _msg("a1", 101, chars=1_000, episode_ref=ref, pulse_id="p1"),
            _msg("a2", 102, chars=1_000, episode_ref=ref, pulse_id="p2"),
            _msg("a3", 103, chars=1_000, episode_ref=ref, pulse_id="p2"),
        ]
        lifecycle = self._make_lifecycle("ok")
        self._run(lifecycle, msgs, Watermarks(low=0, target=2_000, high=3_000))
        # p1 が丸ごと退場し、anchor は a2 へ。p2 は残す量の側なので残る。
        self.assertEqual(self._anchor(lifecycle), "a2")

    def test_messages_bundle_across_episode_boundaries(self):
        """帰属の違うメッセージも束ねて U に届かせる (§3 — 境界に拒否権は無い)。"""
        msgs = [
            _msg("c0", 100, chars=1_000, episode_ref="episode:1"),
            _msg("c1", 101, chars=1_000, episode_ref="episode:2"),
            _msg("c2", 102, chars=1_000),
            _msg("c3", 103, chars=1_000),
        ]
        lifecycle = self._make_lifecycle("ok")
        self._run(lifecycle, msgs, Watermarks(low=2_000, target=2_000, high=3_000))
        # c0 (ep1) + c1 (ep2) で U=2,000 到達 → 束ねて退場、anchor は c2。
        self.assertEqual(self._anchor(lifecycle), "c2")

    def test_protected_band_is_never_evicted(self):
        """残す量ぶんの直近は退場させない (作業の直近が守られる)。"""
        msgs = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(6)]
        lifecycle = self._make_lifecycle("ok")
        self._run(lifecycle, msgs, Watermarks(low=0, target=4_000, high=5_000))
        # 末尾 4,000字 (m2..m5) は残す量。候補は m0/m1 の 2,000字 = U ちょうど。
        self.assertEqual(self._anchor(lifecycle), "m2")

    def test_fold_without_chronicle_entry_is_not_evicted(self):
        """あらすじを持たない範囲は退場させない (下限の手続き強制、§2)。

        圧縮区間は「生ログの代わりに digest を見せる」記録なので、digest が無い圧縮区間は
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
        """既に圧縮区間になっている範囲を二重に記録しない。

        あらすじが一時的に引けないと提示に生ログが戻る (fail-open) ため、計画は
        同じ範囲をもう一度畳もうとする。ここで弾かないと同一範囲の圧縮区間が毎回 1 本
        ずつ積み上がり、JSON と照会コストが単調に増える。
        """
        from sea.session_window import FoldedRange
        msgs = [
            # 先頭 a0 は既存の圧縮区間 [m1,m2] と同じ畳み範囲に入る → 重なり
            # スキップで畳まれず、anchor は動けない (圧縮区間が残る形)
            _msg("a0", 100, chars=500, episode_ref=self.LEGACY_EPISODE_REF),
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
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2000"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            lifecycle.run_metabolism(
                self._persona(msgs), "b", window,
                # 残す量 2,000字 → 保護は k0/k1。候補 a0,m1..m4 のうち計画は
                # [a0,m1,m2] と [m3,m4] を畳もうとするが、前者は既存の圧縮区間
                # [m1,m2] と重なるので二重記録スキップされる。
                Watermarks(low=0, target=2_000, high=8_000), None,
                model_key="std-model",
            )
        # m1/m2 は再度記録されず、新しく畳まれるのは m3/m4 だけ。先頭 a0 は
        # 畳まれなかった (重なりスキップ) ので anchor は動かない。
        self.assertEqual(len(saved), 1)
        self.assertEqual(
            [f.message_ids for f in saved[0]], [["m1", "m2"], ["m3", "m4"]],
        )
        self.assertIsNone(self._anchor(lifecycle))

    # test_partial_fold_records_closed_child_episode_with_digest は削除 (2026-07-28):
    # 部分エピソード記録 (open_episode_ref → 子 episode 化) は新計画から発火
    # しない休眠機構になった (arasuji_levels.md §12-5)。その休眠機構は束 6c
    # (2026-08-22、v3 §7) で `_record_partial_episode` ごと撤去されたので、
    # 「発火しない」ことを見張るテストも要らなくなった。

    def test_anchor_moved_outside_eviction_clears_holes(self):
        """退場経路以外で anchor が差し替わったら圧縮区間を捨てる。

        TTL 失効後の最小ロードで新しい起点が立ち、LLM 成功後の touch がそれを
        永続化する経路がある。古い圧縮区間を残すと、その範囲は提示コンテキストに出ない (anchor の
        外) のに head の Chronicle 枠からは除外され続け、**提示コンテキストにも head にも
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

        # 同じ anchor への再 touch では圧縮区間を消さない
        lifecycle.save_folded_ranges(PERSONA_ID, "std-model", [
            FoldedRange(message_ids=["z9"], chronicle_entry_ids=["e2"]),
        ])
        lifecycle.update_anchor_for_model(persona=self._persona(), model_key="std-model",
                                          anchor_id="z9")
        self.assertEqual(
            [f.message_ids for f in lifecycle.load_folded_ranges(PERSONA_ID, "std-model")],
            [["z9"]],
        )

    # test_middle_fold_keeps_anchor_and_records_hole は削除 (2026-07-28):
    # 「先頭が畳めず途中だけ畳まれる」形は、拒否権を持つ旧計画でしか発生しない。
    # 新計画は常に先頭からの連続畳み。途中の圧縮区間の記帳
    # (anchor を動かさず folds に残す) は test_same_range_is_not_folded_twice
    # (重なりスキップで先頭が残る形) が引き続き固定している。


# ---------------------------------------------------------------------------
# ⑤b 適用側の拒否権と計画の恒久デッドロック
#     (docs/issues/chronicle_eviction_applier_veto_deadlock.md)
# ---------------------------------------------------------------------------


class ApplierVetoDeadlockTest(unittest.TestCase):
    """「計画が知らない拒否権」で anchor が恒久に詰まる二つの顔の根治を固定する。

    顔その1: 全メッセージが Chronicle 除外の fold は、あらすじが永久に生まれ
    ない。「編纂待ち」の見送りで人質に取らず、disabled と同じ吸収限定の退場を
    許す。
    顔その2: あらすじを恒久に失った圧縮区間の記録は Metabolism 冒頭で捨てる。
    残すと提示 (生ログに fail-open) と二重記録判定の生死の読みが食い違い、
    その範囲を含む束ねが毎ラウンド丸ごと拒否される。

    ⚠ 「digest 無しの吸収退場が子 episode を作らない」ことを見ていたケースは
    束 6c (2026-08-22、autonomous_behavior_v3.md §7) で削除した。子 episode を
    刻む ``_record_partial_episode`` がエピソード記録行の退役ごと消えたので、
    作らない相手が存在しない。
    """

    #: 旧世代のメッセージに残っている帰属タグ (EpisodeUnitEvictionTest と同じ扱い)。
    LEGACY_EPISODE_REF = "episode:1"

    def setUp(self):
        self.session_factory, self._engine = _make_session_factory()
        self.addCleanup(self._engine.dispose)
        self.manager = SimpleNamespace(SessionLocal=self.session_factory)

    def _make_lifecycle(self):
        lifecycle = SessionLifecycle(SimpleNamespace(), self.manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: "ok"
        lifecycle.ensure_recall_embeddings = lambda p: None
        return lifecycle

    def _persona(self, messages=()):
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=_history_manager(messages),
        )

    def _run(self, lifecycle, messages, watermarks, *, window=None):
        window = window or _window(messages)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2000"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            lifecycle.run_metabolism(
                self._persona(messages), "b", window, watermarks, None,
                model_key="std-model",
            )

    def _anchor(self, lifecycle):
        entry = lifecycle.load_anchor_entry(PERSONA_ID, "std-model")
        return entry["anchor_id"] if entry else None

    def test_excluded_only_fold_is_absorbed_instead_of_vetoed(self):
        """顔その1: 先頭の「編纂対象ゼロ」fold は吸収され、anchor が進む。

        issue の実測 r0 の形: [e0(除外タグのみ), o0(open U未満), c0, c1, 保護]。
        旧実装は e0 を「あらすじ待ち」で見送り続け、計画が毎ラウンド同じ提案を
        して永久ループしていた。
        """
        msgs = [
            _msg("e0", 100, chars=2_000),                      # 除外タグのみの範囲 (単独で U)
            _msg("o0", 101, chars=500, episode_ref=self.LEGACY_EPISODE_REF),
            _msg("c0", 102, chars=1_000),
            _msg("c1", 103, chars=1_000),
            _msg("k0", 200, chars=1_000),                      # 残す量の側
            _msg("k1", 201, chars=1_000),
        ]
        lifecycle = self._make_lifecycle()
        # e0 の fold にはあらすじが付かない (編纂対象が無いから)
        def _attach(persona, folds):
            for i, fold in enumerate(folds):
                if "e0" not in fold.message_ids:
                    fold.chronicle_entry_ids = [f"entry-{i}"]
                    fold.chronicle_short_ids = [i + 1]
        lifecycle._attach_chronicle_refs = _attach
        # 編纂対象判定: e0 を含む fold だけが対象ゼロ
        lifecycle._fold_has_chronicle_material = (
            lambda p, f: "e0" not in f.message_ids
        )
        saved = []
        lifecycle.save_folded_ranges = lambda pid, mk, folds: saved.append(folds)

        self._run(lifecycle, msgs, Watermarks(low=0, target=2_000, high=5_000))

        # 新計画は [e0] (U 到達で単独 fold) と [o0,c0,c1] を畳む。e0 の fold は
        # あらすじゼロだが吸収限定で退場し (旧実装: 見送りで永久停止)、後続の
        # fold も先頭から連続なので anchor が丸ごと飲み込んで k0 へ。
        self.assertEqual(self._anchor(lifecycle), "k0")
        # 飲み込まれた分は圧縮区間として残らない (head の Chronicle 枠が担当)。
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0], [])

    # test_absorbed_fold_without_digest_records_no_child_episode は削除
    # (2026-08-22、束 6c / v3 §7): 「digest 無しの吸収退場は子 episode を作らない」
    # という主張は、子 episode を作る `_record_partial_episode` が存在していた
    # 時代のもの。エピソードという専用の記録行を持たなくなり、書き手ごと消えた
    # ので、作られないことを見張る相手がいない。吸収退場そのものの挙動は
    # test_excluded_only_fold_is_absorbed_instead_of_vetoed が固定している。

    def test_fold_with_material_but_no_entry_is_still_vetoed(self):
        """顔その1の境界: 編纂対象を**含む**のにあらすじが無い fold は従来どおり
        見送る (LLM 失敗等の一時状態 — 下限 §2 の手続き強制は生きている)。"""
        msgs = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(6)]
        lifecycle = self._make_lifecycle()
        lifecycle._attach_chronicle_refs = lambda p, folds: None  # 引き当て失敗
        lifecycle._fold_has_chronicle_material = lambda p, f: True
        saved = []
        lifecycle.save_folded_ranges = lambda pid, mk, folds: saved.append(folds)
        self._run(lifecycle, msgs, Watermarks(low=2_000, target=1_000, high=5_000))
        self.assertIsNone(self._anchor(lifecycle))
        self.assertEqual(saved, [[]])

    def test_dead_fold_record_is_dropped_and_range_refolds(self):
        """顔その2: あらすじを恒久に失った記録は Metabolism 冒頭で捨てられ、
        その範囲は普通の材料として再畳みされる。

        issue の実測 r0 の形: 既存の圧縮区間 [m2,m3] の digest が引けない状態で、
        計画は [m0..m3] を束ねる。旧実装は重なり 1 件で束ねを丸ごと捨て、
        m0/m1 まで道連れで永久に畳めなかった。
        """
        from sea.session_window import FoldedRange
        msgs = [
            _msg("m0", 100, chars=500), _msg("m1", 101, chars=500),
            _msg("m2", 102, chars=500), _msg("m3", 103, chars=500),
            _msg("k0", 200, chars=1_000), _msg("k1", 201, chars=1_000),
        ]
        lifecycle = self._make_lifecycle()
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs
        # 実 anchor 行 + 実圧縮区間の記録 (digest は恒久に引けない)
        t0 = datetime.now().replace(microsecond=0)
        lifecycle.upsert_anchor_entry(PERSONA_ID, "std-model", {
            "anchor_id": "m0", "updated_at": t0.isoformat(),
        })
        lifecycle.save_folded_ranges(PERSONA_ID, "std-model", [
            FoldedRange(message_ids=["m2", "m3"], start_at=102, end_at=103,
                        chronicle_entry_ids=["dead-entry"]),
        ])
        lifecycle._resolve_fold_digest_status = lambda p, f: (
            (None, True) if "dead-entry" in f.chronicle_entry_ids
            else ("digest", False)
        )

        self._run(lifecycle, msgs, Watermarks(low=2_000, target=1_000, high=5_000))

        # [m0..m3] が一束で畳まれて anchor は保護範囲の先頭 k0 へ
        # (旧実装: 重なり拒否で m0 のまま永久停止)
        self.assertEqual(self._anchor(lifecycle), "k0")
        # 死んだ記録は消え、吸収されたので新しい圧縮区間も残らない
        self.assertEqual(lifecycle.load_folded_ranges(PERSONA_ID, "std-model"), [])

    def test_transient_digest_failure_keeps_the_record(self):
        """顔その2の境界: 照会の一時失敗 (恒久欠落でない) では記録を捨てない。
        捨てると、DB の瞬断のたびに編纂済み範囲が生ログへ戻って再編纂される。"""
        from sea.session_window import FoldedRange
        msgs = [
            _msg("m0", 100, chars=500), _msg("m1", 101, chars=500),
            _msg("m2", 102, chars=500), _msg("m3", 103, chars=500),
            _msg("k0", 200, chars=1_000), _msg("k1", 201, chars=1_000),
        ]
        lifecycle = self._make_lifecycle()
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs
        t0 = datetime.now().replace(microsecond=0)
        lifecycle.upsert_anchor_entry(PERSONA_ID, "std-model", {
            "anchor_id": "m0", "updated_at": t0.isoformat(),
        })
        lifecycle.save_folded_ranges(PERSONA_ID, "std-model", [
            FoldedRange(message_ids=["m2", "m3"], start_at=102, end_at=103,
                        chronicle_entry_ids=["e-alive"]),
        ])
        # 一時失敗: digest は引けないが恒久欠落とは判定されない
        lifecycle._resolve_fold_digest_status = lambda p, f: (None, False)

        self._run(lifecycle, msgs, Watermarks(low=2_000, target=1_000, high=5_000))

        # 記録は生きたまま (m2/m3 を含む束ねは従来どおり二重記録拒否で見送り)
        self.assertEqual(
            [f.message_ids
             for f in lifecycle.load_folded_ranges(PERSONA_ID, "std-model")],
            [["m2", "m3"]],
        )


# ---------------------------------------------------------------------------
# ⑤c 手動削除の道連れ (remove_folds_referencing_entry)
# ---------------------------------------------------------------------------


class RemoveFoldsReferencingEntryTest(unittest.TestCase):
    """あらすじエントリの手動削除は、それを指す圧縮区間の記録を道連れにする。"""

    def setUp(self):
        self.session_factory, self._engine = _make_session_factory()
        self.addCleanup(self._engine.dispose)
        self.manager = SimpleNamespace(SessionLocal=self.session_factory)
        self.lifecycle = SessionLifecycle(SimpleNamespace(), self.manager)
        t0 = datetime.now().replace(microsecond=0)
        for model in ("std-model", "light-model"):
            self.lifecycle.upsert_anchor_entry(PERSONA_ID, model, {
                "anchor_id": "a0", "updated_at": t0.isoformat(),
            })

    def test_removes_whole_records_across_model_rows(self):
        from sea.session_lifecycle import remove_folds_referencing_entry
        from sea.session_window import FoldedRange
        # 複数エントリを指す記録は、1 本の削除でも丸ごと外す (残りの digest が
        # 範囲全体の顔をして、消したエントリぶんの体験が黙って隠れるため)
        self.lifecycle.save_folded_ranges(PERSONA_ID, "std-model", [
            FoldedRange(message_ids=["m1"], chronicle_entry_ids=["e1", "e2"]),
            FoldedRange(message_ids=["m5"], chronicle_entry_ids=["e9"]),
        ])
        self.lifecycle.save_folded_ranges(PERSONA_ID, "light-model", [
            FoldedRange(message_ids=["x1"], chronicle_entry_ids=["e1"]),
        ])

        removed = remove_folds_referencing_entry(self.manager, PERSONA_ID, "e1")
        self.assertEqual(removed, 2)
        self.assertEqual(
            [f.message_ids
             for f in self.lifecycle.load_folded_ranges(PERSONA_ID, "std-model")],
            [["m5"]],
        )
        self.assertEqual(
            self.lifecycle.load_folded_ranges(PERSONA_ID, "light-model"), [],
        )

    def test_unrelated_entry_removes_nothing(self):
        from sea.session_lifecycle import remove_folds_referencing_entry
        from sea.session_window import FoldedRange
        self.lifecycle.save_folded_ranges(PERSONA_ID, "std-model", [
            FoldedRange(message_ids=["m1"], chronicle_entry_ids=["e1"]),
        ])
        self.assertEqual(
            remove_folds_referencing_entry(self.manager, PERSONA_ID, "zzz"), 0,
        )
        self.assertEqual(
            [f.message_ids
             for f in self.lifecycle.load_folded_ranges(PERSONA_ID, "std-model")],
            [["m1"]],
        )


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
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda p, m, model_key=None: dispatched.append(model_key)):
            lifecycle.run_metabolism(
                persona, "b", window,
                Watermarks(low=2_000, target=2_000, high=4_000), None,
                model_key="light-model",
            )
        self.assertEqual(dispatched, ["light-model"])

    def test_compile_groups_pass_through_fold_contiguity_check(self):
        """編纂へ渡す範囲は必ず compile_groups_from_folds を通る (§4-5 の検算)。

        「fold は提示コンテキストの連続区間」の検算は退場計画側の仕事 — 提示コンテキストの
        完全な並び (Chronicle 除外メッセージ込み) を持つのがあの層だけだから。
        ここで固定するのはその配線 (Codex 攻撃レビュー 三巡目 2026-07-27)。
        """
        session_factory, engine = _make_session_factory()
        self.addCleanup(engine.dispose)
        manager = SimpleNamespace(SessionLocal=session_factory)
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.ensure_recall_embeddings = lambda p: None
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs

        messages = [_msg(f"m{i}", 100 + i, chars=1_000) for i in range(5)]
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=_history_manager(messages),
        )
        captured = {}

        def _capture_groups(p, cb=None, **kwargs):
            captured["groups"] = kwargs.get("compile_groups")
            return "ok"

        lifecycle.generate_chronicle = _capture_groups

        checked = []
        from sea.eviction_plan import compile_groups_from_folds as _real

        def _spy(folds, presented):
            checked.append([[str(m.get("id")) for m in f.messages] for f in folds])
            checked.append([str(m.get("id")) for m in presented])
            return _real(folds, presented)

        window = _window(messages)
        with patch.dict(os.environ, {
            "SAIVERSE_SLUICE_ENABLED": "0",
            "SAIVERSE_CHRONICLE_BAND_BUDGET": "2500",
        }), patch("sea.session_lifecycle.compile_groups_from_folds", _spy), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda p, m, model_key=None: None):
            lifecycle.run_metabolism(
                persona, "b", window,
                Watermarks(low=2_000, target=2_000, high=4_000), None,
                model_key="light-model",
            )

        # 検算は fold 群と**提示コンテキストの全メッセージ**を見て行われた
        self.assertTrue(checked, "compile_groups_from_folds が呼ばれていない")
        self.assertEqual(checked[1], [m["id"] for m in messages])
        # 正しい計画なので割れず、そのまま編纂へ渡る
        self.assertEqual(captured["groups"], checked[0])


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


# ---------------------------------------------------------------------------
# ⑦ 失敗した抽出の拾い直しは「Metabolism の頭」— 編纂の有無に依存しない
#    (Sol レビュー 2026-08-06 F4)
# ---------------------------------------------------------------------------


class ExtractionBacklogRecoveryPointTest(unittest.TestCase):
    """付箋の回収点。

    抽出の失敗は「次の記憶の整理でやり直します」と画面が約束している。回収を
    編纂の計画・確認・claim の後ろに置くと、畳むものが無い夜が続いただけで
    その約束が果たされない (以前は generate_chronicle の中ほどにあり、
    「編纂対象なし → return」の向こう側だった)。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        persona_path = Path(self._tmp.name) / "personas" / PERSONA_ID
        persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_path, resource_id=PERSONA_ID,
        )
        self.addCleanup(self._close_adapter)

        self.session_factory, self._engine = _make_session_factory()

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        os.environ.pop("SAIMEMORY_MEMORY", None)
        try:
            self._engine.dispose()
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def _note_a_failed_extraction(self, entry_id="entry-1"):
        """メッセージと Chronicle entry を作り、その抽出失敗を付箋に貼る。"""
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.memory.entity_extractor import record_extraction_failure
        from sai_memory.memory.storage import add_message

        init_arasuji_tables(self.adapter.conn)
        m1 = add_message(self.adapter.conn, "t", "user", "こんにちは", created_at=1000)
        create_entry(
            self.adapter.conn, level=1, content="挨拶した",
            source_ids=[m1], source_count=1, message_count=1, entry_id=entry_id,
        )
        record_extraction_failure(self.adapter.conn, entry_id)
        return entry_id

    def _persona(self, messages=()):
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=self.adapter, history_manager=_history_manager(messages),
        )

    def test_recovered_even_when_there_is_nothing_to_compile(self):
        """⭐ 畳むものが無い回でも付箋は拾い直される。"""
        entry_id = self._note_a_failed_extraction()

        manager = SimpleNamespace(SessionLocal=self.session_factory)
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        # 編纂が走ったら分かるように失敗を仕込む (走らないことが期待)
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: "failed"
        lifecycle.ensure_recall_embeddings = lambda p: None

        seen = []
        with patch("saiverse.memory_weave_llm.resolve_memory_weave_config",
                   return_value=("mock-model", {"provider": "mock"}, "test")), \
                patch("saiverse.memory_weave_llm.build_memory_weave_client",
                      return_value=SimpleNamespace()), \
                patch("sai_memory.memory.entity_extractor.make_batch_callback",
                      return_value=lambda msgs, eid, **_: seen.append(eid)):
            # 提示コンテキストが空 = 畳むものが無い夜
            status = lifecycle.run_metabolism(
                self._persona(), "b", _window([]),
                Watermarks(low=2_000, target=2_000, high=4_000), None,
                model_key="std-model",
            )

        self.assertEqual(status, "nothing")
        self.assertEqual(seen, [entry_id], "静かな夜に付箋が拾い直されていない")
        left = self.adapter.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 0)

    def test_skipped_when_unattended_chronicle_is_not_allowed(self):
        """⭐ 「勝手に編纂するな」と言われている persona では拾い直しも走らない。

        拾い直しは確認ダイアログより手前にあるので、この設定が唯一の同意の関所。
        判定に Pulse の種別は使わない —— ``_current_pulse_type`` は Pulse の外
        (§14 の先回り畳みなど) で残留値になり、認可の根拠にならない。ここでは
        残留値として最も危険な "user" を置いて、それでも走らないことを見る。
        """
        self._note_a_failed_extraction()

        manager = SimpleNamespace(SessionLocal=self.session_factory)
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.is_autonomous_chronicle_enabled_for_persona = lambda p: False
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: "ok"
        lifecycle.ensure_recall_embeddings = lambda p: None

        persona = self._persona()
        persona._current_pulse_type = "user"  # Pulse 外に残った値のつもり

        built = []
        with patch("saiverse.memory_weave_llm.build_memory_weave_client",
                   side_effect=lambda *a, **k: built.append(a) or SimpleNamespace()):
            lifecycle.run_metabolism(
                persona, "b", _window([]),
                Watermarks(low=2_000, target=2_000, high=4_000), None,
                model_key="std-model",
            )

        self.assertEqual(built, [], "確認なしの編纂を断っている persona で課金している")
        left = self.adapter.conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()[0]
        self.assertEqual(left, 1, "付箋は残る (諦めない)")

    def test_no_llm_client_is_built_when_there_are_no_notes(self):
        """付箋が無い回は LLM クライアントを用意しない (毎回の無駄打ちを避ける)。"""
        manager = SimpleNamespace(SessionLocal=self.session_factory)
        lifecycle = SessionLifecycle(SimpleNamespace(), manager)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: "ok"
        lifecycle.ensure_recall_embeddings = lambda p: None

        built = []
        with patch("saiverse.memory_weave_llm.build_memory_weave_client",
                   side_effect=lambda *a, **k: built.append(a) or SimpleNamespace()):
            lifecycle.run_metabolism(
                self._persona(), "b", _window([]),
                Watermarks(low=2_000, target=2_000, high=4_000), None,
                model_key="std-model",
            )

        self.assertEqual(built, [])


if __name__ == "__main__":
    unittest.main()
