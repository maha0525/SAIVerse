"""後から通す採取 (sluice capture) のユニットテスト。

docs/intent/sluice_coverage_gaps.md 第一段 B (判断の主体の再設計後):

- 見積もり (dry): 対象メッセージ件数と予測チャンク数が実行部と同じ刻みから出る
- 本人モード (mode='persona'): チャンク刻みが閾値
  (SAIVERSE_SLUICE_MAX_SPAN_CHARS) を守る / 前置きが毎チャンク同一 (本人の
  システムプロンプト + 読み返しの自己認識) / 機構名義の行は読ませない /
  パンマーカーは動かない / 拾われたメモは origin='readback' と event_date の
  機械刻印を持つ / 過程の判断ターン記録は discardable / 1 件以上採取して完走
  したら本線にダイジェスト一行 (session_digest / committed) が立つ (採取ゼロ
  なら立たない)
- 機構モード (mode='mechanism'、既定): 候補が sluice_candidate_memos に置かれ、
  本人の器 (コア記憶・手帳・約束・会話ログ) には何も書かれない
- 中断: チャンクの途中で失敗しても、処理済みぶんだけ記録が縮み、再実行が
  続きから進む
- 完了: 範囲を通し終えたら記録の行が消える
- 小休止 (C-1): レート制限の小休止中は走行を閉じる / RateLimitError で
  小休止が置かれる
- モデルに入る量 (docs/issues/sluice_skip_ignores_model_context.md 設計 3):
  コンテキスト長の小さいモデルで刻んだチャンクが呼び出し直前の比較 (答えの
  形の指定を含む) を通る / 状態が変わらない走行では見積もりと実行の刻み数が
  一致する / 構造化出力の都合でモデルが差し替わると、見積もりと実行の刻みが
  差し替わった後のモデルとクライアントの応答の上限に合う / 1 通だけで入らない
  とき API ジョブが input_too_large で失敗する
- API: 一覧 (skipped-spans / candidate-memos) と dry 見積もり (model と mode を使う)

LLM はモック。SAIMemory は temp DB (test_sluice と同じハーネスを再利用)。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sea import sluice
from test_sluice import (  # tests/ 直下は sys.path に載る (schema_scan と同じ)
    FakeLLMClient,
    FakeRuntime,
    ResponseLimitedFakeLLMClient,
    _AdapterTestBase,
    _sluice_result,
)


def _ts(i: int) -> str:
    """秒刻みの tz-aware ISO タイムスタンプ (正典順を安定させる)。"""
    return datetime(2026, 1, 1, 10, 0, i, tzinfo=timezone.utc).isoformat()


class _CaptureTestBase(_AdapterTestBase):
    """temp SAIMemory + 記録された範囲を持つテストの共通部品。"""

    def _persona(self):
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            persona_system_instruction="私はエア。テスト用のペルソナ。",
            sai_memory=self.adapter,
        )

    def _lifecycle(self, client, **extra):
        runtime = FakeRuntime(client)
        return SimpleNamespace(runtime=runtime, manager=None, **extra)

    def _append_conversation(self, count: int, chars: int = 10):
        """実会話 (user/assistant 交互) を count 通書き、id の列を返す。"""
        ids = []
        for i in range(count):
            mid = self.adapter.append_persona_message({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": ("う" if i % 2 == 0 else "あ") * chars,
                "timestamp": _ts(i),
            })
            self.assertIsNotNone(mid)
            ids.append(str(mid))
        return ids

    def _record_span(self, start_id: str, end_id: str) -> int:
        from sai_memory.memory.storage import record_sluice_skipped_span
        with self.adapter._db_lock:
            return record_sluice_skipped_span(self.adapter.conn, start_id, end_id)

    def _spans(self):
        from sai_memory.memory.storage import list_sluice_skipped_spans
        with self.adapter._db_lock:
            return list_sluice_skipped_spans(self.adapter.conn)

    def _history_of_call(self, call):
        """generate 1 回ぶんの messages から会話の写し部分 (先頭 system と
        末尾の注入プロンプトを除く) を返す。"""
        return call["messages"][1:-1]


#: コンテキスト長の小さいモデル (docs/issues/sluice_skip_ignores_model_context.md 設計 3)。
SMALL_CAPTURE_MODEL = "capture-small-model"


def _configure_small_model(test, lifecycle, mode, *, fit_tokens, dry=False):
    """会話の写しに使える量が約 ``fit_tokens`` になるコンテキスト長を、
    :data:`SMALL_CAPTURE_MODEL` に設定する。

    会話以外の部分 (前置き・指示文) の見積もりはコンテキスト長に依らないので、
    一度大きな値で測ってから、目標の量が残る長さに設定し直す。``dry`` は
    :func:`sea.sluice._capture_input_fit` にそのまま渡す (見積もりの側の量で合わせる
    — 接続を作らず、応答の枠は 4,096)。戻り値は上限 (トークン)。
    """
    import math

    from saiverse import model_configs

    patcher = patch.dict(model_configs.MODEL_CONFIGS, {
        SMALL_CAPTURE_MODEL: {
            "model": SMALL_CAPTURE_MODEL, "context_length": 1_000_000,
            "provider": "openai",
        },
    })
    patcher.start()
    test.addCleanup(patcher.stop)
    persona = test._persona()
    limit0 = sluice._input_token_budget(SMALL_CAPTURE_MODEL)
    fit0, _ = sluice._capture_input_fit(
        lifecycle, persona, mode=mode, model_key=SMALL_CAPTURE_MODEL, dry=dry,
    )
    non_conversation = limit0 - fit0
    model_configs.MODEL_CONFIGS[SMALL_CAPTURE_MODEL]["context_length"] = math.ceil(
        (fit_tokens + non_conversation + sluice._MAX_OUTPUT_TOKENS)
        / sluice._CONTEXT_USABLE_RATIO
    )
    fit, _ = sluice._capture_input_fit(
        lifecycle, persona, mode=mode, model_key=SMALL_CAPTURE_MODEL, dry=dry,
    )
    test.assertTrue(fit_tokens - 1 <= fit <= fit_tokens + 1, fit)
    return sluice._input_token_budget(SMALL_CAPTURE_MODEL)


class CapturePlanTest(_CaptureTestBase):
    """見積もり (dry) — 件数とチャンク数。"""

    def test_dry_plan_counts_messages_and_chunks(self):
        ids = self._append_conversation(6, chars=10)
        self._record_span(ids[0], ids[-1])
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": "25"}):
            plan = sluice.plan_sluice_capture(self._persona())
        self.assertEqual(plan["target_messages"], 6)
        # 10 字 × 6 通、上限 25 字 → 2 通ずつ 3 チャンク。
        self.assertEqual(plan["estimated_chunks"], 3)
        self.assertEqual(plan["max_span_chars"], 25)
        self.assertEqual(len(plan["spans"]), 1)
        self.assertEqual(plan["spans"][0]["message_count"], 6)
        self.assertEqual(plan["spans"][0]["estimated_chunks"], 3)
        self.assertTrue(plan["spans"][0]["readable"])

    def test_dry_plan_flags_unreadable_span(self):
        ids = self._append_conversation(2)
        self._record_span(ids[0], "missing-end-id")
        plan = sluice.plan_sluice_capture(self._persona())
        self.assertEqual(plan["unreadable_spans"], 1)
        self.assertFalse(plan["spans"][0]["readable"])


class SkippedSpanRecordTest(_CaptureTestBase):
    """飛ばした範囲の記録そのもの (sluice_skipped_spans) の書き込み規則。"""

    def test_recording_the_same_range_twice_does_not_add_a_row(self):
        """同じ範囲の二度目の記録は行を増やさず、既存行の id を返す。

        記録は退場の適用より先に確定するので、退場側 (anchor 前進) が落ちた回は
        記録だけが残り、次回の再試行が同じ範囲をもう一度持ってくる (2026-09-09
        Codex 指摘)。素の INSERT だと同じ会話が二行ぶん採取対象になる。
        """
        ids = self._append_conversation(4, chars=10)
        first = self._record_span(ids[0], ids[-1])
        second = self._record_span(ids[0], ids[-1])
        self.assertEqual(second, first)
        self.assertEqual(len(self._spans()), 1)

        # 範囲が違えば従来どおり別の行として積む。
        third = self._record_span(ids[1], ids[-1])
        self.assertNotEqual(third, first)
        self.assertEqual(len(self._spans()), 2)


class CaptureRunTest(_CaptureTestBase):
    """実行部 (本人モード) — チャンク刻み・前置き・進みの記録・完了。"""

    def test_capture_chunks_respect_threshold_and_clear_record(self):
        ids = self._append_conversation(6, chars=10)
        self._record_span(ids[0], ids[-1])
        # パンマーカーを置いておき、走行後に動いていないことを見る。
        sluice._save_pan_marker(self._persona(), "marker-before")

        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": "25"}):
            summary = sluice.run_sluice_capture(
                lifecycle, self._persona(), mode="persona",
            )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["chunks_processed"], 3)
        self.assertEqual(summary["messages_processed"], 6)
        self.assertEqual(summary["spans_remaining"], 0)
        self.assertEqual(self._spans(), [])  # 通し終えた記録は消える
        self.assertEqual(len(client.calls), 3)

        # 各チャンクの会話の写しは閾値 (25 字) 以下。
        for call in client.calls:
            history = self._history_of_call(call)
            self.assertLessEqual(
                sum(len(m["content"]) for m in history), 25,
            )
            # 前置きは毎チャンク同一: 本人のシステムプロンプト + 短い自己認識。
            preamble = call["messages"][0]
            self.assertEqual(preamble["role"], "system")
            self.assertIn("私はエア。テスト用のペルソナ。", preamble["content"])
            self.assertIn("過去の会話の読み返し", preamble["content"])
            # 注入プロンプト (末尾) は読み返しの対象範囲を明示する。
            self.assertIn("過去の会話", call["messages"][-1]["content"])
        preambles = {c["messages"][0]["content"] for c in client.calls}
        self.assertEqual(len(preambles), 1)  # 前置きの固定 (キャッシュの前提)

        # パンマーカーは動かない。
        self.assertEqual(
            sluice._load_pan_marker(self._persona()), "marker-before",
        )

    def test_capture_excludes_mechanism_records(self):
        ids = self._append_conversation(2, chars=10)
        # 範囲の中に機構名義の行 (<system> 通知) を挟む — 読ませない対象。
        self.adapter.append_persona_message({
            "role": "user",
            "content": "<system>入室しました</system>",
            "timestamp": _ts(1),
            "metadata": {"tags": ["internal", "event_message"]},
        })
        tail = self.adapter.append_persona_message({
            "role": "user", "content": "う" * 10, "timestamp": _ts(2),
        })
        self._record_span(ids[0], str(tail))

        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        summary = sluice.run_sluice_capture(
            lifecycle, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["messages_processed"], 3)
        all_history = [
            m["content"]
            for call in client.calls for m in self._history_of_call(call)
        ]
        self.assertFalse(any("入室しました" in c for c in all_history))

    def test_capture_resumes_from_shrunk_record_after_failure(self):
        ids = self._append_conversation(6, chars=10)
        self._record_span(ids[0], ids[-1])

        # 1 チャンク目は成功、2 チャンク目で LLM が落ちる。
        client = FakeLLMClient([_sluice_result(), RuntimeError("boom")])
        lifecycle = self._lifecycle(client)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": "25"}):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice_capture(
                    lifecycle, self._persona(), mode="persona",
                )

        spans = self._spans()
        self.assertEqual(len(spans), 1)
        # 処理済みチャンク (先頭 2 通) ぶんだけ記録が縮んでいる。
        self.assertEqual(spans[0]["start_message_id"], ids[2])
        self.assertEqual(spans[0]["end_message_id"], ids[-1])

        # 再実行は続き (3 通目) から。
        client2 = FakeLLMClient(_sluice_result())
        lifecycle2 = self._lifecycle(client2)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": "25"}):
            summary = sluice.run_sluice_capture(
                lifecycle2, self._persona(), mode="persona",
            )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["messages_processed"], 4)
        first_history = self._history_of_call(client2.calls[0])
        with self.adapter._db_lock:
            third_content = self.adapter.conn.execute(
                "SELECT content FROM messages WHERE id = ?", (ids[2],)
            ).fetchone()[0]
        self.assertEqual(first_history[0]["content"], third_content)
        self.assertEqual(self._spans(), [])

    def test_capture_oversize_single_message_is_own_chunk(self):
        # 1 通で閾値を超えるメッセージは、その 1 通だけのチャンクで進む
        # (刻めない単位を飛ばすと縮めが進まなくなる)。
        ids = self._append_conversation(2, chars=100)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": "25"}):
            summary = sluice.run_sluice_capture(
                lifecycle, self._persona(), mode="persona",
            )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["chunks_processed"], 2)
        for call in client.calls:
            self.assertEqual(len(self._history_of_call(call)), 1)

    def test_capture_noop_without_records(self):
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        summary = sluice.run_sluice_capture(lifecycle, self._persona())
        self.assertEqual(summary["status"], "noop")
        self.assertEqual(client.calls, [])

    def test_capture_disabled_by_env(self):
        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}):
            summary = sluice.run_sluice_capture(lifecycle, self._persona())
        self.assertEqual(summary["status"], "disabled")
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self._spans()), 1)  # 記録はそのまま

    def test_capture_unreadable_span_fails_closed(self):
        ids = self._append_conversation(2)
        self._record_span(ids[0], "missing-end-id")
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        with self.assertRaises(sluice.SluiceCaptureSpanUnreadableError):
            sluice.run_sluice_capture(lifecycle, self._persona())
        self.assertEqual(len(self._spans()), 1)  # 記録は消さない

    # -- C-1: レート制限の小休止 ------------------------------------------

    def test_capture_stops_during_rate_limit_cooldown(self):
        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(
            client, _metabolism_rate_limit_active=lambda pid: True,
        )
        summary = sluice.run_sluice_capture(lifecycle, self._persona())
        self.assertEqual(summary["status"], "cooldown")
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self._spans()), 1)

    def test_rate_limit_error_notes_cooldown_and_propagates(self):
        from llm_clients.exceptions import RateLimitError

        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        noted = []
        client = FakeLLMClient(RateLimitError("429"))
        lifecycle = self._lifecycle(
            client,
            _note_metabolism_rate_limit=lambda pid, exc: noted.append((pid, exc)),
        )
        with self.assertRaises(RateLimitError):
            sluice.run_sluice_capture(lifecycle, self._persona())
        self.assertEqual(len(noted), 1)
        self.assertEqual(noted[0][0], "tester")


class CaptureApplyTest(_CaptureTestBase):
    """適用が既存のスルースと同じ経路 (コア記憶・手帳・約束・判断ターン記録) で
    書かれることの検証。タスク帳は temp 中央 DB (test_sluice と同じ流儀)。"""

    def setUp(self):
        super().setUp()
        import gc
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import AI, Base

        self._tb_tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tb_tmp.name) / "central.db")
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)
        db = self.SessionLocal()
        try:
            db.add(AI(AIID="tester", HOME_CITYID=1, AINAME="tester"))
            db.commit()
        finally:
            db.close()

        def _cleanup_tb():
            self.engine.dispose()
            gc.collect()
            try:
                self._tb_tmp.cleanup()
            except (PermissionError, OSError):
                pass

        self.addCleanup(_cleanup_tb)

    def test_capture_applies_core_memo_promise_and_records(self):
        from datetime import datetime as _dt

        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])

        result = {
            **_sluice_result(reflection="読み返して思い出した"),
            "core_adds": [{"content": "2026年1月頃、まはーと星の話をした"}],
            "want_memos": [
                {"new_activity_name": "小説を書く", "text": "星の話を書きたい"},
            ],
            "promises": [{"op": "add", "content": "星の話の続きを送る"}],
        }
        client = FakeLLMClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(runtime=runtime, manager=self.manager)
        summary = sluice.run_sluice_capture(
            lifecycle, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["captures_applied"], 3)
        self.assertEqual(summary["captures_failed"], 0)

        # コア記憶 (既存の適用経路)。
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            cores = list_core_memories(self.adapter.conn)
        self.assertEqual(len(cores), 1)
        self.assertIn("星の話", cores[0].content)

        # 手帳メモ — span 刻印はチャンクの範囲 (定常のスルースと同じ機械刻印)。
        # event_date はチャンク末尾メッセージの日付の機械刻印、origin は
        # 'readback' (B-2 — 二つの時刻と由来)。
        memo = self.adapter.conn.execute(
            "SELECT text, span_start_id, span_end_id, idem_key, "
            "event_date, origin FROM memos"
        ).fetchone()
        self.assertIsNotNone(memo)
        self.assertEqual(memo[0], "星の話を書きたい")
        self.assertEqual(memo[1], ids[0])
        self.assertEqual(memo[2], ids[-1])
        self.assertTrue(memo[3].startswith(f"sluice:{ids[0]}..{ids[-1]}"))
        end_created = self.adapter.conn.execute(
            "SELECT created_at FROM messages WHERE id = ?", (ids[-1],)
        ).fetchone()[0]
        expected_event_date = _dt.fromtimestamp(int(end_created)).date().isoformat()
        self.assertEqual(memo[4], expected_event_date)
        self.assertEqual(memo[5], "readback")

        # 約束 (タスク帳)。
        from saiverse import task_book
        tasks = task_book.list_open(self.manager, "tester")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["content"], "星の話の続きを送る")
        self.assertEqual(tasks[0]["origin"], "sluice")

        # 判断ターン記録 — event_message 形式。読み返しの過程は本線の context に
        # 載せない (入口は一本) ので、採取ありでも discardable。
        row = self.adapter.conn.execute(
            "SELECT role, content, scope, line_role FROM messages "
            "WHERE metadata LIKE '%event_message%' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        role, content, scope, line_role = row
        self.assertEqual(role, "user")
        self.assertTrue(content.startswith("<system>"))
        self.assertIn("過去の会話の読み返し — スルースの採取判断", content)
        self.assertIn("エアの判断: 読み返して思い出した", content)
        self.assertEqual(scope, "discardable")
        self.assertEqual(line_role, "main_line")

        # ダイジェスト一行 — 本線 (main_line / committed) に session_digest
        # タグで一行だけ立つ (作業セッションのダイジェスト行と同じ器)。
        from sea.work_session import DIGEST_TAG
        digest_rows = self.adapter.conn.execute(
            "SELECT role, content, scope, line_role FROM messages "
            f"WHERE metadata LIKE '%{DIGEST_TAG}%'"
        ).fetchall()
        self.assertEqual(len(digest_rows), 1)
        d_role, d_content, d_scope, d_line_role = digest_rows[0]
        self.assertEqual(d_role, "user")
        self.assertTrue(d_content.startswith("<system>"))
        self.assertIn("過去の会話", d_content)
        self.assertIn("読み返し", d_content)
        self.assertIn("手帳のメモ 1 件", d_content)
        self.assertIn("コア記憶の操作 1 件", d_content)
        self.assertIn("約束の操作 1 件", d_content)
        self.assertEqual(d_scope, "committed")
        self.assertEqual(d_line_role, "main_line")

    # -- ダイジェストの材料の耐久化 (2026-09-09 Codex 指摘) ----------------
    #
    # 手帳・コア記憶への書き込みには本線の痕跡が必ず残る、という透明性の約束を
    # 守るため、ダイジェストの件数は memory.db に貯める。in-memory の累計だけで
    # 組んでいた頃は (i) 中断した走行の採取分がどの走行のダイジェストにも入らず、
    # (ii) 範囲の記録を縮めた後に本線への追記が落ちると、再実行が noop になって
    # ダイジェストが永久に立たなかった。

    def _memo_result(self, text):
        return {
            **_sluice_result(reflection="読み返して思い出した"),
            "want_memos": [{"new_activity_name": "小説を書く", "text": text}],
        }

    def _digest_rows(self):
        from sea.work_session import DIGEST_TAG
        return self.adapter.conn.execute(
            "SELECT content FROM messages "
            f"WHERE metadata LIKE '%{DIGEST_TAG}%'"
        ).fetchall()

    def test_capture_from_a_cancelled_run_still_gets_a_digest_later(self):
        """中断した走行で採取した分は、次に完走した走行のダイジェストに乗る。

        2 回目は範囲がもう残っていない ("noop") — 旧実装は完走 (ok) かつその
        走行内の採取が 1 件以上のときだけ立てていたので、この形ではダイジェストが
        永久に立たなかった。
        """
        from sea.cancellation import CancellationToken

        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        token = CancellationToken()

        class _CancelAfterFirstCall(FakeLLMClient):
            """1 チャンク目を返した直後に中止が押された状況を作る。"""

            def generate(inner, messages, tools=None, response_schema=None, *,
                         temperature=None, **kwargs):
                out = super().generate(
                    messages, tools, response_schema,
                    temperature=temperature, **kwargs,
                )
                token.cancel(interrupted_by="user")
                return out

        client = _CancelAfterFirstCall(self._memo_result("星の話を書きたい"))
        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client), manager=self.manager,
        )
        first = sluice.run_sluice_capture(
            lifecycle, self._persona(), mode="persona",
            cancellation_token=token,
        )
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(first["captures_applied"], 1)
        self.assertEqual(self._digest_rows(), [])  # 中断した回は立てない
        self.assertEqual(self._spans(), [])        # 範囲は通し終えている

        # 2 回目は通す範囲が無い (noop) が、貯まった 1 件でダイジェストが立つ。
        client2 = FakeLLMClient(_sluice_result())
        lifecycle2 = SimpleNamespace(
            runtime=FakeRuntime(client2), manager=self.manager,
        )
        second = sluice.run_sluice_capture(
            lifecycle2, self._persona(), mode="persona",
        )
        self.assertEqual(second["status"], "noop")
        self.assertEqual(second["captures_applied"], 0)
        self.assertEqual(client2.calls, [])  # LLM は呼ばない
        rows = self._digest_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("手帳のメモ 1 件", rows[0][0])

        # 3 回目は材料が消えているので、同じ一行を二度立てない。
        sluice.run_sluice_capture(
            lifecycle2, self._persona(), mode="persona",
        )
        self.assertEqual(len(self._digest_rows()), 1)

    def test_failed_digest_append_is_recovered_by_the_next_run(self):
        """本線への追記が落ちた走行も、再実行でダイジェストが立ち二重にならない。

        範囲の記録は追記より先に消えているので、旧実装では再実行が noop になり
        ダイジェストが永久に立たなかった。
        """
        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])

        client = FakeLLMClient(self._memo_result("星の話を書きたい"))
        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client), manager=self.manager,
        )
        with patch.object(
            sluice, "_append_capture_digest",
            side_effect=RuntimeError("main line write failed"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice_capture(
                    lifecycle, self._persona(), mode="persona",
                )
        self.assertEqual(self._spans(), [])   # 記録は縮め終えて消えている
        self.assertEqual(self._digest_rows(), [])

        client2 = FakeLLMClient(_sluice_result())
        lifecycle2 = SimpleNamespace(
            runtime=FakeRuntime(client2), manager=self.manager,
        )
        summary = sluice.run_sluice_capture(
            lifecycle2, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "noop")
        rows = self._digest_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("手帳のメモ 1 件", rows[0][0])

        sluice.run_sluice_capture(lifecycle2, self._persona(), mode="persona")
        self.assertEqual(len(self._digest_rows()), 1)

    def test_persona_zero_capture_writes_no_digest(self):
        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(_sluice_result())  # 採取なし (全欄空)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(runtime=runtime, manager=self.manager)
        summary = sluice.run_sluice_capture(
            lifecycle, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["captures_applied"], 0)
        from sea.work_session import DIGEST_TAG
        digest_rows = self.adapter.conn.execute(
            "SELECT id FROM messages "
            f"WHERE metadata LIKE '%{DIGEST_TAG}%'"
        ).fetchall()
        self.assertEqual(digest_rows, [])

    # -- 材料のマージと一行の追記の冪等 (2026-09-09 Codex 第二巡 修正 B/C) ---
    #
    # 材料のマージ (memory.db) と範囲の縮め (別 commit)、一行の追記と材料の
    # 消し込み (別 commit) は、それぞれ二段階の書き込み。間で落ちたときに件数が
    # 欠けたり、同じ読み返しが二行になったりしないことを固定する。

    def test_merge_is_idempotent_for_a_replayed_chunk(self):
        """同じチャンク (先頭 id が同じ) を二度マージしても件数が二重にならない。

        台帳に記録済み結果があるチャンクのやり直しは、LLM を呼ばずに適用だけ
        再実行する — 適用は冪等なので手帳のメモは増えないが、件数の足し込みまで
        冪等でないとダイジェストだけが実際の倍を言う。
        """
        persona = self._persona()
        kwargs = dict(
            messages=2, memos=1, core=0, promises=0,
            period_start="2026-01-01", period_end="2026-01-01",
        )
        sluice._merge_pending_digest(persona, chunk_start_id="chunk-a", **kwargs)
        sluice._merge_pending_digest(persona, chunk_start_id="chunk-a", **kwargs)
        pending = sluice._load_pending_digest(persona)
        self.assertEqual(pending["memos"], 1)
        self.assertEqual(pending["messages"], 2)
        self.assertEqual(pending["last_chunk_id"], "chunk-a")

        # 別のチャンクは普通に足される (冪等キーが効きすぎていない)。
        sluice._merge_pending_digest(persona, chunk_start_id="chunk-b", **kwargs)
        pending = sluice._load_pending_digest(persona)
        self.assertEqual(pending["memos"], 2)
        self.assertEqual(pending["last_chunk_id"], "chunk-b")

    def test_failed_shrink_after_the_merge_keeps_the_tally(self):
        """マージ後・縮め前に落ちた回でも件数は残り、再実行で二重にならない。

        マージが縮めより後だった頃は、この窓 (縮めは確定・マージが未了) で
        チャンクの適用分がダイジェストから永久に欠けた。順序を入れ替えたので、
        落ちた時点で件数は既に貯まっている。
        """
        import sai_memory.memory.storage as memory_storage

        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(self._memo_result("星の話を書きたい"))
        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client), manager=self.manager,
        )
        with patch.object(
            memory_storage, "delete_sluice_skipped_span",
            side_effect=RuntimeError("shrink failed"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice_capture(
                    lifecycle, self._persona(), mode="persona",
                )
        pending = sluice._load_pending_digest(self._persona())
        self.assertEqual(pending["memos"], 1)   # 縮めより先に貯まっている
        self.assertEqual(pending["last_chunk_id"], ids[0])
        self.assertEqual(len(self._spans()), 1)  # 縮めは落ちたので範囲は残る

        # 再実行は同じチャンクをやり直す — 件数は増えず、縮めだけが進む。
        client2 = FakeLLMClient(self._memo_result("星の話を書きたい"))
        lifecycle2 = SimpleNamespace(
            runtime=FakeRuntime(client2), manager=self.manager,
        )
        summary = sluice.run_sluice_capture(
            lifecycle2, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(self._spans(), [])
        rows = self._digest_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("手帳のメモ 1 件", rows[0][0])
        self.assertNotIn("手帳のメモ 2 件", rows[0][0])

    def test_failed_merge_leaves_the_span_for_the_next_run(self):
        """マージが落ちたら縮めも進まない — 再実行がマージからやり直す。

        逆順 (縮め → マージ) だと、この回でチャンクは台帳上完了済みになり、
        再実行は noop でダイジェストが立たなかった。
        """
        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(self._memo_result("星の話を書きたい"))
        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client), manager=self.manager,
        )
        with patch.object(
            sluice, "_merge_pending_digest",
            side_effect=RuntimeError("pending digest write failed"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice_capture(
                    lifecycle, self._persona(), mode="persona",
                )
        self.assertEqual(len(self._spans()), 1)  # 縮めていない
        self.assertEqual(self._digest_rows(), [])

        client2 = FakeLLMClient(self._memo_result("星の話を書きたい"))
        lifecycle2 = SimpleNamespace(
            runtime=FakeRuntime(client2), manager=self.manager,
        )
        summary = sluice.run_sluice_capture(
            lifecycle2, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "ok")
        rows = self._digest_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("手帳のメモ 1 件", rows[0][0])

    def test_failed_clear_does_not_duplicate_the_digest_line(self):
        """一行の追記は成功・材料の消し込みが落ちた回の再実行が、二行目を立てない。

        材料が残ったままなので次の完走が flush をやり直すが、追記の前に永続した
        nonce (flush_nonce) と同じ一行が本線にあるかを見て、追記だけを飛ばす。
        本人の目に同じ読み返しが二度あったように見えるのを止める。
        """
        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(self._memo_result("星の話を書きたい"))
        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client), manager=self.manager,
        )
        with patch.object(
            sluice, "_clear_pending_digest",
            side_effect=RuntimeError("clearing the tally failed"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice_capture(
                    lifecycle, self._persona(), mode="persona",
                )
        self.assertEqual(len(self._digest_rows()), 1)  # 追記は成功している
        pending = sluice._load_pending_digest(self._persona())
        self.assertEqual(pending["memos"], 1)          # 材料は残ったまま
        first_nonce = pending["flush_nonce"]
        self.assertTrue(first_nonce)

        client2 = FakeLLMClient(_sluice_result())
        lifecycle2 = SimpleNamespace(
            runtime=FakeRuntime(client2), manager=self.manager,
        )
        summary = sluice.run_sluice_capture(
            lifecycle2, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "noop")
        self.assertEqual(len(self._digest_rows()), 1)  # 二行目は立たない
        # 材料は消し込まれ、次の読み返しは新しい nonce から始まる。
        self.assertEqual(
            sluice._pending_digest_total(
                sluice._load_pending_digest(self._persona())
            ),
            0,
        )


    def test_silent_append_failure_keeps_the_pending_tally(self):
        """追記が None (adapter が失敗を飲んだ形) の回は送出し、材料を消さない。

        adapter.append_persona_message は行が入らなかったとき例外でなく None を
        返す — 検査しないと flush が消し込みまで進み、一行が立たないまま材料が
        消えて回収不能になる (2026-09-09 Codex 第三巡)。
        """
        ids = self._append_conversation(2)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(self._memo_result("星の話を書きたい"))
        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client), manager=self.manager,
        )
        with patch.object(
            type(self.adapter), "append_persona_message", return_value=None,
        ):
            with self.assertRaises(sluice.SluiceStorageUnavailableError):
                sluice.run_sluice_capture(
                    lifecycle, self._persona(), mode="persona",
                )
        self.assertEqual(len(self._digest_rows()), 0)  # 一行は立っていない
        pending = sluice._load_pending_digest(self._persona())
        self.assertEqual(pending["memos"], 1)          # 材料は残ったまま

        # 次の完走 (範囲ゼロ) が材料を拾って一行を立てる — 復旧経路。
        client2 = FakeLLMClient(_sluice_result())
        lifecycle2 = SimpleNamespace(
            runtime=FakeRuntime(client2), manager=self.manager,
        )
        summary = sluice.run_sluice_capture(
            lifecycle2, self._persona(), mode="persona",
        )
        self.assertEqual(summary["status"], "noop")
        self.assertEqual(len(self._digest_rows()), 1)


class MechanismModeTest(_CaptureTestBase):
    """機構モード (既定) — 候補テーブルに置くだけで、本人の器に書かない。"""

    def _candidates(self, status="open"):
        from sai_memory.memory.storage import list_sluice_candidate_memos
        with self.adapter._db_lock:
            return list_sluice_candidate_memos(
                self.adapter.conn, status=status,
            )

    def test_mechanism_writes_candidates_only(self):
        from datetime import datetime as _dt

        ids = self._append_conversation(4, chars=10)
        self._record_span(ids[0], ids[-1])
        before_message_count = self.adapter.conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

        result = {
            "want_memos": [
                {
                    "activity_name": "小説を書く",
                    "text": "星の話を書きたい",
                    "source_refs": ["msg:2"],
                },
            ],
            "did_memos": [],
        }
        client = FakeLLMClient(result)
        lifecycle = self._lifecycle(client)
        summary = sluice.run_sluice_capture(lifecycle, self._persona())

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["mode"], "mechanism")
        self.assertEqual(summary["captures_applied"], 1)
        self.assertEqual(self._spans(), [])  # 記録の縮め・完了は本人モードと同じ

        # 候補テーブルに置かれる。event_date は根拠 (msg:2 = 2 通目) の
        # メッセージ時刻からの機械刻印。
        candidates = self._candidates()
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(cand["kind"], "want")
        self.assertEqual(cand["activity_name"], "小説を書く")
        self.assertEqual(cand["text"], "星の話を書きたい")
        self.assertEqual(cand["status"], "open")
        self.assertEqual(cand["span_start_id"], ids[0])
        self.assertEqual(cand["span_end_id"], ids[-1])
        ref_created = self.adapter.conn.execute(
            "SELECT created_at FROM messages WHERE id = ?", (ids[1],)
        ).fetchone()[0]
        self.assertEqual(
            cand["event_date"],
            _dt.fromtimestamp(int(ref_created)).date().isoformat(),
        )

        # 本人の器には何も書かれない: 手帳・コア記憶・会話ログ (messages)。
        self.assertEqual(
            self.adapter.conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0],
            0,
        )
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            self.assertEqual(list_core_memories(self.adapter.conn), [])
        after_message_count = self.adapter.conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]
        self.assertEqual(after_message_count, before_message_count)

    def test_mechanism_prompt_is_mechanism_voice(self):
        ids = self._append_conversation(2, chars=10)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient({"want_memos": [], "did_memos": []})
        lifecycle = self._lifecycle(client)
        sluice.run_sluice_capture(lifecycle, self._persona())

        self.assertEqual(len(client.calls), 1)
        messages = client.calls[0]["messages"]
        # Chronicle 生成と同じ型: 単発の user プロンプトだけ。本人のシステム
        # プロンプトは着せない。
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        prompt = messages[0]["content"]
        self.assertNotIn("私はエア。テスト用のペルソナ。", prompt)
        self.assertIn("会話の写し", prompt)
        self.assertIn("[msg:1]", prompt)
        self.assertIn("候補", prompt)
        # 応答スキーマは候補の二欄だけ (コア記憶・約束の候補は出さない)。
        schema = client.calls[0]["response_schema"]
        self.assertEqual(
            set(schema["properties"].keys()), {"want_memos", "did_memos"},
        )

    def test_mechanism_dedupes_candidates_by_content(self):
        from sai_memory.memory.storage import add_sluice_candidate_memo

        ids = self._append_conversation(2, chars=10)
        self._record_span(ids[0], ids[-1])
        # 同じ範囲・同じ種類・同じ本文の候補を先に置いておく (再適用の模擬)。
        with self.adapter._db_lock:
            add_sluice_candidate_memo(
                self.adapter.conn,
                span_start_id=ids[0], span_end_id=ids[-1],
                kind="want", activity_name="小説を書く",
                text="星の話を書きたい", event_date=None,
            )
        result = {
            "want_memos": [
                {
                    "activity_name": "小説を書く",
                    "text": "星の話を書きたい",
                    "source_refs": [],
                },
            ],
            "did_memos": [],
        }
        client = FakeLLMClient(result)
        lifecycle = self._lifecycle(client)
        summary = sluice.run_sluice_capture(lifecycle, self._persona())
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(len(self._candidates()), 1)  # 二重に並ばない

    def test_unknown_mode_is_rejected(self):
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._lifecycle(client)
        with self.assertRaises(ValueError):
            sluice.run_sluice_capture(
                lifecycle, self._persona(), mode="third-subject",
            )


class CaptureModelFitTest(_CaptureTestBase):
    """刻みがモデルに入る量に合う (docs/issues/sluice_skip_ignores_model_context.md 設計 3)。

    100 字 × 12 通を、会話に使える量が約 410 トークンのモデルで刻む。本文の
    字数だけなら 4 通 (400) 入るが、写しの書式 (本人モードは 1 通 +4、機構モードは
    ``[msg:N] 日付 名前: `` と改行) を足すと 3 通までしか入らない — 4 チャンクに
    刻まれることが、書式の分を数えていることの検証になる。
    """

    def _run_mode(self, mode, client_result):
        ids = self._append_conversation(12, chars=100)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(client_result)
        lifecycle = self._lifecycle(client)
        limit = _configure_small_model(self, lifecycle, mode, fit_tokens=410)
        plan = sluice.plan_sluice_capture(
            self._persona(), mode=mode, model_key=SMALL_CAPTURE_MODEL,
            lifecycle=lifecycle,
        )
        summary = sluice.run_sluice_capture(
            lifecycle, self._persona(), mode=mode, model_key=SMALL_CAPTURE_MODEL,
        )
        return plan, summary, client, limit

    def _assert_chunks_fit(self, plan, summary, client, limit):
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["messages_processed"], 12)
        self.assertEqual(summary["chunks_processed"], 4)
        # 状態が変わらない走行では、見積もりと実行の刻み数が一致する。
        self.assertEqual(plan["estimated_chunks"], summary["chunks_processed"])
        self.assertEqual(plan["target_messages"], 12)
        # 返り値の刻みの大きさは、実際に使う大きさ (固定の 100,000 字ではない)。
        self.assertLessEqual(plan["max_span_chars"], 411)
        # どのチャンクも呼び出し直前の比較 (答えの形の指定を含む) を通って
        # LLM に届いている。
        self.assertEqual(len(client.calls), 4)
        for call in client.calls:
            self.assertIsNotNone(call["response_schema"])
            self.assertLessEqual(
                sluice._estimate_input_tokens(
                    call["messages"], SMALL_CAPTURE_MODEL,
                    response_schema=call["response_schema"],
                ),
                limit,
            )

    def test_persona_mode_chunks_pass_the_pre_call_check(self):
        plan, summary, client, limit = self._run_mode("persona", _sluice_result())
        self._assert_chunks_fit(plan, summary, client, limit)
        for call in client.calls:
            self.assertEqual(len(self._history_of_call(call)), 3)
            self.assertIs(call["response_schema"], sluice._RESPONSE_SCHEMA)

    def test_mechanism_mode_chunks_pass_the_pre_call_check(self):
        plan, summary, client, limit = self._run_mode(
            "mechanism", {"want_memos": [], "did_memos": []},
        )
        self._assert_chunks_fit(plan, summary, client, limit)
        for call in client.calls:
            self.assertIn("（3 通。", call["messages"][0]["content"])
            self.assertIs(call["response_schema"], sluice._CANDIDATE_SCHEMA)

    def _assert_pre_call_check_arguments(self, mode, result, schema):
        """呼び出し直前の比較に、答えの形の指定と、呼び出しに使うクライアントが渡る。"""
        ids = self._append_conversation(2, chars=10)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(result)
        lifecycle = self._lifecycle(client)
        with patch.object(
            sluice, "_ensure_input_fits", wraps=sluice._ensure_input_fits,
        ) as spy:
            summary = sluice.run_sluice_capture(lifecycle, self._persona(), mode=mode)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(spy.call_count, 1)
        self.assertIs(spy.call_args.kwargs["response_schema"], schema)
        self.assertIs(spy.call_args.kwargs["llm_client"], client)

    def test_persona_mode_pre_call_check_counts_the_schema_and_the_client(self):
        self._assert_pre_call_check_arguments(
            "persona", _sluice_result(), sluice._RESPONSE_SCHEMA,
        )

    def test_mechanism_mode_pre_call_check_counts_the_schema_and_the_client(self):
        self._assert_pre_call_check_arguments(
            "mechanism", {"want_memos": [], "did_memos": []},
            sluice._CANDIDATE_SCHEMA,
        )

    def test_job_fails_with_input_too_large_when_one_message_does_not_fit(self):
        """1 通だけで入らないメッセージは 1 通のチャンクになり、呼び出し直前の
        比較で止まる。ジョブは input_too_large で失敗し、LLM は呼ばれない。"""
        from api.routes.people.sluice import _create_job, _run_capture_job

        ids = []
        for i, chars in enumerate((2_000, 100, 100)):
            mid = self.adapter.append_persona_message({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "う" * chars,
                "timestamp": _ts(i),
            })
            ids.append(str(mid))
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(RuntimeError("must not be called"))
        lifecycle = self._lifecycle(client)
        _configure_small_model(self, lifecycle, "mechanism", fit_tokens=410)

        job_id = _create_job("tester")
        _run_capture_job(
            job_id, self._persona(), lifecycle, SMALL_CAPTURE_MODEL,
            mode="mechanism",
        )
        self._assert_job_failed_input_too_large(job_id, client, ids[0])

    def test_job_fails_with_input_too_large_when_the_instruction_alone_does_not_fit(self):
        """会話ではなく会話以外の部分 (前置きと指示文) だけで入る量を使い切る
        モデルでも、同じ失敗になる — 文面は「会話が長い」と言わない。"""
        from api.routes.people.sluice import _create_job, _run_capture_job

        ids = self._append_conversation(3, chars=100)
        self._record_span(ids[0], ids[-1])
        client = FakeLLMClient(RuntimeError("must not be called"))
        lifecycle = self._lifecycle(client)
        # 会話に使える量が負 = 指示文だけで上限を越えている。
        _configure_small_model(self, lifecycle, "persona", fit_tokens=-50)

        job_id = _create_job("tester")
        _run_capture_job(
            job_id, self._persona(), lifecycle, SMALL_CAPTURE_MODEL,
            mode="persona",
        )
        self._assert_job_failed_input_too_large(job_id, client, ids[0])

    def _assert_job_failed_input_too_large(self, job_id, client, span_start_id):
        from api.routes.people.sluice import _get_job

        job = _get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "input_too_large")
        self.assertEqual(
            job["error"],
            "選んだモデルでは、一度に送れる量に収まりませんでした。"
            "コンテキスト長の大きいモデルを選んで再実行してください。",
        )
        self.assertIn(SMALL_CAPTURE_MODEL, job["error_detail"])
        self.assertEqual(client.calls, [])
        # 記録は縮んでいない (次に大きいモデルで再実行できる)。
        spans = self._spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["start_message_id"], span_start_id)


class CaptureStructuredOutputSwitchTest(_CaptureTestBase):
    """構造化出力の都合でモデルが差し替わるとき、刻みは差し替わった後のモデルで
    見積もる (docs/issues/sluice_skip_ignores_model_context.md「レビューの裁定」
    第一巡の 4・第二巡の 1)。

    指定は大きいモデル (コンテキスト長 1,000,000) だが、runtime は構造化出力の
    ために小さいモデルへ差し替える。差し替わった後のクライアントは 4,306 の応答の
    上限を送る。小さいモデルで会話に使える量は、実行 (そのクライアントの応答の
    上限を引く) で約 410 トークン、接続を作らない見積もり (応答の枠 4,096) で
    約 620 トークンにしてある。100 字 × 12 通は、実行では 3 通ずつ 4 チャンク、
    見積もりでは 3 チャンク (本人モードは 5 通ずつ、機構モードは 4 通ずつ)、
    runtime の無い見積もり (差し替え前の大きいモデル) では 1 チャンクに刻まれる。
    """

    LARGE = "capture-large-model"
    CLIENT_RESPONSE_LIMIT = 4_306

    def setUp(self):
        super().setUp()
        from saiverse import model_configs

        patcher = patch.dict(model_configs.MODEL_CONFIGS, {
            self.LARGE: {
                "model": self.LARGE, "context_length": 1_000_000,
                "provider": "openai",
            },
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _switching_lifecycle(self, client):
        class _SwitchingRuntime(FakeRuntime):
            def __init__(inner, llm_client):
                super().__init__(llm_client)
                inner.selections = []
                inner.resolutions = []

            def select_llm_client(inner, node_def, persona, execution_context=None,
                                  needs_structured_output=False, state=None):
                inner.selections.append(
                    (execution_context.model_key, needs_structured_output),
                )
                if needs_structured_output:
                    return inner.client, SMALL_CAPTURE_MODEL
                return inner.client, execution_context.model_key

            def resolve_llm_model(inner, persona, execution_context=None,
                                  needs_structured_output=False, state=None):
                inner.resolutions.append(
                    (execution_context.model_key, needs_structured_output),
                )
                if needs_structured_output:
                    return SMALL_CAPTURE_MODEL
                return execution_context.model_key

        return SimpleNamespace(runtime=_SwitchingRuntime(client), manager=None)

    def _run_mode(self, mode, result):
        ids = self._append_conversation(12, chars=100)
        self._record_span(ids[0], ids[-1])
        client = ResponseLimitedFakeLLMClient(result, self.CLIENT_RESPONSE_LIMIT)
        lifecycle = self._switching_lifecycle(client)
        _configure_small_model(self, lifecycle, mode, fit_tokens=410)
        # 設定の手順が小さいモデルを直接指定して選んだ分は、記録から外す。
        lifecycle.runtime.selections.clear()
        lifecycle.runtime.resolutions.clear()
        limit = sluice._input_token_budget(SMALL_CAPTURE_MODEL, llm_client=client)
        self.assertEqual(
            limit,
            sluice._input_token_budget(SMALL_CAPTURE_MODEL)
            - (self.CLIENT_RESPONSE_LIMIT - sluice._MAX_OUTPUT_TOKENS),
        )
        dry_fit, _ = sluice._capture_input_fit(
            lifecycle, self._persona(), mode=mode, model_key=self.LARGE, dry=True,
        )
        run_fit, _ = sluice._capture_input_fit(
            lifecycle, self._persona(), mode=mode, model_key=self.LARGE,
        )
        # 見積もりは応答の枠を 4,096 で数えるので、クライアントの上限との差だけ広い。
        self.assertEqual(
            dry_fit - run_fit, self.CLIENT_RESPONSE_LIMIT - sluice._MAX_OUTPUT_TOKENS,
        )
        lifecycle.runtime.selections.clear()
        lifecycle.runtime.resolutions.clear()

        # runtime の無い見積もりは、差し替え前の大きいモデルで刻む (1 チャンク)。
        plan_without_runtime = sluice.plan_sluice_capture(
            self._persona(), mode=mode, model_key=self.LARGE,
        )
        self.assertEqual(plan_without_runtime["estimated_chunks"], 1)

        # runtime のある見積もり (API の dry と同じ) は、接続を作らずに差し替わった
        # 後のモデルを決め、応答の枠 4,096 で刻む (3 チャンク)。
        plan = sluice.plan_sluice_capture(
            self._persona(), mode=mode, model_key=self.LARGE, lifecycle=lifecycle,
        )
        self.assertEqual(plan["estimated_chunks"], 3)
        self.assertEqual(lifecycle.runtime.selections, [])
        self.assertEqual(lifecycle.runtime.resolutions, [(self.LARGE, True)])

        # 実行は、差し替わった後のモデルとそのクライアントの応答の上限で刻む
        # (4 チャンク — 見積もりより細かい)。
        summary = sluice.run_sluice_capture(
            lifecycle, self._persona(), mode=mode, model_key=self.LARGE,
        )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["chunks_processed"], 4)
        self.assertEqual(len(client.calls), 4)
        for call in client.calls:
            self.assertLessEqual(
                sluice._estimate_input_tokens(
                    call["messages"], SMALL_CAPTURE_MODEL,
                    response_schema=call["response_schema"],
                ),
                limit,
            )
        # 実行は、指定のモデルから構造化出力の選び方 (接続を作る口) を通っている。
        self.assertTrue(lifecycle.runtime.selections)
        self.assertEqual(
            set(lifecycle.runtime.selections), {(self.LARGE, True)},
        )

    def test_persona_mode_chunks_follow_the_switched_model(self):
        self._run_mode("persona", _sluice_result())

    def test_mechanism_mode_chunks_follow_the_switched_model(self):
        self._run_mode("mechanism", {"want_memos": [], "did_memos": []})


class CaptureDryDoesNotConnectTest(_CaptureTestBase):
    """見積もり (dry) は接続を作らない (docs/issues/sluice_skip_ignores_model_context.md
    「レビューの裁定」第二巡の 1)。本物の SEARuntime で確かめる。

    本人の標準モデル (コンテキスト長 1,000,000) は構造化出力に対応せず、runtime の
    規則で軽量モデル (:data:`SMALL_CAPTURE_MODEL`、会話に使える量は応答の枠 4,096 で
    約 410 トークン) へ差し替わる。見積もりは select_llm_client を呼ばず、接続を作る
    口 (ReplyModelBinding の client_for / client_for_model / structured_output_client /
    _create_private、factory の get_llm_client)、llama.cpp のサーバーの起動
    (_ensure_llama_server)、メディアの置き場のフォルダを作る resolve_media_uri に
    触れずに、軽量モデルのコンテキスト長で刻む (100 字 × 12 通 → 4 チャンク)。
    """

    LARGE = "capture-large-without-structured-output"

    def setUp(self):
        super().setUp()
        from saiverse import model_configs

        patcher = patch.dict(model_configs.MODEL_CONFIGS, {
            self.LARGE: {
                "model": self.LARGE, "context_length": 1_000_000,
                "provider": "openai", "supports_structured_output": False,
            },
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _persona(self):
        persona = super()._persona()
        persona.model = self.LARGE
        persona.lightweight_model = SMALL_CAPTURE_MODEL
        return persona

    def _forbid_connections(self, runtime):
        """接続・サーバー起動・フォルダ作成の口を、呼ばれたら失敗する Mock にする。"""
        from saiverse.persona_model_selection import ReplyModelBinding
        from sea.runtime import SEARuntime

        forbidden = AssertionError("the dry estimate must not connect")
        mocks = {}
        for owner, name in (
            (runtime, "select_llm_client"),
            (SEARuntime, "_ensure_llama_server"),
            (ReplyModelBinding, "client_for"),
            (ReplyModelBinding, "client_for_model"),
            (ReplyModelBinding, "structured_output_client"),
            (ReplyModelBinding, "_create_private"),
        ):
            patcher = patch.object(owner, name, side_effect=forbidden)
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        for target in (
            "llm_clients.get_llm_client",
            "llm_clients.factory.get_llm_client",
            "saiverse.media_utils.resolve_media_uri",
        ):
            patcher = patch(target, side_effect=forbidden)
            mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)
        return mocks

    def _plan_mode(self, mode):
        from sea.runtime import SEARuntime

        ids = self._append_conversation(12, chars=100)
        self._record_span(ids[0], ids[-1])
        runtime = SEARuntime(SimpleNamespace(building_histories={}))
        lifecycle = SimpleNamespace(runtime=runtime, manager=None)
        mocks = self._forbid_connections(runtime)
        _configure_small_model(self, lifecycle, mode, fit_tokens=410, dry=True)

        # runtime の無い見積もりは、差し替え前の大きいモデルで刻む。
        plan_without_runtime = sluice.plan_sluice_capture(self._persona(), mode=mode)
        self.assertEqual(plan_without_runtime["estimated_chunks"], 1)

        # runtime のある見積もりは、接続を作らずに軽量モデルへ差し替え、その
        # コンテキスト長で刻む。
        plan = sluice.plan_sluice_capture(
            self._persona(), mode=mode, lifecycle=lifecycle,
        )
        self.assertEqual(plan["target_messages"], 12)
        self.assertEqual(plan["estimated_chunks"], 4)
        self.assertLessEqual(plan["max_span_chars"], 411)
        for name, mock in mocks.items():
            with self.subTest(forbidden=name):
                mock.assert_not_called()

    def test_persona_mode_dry_plan_does_not_connect(self):
        self._plan_mode("persona")

    def test_mechanism_mode_dry_plan_does_not_connect(self):
        self._plan_mode("mechanism")


class CaptureApiTest(_CaptureTestBase):
    """API の読み口 (skipped-spans 一覧) と dry 見積もり。"""

    def _manager(self, lifecycle=None):
        return SimpleNamespace(
            personas={"tester": self._persona()},
            sea_runtime=SimpleNamespace(session_lifecycle=lifecycle),
        )

    def test_skipped_spans_listing_via_loaded_persona(self):
        from api.routes.people.sluice import list_sluice_skipped_spans_api

        ids = self._append_conversation(3)
        self._record_span(ids[0], ids[-1])
        out = list_sluice_skipped_spans_api("tester", manager=self._manager())
        self.assertEqual(out["total"], 1)
        span = out["spans"][0]
        self.assertEqual(span["start_message_id"], ids[0])
        self.assertEqual(span["end_message_id"], ids[-1])
        self.assertEqual(span["message_count"], 3)
        self.assertTrue(span["readable"])
        self.assertIn("created_at", span)

    def test_candidate_memos_listing(self):
        from sai_memory.memory.storage import add_sluice_candidate_memo

        from api.routes.people.sluice import list_sluice_candidate_memos_api

        ids = self._append_conversation(2)
        with self.adapter._db_lock:
            add_sluice_candidate_memo(
                self.adapter.conn,
                span_start_id=ids[0], span_end_id=ids[-1],
                kind="did", activity_name="散歩",
                text="川沿いを歩いた", event_date="2026-01-01",
            )
        out = list_sluice_candidate_memos_api("tester", manager=self._manager())
        self.assertEqual(out["total"], 1)
        cand = out["candidates"][0]
        self.assertEqual(cand["kind"], "did")
        self.assertEqual(cand["activity_name"], "散歩")
        self.assertEqual(cand["text"], "川沿いを歩いた")
        self.assertEqual(cand["event_date"], "2026-01-01")
        self.assertEqual(cand["status"], "open")

    def test_capture_dry_returns_estimate_without_job(self):
        from fastapi import BackgroundTasks

        from api.routes.people.sluice import (
            SluiceCaptureRequest,
            start_sluice_capture,
        )

        ids = self._append_conversation(4, chars=10)
        self._record_span(ids[0], ids[-1])
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_MAX_SPAN_CHARS": "25"}):
            out = asyncio.run(start_sluice_capture(
                "tester", SluiceCaptureRequest(dry=True), BackgroundTasks(),
                manager=self._manager(),
            ))
        self.assertEqual(out["target_messages"], 4)
        self.assertEqual(out["estimated_chunks"], 2)

    def test_capture_dry_uses_the_requested_model_and_mode(self):
        """dry の見積もりはリクエストの model と mode で刻む — 実行と同じ数を言う。"""
        from fastapi import BackgroundTasks

        from api.routes.people.sluice import (
            SluiceCaptureRequest,
            start_sluice_capture,
        )

        ids = self._append_conversation(12, chars=100)
        self._record_span(ids[0], ids[-1])
        lifecycle = self._lifecycle(FakeLLMClient(_sluice_result()))
        _configure_small_model(self, lifecycle, "persona", fit_tokens=410)
        manager = self._manager(lifecycle)

        # 本人のモデル (claude-x、設定なし) は固定の字数の上限だけで刻む。
        out_default = asyncio.run(start_sluice_capture(
            "tester", SluiceCaptureRequest(dry=True), BackgroundTasks(),
            manager=manager,
        ))
        self.assertEqual(out_default["estimated_chunks"], 1)
        # 小さいモデルを選ぶと、そのモデルに入る量で刻む。
        out_small = asyncio.run(start_sluice_capture(
            "tester",
            SluiceCaptureRequest(dry=True, mode="persona", model=SMALL_CAPTURE_MODEL),
            BackgroundTasks(), manager=manager,
        ))
        self.assertEqual(out_small["estimated_chunks"], 4)
        self.assertLess(out_small["max_span_chars"], 100_000)

    def test_capture_unknown_persona_404(self):
        from fastapi import BackgroundTasks, HTTPException

        from api.routes.people.sluice import (
            SluiceCaptureRequest,
            start_sluice_capture,
        )

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(start_sluice_capture(
                "nobody", SluiceCaptureRequest(dry=True), BackgroundTasks(),
                manager=SimpleNamespace(personas={}, sea_runtime=None),
            ))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
