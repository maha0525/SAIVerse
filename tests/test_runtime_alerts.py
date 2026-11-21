"""動作中の警告 (saiverse/runtime_alerts.py) の保存・まとめ・閉じる・API・DB 移行。

docs/intent/runtime_alerts.md の「この設計で保証すること」のうち、サーバー側で
固定できるもの:

- 同じ識別キーでまだ閉じていない知らせは一枚にまとまり、回数・最後の時刻・
  見出し・本文・詳細が最後の発生の内容になる
- 閉じた後に同じキーで起きたら新しい一枚になる
- 閉じた知らせは一覧に出ない。閉じたことは常設 SSE で全タブへ送る
- 見出しと本文は ui_message の形でだけ受け取る
- 書き込み先を受け取っていないプロセスでは何も書かない (本番の DB に倒れない)
- 既存の DB に表を足す移行

DB は一時ファイル。本番の ~/.saiverse には触れない。
"""
from __future__ import annotations

import asyncio
import gc
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from api.routes import system as system_routes
from api.ui_messages import ui_message
from database.models import Base, RuntimeAlert
from saiverse import runtime_alerts


def _title(n: int = 1) -> dict:
    return ui_message("api.runtimeAlerts.memorizeFailed.title", n=n)


def _message(name: str = "エリス") -> dict:
    return ui_message("api.runtimeAlerts.memorizeFailed.message", persona=name)


class RuntimeAlertsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "alerts.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine, tables=[RuntimeAlert.__table__])
        self.factory = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        runtime_alerts.configure(self.factory)
        self.addCleanup(runtime_alerts.configure, None)

        self.broadcasts: list = []
        patcher = patch("saiverse.addon_events.broadcast_event", side_effect=self.broadcasts.append)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.now = 1_000_000
        clock = patch("saiverse.runtime_alerts.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self) -> None:
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def _rows(self) -> list:
        db = self.factory()
        try:
            return db.query(RuntimeAlert).order_by(RuntimeAlert.ALERT_ID).all()
        finally:
            db.close()


class PostRuntimeAlertTest(RuntimeAlertsTestBase):
    def test_first_occurrence_inserts_and_broadcasts(self) -> None:
        alert = runtime_alerts.post_runtime_alert(
            "memorize_failed:p1", "error", _title(), _message(), details={"persona_id": "p1"},
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert["key"], "memorize_failed:p1")
        self.assertEqual(alert["level"], "error")
        self.assertEqual(alert["title"], _title())
        self.assertEqual(alert["message"], _message())
        self.assertEqual(alert["details"], {"persona_id": "p1"})
        self.assertEqual(alert["count"], 1)
        self.assertEqual((alert["first_at"], alert["last_at"]), (self.now, self.now))
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self.broadcasts, [
            {"type": "runtime_alert", "action": "upsert", "alert": alert},
        ])
        # 画面の decodeUIMessages は content / metadata の中を変換しないので、その名前には置かない
        self.assertNotIn("content", alert)
        self.assertNotIn("metadata", alert)

    def test_same_open_key_merges_and_takes_the_last_content(self) -> None:
        first = runtime_alerts.post_runtime_alert(
            "memorize_failed:p1", "warning", _title(1), _message("前"), details={"n": 1},
        )
        self.now += 60
        second = runtime_alerts.post_runtime_alert(
            "memorize_failed:p1", "error", _title(2), _message("後"), details={"n": 2},
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["count"], 2)
        self.assertEqual(second["first_at"], first["first_at"])
        self.assertEqual(second["last_at"], first["last_at"] + 60)
        self.assertEqual(second["level"], "error")
        self.assertEqual(second["title"], _title(2))
        self.assertEqual(second["message"], _message("後"))
        self.assertEqual(second["details"], {"n": 2})
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(len(self.broadcasts), 2)

    def test_different_keys_are_separate_cards(self) -> None:
        a = runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())
        b = runtime_alerts.post_runtime_alert("memorize_failed:p2", "error", _title(), _message())
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(self._rows()), 2)

    def test_occurrence_after_dismiss_is_a_new_card(self) -> None:
        first = runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())
        runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())
        self.assertTrue(runtime_alerts.dismiss_runtime_alert(first["id"]))
        self.now += 10
        again = runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())
        self.assertNotEqual(again["id"], first["id"])
        self.assertEqual(again["count"], 1)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].OCCURRENCE_COUNT, 2)
        self.assertIsNotNone(rows[0].DISMISSED_AT)
        self.assertIsNone(rows[1].DISMISSED_AT)

    def test_concurrent_posts_of_one_key_stay_one_card(self) -> None:
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].OCCURRENCE_COUNT, 8)

    def test_open_key_is_unique_in_the_db(self) -> None:
        runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())
        with self.assertRaises(Exception):
            with self.engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO runtime_alerts (ALERT_KEY, LEVEL, TITLE_JSON, MESSAGE_JSON, "
                    "OCCURRENCE_COUNT, FIRST_AT, LAST_AT) "
                    "VALUES ('memorize_failed:p1', 'error', '{}', '{}', 1, 0, 0)"
                ))

    def test_contract_violations_raise(self) -> None:
        with self.assertRaises(ValueError):
            runtime_alerts.post_runtime_alert("k", "info", _title(), _message())
        with self.assertRaises(ValueError):
            runtime_alerts.post_runtime_alert("k", "error", "記憶の保存に失敗しました", _message())
        with self.assertRaises(ValueError):
            runtime_alerts.post_runtime_alert("k", "error", _title(), {"content": "x"})
        with self.assertRaises(ValueError):
            runtime_alerts.post_runtime_alert("", "error", _title(), _message())
        self.assertEqual(self._rows(), [])

    def test_unconfigured_process_writes_nothing(self) -> None:
        runtime_alerts.configure(None)
        self.assertIsNone(
            runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())
        )
        self.assertEqual(self.broadcasts, [])
        with self.assertRaises(runtime_alerts.RuntimeAlertsUnavailable):
            runtime_alerts.list_runtime_alerts()

    def test_db_failure_does_not_raise_to_the_caller(self) -> None:
        def broken_factory():
            raise RuntimeError("db is gone")

        runtime_alerts.configure(broken_factory)
        self.assertIsNone(
            runtime_alerts.post_runtime_alert("memorize_failed:p1", "error", _title(), _message())
        )
        self.assertEqual(self.broadcasts, [])


class DismissAndListTest(RuntimeAlertsTestBase):
    def test_dismiss_broadcasts_and_is_idempotent(self) -> None:
        alert = runtime_alerts.post_runtime_alert("k1", "warning", _title(), _message())
        self.broadcasts.clear()
        self.assertIs(runtime_alerts.dismiss_runtime_alert(alert["id"]), True)
        self.assertEqual(self.broadcasts, [
            {"type": "runtime_alert", "action": "dismiss", "id": alert["id"]},
        ])
        self.assertIs(runtime_alerts.dismiss_runtime_alert(alert["id"]), False)
        self.assertIsNone(runtime_alerts.dismiss_runtime_alert(9999))
        self.assertEqual(len(self.broadcasts), 1)

    def test_list_excludes_dismissed_and_is_newest_first(self) -> None:
        old = runtime_alerts.post_runtime_alert("k-old", "warning", _title(), _message())
        self.now += 10
        gone = runtime_alerts.post_runtime_alert("k-gone", "warning", _title(), _message())
        self.now += 10
        new = runtime_alerts.post_runtime_alert("k-new", "error", _title(), _message())
        runtime_alerts.dismiss_runtime_alert(gone["id"])
        self.now += 10
        # 古い方がもう一度起きると、最後の時刻で一番上に来る
        runtime_alerts.post_runtime_alert("k-old", "warning", _title(), _message())
        listed = runtime_alerts.list_runtime_alerts()
        self.assertEqual([a["id"] for a in listed], [old["id"], new["id"]])


class RuntimeAlertsApiTest(RuntimeAlertsTestBase):
    def setUp(self) -> None:
        super().setUp()
        app = FastAPI()
        app.include_router(system_routes.router, prefix="/api/system")
        self.client = TestClient(app)

    def test_list_and_dismiss_routes(self) -> None:
        a = runtime_alerts.post_runtime_alert("k1", "warning", _title(), _message())
        self.now += 5
        b = runtime_alerts.post_runtime_alert("k2", "error", _title(), _message())

        res = self.client.get("/api/system/runtime-alerts")
        self.assertEqual(res.status_code, 200)
        alerts = res.json()["alerts"]
        self.assertEqual([x["id"] for x in alerts], [b["id"], a["id"]])
        self.assertEqual(alerts[0]["title"], _title())

        res = self.client.post(f"/api/system/runtime-alerts/{a['id']}/dismiss")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"dismissed": True})
        res = self.client.post(f"/api/system/runtime-alerts/{a['id']}/dismiss")
        self.assertEqual(res.json(), {"dismissed": False})
        self.assertEqual(
            [x["id"] for x in self.client.get("/api/system/runtime-alerts").json()["alerts"]],
            [b["id"]],
        )

    def test_unknown_alert_is_404(self) -> None:
        res = self.client.post("/api/system/runtime-alerts/12345/dismiss")
        self.assertEqual(res.status_code, 404)

    def test_not_configured_is_503(self) -> None:
        runtime_alerts.configure(None)
        self.assertEqual(self.client.get("/api/system/runtime-alerts").status_code, 503)
        self.assertEqual(self.client.post("/api/system/runtime-alerts/1/dismiss").status_code, 503)


class BroadcastChannelTest(unittest.TestCase):
    """runtime_alert は既存の常設 SSE の受け手キューへ届き、アドオンイベントの形は変わらない。"""

    def test_runtime_alert_and_addon_event_reach_subscribers(self) -> None:
        from saiverse import addon_events

        async def scenario() -> list:
            loop = asyncio.get_running_loop()
            prev_loop = addon_events._event_loop
            addon_events.set_event_loop(loop)
            queue: asyncio.Queue = asyncio.Queue()
            addon_events.subscribe(queue)
            try:
                addon_events.broadcast_event({"type": "runtime_alert", "action": "dismiss", "id": 3})
                addon_events.emit_addon_event("addon-x", "done", data={"a": 1}, message_id="m1")
                # 別スレッドからの送信 (スケジュールの処理などから呼ばれる場合)
                th = threading.Thread(target=addon_events.broadcast_event, args=(
                    {"type": "runtime_alert", "action": "dismiss", "id": 4},
                ))
                th.start()
                th.join()
                got = [await asyncio.wait_for(queue.get(), timeout=2) for _ in range(3)]
            finally:
                addon_events.unsubscribe(queue)
                addon_events._event_loop = prev_loop
            return got

        got = asyncio.run(scenario())
        self.assertEqual(got, [
            {"type": "runtime_alert", "action": "dismiss", "id": 3},
            {"type": "addon_event", "addon": "addon-x", "event": "done", "message_id": "m1", "data": {"a": 1}},
            {"type": "runtime_alert", "action": "dismiss", "id": 4},
        ])


class RuntimeAlertsMigrationTest(unittest.TestCase):
    """既存の DB (runtime_alerts の表が無い) に表を足す移行。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "old.db")
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            tables = [t for t in Base.metadata.sorted_tables if t.name != "runtime_alerts"]
            Base.metadata.create_all(engine, tables=tables)
        finally:
            engine.dispose()

    def tearDown(self) -> None:
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def _inspect(self):
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            insp = inspect(engine)
            return (
                insp.has_table("runtime_alerts"),
                {c["name"] for c in insp.get_columns("runtime_alerts")} if insp.has_table("runtime_alerts") else set(),
                {i["name"] for i in insp.get_indexes("runtime_alerts")} if insp.has_table("runtime_alerts") else set(),
            )
        finally:
            engine.dispose()

    def test_ensure_creates_the_table_and_is_idempotent(self) -> None:
        from database.migrate import ensure_runtime_alerts_table, needs_migration

        self.assertTrue(needs_migration(self.db_path))
        ensure_runtime_alerts_table(self.db_path)
        has_table, cols, indexes = self._inspect()
        self.assertTrue(has_table)
        self.assertEqual(cols, {
            "ALERT_ID", "ALERT_KEY", "LEVEL", "TITLE_JSON", "MESSAGE_JSON", "DETAILS_JSON",
            "OCCURRENCE_COUNT", "FIRST_AT", "LAST_AT", "DISMISSED_AT",
        })
        self.assertIn("uq_runtime_alerts_open_key", indexes)
        self.assertIn("idx_runtime_alerts_open", indexes)
        self.assertFalse(needs_migration(self.db_path))
        ensure_runtime_alerts_table(self.db_path)  # 二度呼んでも安全

    def test_generic_additive_migration_also_creates_the_table(self) -> None:
        from database.migrate import needs_migration, try_additive_migration

        self.assertTrue(try_additive_migration(self.db_path))
        self.assertFalse(needs_migration(self.db_path))
        self.assertTrue(self._inspect()[0])


if __name__ == "__main__":
    unittest.main()
