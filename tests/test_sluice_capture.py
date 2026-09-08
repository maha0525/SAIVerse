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
- API: 一覧 (skipped-spans / candidate-memos) と dry 見積もり

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
