"""W5/M8 回帰: Building→個人記憶の転記 cursor 後行確定と冪等性。

記憶監査 (docs/handoff/2026-07-12_memory_persona_boundary_audit.md 第6片) の
「必要な回帰」: 途中 message の append 失敗、mark 失敗、DB lock、プロセス
再起動で、欠落も重複もなく最終的に全 heard message が取り込まれること。
"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from database.building_messages import (
    insert_building_message,
    mark_ingested as real_mark_ingested,
)
from persona.history_manager import HistoryManager
from builtin_data.tools.get_building_messages import (
    auto_ingest_building_messages,
    get_building_messages,
    BUILDING_MSG_REF_KEY,
)


class FakeMemoryAdapter:
    """memory.db の代役: 内容で失敗を注入でき、provenance 照会に応じる。"""

    def __init__(self):
        self.appended = []
        self.fail_contents = set()   # 含まれる文字列 → None を返す (静かな失敗)
        self.raise_contents = set()  # 含まれる文字列 → raise (DB lock 型)
        self.lookup_fail_refs = set()  # 含まれる ref → 照会自体が例外
        self.lookup_calls = 0
        self._mid = 0

    def is_ready(self):
        return True

    def append_persona_message(self, message, **_kw):
        content = message.get("content", "")
        if any(s in content for s in self.raise_contents):
            raise RuntimeError("database is locked")
        if any(s in content for s in self.fail_contents):
            return None
        self._mid += 1
        self.appended.append(message)
        return str(self._mid)

    def find_message_by_building_ref(self, ref):
        self.lookup_calls += 1
        if ref in self.lookup_fail_refs:
            raise RuntimeError("provenance lookup db lock")
        for i, m in enumerate(self.appended):
            meta = m.get("metadata") or {}
            if isinstance(meta, dict) and meta.get(BUILDING_MSG_REF_KEY) == ref:
                return str(i + 1)
        return None

    def appended_count(self, needle: str) -> int:
        return sum(1 for m in self.appended if needle in m.get("content", ""))


class BuildingIngestM8Test(unittest.TestCase):
    BID = "room"
    LISTENER = "listener"

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autocommit=False, autoflush=False
        )
        self.addCleanup(self.engine.dispose)

        self.adapter = FakeMemoryAdapter()
        self.persona = self._build_persona()
        self.manager = SimpleNamespace(
            SessionLocal=self.SessionLocal,
            id_to_name_map={"speaker": "話し手"},
            personas={self.LISTENER: self.persona},
            all_personas={self.LISTENER: self.persona},
        )

    def _build_persona(self, pulse_cursors=None):
        hm = HistoryManager(
            persona_id=self.LISTENER,
            persona_log_path=Path("personas") / self.LISTENER / "log.json",
            building_memory_paths={},
            initial_persona_history=[],
            db_session_factory=self.SessionLocal,
            memory_adapter=self.adapter,
        )
        # 既定は「この部屋の記録はあるが、まだ 1 件も読んでいない」(= 0)。
        # 記録そのものが無い状態は別の意味 (= 現在の末尾から始める) を持つので、
        # 転記の振る舞いを見るテストの土台に混ぜない。
        cursors = {self.BID: 0} if pulse_cursors is None else dict(pulse_cursors)
        return SimpleNamespace(
            persona_id=self.LISTENER,
            current_building_id=self.BID,
            history_manager=hm,
            pulse_cursors=cursors,
            entry_markers={},
            buildings={},
            sai_memory=self.adapter,
            # tool 版 (get_building_messages) は persona 側の map を読む (原実装踏襲)
            id_to_name_map={"speaker": "話し手"},
        )

    def _insert(self, role, content, *, persona_id=None, heard=True):
        msg = {
            "role": role,
            "content": content,
            "persona_id": persona_id,
            "timestamp": "2026-07-21T12:00:00+00:00",
            "heard_by": [self.LISTENER] if heard else ["someone_else"],
        }
        saved = insert_building_message(self.SessionLocal, self.BID, msg)
        assert saved and saved.get("message_id")
        return saved

    def _ingested_by(self, message_id):
        import json as _json
        from database.models import BuildingMessage
        db = self.SessionLocal()
        try:
            row = db.query(BuildingMessage).filter_by(
                building_id=self.BID, message_id=message_id
            ).first()
            return _json.loads(row.ingested_by) if row and row.ingested_by else []
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 正常系
    # ------------------------------------------------------------------

    def test_missing_cursor_record_starts_from_the_startup_watermark(self):
        """読んだ位置の記録が無い部屋では、たまっている履歴を読み込まない。

        記録が無いのを 0 (= 1 件目から全部未読) と解釈すると、部屋の全履歴を
        一度に転記する。件数の上限は無く、そのまま有料の推論に載る。入室処理
        (_mark_entry) が初回訪問で下しているのと同じ判断に揃える。
        """
        self._insert("user", "むかしの会話1")
        m2 = self._insert("user", "むかしの会話2")
        # 起動時、この部屋には既に 2 件あった
        self.manager.startup_seq_watermark = {self.BID: int(m2["seq"])}
        persona = self._build_persona(pulse_cursors={})  # 記録が無い状態

        count = auto_ingest_building_messages(persona, self.manager)
        self.assertEqual(count, 0)
        self.assertEqual(self.adapter.appended, [])
        self.assertEqual(persona.pulse_cursors[self.BID], int(m2["seq"]))

        # 以後に届いた発言は普通に読む (記録が無いことを「もう読まない」にはしない)
        self._insert("user", "あたらしい会話")
        count = auto_ingest_building_messages(persona, self.manager)
        self.assertEqual(count, 1)
        self.assertEqual(self.adapter.appended_count("あたらしい会話"), 1)

    def test_messages_arriving_after_startup_are_not_swallowed(self):
        """記録が無いペルソナでも、起動後に届いた発言は読まれる。

        既読の境界を「最初に読みに来た時点の末尾」で決めると、起動から
        そのペルソナが最初に喋るまでの間にユーザーが送った発言まで既読になる。
        境界は誰も書き込めない時点 (起動時にひかえた末尾) で取る。
        """
        old = self._insert("user", "起動前からあった発言")
        self.manager.startup_seq_watermark = {self.BID: int(old["seq"])}
        # 起動後、このペルソナが最初に喋る前にユーザーが送った発言
        self._insert("user", "起動後に届いた発言")

        persona = self._build_persona(pulse_cursors={})  # 記録が無い状態
        count = auto_ingest_building_messages(persona, self.manager)

        self.assertEqual(count, 1)
        self.assertEqual(self.adapter.appended_count("起動後に届いた発言"), 1)
        self.assertEqual(self.adapter.appended_count("起動前からあった発言"), 0)

    def test_first_message_into_an_empty_room_is_not_swallowed(self):
        """起動時に空だった部屋へ届いた 1 通目は読まれる。

        水位を「行がある部屋」だけで作ると、空の部屋は水位に載らない。そこへ
        最初のメッセージが届いたとき、その場の末尾を境界にすると**そのメッセージ
        自身が境界**になり、誰にも読まれない。空の部屋の水位は 0。
        """
        # 起動時、この部屋は空 → 水位は 0
        self.manager.startup_seq_watermark = {self.BID: 0}
        self._insert("user", "空の部屋に届いた1通目")

        persona = self._build_persona(pulse_cursors={})  # 記録が無い状態
        count = auto_ingest_building_messages(persona, self.manager)

        self.assertEqual(count, 1)
        self.assertEqual(self.adapter.appended_count("空の部屋に届いた1通目"), 1)

    def test_room_created_after_startup_reads_from_the_beginning(self):
        """起動後に作られた部屋 (水位に載っていない) は、最初から読む。

        作られた時点で空なので「全部読む」= その部屋の会話だけ。ここで現在の
        末尾を数えると、数えた時点で届いていた分が読まれなくなる。
        """
        self.manager.startup_seq_watermark = {}  # この部屋は起動時に存在しない
        self._insert("user", "新しい部屋の1通目")

        persona = self._build_persona(pulse_cursors={})
        count = auto_ingest_building_messages(persona, self.manager)

        self.assertEqual(count, 1)
        self.assertEqual(self.adapter.appended_count("新しい部屋の1通目"), 1)

    def test_happy_path_ingests_all_and_advances_cursor(self):
        m1 = self._insert("user", "こんにちは")
        m2 = self._insert("assistant", "やあ、いい天気だね", persona_id="speaker")
        m3 = self._insert("host", "<b>世界イベント: 雨が降り始めた</b>")
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 3)
        self.assertEqual(
            self.persona.pulse_cursors[self.BID], int(m3["seq"])
        )
        # DB の ingested_by が永続化されている (manager 明示渡しの回帰 —
        # 旧実装は contextvar 経由で常に None になり永続化されなかった)
        for m in (m1, m2, m3):
            self.assertIn(self.LISTENER, self._ingested_by(m["message_id"]))
        # provenance キーが刻まれている
        self.assertEqual(self.adapter.appended_count("こんにちは"), 1)
        refs = [
            (m.get("metadata") or {}).get(BUILDING_MSG_REF_KEY)
            for m in self.adapter.appended
        ]
        self.assertTrue(all(refs), f"all entries carry provenance refs: {refs}")

    def test_rule_skips_consume_cursor(self):
        self._insert("user", "聞こえない話", heard=False)
        m2 = self._insert("assistant", "自分の発話", persona_id=self.LISTENER)
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 0)
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m2["seq"]))
        self.assertEqual(self.adapter.appended, [])

    # ------------------------------------------------------------------
    # append 失敗 (静かな None / DB lock 例外) — 停止と再試行
    # ------------------------------------------------------------------

    def test_append_silent_failure_stops_round_then_recovers(self):
        m1 = self._insert("user", "最初のメッセージ")
        self._insert("user", "二番目は保存に失敗する")
        m3 = self._insert("user", "三番目")
        self.adapter.fail_contents = {"二番目"}

        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 1)
        # cursor は失敗メッセージの手前 (連続消費の水位)
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m1["seq"]))
        self.assertEqual(self.adapter.appended_count("三番目"), 0)

        # 障害が解ければ次ラウンドで残り全部が取り込まれる — 欠落も重複もない
        self.adapter.fail_contents = set()
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 2)
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m3["seq"]))
        for needle in ("最初のメッセージ", "二番目", "三番目"):
            self.assertEqual(self.adapter.appended_count(needle), 1, needle)

    def test_append_exception_stops_round_then_recovers(self):
        m1 = self._insert("user", "先頭")
        self._insert("user", "ロックで落ちる")
        self.adapter.raise_contents = {"ロックで落ちる"}

        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 1)
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m1["seq"]))

        self.adapter.raise_contents = set()
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 1)
        self.assertEqual(self.adapter.appended_count("ロックで落ちる"), 1)

    # ------------------------------------------------------------------
    # mark 失敗 — append 済み limbo の provenance 修復 (重複なし)
    # ------------------------------------------------------------------

    def _run_with_mark_failure_for(self, message_id):
        """指定 message_id の DB マークだけ 1 回失敗させて 1 ラウンド実行する。"""
        calls = {"failed": False}

        def flaky_mark(session_factory, building_id, mid, persona_id):
            if mid == message_id and not calls["failed"]:
                calls["failed"] = True
                raise RuntimeError("mark write failed")
            return real_mark_ingested(session_factory, building_id, mid, persona_id)

        with patch(
            "database.building_messages.mark_ingested", side_effect=flaky_mark
        ):
            return auto_ingest_building_messages(self.persona, self.manager)

    def test_mark_failure_stops_round_then_provenance_repairs(self):
        m1 = self._insert("user", "一つ目")
        m2 = self._insert("user", "マークに失敗するメッセージ")
        m3 = self._insert("user", "三つ目")

        self._run_with_mark_failure_for(m2["message_id"])
        # append は成功したが mark で停止 — cursor は手前、appended は 1 回
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m1["seq"]))
        self.assertEqual(self.adapter.appended_count("マークに失敗する"), 1)
        self.assertNotIn(self.LISTENER, self._ingested_by(m2["message_id"]))

        # 次ラウンド: provenance 修復で append は再実行されず、マークだけ直る
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 2)  # m2 (修復) + m3
        self.assertEqual(self.adapter.appended_count("マークに失敗する"), 1)
        self.assertEqual(self.adapter.appended_count("三つ目"), 1)
        self.assertIn(self.LISTENER, self._ingested_by(m2["message_id"]))
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m3["seq"]))

    def test_restart_after_mark_failure_no_duplicate(self):
        m1 = self._insert("user", "再起動前の既読")
        m2 = self._insert("user", "再起動を跨ぐメッセージ")

        self._run_with_mark_failure_for(m2["message_id"])
        cursor_persisted = self.persona.pulse_cursors[self.BID]
        self.assertEqual(cursor_persisted, int(m1["seq"]))

        # プロセス再起動を模す: persona / HistoryManager を作り直し、cursor は
        # 永続化済みの値から復元 (memory.db = FakeMemoryAdapter は共有)
        self.persona = self._build_persona(
            pulse_cursors={self.BID: cursor_persisted}
        )
        self.manager.personas[self.LISTENER] = self.persona
        self.manager.all_personas[self.LISTENER] = self.persona

        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 1)
        self.assertEqual(self.adapter.appended_count("再起動を跨ぐ"), 1)
        self.assertIn(self.LISTENER, self._ingested_by(m2["message_id"]))
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m2["seq"]))

    def test_provenance_lookup_failure_stops_round_no_duplicate(self):
        """2026-07-21 Codex レビュー P2: 照会失敗を「見つからなかった」に倒す
        設計は、直後の append 自体は成功してしまうケースで重複保存を生む。
        照会失敗はラウンドを停止させ、cursor を進めず次ラウンドで再照会させる
        必要がある。"""
        m1 = self._insert("user", "一つ目")
        m2 = self._insert("user", "マークに失敗するメッセージ")
        m3 = self._insert("user", "三つ目")

        self._run_with_mark_failure_for(m2["message_id"])
        self.assertEqual(self.adapter.appended_count("マークに失敗する"), 1)
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m1["seq"]))

        # 次ラウンド: 宙に浮いた転記の provenance 照会自体が例外で失敗する
        ref = f"{self.BID}:{m2['message_id']}"
        self.adapter.lookup_fail_refs = {ref}
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 0)  # このラウンドは何も進まない (停止)
        # 照会失敗で即停止するため、append の再試行自体に到達しない
        # (「見つからなかった」に倒していれば append が実行され重複していた)
        self.assertEqual(self.adapter.appended_count("マークに失敗する"), 1)
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m1["seq"]))
        self.assertNotIn(self.LISTENER, self._ingested_by(m2["message_id"]))

        # 障害回復後: provenance 修復が正しく機能し、二重 append なし
        self.adapter.lookup_fail_refs = set()
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 2)  # m2 (修復) + m3
        self.assertEqual(self.adapter.appended_count("マークに失敗する"), 1)
        self.assertIn(self.LISTENER, self._ingested_by(m2["message_id"]))
        self.assertEqual(self.persona.pulse_cursors[self.BID], int(m3["seq"]))

    # ------------------------------------------------------------------
    # tool 版 (get_building_messages) — 共通核の配線と件数表示
    # ------------------------------------------------------------------

    def test_tool_wrapper_reports_counts(self):
        from tools.context import persona_context

        self._insert("user", "ツール経由のメッセージ")
        self._insert("assistant", "他ペルソナの発話", persona_id="speaker")
        with persona_context(
            self.LISTENER, Path("personas") / self.LISTENER, manager=self.manager
        ):
            summary = get_building_messages()
        self.assertIn("2件の新規メッセージを認識しました", summary)
        self.assertIn("話し手", summary)

    # ------------------------------------------------------------------
    # 帰属 (層0タグ) — origin_episode / line_role / scope の刻印
    # (docs/issues/user_messages_missing_episode_attribution.md、2026-08-09 裁定)
    # ------------------------------------------------------------------

    def _appended_by_needle(self, needle: str):
        matches = [m for m in self.adapter.appended if needle in m.get("content", "")]
        self.assertEqual(len(matches), 1, f"exactly one entry for {needle!r}")
        return matches[0]

    def test_transcription_stamps_line_role_and_scope(self):
        """エピソードが開いていなくても line_role / scope は常に明示される。"""
        self._insert("user", "帰属テストのユーザー発言")
        self._insert("assistant", "帰属テストの他ペルソナ発話", persona_id="speaker")
        self._insert("host", "<b>帰属テストの世界イベント</b>")
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 3)
        for needle in ("ユーザー発言", "他ペルソナ発話", "世界イベント"):
            entry = self._appended_by_needle(needle)
            self.assertEqual(entry.get("line_role"), "main_line", needle)
            self.assertEqual(entry.get("scope"), "committed", needle)
            # 開いている出来事が無ければ origin_episode は付けない
            self.assertNotIn("origin_episode", entry.get("metadata") or {}, needle)

    # test_transcription_stamps_open_episode は削除 (2026-08-22、束 6c /
    # autonomous_behavior_v3.md §7): 転記 entry へ「開いている出来事」を刻む
    # 帰属タグ (origin_episode) は、エピソードという専用の記録行の退役と一緒に
    # 刻印ごと消えた。刻まれないことは
    # test_transcription_stamps_line_role_and_scope が固定している。

    def test_inherited_origin_episode_is_dropped(self):
        """話し手側 metadata から deepcopy で継承された origin_episode は捨てる。

        episode:N の N はペルソナ内連番なので、受信側でそのまま残すと別の
        出来事を指す嘘の帰属になる。刻印自体が退役した今 (束 6c) も、継承値を
        落とす掃除は必要 — 転記は話し手の metadata を丸ごと deepcopy するため、
        黙って持ち込まれる。
        """
        msg = {
            "role": "assistant",
            "content": "話し手の出来事参照を運ぶ発話",
            "persona_id": "speaker",
            "timestamp": "2026-08-09T12:00:00+00:00",
            "heard_by": [self.LISTENER],
            "metadata": {"origin_episode": "episode:999"},
        }
        saved = insert_building_message(self.SessionLocal, self.BID, msg)
        assert saved and saved.get("message_id")
        count = auto_ingest_building_messages(self.persona, self.manager)
        self.assertEqual(count, 1)
        entry = self._appended_by_needle("話し手の出来事参照を運ぶ発話")
        self.assertNotIn("origin_episode", entry.get("metadata") or {})


if __name__ == "__main__":
    unittest.main()
