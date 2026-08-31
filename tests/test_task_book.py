"""タスク帳 (saiverse/task_book.py + database.models.TaskBookEntry) のテスト。

autonomous_behavior_v3.md §4.1-2 の設計の芯を検査する:

- CRUD (追加 / 更新 / 完了 / 取り下げ / 一覧)
- **期限なし (DUE_AT=NULL) の行は正当で、締め切り引き当て一覧には出ない**
- 状態遷移は open → done / withdrawn の一方通行 (閉じた行は再操作不可)
- 完遂の接地 (§9-5): 相手のある一件の完了には証跡 (artifact_ref / outcome) が要る

一時 SQLite + SimpleNamespace(SessionLocal=...) の manager 偽装で、本番
~/.saiverse には一切触れない (tests/test_experience_inheritance.py と同じ流儀)。
"""
import gc
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import AI, Base, TaskBookEntry
from saiverse import clock
from saiverse import task_book as TB


def _seed_persona(SessionLocal, persona_id: str) -> None:
    """ai テーブルにペルソナ行を作る — add_entry のペルソナ実在検査 (アプリ層)
    を通すため。FK は宣言のみなので HOME_CITYID の city 行は不要。"""
    db = SessionLocal()
    try:
        db.add(AI(AIID=persona_id, HOME_CITYID=1, AINAME=persona_id))
        db.commit()
    finally:
        db.close()


class TaskBookTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)
        for persona_id in ("p1", "p2"):
            _seed_persona(self.SessionLocal, persona_id)

    def tearDown(self):
        clock.disable_virtual()
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def _count_rows(self) -> int:
        db = self.SessionLocal()
        try:
            return db.query(TaskBookEntry).count()
        finally:
            db.close()

    # ---- 追加と取得 ----

    def test_add_entry_roundtrip(self):
        """追加した一件の全欄が dict で往復する。"""
        entry = TB.add_entry(
            self.manager, "p1", "水曜までに挿絵を仕上げる",
            origin="sluice", due_at=1_800_000_000, counterpart="user",
            origin_ref="message:abc", meta={"k": "v"},
        )
        self.assertEqual(entry["persona_id"], "p1")
        self.assertEqual(entry["content"], "水曜までに挿絵を仕上げる")
        self.assertEqual(entry["due_at"], 1_800_000_000)
        self.assertEqual(entry["counterpart"], "user")
        self.assertEqual(entry["origin"], "sluice")
        self.assertEqual(entry["origin_ref"], "message:abc")
        self.assertEqual(entry["status"], TB.STATUS_OPEN)
        self.assertIsNone(entry["artifact_ref"])
        self.assertIsNone(entry["outcome"])
        self.assertIsNone(entry["closed_at"])
        self.assertEqual(entry["meta"], {"k": "v"})
        self.assertEqual(entry["revision"], 0)
        self.assertIsInstance(entry["created_at"], int)
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched, entry)

    def test_add_entry_without_due_is_legitimate(self):
        """due 省略 = 期限なし (NULL)。期限のない約束は正当な行 (§4.1)。"""
        entry = TB.add_entry(
            self.manager, "p1", "ずっと一緒にいる", origin="sluice",
            counterpart="user",
        )
        self.assertIsNone(entry["due_at"])
        self.assertEqual(entry["status"], TB.STATUS_OPEN)

    def test_add_entry_rejects_empty_content_and_origin(self):
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(self.manager, "p1", "   ", origin="user")
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(self.manager, "p1", "中身", origin="")
        self.assertEqual(self._count_rows(), 0)

    # ---- 受け入れ不変条件: 拒否は「期限も相手も無い行」だけ ----

    def test_add_entry_rejects_row_with_neither_due_nor_counterpart(self):
        """期限も相手も無い一件は手帳 (やりたいメモ) の領分 — タスク帳は拒否。"""
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(self.manager, "p1", "いつか小説を書きたい", origin="sluice")
        self.assertEqual(self._count_rows(), 0)

    def test_add_entry_accepts_due_only_self_task(self):
        """期限つきの自分だけの一件は合法 (§4「帰宅前に仕上げたいもの」型)。"""
        entry = TB.add_entry(
            self.manager, "p1", "帰宅前に下書きを仕上げる",
            origin="sluice", due_at=1_800_000_000,
        )
        self.assertIsNone(entry["counterpart"])
        self.assertEqual(entry["due_at"], 1_800_000_000)
        self.assertEqual(entry["status"], TB.STATUS_OPEN)

    def test_add_entry_accepts_system_task_without_due_or_counterpart(self):
        """システムタスク (origin='system') は期限も相手も無しで合法。"""
        entry = TB.add_entry(
            self.manager, "p1", "自己定義の聞き取り", origin="system"
        )
        self.assertIsNone(entry["due_at"])
        self.assertIsNone(entry["counterpart"])
        self.assertEqual(entry["origin"], "system")

    def test_add_entry_rejects_non_int_due_at(self):
        """SQLite は列型を強制しない — 文字列・float・bool の due_at は入口で
        拒否する (永続すると DUE_AT 昇順の引き当てを毒する)。bool は int の
        サブクラスなので明示拒否の検算対象。"""
        for bad in ("not-an-epoch", 1.5, True):
            with self.assertRaises(TB.TaskBookError):
                TB.add_entry(
                    self.manager, "p1", "中身", origin="user", due_at=bad
                )
        self.assertEqual(self._count_rows(), 0)

    def test_update_entry_rejects_non_int_due_at(self):
        entry = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        for bad in ("not-an-epoch", 1.5, True):
            with self.assertRaises(TB.TaskBookError):
                TB.update_entry(
                    self.manager, "p1", entry["task_id"], due_at=bad
                )
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["due_at"], 1000)  # 拒否後も無傷

    def test_update_entry_expected_revision_cas(self):
        """スナップショット時点の revision で CAS する口 (スルースの競合検出)。

        古い revision を渡した update は revision mismatch で拒否され、正しい
        revision なら通る。省略 (None) は従来どおり読み直した現在値で CAS。
        """
        entry = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        self.assertEqual(entry["revision"], 0)
        # 他所の更新が確定して revision 0 → 1。
        TB.update_entry(self.manager, "p1", entry["task_id"], content="他所の更新")
        # スナップショット (revision=0) に基づく古い判断は拒否される。
        with self.assertRaises(TB.TaskBookError):
            TB.update_entry(
                self.manager, "p1", entry["task_id"],
                content="古い判断", expected_revision=0,
            )
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["content"], "他所の更新")  # 上書きされていない
        # 現在の revision (1) を渡せば通る。
        updated = TB.update_entry(
            self.manager, "p1", entry["task_id"],
            content="現在値に基づく更新", expected_revision=1,
        )
        self.assertEqual(updated["content"], "現在値に基づく更新")
        self.assertEqual(updated["revision"], 2)

    def test_update_entry_expected_revision_rejects_non_int(self):
        entry = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        for bad in (True, "0", 1.5):
            with self.assertRaises(TB.TaskBookError):
                TB.update_entry(
                    self.manager, "p1", entry["task_id"],
                    content="新", expected_revision=bad,
                )
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["content"], "中身")  # 拒否後も無傷

    def test_raw_sql_string_due_at_is_rejected_by_check_constraint(self):
        """DB 境界の最終保証 — アプリ層を通らない生 SQL でも、文字列の DUE_AT
        は CHECK (ck_task_book_due_at_integer) が拒否する。"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO task_book "
                    "(TASK_ID, PERSONA_ID, CONTENT, DUE_AT, ORIGIN, STATUS, "
                    "CREATED_AT) "
                    "VALUES ('t-raw', 'p1', '中身', 'x', 'user', 'open', 1000)"
                )
        finally:
            conn.close()
        self.assertEqual(self._count_rows(), 0)

    def test_counterpart_whitespace_only_is_treated_as_none(self):
        """空白だけの counterpart は「相手なし」— 不変条件も保存値も None 扱い。"""
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(self.manager, "p1", "中身", origin="user", counterpart="   ")
        entry = TB.add_entry(
            self.manager, "p1", "中身", origin="user",
            counterpart="   ", due_at=1000,
        )
        self.assertIsNone(entry["counterpart"])
        stripped = TB.add_entry(
            self.manager, "p1", "中身", origin="user", counterpart="  user  "
        )
        self.assertEqual(stripped["counterpart"], "user")

    def test_created_at_uses_virtual_clock(self):
        """時刻刻印は clock.now() 経由 (仮想クロック尊重)。"""
        clock.enable_virtual(datetime(2030, 1, 2, 3, 4, 5))
        entry = TB.add_entry(
            self.manager, "p1", "中身", origin="user", counterpart="user"
        )
        self.assertEqual(
            entry["created_at"], int(datetime(2030, 1, 2, 3, 4, 5).timestamp())
        )

    # ---- 一覧: 期限なし行は締め切り引き当てに乗らない ----

    def test_dueless_entry_not_in_due_listing(self):
        """期限なしの行は list_open では見えるが list_open_with_due には出ない。"""
        dueless = TB.add_entry(
            self.manager, "p1", "期限なしの約束", origin="sluice", counterpart="user"
        )
        with_due = TB.add_entry(
            self.manager, "p1", "締め切りの依頼", origin="user", due_at=2_000_000_000
        )
        open_ids = [e["task_id"] for e in TB.list_open(self.manager, "p1")]
        self.assertIn(dueless["task_id"], open_ids)
        self.assertIn(with_due["task_id"], open_ids)
        due_ids = [e["task_id"] for e in TB.list_open_with_due(self.manager, "p1")]
        self.assertEqual(due_ids, [with_due["task_id"]])

    def test_due_listing_sorted_by_due_ascending(self):
        """締め切り引き当て一覧は DUE_AT 昇順 (決定論、intent §5)。"""
        late = TB.add_entry(self.manager, "p1", "後", origin="user", due_at=3000)
        early = TB.add_entry(self.manager, "p1", "先", origin="user", due_at=1000)
        mid = TB.add_entry(self.manager, "p1", "中", origin="user", due_at=2000)
        due_ids = [e["task_id"] for e in TB.list_open_with_due(self.manager, "p1")]
        self.assertEqual(
            due_ids, [early["task_id"], mid["task_id"], late["task_id"]]
        )

    def test_listings_are_persona_scoped(self):
        """一覧はペルソナ単位 — 他ペルソナの行は見えない。"""
        mine = TB.add_entry(self.manager, "p1", "自分の", origin="user", due_at=1000)
        TB.add_entry(self.manager, "p2", "他人の", origin="user", due_at=500)
        self.assertEqual(
            [e["task_id"] for e in TB.list_open(self.manager, "p1")],
            [mine["task_id"]],
        )
        self.assertEqual(
            [e["task_id"] for e in TB.list_open_with_due(self.manager, "p1")],
            [mine["task_id"]],
        )

    def test_system_task_listing_filters_by_origin(self):
        """システムタスク一覧は ORIGIN='system' の open 行だけを返す (§9-5)。"""
        TB.add_entry(
            self.manager, "p1", "ユーザー依頼", origin="user", counterpart="user"
        )
        sys_task = TB.add_entry(self.manager, "p1", "自己定義の聞き取り", origin="system")
        done_sys = TB.add_entry(self.manager, "p1", "解消済み", origin="system")
        TB.complete_entry(
            self.manager, "p1", done_sys["task_id"], outcome="済んだ"
        )
        listed = TB.list_open_system_tasks(self.manager, "p1")
        self.assertEqual([e["task_id"] for e in listed], [sys_task["task_id"]])

    # ---- 更新 ----

    def test_update_content_and_due(self):
        entry = TB.add_entry(self.manager, "p1", "旧", origin="user", due_at=1000)
        updated = TB.update_entry(
            self.manager, "p1", entry["task_id"], content="新", due_at=2000
        )
        self.assertEqual(updated["content"], "新")
        self.assertEqual(updated["due_at"], 2000)

    def test_update_can_clear_due(self):
        """due_at=None の明示指定は「期限を外す」— 省略 (変更しない) と区別する。

        相手のいる行なら期限を外せる (期限のない約束は正当な行、§4.1)。
        """
        entry = TB.add_entry(
            self.manager, "p1", "中身", origin="user",
            due_at=1000, counterpart="user",
        )
        untouched = TB.update_entry(
            self.manager, "p1", entry["task_id"], content="中身2"
        )
        self.assertEqual(untouched["due_at"], 1000)  # 省略 = 変更しない
        cleared = TB.update_entry(
            self.manager, "p1", entry["task_id"], due_at=None
        )
        self.assertIsNone(cleared["due_at"])
        self.assertEqual(TB.list_open_with_due(self.manager, "p1"), [])

    def test_update_cannot_clear_due_on_counterpartless_row(self):
        """期限つきの自分だけの一件 (相手なし・origin 非 system) から期限を
        外すと「期限も相手も無い行」になる — 更新後の姿でも不変条件は効く。"""
        entry = TB.add_entry(
            self.manager, "p1", "帰宅前に仕上げる", origin="sluice", due_at=1000
        )
        with self.assertRaises(TB.TaskBookError):
            TB.update_entry(self.manager, "p1", entry["task_id"], due_at=None)
        # content 同時変更でもすり抜けない。
        with self.assertRaises(TB.TaskBookError):
            TB.update_entry(
                self.manager, "p1", entry["task_id"], content="新", due_at=None
            )
        # 行は無傷 (期限も content も変わっていない)。
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["due_at"], 1000)
        self.assertEqual(fetched["content"], "帰宅前に仕上げる")
        # due_at 省略 (変更しない) の content 更新は通る。
        updated = TB.update_entry(
            self.manager, "p1", entry["task_id"], content="新しい中身"
        )
        self.assertEqual(updated["due_at"], 1000)

    def test_update_can_clear_due_on_system_row(self):
        """システムタスク (origin='system') は期限も相手も任意 — 期限を外せる。"""
        entry = TB.add_entry(
            self.manager, "p1", "自己定義の聞き取り", origin="system", due_at=1000
        )
        cleared = TB.update_entry(
            self.manager, "p1", entry["task_id"], due_at=None
        )
        self.assertIsNone(cleared["due_at"])

    def test_update_requires_some_change(self):
        entry = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        with self.assertRaises(TB.TaskBookError):
            TB.update_entry(self.manager, "p1", entry["task_id"])

    def test_update_is_persona_scoped(self):
        """他ペルソナの task_id は (存在しても) 触れない。"""
        entry = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        with self.assertRaises(TB.TaskBookError):
            TB.update_entry(self.manager, "p2", entry["task_id"], content="乗っ取り")

    # ---- 状態遷移: open → done / withdrawn の一方通行 ----

    def test_complete_without_counterpart_needs_no_evidence(self):
        """自分だけの一件は証跡任意で完了できる (§9-5)。"""
        entry = TB.add_entry(
            self.manager, "p1", "自分の練習", origin="user", due_at=1000
        )
        done = TB.complete_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(done["status"], TB.STATUS_DONE)
        self.assertIsInstance(done["closed_at"], int)

    def test_complete_with_counterpart_requires_evidence(self):
        """相手のある一件の完了は artifact_ref か outcome が必須 (接地、§9-5)。"""
        entry = TB.add_entry(
            self.manager, "p1", "依頼", origin="user", counterpart="user"
        )
        with self.assertRaises(TB.TaskBookError):
            TB.complete_entry(self.manager, "p1", entry["task_id"])
        # 顛末一行があれば通る (行為系の代替)
        done = TB.complete_entry(
            self.manager, "p1", entry["task_id"], outcome="送付済み"
        )
        self.assertEqual(done["status"], TB.STATUS_DONE)
        self.assertEqual(done["outcome"], "送付済み")

    def test_complete_with_artifact_ref(self):
        entry = TB.add_entry(
            self.manager, "p1", "挿絵", origin="user", counterpart="user"
        )
        done = TB.complete_entry(
            self.manager, "p1", entry["task_id"], artifact_ref="item:42"
        )
        self.assertEqual(done["artifact_ref"], "item:42")

    def test_withdraw_entry(self):
        entry = TB.add_entry(self.manager, "p1", "取り下げる", origin="user", due_at=1000)
        withdrawn = TB.withdraw_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(withdrawn["status"], TB.STATUS_WITHDRAWN)
        self.assertIsInstance(withdrawn["closed_at"], int)
        self.assertEqual(TB.list_open(self.manager, "p1"), [])

    def test_closed_entries_reject_further_transitions(self):
        """done / withdrawn の行は更新・完了・取り下げのどれも受けない。"""
        done = TB.add_entry(self.manager, "p1", "済ませる", origin="user", due_at=1000)
        TB.complete_entry(self.manager, "p1", done["task_id"])
        withdrawn = TB.add_entry(
            self.manager, "p1", "取り下げる", origin="user", due_at=1000
        )
        TB.withdraw_entry(self.manager, "p1", withdrawn["task_id"])
        for task_id in (done["task_id"], withdrawn["task_id"]):
            with self.assertRaises(TB.TaskBookError):
                TB.complete_entry(self.manager, "p1", task_id, outcome="x")
            with self.assertRaises(TB.TaskBookError):
                TB.withdraw_entry(self.manager, "p1", task_id)
            with self.assertRaises(TB.TaskBookError):
                TB.update_entry(self.manager, "p1", task_id, content="x")

    def test_closed_entry_still_readable(self):
        """閉じた行も get_entry では読める (顛末の参照、物理削除しない)。"""
        entry = TB.add_entry(self.manager, "p1", "済ませる", origin="user", due_at=1000)
        TB.complete_entry(self.manager, "p1", entry["task_id"])
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["status"], TB.STATUS_DONE)

    def test_get_entry_missing_raises(self):
        with self.assertRaises(TB.TaskBookError):
            TB.get_entry(self.manager, "p1", "no-such-id")

    # ---- 遷移の最終保証は単一条件 UPDATE の WHERE 句 ----

    def test_concurrent_close_is_caught_by_guarded_update(self):
        """二つのセッションが同じ open 行を読んだ後、先に complete された行への
        withdraw は TaskBookError になる。

        後発セッションの事前 SELECT が古い姿 (open) を掴んでいても遷移が
        通らないことを検査するため、_get_open_entry を「open と読めた」体の
        偽物に差し替えて事前検査を素通しさせ、UPDATE の WHERE 句
        (STATUS='open') だけで弾かれることを確かめる。
        """
        entry = TB.add_entry(
            self.manager, "p1", "依頼", origin="user", counterpart="user"
        )
        # セッション A が先に完了させる。
        TB.complete_entry(self.manager, "p1", entry["task_id"], outcome="済み")
        # セッション B は事前 SELECT では open と読めていた (stale read) 体。
        stale = SimpleNamespace(STATUS=TB.STATUS_OPEN, COUNTERPART=None, REVISION=0)
        with mock.patch.object(TB, "_get_open_entry", return_value=stale):
            with self.assertRaises(TB.TaskBookError):
                TB.withdraw_entry(self.manager, "p1", entry["task_id"])
            with self.assertRaises(TB.TaskBookError):
                TB.complete_entry(
                    self.manager, "p1", entry["task_id"], outcome="二重完了"
                )
        # 行は最初の完了のまま (取り下げに化けていない)。
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["status"], TB.STATUS_DONE)
        self.assertEqual(fetched["outcome"], "済み")

    # ---- 楽観ロック (REVISION): update/update の上書き消失防止 ----

    def test_concurrent_update_update_is_caught_by_revision(self):
        """二つのセッションが同じ行 (revision 0) を読み、先発の update が成功
        (revision 1) した後、後発の update は revision 不一致で TaskBookError。

        STATUS='open' の WHERE だけでは open のままの行への update/update を
        検出できない — 後発が読んだ時点の REVISION を WHERE に含めることで、
        後書きが先書きを黙って上書きする消失を防ぐ。後発セッションの stale read
        は _get_open_entry の差し替えで再現する (既存の close 競合テストと同じ)。
        """
        entry = TB.add_entry(
            self.manager, "p1", "初稿", origin="user", counterpart="user"
        )
        self.assertEqual(entry["revision"], 0)
        # 先発セッション: 実経路で update が成功する (revision 0 → 1)。
        first = TB.update_entry(
            self.manager, "p1", entry["task_id"], content="先発の改稿"
        )
        self.assertEqual(first["revision"], 1)
        # 後発セッション: 読んだ時点の姿 (revision 0) のまま update しようとする。
        stale = SimpleNamespace(
            STATUS=TB.STATUS_OPEN, COUNTERPART="user", ORIGIN="user",
            DUE_AT=None, REVISION=0,
        )
        with mock.patch.object(TB, "_get_open_entry", return_value=stale):
            with self.assertRaises(TB.TaskBookError):
                TB.update_entry(
                    self.manager, "p1", entry["task_id"], content="後発の上書き"
                )
        # 行は先発の姿のまま (後発に上書きされていない)。
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["content"], "先発の改稿")
        self.assertEqual(fetched["revision"], 1)

    def test_concurrent_update_then_stale_complete_is_caught(self):
        """update と complete の交差も revision で検出する — 先発の update 後、
        古い姿 (revision 0) を掴んだままの complete は TaskBookError。行は open
        のままなので STATUS の WHERE では止まらず、revision だけが防波堤。"""
        entry = TB.add_entry(
            self.manager, "p1", "依頼", origin="user", counterpart="user"
        )
        first = TB.update_entry(
            self.manager, "p1", entry["task_id"], content="改稿"
        )
        self.assertEqual(first["revision"], 1)
        stale = SimpleNamespace(
            STATUS=TB.STATUS_OPEN, COUNTERPART="user", ORIGIN="user",
            DUE_AT=None, REVISION=0,
        )
        with mock.patch.object(TB, "_get_open_entry", return_value=stale):
            with self.assertRaises(TB.TaskBookError):
                TB.complete_entry(
                    self.manager, "p1", entry["task_id"], outcome="古い完了"
                )
        # 行は open のまま無傷 (完了に化けていない)。
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["status"], TB.STATUS_OPEN)
        self.assertEqual(fetched["content"], "改稿")
        self.assertIsNone(fetched["outcome"])

    def test_successful_transitions_increment_revision(self):
        """成功した遷移は revision を +1 する (add=0 → update=1 → complete=2)。"""
        entry = TB.add_entry(
            self.manager, "p1", "初稿", origin="user", counterpart="user"
        )
        self.assertEqual(entry["revision"], 0)
        updated = TB.update_entry(
            self.manager, "p1", entry["task_id"], content="改稿"
        )
        self.assertEqual(updated["revision"], 1)
        done = TB.complete_entry(
            self.manager, "p1", entry["task_id"], outcome="送付済み"
        )
        self.assertEqual(done["revision"], 2)

    # ---- ペルソナ実在検査とペルソナ削除の後始末 ----

    def test_add_entry_rejects_unknown_persona(self):
        """ai テーブルに居ないペルソナ名義の追加はアプリ層検査で拒否する
        (FK は宣言のみ — pocketbook.add_memo の activity 実在確認と同じ筋)。"""
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(
                self.manager, "ghost", "中身", origin="user", counterpart="user"
            )
        self.assertEqual(self._count_rows(), 0)

    def test_purge_persona_entries_removes_all_rows_for_persona(self):
        """ペルソナ削除経路 (delete_ai) が呼ぶ purge_persona_entries は、当該
        ペルソナの行を open / closed を問わず消し、他ペルソナの行は残す。"""
        open_entry = TB.add_entry(
            self.manager, "p1", "開いている一件", origin="user", counterpart="user"
        )
        closed_entry = TB.add_entry(
            self.manager, "p1", "閉じた一件", origin="user", due_at=1000
        )
        TB.complete_entry(self.manager, "p1", closed_entry["task_id"])
        keep = TB.add_entry(
            self.manager, "p2", "他人の一件", origin="user", counterpart="user"
        )
        db = self.SessionLocal()
        try:
            deleted = TB.purge_persona_entries(db, "p1")
            db.commit()  # commit は呼び手 (delete_ai) の責任 — ここでは呼び手役
        finally:
            db.close()
        self.assertEqual(deleted, 2)
        for task_id in (open_entry["task_id"], closed_entry["task_id"]):
            with self.assertRaises(TB.TaskBookError):
                TB.get_entry(self.manager, "p1", task_id)
        self.assertEqual(
            [e["task_id"] for e in TB.list_open(self.manager, "p2")],
            [keep["task_id"]],
        )

    # ---- 冪等キー (idem_key) ----

    def test_idem_key_same_key_returns_same_task(self):
        """同じ (persona, idem_key) で二度 add しても行は増えず同じ TASK_ID。"""
        first = TB.add_entry(
            self.manager, "p1", "挿絵を仕上げる", origin="sluice",
            counterpart="user", idem_key="sluice-run-1:op-1",
        )
        second = TB.add_entry(
            self.manager, "p1", "挿絵を仕上げる (再試行)", origin="sluice",
            counterpart="user", idem_key="sluice-run-1:op-1",
        )
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(second["content"], first["content"])  # 既存が勝つ
        self.assertEqual(self._count_rows(), 1)

    def test_idem_key_different_keys_create_different_rows(self):
        a = TB.add_entry(
            self.manager, "p1", "中身", origin="sluice",
            counterpart="user", idem_key="run-1:op-1",
        )
        b = TB.add_entry(
            self.manager, "p1", "中身", origin="sluice",
            counterpart="user", idem_key="run-1:op-2",
        )
        self.assertNotEqual(a["task_id"], b["task_id"])
        self.assertEqual(self._count_rows(), 2)

    def test_no_idem_key_creates_new_row_each_time(self):
        """キー無しは従来どおり毎回新規 (NULL は一意制約にかからない)。"""
        a = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        b = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        self.assertNotEqual(a["task_id"], b["task_id"])
        self.assertEqual(self._count_rows(), 2)

    # ---- 完了証跡の空白すり抜け防止 ----

    def test_blank_evidence_does_not_satisfy_grounding(self):
        """空白だけの outcome / artifact_ref は証跡にならない (§9-5)。"""
        entry = TB.add_entry(
            self.manager, "p1", "依頼", origin="user", counterpart="user"
        )
        with self.assertRaises(TB.TaskBookError):
            TB.complete_entry(self.manager, "p1", entry["task_id"], outcome="   ")
        with self.assertRaises(TB.TaskBookError):
            TB.complete_entry(
                self.manager, "p1", entry["task_id"], artifact_ref="  \t "
            )
        self.assertEqual(
            TB.get_entry(self.manager, "p1", entry["task_id"])["status"],
            TB.STATUS_OPEN,
        )

    def test_non_string_evidence_does_not_satisfy_grounding(self):
        """outcome=True / artifact_ref=123 は "True"/"123" の顔で証跡ガードを
        通過しない — 暗黙 str() 変換の全廃。行は open のまま無傷。"""
        entry = TB.add_entry(
            self.manager, "p1", "依頼", origin="user", counterpart="user"
        )
        with self.assertRaises(TB.TaskBookError):
            TB.complete_entry(self.manager, "p1", entry["task_id"], outcome=True)
        with self.assertRaises(TB.TaskBookError):
            TB.complete_entry(
                self.manager, "p1", entry["task_id"], artifact_ref=123
            )
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["status"], TB.STATUS_OPEN)
        self.assertIsNone(fetched["outcome"])
        self.assertIsNone(fetched["artifact_ref"])

    def test_add_entry_rejects_non_string_arguments(self):
        """文字列引数の暗黙 str() 変換は全廃 — str 以外は TaskBookError。"""
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(self.manager, "p1", 123, origin="user", due_at=1000)
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(self.manager, "p1", "中身", origin=123, due_at=1000)
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(
                self.manager, "p1", "中身", origin="user", counterpart=123
            )
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(
                self.manager, "p1", "中身", origin="user",
                due_at=1000, origin_ref=123,
            )
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(
                self.manager, "p1", "中身", origin="user",
                due_at=1000, idem_key=123,
            )
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(self.manager, 123, "中身", origin="user", due_at=1000)
        # meta のキーも str 限定 — json.dumps は 1 を "1" に黙って文字列化する。
        with self.assertRaises(TB.TaskBookError):
            TB.add_entry(
                self.manager, "p1", "中身", origin="user",
                due_at=1000, meta={1: "v"},
            )
        self.assertEqual(self._count_rows(), 0)

    def test_update_entry_rejects_non_string_content_and_ids(self):
        entry = TB.add_entry(self.manager, "p1", "中身", origin="user", due_at=1000)
        with self.assertRaises(TB.TaskBookError):
            TB.update_entry(self.manager, "p1", entry["task_id"], content=123)
        with self.assertRaises(TB.TaskBookError):
            TB.update_entry(self.manager, "p1", 123, content="新")
        fetched = TB.get_entry(self.manager, "p1", entry["task_id"])
        self.assertEqual(fetched["content"], "中身")  # 拒否後も無傷

    def test_evidence_is_stored_stripped(self):
        entry = TB.add_entry(
            self.manager, "p1", "依頼", origin="user", counterpart="user"
        )
        done = TB.complete_entry(
            self.manager, "p1", entry["task_id"],
            artifact_ref="  item:42  ", outcome="  送付済み  ",
        )
        self.assertEqual(done["artifact_ref"], "item:42")
        self.assertEqual(done["outcome"], "送付済み")


class TaskBookMigrationTests(unittest.TestCase):
    """migrate.py の軽量シンク (ensure_task_book_table) の冪等性。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")

    def tearDown(self):
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_ensure_task_book_table_creates_and_is_idempotent(self):
        from database.migrate import ensure_task_book_table
        # ai テーブル等の無い素の DB でもテーブル追加だけで通る (FK は宣言のみ)
        engine = create_engine(f"sqlite:///{self.db_path}")
        engine.dispose()
        ensure_task_book_table(self.db_path)
        ensure_task_book_table(self.db_path)  # 冪等
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            # add_entry のペルソナ実在検査 (アプリ層) を通すため ai 表と行を用意
            AI.__table__.create(bind=engine)
            SessionLocal = sessionmaker(bind=engine)
            _seed_persona(SessionLocal, "p1")
            manager = SimpleNamespace(SessionLocal=SessionLocal)
            entry = TB.add_entry(manager, "p1", "中身", origin="user", due_at=1000)
            self.assertEqual(entry["status"], TB.STATUS_OPEN)
        finally:
            engine.dispose()

    def test_ensure_task_book_table_adds_idem_key_to_old_schema(self):
        """IDEM_KEY の無い旧スキーマの DB に、列と UNIQUE インデックスを後付けする。"""
        import sqlite3
        from database.migrate import ensure_task_book_table
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE task_book (
                    "TASK_ID" VARCHAR(36) NOT NULL PRIMARY KEY,
                    "PERSONA_ID" VARCHAR(255) NOT NULL,
                    "CONTENT" TEXT NOT NULL,
                    "DUE_AT" INTEGER,
                    "COUNTERPART" VARCHAR(255),
                    "ORIGIN" VARCHAR(32) NOT NULL,
                    "ORIGIN_REF" VARCHAR(255),
                    "STATUS" VARCHAR(16) NOT NULL,
                    "ARTIFACT_REF" VARCHAR(255),
                    "OUTCOME" TEXT,
                    "CREATED_AT" INTEGER NOT NULL,
                    "CLOSED_AT" INTEGER,
                    "META_JSON" TEXT
                )
                """
            )
            # REVISION 後付け前の既存行 — backfill (NULL → 0) の検査対象。
            conn.execute(
                "INSERT INTO task_book "
                "(TASK_ID, PERSONA_ID, CONTENT, DUE_AT, ORIGIN, STATUS, "
                "CREATED_AT) "
                "VALUES ('t-legacy', 'p1', '旧行', 1000, 'user', 'open', 1000)"
            )
            conn.commit()
        finally:
            conn.close()
        ensure_task_book_table(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(task_book)")}
            self.assertIn("IDEM_KEY", cols)
            self.assertIn("REVISION", cols)
            idx_names = {
                r[1] for r in conn.execute("PRAGMA index_list(task_book)")
            }
            self.assertIn("uq_task_book_idem", idx_names)
        finally:
            conn.close()
        # 後付け後の DB で idem_key の get-or-create が効く。
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            AI.__table__.create(bind=engine)
            SessionLocal = sessionmaker(bind=engine)
            _seed_persona(SessionLocal, "p1")
            manager = SimpleNamespace(SessionLocal=SessionLocal)
            a = TB.add_entry(
                manager, "p1", "中身", origin="user",
                counterpart="user", idem_key="k1",
            )
            b = TB.add_entry(
                manager, "p1", "中身", origin="user",
                counterpart="user", idem_key="k1",
            )
            self.assertEqual(a["task_id"], b["task_id"])
            # 後付け前の既存行は REVISION が 0 へ backfill され、遷移できる。
            legacy = TB.get_entry(manager, "p1", "t-legacy")
            self.assertEqual(legacy["revision"], 0)
            done = TB.complete_entry(manager, "p1", "t-legacy", outcome="済み")
            self.assertEqual(done["status"], TB.STATUS_DONE)
            self.assertEqual(done["revision"], 1)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
