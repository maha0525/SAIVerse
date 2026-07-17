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
from sea.session_lifecycle import SessionLifecycle

PERSONA_ID = "tester"


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


class FakeArasujiGenerator:
    """generate_unprocessed の呼び出しを記録するだけの ArasujiGenerator 代替。

    クラス属性 ``calls`` / ``raise_error`` をテストが操作する。
    (arasuji_entries には書かないため、qualifying 判定は毎回同じ窓を見る —
    claim の dedup だけで二重実行が止まることを検証できる。)
    """

    calls: list = []
    raise_error: bool = False

    def __init__(self, client, conn, **kwargs):
        self.kwargs = kwargs

    def generate_unprocessed(self, messages, progress_callback=None,
                             cancel_check=None, batch_callback=None):
        FakeArasujiGenerator.calls.append(len(messages))
        if FakeArasujiGenerator.raise_error:
            raise RuntimeError("llm down")
        return (["lv1"], [])


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
        FakeArasujiGenerator.calls = []
        FakeArasujiGenerator.raise_error = False

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

    def _generate(self, lifecycle):
        with patch("saiverse.model_configs.find_model_config",
                   return_value=("mock-model", {"provider": "mock", "context_length": 1000})), \
                patch("llm_clients.factory.get_llm_client", return_value=SimpleNamespace()), \
                patch("sai_memory.arasuji.generator.ArasujiGenerator", FakeArasujiGenerator), \
                patch("sai_memory.memory.entity_extractor.make_batch_callback",
                      side_effect=RuntimeError("skip entity extraction")):
            return lifecycle.generate_chronicle(self._persona(), force=True)

    def test_same_window_second_begin_is_skipped(self):
        """① 同じ窓の二重実行は claim (created=False) でスキップされる。"""
        lifecycle = self._make_lifecycle()
        first = self._generate(lifecycle)
        self.assertEqual(first, "ok")
        self.assertEqual(len(FakeArasujiGenerator.calls), 1)

        # FakeArasujiGenerator は arasuji_entries に書かないため窓は同一のまま。
        # 台帳の dedup だけが二重編纂 (二重 LLM コスト) を止める。
        second = self._generate(lifecycle)
        self.assertEqual(second, "deferred")
        self.assertEqual(len(FakeArasujiGenerator.calls), 1)  # 生成は走らない

    def test_failed_generation_returns_failed_and_window_growth_retries(self):
        """② (編纂側) 生成失敗 → "failed"。同じ窓の再試行は deferred、
        窓が伸びる (新メッセージ) と新しい claim で自然再試行される。"""
        lifecycle = self._make_lifecycle()
        FakeArasujiGenerator.raise_error = True
        self.assertEqual(self._generate(lifecycle), "failed")
        self.assertEqual(len(FakeArasujiGenerator.calls), 1)

        # 同じ窓: failed 行が (kind, key) を占有 → deferred (生成は走らない)
        FakeArasujiGenerator.raise_error = False
        self.assertEqual(self._generate(lifecycle), "deferred")
        self.assertEqual(len(FakeArasujiGenerator.calls), 1)

        # 窓が伸びる → 窓末尾 ID が変わり新しい claim → 再試行成功
        self.adapter.append_persona_message({
            "role": "user", "content": "新しいメッセージ",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(self._generate(lifecycle), "ok")
        self.assertEqual(len(FakeArasujiGenerator.calls), 2)

    def test_degrades_without_ledger(self):
        """⑦ 台帳が無い環境では claim なしで従来どおり実行される。"""
        lifecycle = self._make_lifecycle(with_ledger=False)
        self.assertEqual(self._generate(lifecycle), "ok")
        # dedup が無いので毎回実行される (従来挙動)
        self.assertEqual(self._generate(lifecycle), "ok")
        self.assertEqual(len(FakeArasujiGenerator.calls), 2)


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
        lifecycle.generate_track_chronicle = lambda p: None
        lifecycle.ensure_recall_embeddings = lambda p: None
        return lifecycle

    def _persona(self):
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=SimpleNamespace(),
        )

    def _run(self, lifecycle, model_key=None):
        current_messages = [{"id": f"m{i}", "content": "x"} for i in range(5)]
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            # keep_count=2 → evict_count=3 → new anchor 候補 = m3
            lifecycle.run_metabolism(
                self._persona(), "b", current_messages, 2, None, model_key=model_key,
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

        persona = SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model="std-model",
            sai_memory=None, history_manager=SimpleNamespace(),
        )
        dispatched = []
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda p, m, model_key=None: dispatched.append(model_key)):
            lifecycle.run_metabolism(
                persona, "b",
                [{"id": f"m{i}", "content": "x"} for i in range(5)], 2, None,
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
