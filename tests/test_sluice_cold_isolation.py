"""v0.2 形の記憶 DB を持つペルソナの「冷たい復帰」の一気通貫テスト。

docs/intent/sluice_coverage_gaps.md 「検証の旅」の**隔離環境**の段を、恒久の
統合テストとして固定したもの。再現するのは稟乃さんの実機事故 (2026-09-07〜08、
intent の「出自」節) の形そのもの:

- v0.2 時代の記憶 DB を v0.3 系へ更新した直後のペルソナ
- Chronicle (あらすじ) は古い時点で途切れていて、それ以降は未編纂の生ログが
  巨大な塊として残っている
- スルースのパンマーカーは一度も置かれていない (= 窓全体が未採取の担当範囲)

実機で起きたことは三つ重なっていた: (1) 起動直後の読み戻しが未編纂の塊を
丸ごと窓へ開き (212 万字)、(2) 話しかけた時点の非常畳みでスルースがその窓を
一度に読もうとし、(3) OpenAI が返した 429 の本文 "Request too large …" が
コンテキスト超過と誤分類されて後退方式が空転した (夜通し 429 が 14,475 回、
返事は一度も生成されなかった)。

ここで確かめるのは、第一段の実装 (コミット 305ab748 / fa6eed8d) がこの形で
設計どおりに働くこと:

1. :class:`ColdMetabolismTest` — 話しかけて即座に返事が返る形。巨大な未提示
   履歴を持つ DB で非常畳みを回し、スルースの LLM 呼び出しが一発も飛ばず
   (skipped_cold)、退場は進み、飛ばした範囲が ``sluice_skipped_spans`` に
   残り、二回目の入口は畳むものが無くなって引き返すこと。
2. :class:`ColdRefillTest` — 読み戻しが巨大な塊を開かないこと (212 万字の
   回帰)。未編纂の生ログは後ろから残す量まで読んだら終わり。
3. :class:`RateLimitMisclassificationTest` — 429 の誤分類の回帰。レート制限は
   一発で走行を閉じ、ペルソナ単位の小休止が次の入口を止めること。
4. :class:`ColdCaptureTest` — 1 で記録された範囲を後から通す採取が、機構
   モードと本人モードの両方で閾値を守って完走すること。

LLM はモック (tests/test_sluice.py の :class:`FakeLLMClient` /
:class:`FakeRuntime` を再利用)。SAIMemory と中央 DB は temp ディレクトリの
実 DB で、本番の ``~/.saiverse`` には一切触らない。

編纂 (Chronicle 生成) の本体はこのファイルの検証対象ではないので、ペルソナの
``CHRONICLE_ENABLED`` を False にして「編纂なしで前進する設計」の側を通す —
巨大 DB での編纂そのものは tests/test_metabolism_two_layer.py の領分。既存の
あらすじエントリ (古い時点で途切れた Chronicle) は実物を作って置いてあるので、
最前線の導出と読み戻しの分岐は本物の照会を通る。
"""
from __future__ import annotations

import gc
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sea import sluice
from test_sluice import (  # tests/ 直下は sys.path に載る (schema_scan と同じ)
    DummyEmbedder,
    FakeLLMClient,
    FakeRuntime,
    _sluice_result,
)

PERSONA_ID = "tester"
MODEL = "cold-isolation-model"

#: 一通の長さ。本番の会話一往復ぶんの目安 (実機は 3,744 通 / 212 万字)。
MESSAGE_CHARS = 1_000
#: v0.2 形の履歴の通数。
TOTAL_MESSAGES = 240
#: あらすじが覆っている先頭の通数 (= Chronicle が止まっている位置)。
COMPILED_MESSAGES = 20

TARGET_CHARS = 40_000    # 残す量
HIGH_CHARS = 120_000     # 上限
BAND_BUDGET = 10_000     # あらすじ一枚ぶんの材料字数 (U)
MAX_SPAN_CHARS = 100_000  # 一発のスルースに入る量

#: Chronicle が止まっている時点 (実機の稟乃さんは 2026-03-30 で止まっていた)。
HISTORY_START = datetime(2026, 3, 30, 9, 0, 0, tzinfo=timezone.utc)


def _ts(i: int) -> str:
    """i 通目のタイムスタンプ (1 時間刻み — 240 通で 10 日ぶんの履歴になる)。"""
    return (HISTORY_START + timedelta(hours=i)).isoformat()


class _ColdWorldBase(unittest.TestCase):
    """v0.2 形の記憶 DB (巨大な未編纂履歴 + 古いあらすじ + マーカー無し) を持つ
    ペルソナを temp DB に組み立てる共通 setup。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / PERSONA_ID
        self.persona_path.mkdir(parents=True, exist_ok=True)

        env = patch.dict(os.environ, {
            "SAIMEMORY_MEMORY": "1",
            "SAIVERSE_CHRONICLE_BAND_BUDGET": str(BAND_BUDGET),
            "SAIVERSE_SLUICE_MAX_SPAN_CHARS": str(MAX_SPAN_CHARS),
        })
        env.start()
        self.addCleanup(env.stop)

        embedder = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        embedder.start()
        self.addCleanup(embedder.stop)

        # 水位はモデル定義から実際に解決させる (get_metabolism_watermarks を
        # 差し替えない — 発火判定そのものが検証対象なので)。
        from saiverse import model_configs
        models = patch.dict(model_configs.MODEL_CONFIGS, {
            MODEL: {
                "model": MODEL,
                "metabolism_target_chars": TARGET_CHARS,
                "metabolism_high_chars": HIGH_CHARS,
            },
        })
        models.start()
        self.addCleanup(models.stop)

        # 退場後の可視化 (dynamic_state) は本筋ではないので黙らせる。
        dyn = patch(
            "saiverse.dynamic_state.DynamicStateManager.on_metabolism",
            lambda *a, **k: None,
        )
        dyn.start()
        self.addCleanup(dyn.stop)

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=self.persona_path, resource_id=PERSONA_ID,
        )
        self.addCleanup(self._close_adapter)

        self._make_central_db()
        self.message_ids = self._seed_v02_history()
        self.persona = self._make_persona()

    # -- teardown -------------------------------------------------------

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def _cleanup_central(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._central_tmp.cleanup()
        except (PermissionError, OSError):
            pass

    # -- 世界の組み立て --------------------------------------------------

    def _make_central_db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from database.models import AI, Base
        from saiverse.execution_ledger import ExecutionLedger

        self._central_tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self._central_tmp.name) / "central.db")
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.ledger = ExecutionLedger(self.SessionLocal)
        self.manager = SimpleNamespace(
            SessionLocal=self.SessionLocal, execution_ledger=self.ledger,
        )
        db = self.SessionLocal()
        try:
            # 編纂そのものはこのファイルの検証対象ではない (モジュール docstring)。
            db.add(AI(
                AIID=PERSONA_ID, HOME_CITYID=1, AINAME="エア",
                CHRONICLE_ENABLED=False,
            ))
            db.commit()
        finally:
            db.close()
        self.addCleanup(self._cleanup_central)

    def _seed_v02_history(self):
        """巨大な生ログ + 先頭だけを覆う古いあらすじ (= 途切れた Chronicle)。"""
        ids = []
        for i in range(TOTAL_MESSAGES):
            mid = self.adapter.append_persona_message({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": ("う" if i % 2 == 0 else "あ") * MESSAGE_CHARS,
                "timestamp": _ts(i),
            })
            self.assertIsNotNone(mid)
            ids.append(str(mid))

        from sai_memory.arasuji.storage import create_entry
        covered = ids[:COMPILED_MESSAGES]
        times = [self._created_at(mid) for mid in covered]
        with self.adapter._db_lock:
            create_entry(
                self.adapter.conn, level=1,
                content="更新前の時代のあらすじ (ここで編纂が止まっている)",
                source_ids=covered,
                start_time=times[0], end_time=times[-1],
                source_count=len(covered), message_count=len(covered),
            )
        return ids

    def _make_persona(self):
        from persona.history_manager import HistoryManager

        history_manager = HistoryManager(
            PERSONA_ID, self.persona_path / "log.json", {},
            memory_adapter=self.adapter,
        )
        return SimpleNamespace(
            persona_id=PERSONA_ID, persona_name="エア", model=MODEL,
            persona_system_instruction="私はエア。テスト用のペルソナ。",
            sai_memory=self.adapter, history_manager=history_manager,
        )

    def _lifecycle(self, client, presented_ids="auto"):
        from sea.session_lifecycle import SessionLifecycle

        runtime = FakeRuntime(client, presented_ids=presented_ids)
        lifecycle = SessionLifecycle(runtime, self.manager)
        lifecycle.ensure_recall_embeddings = lambda p: None
        return lifecycle

    # -- 読み口 ----------------------------------------------------------

    def _created_at(self, message_id):
        with self.adapter._db_lock:
            return self.adapter.conn.execute(
                "SELECT created_at FROM messages WHERE id = ?", (message_id,)
            ).fetchone()[0]

    def _index_of(self, message_id):
        return self.message_ids.index(str(message_id))

    def _skipped_spans(self):
        from sai_memory.memory.storage import list_sluice_skipped_spans
        with self.adapter._db_lock:
            return list_sluice_skipped_spans(self.adapter.conn)

    def _anchor_of(self, lifecycle):
        entry = lifecycle.load_anchor_entry(PERSONA_ID, MODEL)
        return entry.get("anchor_id") if entry else None

    def _window_row_chars(self, lifecycle):
        from sea.eviction_plan import stored_message_chars
        window = lifecycle.get_presented_window(self.persona, MODEL)
        return stored_message_chars(window.presented)

    def _set_anchor(self, lifecycle, message_id):
        lifecycle.upsert_anchor_entry(PERSONA_ID, MODEL, {
            "anchor_id": message_id,
            # 「十分に過去」= 確実に冷えている (冷たい復帰の再現)。
            "updated_at": (datetime.now() - timedelta(days=3650)).isoformat(),
        })

    def _run_cold_metabolism(self, lifecycle):
        """話しかけた瞬間の非常畳みを一度回す (稟乃さんの実機の入口)。"""
        return lifecycle.maybe_run_emergency_precompaction(
            self.persona, "b", None, model_key=MODEL,
        )


class ColdMetabolismTest(_ColdWorldBase):
    """項目 1: 話しかけて即座に返事が返る形。

    稟乃さんの実機 (2026-09-08) では、この入口でスルースが 212 万字を一度に
    読もうとして夜通し 429 を撃ち続け、返事が一度も生成されなかった。第一段 A の
    「入らない量は走らせない」で、ここは LLM を一発も呼ばずに終わる。
    """

    def test_cold_emergency_precompaction_skips_sluice_and_evicts(self):
        client = FakeLLMClient(RuntimeError("no LLM call is expected here"))
        lifecycle = self._lifecycle(client)

        # 更新直後の状態: session_anchor 行はまだ無く、起点は編纂の最前線
        # (= あらすじが覆う最後の次) から導出される。
        anchor_id, resolution = lifecycle.resolve_metabolism_anchor(
            self.persona, model_key=MODEL, persist_advance=False,
        )
        self.assertEqual(resolution, "frontier")
        self.assertEqual(anchor_id, self.message_ids[COMPILED_MESSAGES])

        ret = self._run_cold_metabolism(lifecycle)

        # (a) スルースの LLM 呼び出しは一発も飛ばない。
        self.assertEqual(client.calls, [])
        self.assertEqual(ret, "ok")

        # (b) 退場は進む — 起点が未編纂の塊の中を大きく前進している。
        new_anchor = self._anchor_of(lifecycle)
        self.assertIsNotNone(new_anchor)
        evicted = self._index_of(new_anchor) - COMPILED_MESSAGES
        self.assertGreater(evicted, 100)

        # (c) 飛ばした範囲が記録される (後から通す仕組みが読む一次情報)。
        spans = self._skipped_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["start_message_id"], self.message_ids[COMPILED_MESSAGES])
        self.assertLess(self._index_of(spans[0]["end_message_id"]), self._index_of(new_anchor))

        # 畳んだ結果、窓は上限以下に収まっている = この後の応答が通る形。
        self.assertLessEqual(self._window_row_chars(lifecycle), HIGH_CHARS)

    def test_second_entry_converges_without_any_llm_call(self):
        """夜通しループの回帰: 入口を繰り返し叩いても LLM 呼び出しは増えない。

        実機では 1 周 21 秒の後退方式が朝まで回り続けた。ここでは畳みが一度で
        収束し、二度目以降の入口は畳むものが無いと判断して引き返す。
        """
        client = FakeLLMClient(RuntimeError("no LLM call is expected here"))
        lifecycle = self._lifecycle(client)

        first = self._run_cold_metabolism(lifecycle)
        self.assertEqual(first, "ok")
        anchor_after_first = self._anchor_of(lifecycle)

        for _ in range(3):
            self.assertEqual(self._run_cold_metabolism(lifecycle), "skip")

        self.assertEqual(client.calls, [])                 # LLM は 0 回のまま
        self.assertEqual(self._anchor_of(lifecycle), anchor_after_first)
        self.assertEqual(len(self._skipped_spans()), 1)    # 記録も増えない

    def test_unreadable_pan_marker_blocks_both_the_skip_and_the_eviction(self):
        """マーカーが読めない回は、飛ばしも通常のスルースも走らせず退場を止める。

        読み取り障害を「スルース未走行」へ丸めていた頃 (2026-09-09 Codex 指摘)、
        担当範囲が窓全体に広がり、既に採取済みの履歴まで
        ``sluice_skipped_spans`` に「通っていない範囲」として記録したうえで
        前進・退場を許していた。判定はマーカーの上に立つので、読めない回は
        判定そのものを見送って次回の maybe_run_metabolism に委ねる。
        """
        import sai_memory.memory.storage as memory_storage

        client = FakeLLMClient(RuntimeError("no LLM call is expected here"))
        lifecycle = self._lifecycle(client)
        real_get = memory_storage.get_embed_metadata_strict

        def _fail_on_marker_keys(conn, key):
            # 壊すのはマーカーの読み出しだけ (埋め込みの帳簿など他の KV は素通し)。
            # 例外は sqlite3 が実際に投げる形 (ロック競合) にする — 読みを
            # strict 版へ差し替える前は、この型が入口の except OperationalError
            # に飲まれて「マーカー不在」に化けていた (2026-09-09 Codex 第二巡)。
            if key in (sluice._PAN_MARKER_KEY, sluice._LEGACY_PAN_MARKER_KEY):
                raise sqlite3.OperationalError("database is locked")
            return real_get(conn, key)

        with patch(
            "sai_memory.memory.storage.get_embed_metadata_strict",
            side_effect=_fail_on_marker_keys,
        ):
            ret = self._run_cold_metabolism(lifecycle)

        self.assertEqual(ret, "failed")
        self.assertEqual(client.calls, [])          # スルースも走らせない
        self.assertEqual(self._skipped_spans(), [])  # 記録も作らない
        # 起点は最前線に立ったまま (退場も機構1 の前進も起きていない)。
        self.assertEqual(
            self._anchor_of(lifecycle), self.message_ids[COMPILED_MESSAGES],
        )

    def test_skipped_span_is_recorded_before_the_window_moves(self):
        """記録が書けない回は退場しない (fail-closed) — 記録なしで範囲を提示から
        出さない、が代替の制約 (intent 追加の決定 1)。"""
        client = FakeLLMClient(RuntimeError("no LLM call is expected here"))
        lifecycle = self._lifecycle(client)
        with patch.object(
            lifecycle, "_record_sluice_skipped_span",
            side_effect=RuntimeError("memory.db write failed"),
        ):
            ret = self._run_cold_metabolism(lifecycle)
        self.assertEqual(ret, "failed")
        self.assertEqual(self._skipped_spans(), [])
        # 起点は最前線に立ったまま (退場は適用されていない)。
        self.assertEqual(
            self._anchor_of(lifecycle), self.message_ids[COMPILED_MESSAGES],
        )


class ColdRefillTest(_ColdWorldBase):
    """項目 2: 読み戻しが巨大な塊を開かない (212 万字の回帰)。

    稟乃さんの実機では、起動直後の読み戻しが「あらすじの材料の最古の行から
    起点まで」を一息に読み、あらすじと起点の間に溜まった未編纂 (v0.2 からの
    更新直後は数千通) を丸ごと窓へ開いた。生の未編纂に「丸ごと開く単位」は
    無く、後ろ (新しい側) から残す量まで読んだら終わり
    (intent 追加の決定 2)。
    """

    def _thin_window_lifecycle(self):
        """窓が残す量を大きく下回った状態 (= 読み戻しの発火条件) を作る。"""
        client = FakeLLMClient(RuntimeError("refill must not call the LLM"))
        lifecycle = self._lifecycle(client)
        self._set_anchor(lifecycle, self.message_ids[-5])
        return lifecycle, client

    def test_refill_reads_the_uncompiled_tail_only_up_to_target(self):
        from sea.eviction_plan import Watermarks

        lifecycle, client = self._thin_window_lifecycle()
        plan = lifecycle._plan_window_refill(
            self.persona, MODEL, self.message_ids[-5],
            Watermarks(target=TARGET_CHARS, high=HIGH_CHARS), strict=True,
        )
        self.assertIsNotNone(plan)

        # 開いたのは未編纂の生ログの末尾だけ。古い側のあらすじ (先頭 20 通を
        # 覆う一枚) は開いていない — 残す量に届いた時点で終わっている。
        self.assertGreater(plan["opened_raw_tail"], 0)
        self.assertEqual(plan["opened_older"], 0)

        # 開いた量は残す量の近傍 (行き過ぎても一通ぶん) — 履歴の全量ではない。
        self.assertGreaterEqual(plan["final_chars"], TARGET_CHARS)
        self.assertLess(plan["final_chars"], TARGET_CHARS + 2 * MESSAGE_CHARS)
        self.assertLess(
            plan["final_chars"], TOTAL_MESSAGES * MESSAGE_CHARS // 4,
        )
        self.assertEqual(client.calls, [])  # 帳簿の付け替えだけ (LLM 無し)

    def test_refill_end_to_end_leaves_the_window_near_the_target(self):
        lifecycle, client = self._thin_window_lifecycle()
        self.assertEqual(
            lifecycle.maybe_run_window_refill(self.persona, "b", model_key=MODEL),
            "ok",
        )
        rows = self._window_row_chars(lifecycle)
        self.assertGreaterEqual(rows, TARGET_CHARS)
        self.assertLess(rows, TARGET_CHARS + 2 * MESSAGE_CHARS)
        self.assertLessEqual(rows, HIGH_CHARS)  # 開いた直後に上限を割らない
        self.assertEqual(client.calls, [])

    def test_refill_then_emergency_precompaction_still_needs_no_llm(self):
        """読み戻し → 話しかけ、の並びで会話が成立する (実機の並びの再現)。"""
        lifecycle, client = self._thin_window_lifecycle()
        lifecycle.maybe_run_window_refill(self.persona, "b", model_key=MODEL)
        # 読み戻しの結果が上限を超えていないので、非常畳みは発火しない。
        self.assertEqual(self._run_cold_metabolism(lifecycle), "skip")
        self.assertEqual(client.calls, [])


class RateLimitMisclassificationTest(_ColdWorldBase):
    """項目 3: 429 の誤分類の回帰。

    実機の近因は、OpenAI が 1 分あたりのトークン上限で返した 429 の本文
    "Request too large …" が、コンテキスト超過判定の文字列一覧に一致して
    「入りきらない」と誤分類されたこと。後退方式 (直近の 1〜2 通を外して再試行)
    が 212 万字に対して回り続け、夜通しで 429 が 14,475 回になった。
    """

    def _rate_limit_error(self):
        from llm_clients.exceptions import RateLimitError
        # 実機のログと同じ文面 (コンテキスト超過の語を含む 429)。
        return RateLimitError(
            "Request too large for gpt-5.1-instant in organization org-x on "
            "tokens per min (TPM): Limit 200000, Requested 1200000."
        )

    def _run_with_sluice_reaching_the_llm(self, lifecycle):
        """量の条件を外して (閾値を巨大に) スルースを実際に走らせる。"""
        with patch.dict(
            os.environ,
            {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": str(10_000_000)},
        ):
            return self._run_cold_metabolism(lifecycle)

    def test_rate_limit_closes_the_run_after_a_single_call(self):
        client = FakeLLMClient(self._rate_limit_error())
        lifecycle = self._lifecycle(client)

        ret = self._run_with_sluice_reaching_the_llm(lifecycle)

        # (a) 後退方式のループに入らない — 呼び出しは一巡 (1 回) で止まる。
        self.assertEqual(len(client.calls), 1)
        # (b) 走行は失敗として閉じる (退場は据え置き、記録も書かれない)。
        self.assertEqual(ret, "failed")
        self.assertEqual(self._skipped_spans(), [])
        self.assertEqual(
            self._anchor_of(lifecycle), self.message_ids[COMPILED_MESSAGES],
        )

    def test_cooldown_stops_the_next_entries(self):
        client = FakeLLMClient(self._rate_limit_error())
        lifecycle = self._lifecycle(client)
        self._run_with_sluice_reaching_the_llm(lifecycle)

        # (c) ペルソナ単位の小休止が置かれ、次の入口は仕事を始めない。
        self.assertTrue(lifecycle._metabolism_rate_limit_active(PERSONA_ID))
        self.assertEqual(self._run_cold_metabolism(lifecycle), "skip")
        self.assertEqual(len(client.calls), 1)  # 二度目の 429 は撃たない

        # 通常の Metabolism 入口も同じ小休止で止まる。
        with patch.object(lifecycle, "load_anchor_entry") as load:
            lifecycle.maybe_run_metabolism(self.persona, "b", model_key=MODEL)
        load.assert_not_called()

    def test_context_overflow_marker_no_longer_reclassifies_the_429(self):
        """文面ではなく例外の型で裁く (誤分類の根を塞いだことの固定)。

        同じ文面でも型が RateLimitError でなければ小休止は立たない。
        """
        client = FakeLLMClient(RuntimeError(
            "Request too large for gpt-5.1-instant on tokens per min (TPM)"
        ))
        lifecycle = self._lifecycle(client)
        ret = self._run_with_sluice_reaching_the_llm(lifecycle)
        self.assertEqual(ret, "failed")
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(lifecycle._metabolism_rate_limit_active(PERSONA_ID))


class ColdCaptureTest(_ColdWorldBase):
    """項目 4: 1 で記録された範囲を後から通す採取の完走。

    第一段 A が飛ばした範囲 (稟乃さんの形では数千通ぶん) を、ユーザーの明示の
    操作で通し直す。判断の主体は二本立て (intent B 節) — 機構が候補を拾う側と、
    現在の本人が読み返す側。ここで見るのは「巨大 DB の一気通貫」で、単体の
    細部は tests/test_sluice_capture.py が持つ。
    """

    #: 採取のチャンクの上限 (= 一発の呼び出しに入る量)。180 通ぶんの範囲が
    #: 複数チャンクに刻まれることを見たいので、小さめに取る。
    CAPTURE_MAX_CHARS = 20_000

    def _record_the_cold_span(self):
        """項目 1 と同じ経路で範囲を記録させ、その範囲を返す。"""
        lifecycle = self._lifecycle(
            FakeLLMClient(RuntimeError("no LLM call is expected here")),
        )
        self.assertEqual(self._run_cold_metabolism(lifecycle), "ok")
        spans = self._skipped_spans()
        self.assertEqual(len(spans), 1)
        return spans[0]

    def _span_message_count(self, span):
        return (
            self._index_of(span["end_message_id"])
            - self._index_of(span["start_message_id"]) + 1
        )

    def _history_of_call(self, call):
        """本人モードの 1 コールぶんの会話の写し (前置きと注入プロンプトを除く)。"""
        return call["messages"][1:-1]

    class _VaryingSluiceClient(FakeLLMClient):
        """チャンクごとに違う本文のメモを返すフェイク。

        手帳には内容ベースの重複防止 (同じ日・同じアクティビティ・同じ種類・
        同じ本文は書かない) があるので、全チャンクが同じ本文を返すと 9 チャンク
        走っても手帳の行は 1 件になる。チャンクごとの刻印を見たいテストは
        本文を変える。
        """

        def generate(self, messages, tools=None, response_schema=None, *,
                     temperature=None, **kwargs):
            nth = len(self.calls) + 1
            self.result = _sluice_result(
                reflection=f"読み返して思い出した ({nth})",
                want_memos=[{
                    "new_activity_name": "小説を書く",
                    "text": f"星の話を書きたい その{nth}",
                }],
            )
            return super().generate(
                messages, tools, response_schema,
                temperature=temperature, **kwargs,
            )

    def test_mechanism_mode_runs_the_whole_span_into_candidates_only(self):
        from sai_memory.memory.storage import list_sluice_candidate_memos

        span = self._record_the_cold_span()
        expected_messages = self._span_message_count(span)
        before_messages = self.adapter.conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

        client = FakeLLMClient({
            "want_memos": [{
                "activity_name": "小説を書く",
                "text": "星の話を書きたい",
                "source_refs": ["msg:1"],
            }],
            "did_memos": [],
        })
        lifecycle = self._lifecycle(client)
        with patch.dict(
            os.environ,
            {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": str(self.CAPTURE_MAX_CHARS)},
        ):
            summary = sluice.run_sluice_capture(
                lifecycle, self.persona, mode="mechanism",
            )

        # (a) チャンクが閾値を守って完走する。
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["messages_processed"], expected_messages)
        self.assertGreater(summary["chunks_processed"], 1)
        self.assertEqual(len(client.calls), summary["chunks_processed"])
        for call in client.calls:
            prompt = call["messages"][0]["content"]
            # 機構モードは単発の user プロンプト一枚 (Chronicle 生成と同じ型)。
            self.assertEqual(len(call["messages"]), 1)
            self.assertLess(len(prompt), self.CAPTURE_MAX_CHARS * 2)

        # (b) 書かれるのは候補テーブルだけ — 本人の器は無傷。
        with self.adapter._db_lock:
            candidates = list_sluice_candidate_memos(self.adapter.conn, status="open")
        self.assertEqual(len(candidates), summary["chunks_processed"])
        self.assertEqual(
            self.adapter.conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0], 0,
        )
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            self.assertEqual(list_core_memories(self.adapter.conn), [])
        self.assertEqual(
            self.adapter.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            before_messages,
        )

        # (d) 通し終えた範囲の記録は消える。
        self.assertEqual(self._skipped_spans(), [])
        self.assertEqual(summary["spans_remaining"], 0)

    def test_persona_mode_writes_memos_with_event_date_and_one_digest(self):
        span = self._record_the_cold_span()
        expected_messages = self._span_message_count(span)

        client = self._VaryingSluiceClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        with patch.dict(
            os.environ,
            {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": str(self.CAPTURE_MAX_CHARS)},
        ):
            summary = sluice.run_sluice_capture(
                lifecycle, self.persona, mode="persona",
            )

        # (a) チャンクが閾値を守って完走する。
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["messages_processed"], expected_messages)
        self.assertGreater(summary["chunks_processed"], 1)
        for call in client.calls:
            history = self._history_of_call(call)
            self.assertLessEqual(
                sum(len(m["content"]) for m in history), self.CAPTURE_MAX_CHARS,
            )
            preamble = call["messages"][0]
            self.assertEqual(preamble["role"], "system")
            self.assertIn("過去の会話の読み返し", preamble["content"])

        # (c) メモは origin='readback' と、採取元の会話の日 (機械刻印) を持つ。
        rows = self.adapter.conn.execute(
            "SELECT text, origin, event_date FROM memos ORDER BY rowid"
        ).fetchall()
        self.assertEqual(len(rows), summary["chunks_processed"])
        span_dates = {
            datetime.fromtimestamp(self._created_at(mid)).date().isoformat()
            for mid in self.message_ids[
                self._index_of(span["start_message_id"]):
                self._index_of(span["end_message_id"]) + 1
            ]
        }
        event_dates = []
        for text, origin, event_date in rows:
            self.assertTrue(text.startswith("星の話を書きたい"))
            self.assertEqual(origin, "readback")
            # できごとの日は範囲の中の日で、走行日 (今日) ではない。
            self.assertIn(event_date, span_dates)
            self.assertNotEqual(event_date, datetime.now().date().isoformat())
            event_dates.append(event_date)
        # チャンクが進むほど、できごとの日は新しくなる (機械刻印が範囲の時刻を
        # 追っている = 過去由来の記録が時間軸の本来の場所に並ぶ)。
        self.assertEqual(event_dates, sorted(event_dates))
        self.assertGreater(len(set(event_dates)), 1)

        # (c) 本線に立つのはダイジェスト一行だけ (入口は一本)。
        from sea.work_session import DIGEST_TAG
        digests = self.adapter.conn.execute(
            "SELECT content, scope, line_role FROM messages "
            f"WHERE metadata LIKE '%{DIGEST_TAG}%'"
        ).fetchall()
        self.assertEqual(len(digests), 1)
        content, scope, line_role = digests[0]
        self.assertTrue(content.startswith("<system>"))
        self.assertIn("読み返し", content)
        self.assertEqual(scope, "committed")
        self.assertEqual(line_role, "main_line")

        # (d) 完了後に範囲の記録が消える。パンマーカーは動かない。
        self.assertEqual(self._skipped_spans(), [])
        self.assertIsNone(sluice._load_pan_marker(self.persona))

    def test_capture_resumes_from_where_it_stopped(self):
        """途中で落ちても、記録が縮んで続きから再開する (巨大な範囲の前提)。"""
        span = self._record_the_cold_span()
        expected_messages = self._span_message_count(span)

        client = FakeLLMClient([
            {"want_memos": [], "did_memos": []},
            RuntimeError("provider hiccup"),
        ])
        lifecycle = self._lifecycle(client)
        capture_env = {
            "SAIVERSE_SLUICE_MAX_SPAN_CHARS": str(self.CAPTURE_MAX_CHARS),
        }
        with patch.dict(os.environ, capture_env):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice_capture(lifecycle, self.persona, mode="mechanism")

        spans = self._skipped_spans()
        self.assertEqual(len(spans), 1)
        shrunk_start = self._index_of(spans[0]["start_message_id"])
        self.assertGreater(shrunk_start, self._index_of(span["start_message_id"]))

        client2 = FakeLLMClient({"want_memos": [], "did_memos": []})
        lifecycle2 = self._lifecycle(client2)
        with patch.dict(os.environ, capture_env):
            summary = sluice.run_sluice_capture(
                lifecycle2, self.persona, mode="mechanism",
            )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(
            summary["messages_processed"],
            expected_messages - (shrunk_start - self._index_of(span["start_message_id"])),
        )
        self.assertEqual(self._skipped_spans(), [])


class EmbedMetadataStrictReadTest(unittest.TestCase):
    """マーカーの読み口が「無い」と「読めない」を区別すること (第二巡 修正 A)。

    上の :meth:`ColdMetabolismTest.test_unreadable_pan_marker_blocks_both_the_skip_and_the_eviction`
    が守る fail-closed は、この読み口が例外を通してはじめて発火する。通常の
    ``get_embed_metadata`` はあらゆる :class:`sqlite3.OperationalError` を
    「テーブルがまだ無い旧 DB」とみなして None を返すので、ロック競合や I/O 障害が
    入口で「マーカー不在」に化けていた。
    """

    def test_locked_database_is_raised_not_swallowed(self):
        from sai_memory.memory.storage import get_embed_metadata_strict

        class _LockedConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            get_embed_metadata_strict(_LockedConn(), "any-key")

    def test_missing_table_is_still_absence(self):
        """旧 DB (embed_metadata テーブルが無い) は従来どおり None。"""
        from sai_memory.memory.storage import get_embed_metadata_strict

        conn = sqlite3.connect(":memory:")
        try:
            self.assertIsNone(get_embed_metadata_strict(conn, "any-key"))
        finally:
            conn.close()

    def test_existing_table_returns_the_value(self):
        from sai_memory.memory.storage import (
            get_embed_metadata_strict,
            set_embed_metadata,
        )

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE embed_metadata ("
                "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
            )
            set_embed_metadata(conn, "k", "v")
            self.assertEqual(get_embed_metadata_strict(conn, "k"), "v")
            self.assertIsNone(get_embed_metadata_strict(conn, "other"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
