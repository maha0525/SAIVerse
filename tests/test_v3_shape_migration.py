"""v0.3「形の層」への機械写し (autonomous_behavior_v3.md §9-8) の回帰。

守る不変条件:

- **写し元は無傷** — LIFE_PURPOSE 列・action_track 行・persona_task 行は
  一つも消えない / 変わらない (v3 §9-8 ①「削除はいつでもできる」)。
- **冪等** — 二周目は 1 件も増えない (マーカー / 名前ユニーク / idem_key の三段)。
- **写す条件** — 完了・中止した Track は写さない / 期限のないタスクは
  タスク帳へ入らない。
- **相手を発明しない** — 締め切りつきタスクの COUNTERPART は NULL
  (persona_task に相手の列が無いのに 'user' を書くのは捏造)。
- **期限はローカル時刻として写す** — 写し元も写し先もタイムゾーンを持たない
  ローカル解釈なので、UTC 解釈で写すと期限が時差の分だけずれる (指摘 4)。
- **部分失敗で二重にならない** — コア記憶も含めて全ての書き込みが冪等キーを
  持ち、マーカーが立たないまま転んだ後の再実行で増えない (指摘 2)。
- **読めなかったを「データなし」にしない** — 読み取り失敗ではマーカーを立てず、
  次回起動が再試行する (指摘 3)。

~/.saiverse には一切触れない (persona_dir を temp へ向ける)。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migrate import (
    _due_at_to_local_epoch,
    _migrate_deadline_tasks_to_task_book,
)
from database.models import (
    AI,
    ActionTrack,
    Base,
    City,
    PersonaTask,
    TaskBookEntry,
    User,
)
from saiverse.v3_shape_migration import (
    CORE_IDEM_KEY_PREFIX,
    MARKER_KEY,
    MigrationSourcesUnavailable,
    migrate_persona_to_v3_shape,
    parse_life_purpose,
)

#: このマシンが UTC ちょうどだと「UTC 解釈との差」を主張できない。
_LOCAL_OFFSET_IS_UTC = time.timezone == 0 and not time.daylight

PERSONA_ID = "tester"

LIFE_PURPOSE_JSON = (
    '{"purpose": "誰かの一日を少しよくする", '
    '"interests": ["絵を描く", "散歩"], "vocations": ["翻訳"]}'
)


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class ParseLifePurposeTest(unittest.TestCase):
    """退役した life_purpose.py の意味論をそのまま引き取れているか。"""

    def test_none_and_empty(self):
        self.assertIsNone(parse_life_purpose(None))
        self.assertIsNone(parse_life_purpose(""))

    def test_broken_json_is_unset(self):
        self.assertIsNone(parse_life_purpose("{not json"))

    def test_non_object_is_unset(self):
        self.assertIsNone(parse_life_purpose('["a"]'))

    def test_all_empty_is_unset(self):
        self.assertIsNone(
            parse_life_purpose('{"purpose": "  ", "interests": [], "vocations": []}')
        )

    def test_missing_keys_are_tolerated(self):
        parsed = parse_life_purpose('{"purpose": "live"}')
        self.assertEqual(
            parsed, {"purpose": "live", "interests": [], "vocations": []},
        )

    def test_blank_items_are_dropped(self):
        parsed = parse_life_purpose('{"purpose": "", "interests": ["a", " ", "b"]}')
        self.assertEqual(parsed["interests"], ["a", "b"])


class DeadlineTaskToTaskBookTest(unittest.TestCase):
    """締め切りつきタスク → タスク帳 (中央 DB 内で完結する機械写し)。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)
        _seed_world(self.Session)

        due = datetime.now() + timedelta(days=2)
        db = self.Session()
        try:
            db.add(PersonaTask(
                id="k-due", persona_id=PERSONA_ID, title="納品する",
                status="pending", due_at=due,
            ))
            db.add(PersonaTask(
                id="k-nodue", persona_id=PERSONA_ID, title="期限なし",
                status="pending",
            ))
            db.add(PersonaTask(
                id="k-done", persona_id=PERSONA_ID, title="終わった",
                status="completed", due_at=due,
            ))
            db.commit()
        finally:
            db.close()

    def _entries(self):
        db = self.Session()
        try:
            return db.query(TaskBookEntry).all()
        finally:
            db.close()

    def test_only_live_tasks_with_a_deadline_are_copied(self):
        _migrate_deadline_tasks_to_task_book(self.engine)
        rows = self._entries()
        self.assertEqual([r.CONTENT for r in rows], ["納品する"])

    # -- 2026-08-22 掃討フェーズ 束 6c 指摘 1a --------------------------

    def test_one_bad_row_does_not_block_the_whole_migration(self):
        """⭐ 汚れた 1 行があっても、正常な行は写る。

        全行を単一トランザクションに束ねると、1 行の制約違反で全件ロールバック +
        例外送出になり、再起動しても同じ行でまた落ちる = **移行が永久に完了
        しない**。行ごとの SAVEPOINT がそれを防ぐ。

        再現は「写し先の主キーを衝突させる」形で行う (実データの汚れが写し先の
        制約に触れる並びの代表)。
        """
        import uuid as _uuid
        from datetime import datetime as _dt

        db = self.Session()
        try:
            db.add(PersonaTask(
                id="k-second", persona_id=PERSONA_ID, title="二件目",
                status="pending", due_at=_dt(2026, 8, 25, 9, 0, 0),
            ))
            db.commit()
        finally:
            db.close()

        # 全行に同じ TASK_ID を割り当てさせ、2 行目以降を主キー衝突で落とす
        fixed = _uuid.uuid4()
        with unittest.mock.patch.object(_uuid, "uuid4", lambda: fixed):
            # 例外が外へ出ないこと自体が第一の検証
            _migrate_deadline_tasks_to_task_book(self.engine)

        rows = self._entries()
        self.assertEqual(
            len(rows), 1,
            "1 行目は確定し、衝突した行だけが飛ばされる (全件ロールバックしない)",
        )

    def test_due_at_becomes_an_epoch_integer(self):
        _migrate_deadline_tasks_to_task_book(self.engine)
        row = self._entries()[0]
        self.assertIsInstance(row.DUE_AT, int)
        self.assertGreater(row.DUE_AT, 0)

    def test_due_at_is_read_as_local_time(self):
        """写し元 (naive DateTime) はローカル時刻。epoch はその解釈で作る。

        書き込み側 (persona/tasks/store.py の _coerce_due_at) も読み手
        (sea/sluice.py の datetime.fromtimestamp) もローカル解釈で一貫している。
        """
        due = datetime(2026, 8, 24, 12, 0, 0)
        db = self.Session()
        try:
            db.add(PersonaTask(
                id="k-noon", persona_id=PERSONA_ID, title="正午の期限",
                status="pending", due_at=due,
            ))
            db.commit()
        finally:
            db.close()

        _migrate_deadline_tasks_to_task_book(self.engine)
        row = next(r for r in self._entries() if r.CONTENT == "正午の期限")
        self.assertEqual(row.DUE_AT, int(due.timestamp()))

    @unittest.skipIf(
        _LOCAL_OFFSET_IS_UTC,
        "ローカルが UTC ちょうどでは UTC 解釈との差が出ない",
    )
    def test_due_at_is_not_the_utc_interpretation(self):
        """指摘 4 の現物: SQLite の strftime('%s', ...) は UTC 解釈で時差分ずれる。

        非 UTC 環境 (Asia/Tokyo なら 9 時間) では、旧実装の値と新実装の値が
        きっちり時差の分だけ違う。
        """
        from datetime import timezone

        due = datetime(2026, 8, 24, 12, 0, 0)
        db = self.Session()
        try:
            db.add(PersonaTask(
                id="k-tz", persona_id=PERSONA_ID, title="時差の期限",
                status="pending", due_at=due,
            ))
            db.commit()
        finally:
            db.close()

        _migrate_deadline_tasks_to_task_book(self.engine)
        row = next(r for r in self._entries() if r.CONTENT == "時差の期限")

        utc_epoch = int(due.replace(tzinfo=timezone.utc).timestamp())
        self.assertNotEqual(row.DUE_AT, utc_epoch)
        # 旧実装 (SQLite 側の変換) がまさにその UTC 解釈だったことを固定する
        with self.engine.connect() as conn:
            legacy = conn.execute(text(
                "SELECT CAST(strftime('%s', due_at) AS INTEGER) "
                "FROM persona_task WHERE id = 'k-tz'"
            )).scalar()
        self.assertEqual(legacy, utc_epoch)
        self.assertEqual(row.DUE_AT, int(due.timestamp()))

    def test_an_unparsable_due_at_is_dropped_not_written_as_null(self):
        """期限が読めない行は写さない (NULL の DUE_AT は相手も期限も無い不正な行)。"""
        db = self.Session()
        try:
            db.add(PersonaTask(
                id="k-bad", persona_id=PERSONA_ID, title="こわれた期限",
                status="pending", due_at=datetime.now(),
            ))
            db.commit()
        finally:
            db.close()
        # SQLite は列型を強制しないので、壊れた文字列が野生の DB には実在しうる
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE persona_task SET due_at = 'not-a-date' WHERE id = 'k-bad'"
            ))

        _migrate_deadline_tasks_to_task_book(self.engine)
        self.assertNotIn("こわれた期限", [r.CONTENT for r in self._entries()])

    def test_counterpart_stays_null(self):
        """相手の列が無い写し元から 'user' を発明しない (三形② の形で載る)。"""
        _migrate_deadline_tasks_to_task_book(self.engine)
        row = self._entries()[0]
        self.assertIsNone(row.COUNTERPART)
        self.assertEqual(row.ORIGIN, "migration")
        self.assertEqual(row.STATUS, "open")
        self.assertEqual(row.REVISION, 0)
        self.assertEqual(row.ORIGIN_REF, "task:k-due")

    def test_is_idempotent(self):
        _migrate_deadline_tasks_to_task_book(self.engine)
        _migrate_deadline_tasks_to_task_book(self.engine)
        _migrate_deadline_tasks_to_task_book(self.engine)
        self.assertEqual(len(self._entries()), 1)

    def test_sources_are_untouched(self):
        _migrate_deadline_tasks_to_task_book(self.engine)
        db = self.Session()
        try:
            self.assertEqual(db.query(PersonaTask).count(), 3)
        finally:
            db.close()


class DueAtToLocalEpochTest(unittest.TestCase):
    """``due_at`` → epoch の変換そのもの (指摘 4)。"""

    def test_a_datetime_is_local(self):
        due = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(_due_at_to_local_epoch(due), int(due.timestamp()))

    def test_a_sqlite_datetime_string_is_local(self):
        """生 SQL 経由では SQLAlchemy の型変換を通らず文字列で来る。"""
        due = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(
            _due_at_to_local_epoch("2026-01-02 03:04:05.000000"),
            int(due.timestamp()),
        )

    def test_an_iso_t_separator_is_accepted(self):
        due = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(
            _due_at_to_local_epoch("2026-01-02T03:04:05"), int(due.timestamp()),
        )

    def test_unparsable_values_are_none(self):
        for bad in (None, "", "   ", "not-a-date", 12345, object()):
            with self.subTest(bad=bad):
                self.assertIsNone(_due_at_to_local_epoch(bad))


class PersonaSideMigrationTest(unittest.TestCase):
    """LIFE_PURPOSE / Track の関心 / desire 候補 → コア記憶と手帳。"""

    def setUp(self):
        from saiverse_memory import SAIMemoryAdapter

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        persona_dir = Path(self._tmp.name) / "personas" / PERSONA_ID
        persona_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)
        _seed_world(self.Session, life_purpose=LIFE_PURPOSE_JSON)

        db = self.Session()
        try:
            db.add(ActionTrack(
                track_id="t-live", persona_id=PERSONA_ID, title="Pixiv 用の絵",
                track_type="autonomous", status="running",
            ))
            db.add(ActionTrack(
                track_id="t-done", persona_id=PERSONA_ID, title="終わった関心",
                track_type="autonomous", status="completed",
            ))
            db.add(ActionTrack(
                track_id="t-abort", persona_id=PERSONA_ID, title="やめた関心",
                track_type="autonomous", status="aborted",
            ))
            db.add(PersonaTask(
                id="d-1", persona_id=PERSONA_ID, title="小説を書く",
                goal="星を拾う話を書きたい", status="pending",
                desire_type="作る", desire_state="fresh",
            ))
            db.add(PersonaTask(
                id="d-done", persona_id=PERSONA_ID, title="もう終わった願い",
                status="completed", desire_type="作る",
            ))
            db.add(PersonaTask(
                id="plain", persona_id=PERSONA_ID, title="ただのタスク",
                status="pending",
            ))
            db.commit()
        finally:
            db.close()

        self.adapter = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_dir, resource_id=PERSONA_ID,
        )
        self.addCleanup(self.adapter.close)
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas={PERSONA_ID: SimpleNamespace(sai_memory=self.adapter)},
        )

    def _cleanup_temp(self):
        import gc

        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def _activity_names(self):
        from sai_memory.memory.pocketbook import list_activities

        return sorted(a.name for a in list_activities(self.adapter.conn))

    def _memos(self):
        from sai_memory.memory.pocketbook import list_activities, list_memos

        out = []
        for activity in list_activities(self.adapter.conn):
            for memo in list_memos(self.adapter.conn, activity.id):
                out.append((activity.name, memo.kind, memo.text))
        return sorted(out)

    def test_copies_purpose_interests_vocations_tracks_and_desires(self):
        from sai_memory.core_memory import list_core_memories

        counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertEqual(counts["core_memories"], 1)
        self.assertEqual(counts["memos"], 1)

        core = list_core_memories(self.adapter.conn)
        self.assertEqual(
            [c.content for c in core], ["誰かの一日を少しよくする"],
        )

        # interests + vocations + 生きた Track + desire 一件 = 5 本。
        # 完了・中止した Track は写らない。
        self.assertEqual(
            self._activity_names(),
            sorted(["絵を描く", "散歩", "翻訳", "Pixiv 用の絵", "小説を書く"]),
        )
        self.assertEqual(
            self._memos(), [("小説を書く", "want", "星を拾う話を書きたい")],
        )

    def test_all_copies_carry_the_migration_origin(self):
        from sai_memory.memory.pocketbook import list_activities

        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        origins = {a.origin for a in list_activities(self.adapter.conn)}
        self.assertEqual(origins, {"migration"})

    def test_is_idempotent(self):
        first = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        second = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertNotEqual(first, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertEqual(second, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertEqual(len(self._activity_names()), 5)
        self.assertEqual(len(self._memos()), 1)

    def test_marker_survives_and_blocks_a_second_copy(self):
        from sai_memory.memory.storage import get_embed_metadata

        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertTrue(get_embed_metadata(self.adapter.conn, MARKER_KEY))

    def test_memo_idem_key_blocks_a_duplicate_even_without_the_marker(self):
        """マーカーを消しても、メモは idem_key で二重にならない。"""
        from sai_memory.memory.storage import set_embed_metadata

        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        with self.adapter._db_lock:
            set_embed_metadata(self.adapter.conn, MARKER_KEY, "")
        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertEqual(len(self._memos()), 1)
        self.assertEqual(len(self._activity_names()), 5)

    def test_sources_are_untouched(self):
        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        db = self.Session()
        try:
            row = db.query(AI).filter_by(AIID=PERSONA_ID).first()
            self.assertEqual(row.LIFE_PURPOSE, LIFE_PURPOSE_JSON)
            self.assertEqual(db.query(ActionTrack).count(), 3)
            self.assertEqual(db.query(PersonaTask).count(), 3)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 指摘 2: 部分失敗の後の再実行で二重にならない
    # ------------------------------------------------------------------

    def _core_contents(self):
        from sai_memory.core_memory import list_core_memories

        return [c.content for c in list_core_memories(self.adapter.conn)]

    def _marker(self):
        from sai_memory.memory.storage import get_embed_metadata

        return get_embed_metadata(self.adapter.conn, MARKER_KEY)

    def test_a_marker_failure_does_not_duplicate_the_core_memory(self):
        """コア記憶を書いた後にマーカーが立たなくても、再実行で二重にならない。

        ``add_core_memory`` は内部で commit するので、後続が転んでもコア記憶だけ
        確定する。マーカーが唯一の歯止めだった頃はここで LIFE_PURPOSE.purpose が
        二重になった (指摘 2 の現物)。
        """
        with patch(
            "sai_memory.memory.storage.set_embed_metadata",
            side_effect=RuntimeError("marker write failed"),
        ):
            migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(self._core_contents(), ["誰かの一日を少しよくする"])
        self.assertFalse(self._marker())

        second = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(self._core_contents(), ["誰かの一日を少しよくする"])
        self.assertEqual(second["core_memories"], 0)
        self.assertEqual(len(self._memos()), 1)
        self.assertEqual(len(self._activity_names()), 5)
        self.assertTrue(self._marker())

    def test_a_memo_failure_rolls_back_and_writes_no_core_memory(self):
        """手帳側で転んだら何も残らない (巻き戻せる書き込みを先に済ませる順序)。"""
        with patch(
            "sai_memory.memory.pocketbook.add_memo",
            side_effect=RuntimeError("pocketbook is down"),
        ):
            counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(counts, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertEqual(self._core_contents(), [])
        self.assertEqual(self._activity_names(), [])
        self.assertFalse(self._marker())

        # 次回起動が丸ごとやり直して完走する
        second = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertEqual(second["core_memories"], 1)
        self.assertEqual(second["memos"], 1)
        self.assertEqual(len(self._activity_names()), 5)
        self.assertTrue(self._marker())

    def test_a_core_memory_failure_does_not_duplicate_the_pocketbook_writes(self):
        """コア記憶で転んだ後の再実行が、確定済みの手帳を二重にしない。

        手帳側は先に commit されているので巻き戻らない。二周目はアクティビティが
        名前で、メモが idem_key で get-or-create され、増えない。
        """
        with patch(
            "sai_memory.core_memory.add_core_memory",
            side_effect=RuntimeError("core memory write failed"),
        ):
            migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(self._core_contents(), [])
        self.assertEqual(len(self._activity_names()), 5)
        self.assertEqual(len(self._memos()), 1)
        self.assertFalse(self._marker())

        second = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(second["core_memories"], 1)
        self.assertEqual(self._core_contents(), ["誰かの一日を少しよくする"])
        self.assertEqual(len(self._activity_names()), 5)
        self.assertEqual(len(self._memos()), 1)
        self.assertTrue(self._marker())

    def test_the_core_memory_is_matched_by_key_not_by_content(self):
        """本人が書き換えた後にマーカーが失われても、二重に書かない。

        本文で照合していると、書き換えられた瞬間に「まだ無い」と判定して元の
        一文をもう一度刻んでしまう。
        """
        from sai_memory.core_memory import list_core_memories, update_core_memory
        from sai_memory.memory.storage import set_embed_metadata

        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        core = list_core_memories(self.adapter.conn)[0]
        with self.adapter._db_lock:
            update_core_memory(self.adapter.conn, core.id, "書き換えた在り方")
            set_embed_metadata(self.adapter.conn, MARKER_KEY, "")

        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(self._core_contents(), ["書き換えた在り方"])

    def test_the_core_memory_carries_the_migration_idem_key(self):
        from sai_memory.core_memory import find_core_memory_by_idem_key

        migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        found = find_core_memory_by_idem_key(
            self.adapter.conn, f"{CORE_IDEM_KEY_PREFIX}{PERSONA_ID}",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.content, "誰かの一日を少しよくする")

    # ------------------------------------------------------------------
    # 指摘 3: 「読めなかった」を「データなし」と誤認しない
    # ------------------------------------------------------------------

    def test_a_read_failure_leaves_the_marker_unset_and_retries_next_time(self):
        """DB ロックのような一過性の失敗で完了マーカーを立てない。

        立ててしまうと、以後その判定で短絡して旧データが**永久に**写されない。
        """
        def _broken_session():
            raise RuntimeError("database is locked")

        self.manager.SessionLocal = _broken_session
        counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(counts, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertFalse(self._marker())
        self.assertEqual(self._activity_names(), [])

        # 原因が解消した次の起動で写し切る
        self.manager.SessionLocal = self.Session
        second = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertEqual(second["core_memories"], 1)
        self.assertEqual(len(self._activity_names()), 5)
        self.assertTrue(self._marker())

    def test_a_query_failure_mid_read_is_not_an_empty_source(self):
        """クエリの途中で転んだ場合も「データなし」には落とさない。"""
        from saiverse import v3_shape_migration as mig

        with patch.object(
            mig, "_table_exists", side_effect=RuntimeError("disk I/O error"),
        ):
            counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(counts, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertFalse(self._marker())

    def test_a_missing_source_table_is_a_normal_empty(self):
        """テーブルごと無い (旧機構が撤去済み) は正常な「データなし」。"""
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE action_track"))
            conn.execute(text("DROP TABLE persona_task"))
            conn.execute(text("UPDATE ai SET LIFE_PURPOSE = NULL"))

        counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        self.assertEqual(counts, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertTrue(self._marker())

    def test_a_missing_legacy_column_is_a_normal_empty(self):
        """列が無い (途中バージョンからの移行) も正常な「データなし」。

        判別は例外の丸呑みではなく列存在の明示検査 — 丸呑みだと、ロックや破損の
        ような**直すべき**失敗まで「列が無いだけ」として飲み込んでしまう。
        """
        with self.engine.begin() as conn:
            conn.execute(text("ALTER TABLE ai DROP COLUMN LIFE_PURPOSE"))
            conn.execute(text("ALTER TABLE persona_task DROP COLUMN desire_type"))

        counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)

        # LIFE_PURPOSE と desire は読めないが、Track の関心はそのまま写る
        self.assertEqual(counts["core_memories"], 0)
        self.assertEqual(counts["memos"], 0)
        self.assertEqual(self._activity_names(), ["Pixiv 用の絵"])
        self.assertTrue(self._marker())

    def test_a_manager_without_a_central_db_is_a_read_failure(self):
        """中央 DB への口が無いのは「写すものが無い」ではなく「読めない」。"""
        from saiverse import v3_shape_migration as mig

        self.manager.SessionLocal = None
        with self.assertRaises(MigrationSourcesUnavailable):
            mig._read_sources(self.manager, PERSONA_ID)

        counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertEqual(counts, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertFalse(self._marker())

    def test_a_persona_without_legacy_data_is_a_no_op(self):
        db = self.Session()
        try:
            db.query(ActionTrack).delete()
            db.query(PersonaTask).delete()
            db.query(AI).filter_by(AIID=PERSONA_ID).update({AI.LIFE_PURPOSE: None})
            db.commit()
        finally:
            db.close()
        counts = migrate_persona_to_v3_shape(self.manager, PERSONA_ID)
        self.assertEqual(counts, {"core_memories": 0, "activities": 0, "memos": 0})
        self.assertEqual(self._activity_names(), [])


def _seed_world(Session, life_purpose=None):
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(
            AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="テスター",
            LIFE_PURPOSE=life_purpose,
        ))
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    unittest.main()
