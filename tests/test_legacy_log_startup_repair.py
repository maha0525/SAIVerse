"""起動時の検算が、見つけた取り込み漏れをその場で直すこと。

見つけられるのに放置してバナーだけ出すと、ユーザーは「取り込まれていません」と
言われるだけで何もできない (2026-08-16 まはー裁定)。ここで固定するのは:

- 未取り込みの部屋は起動のたびに自動で取り込まれ、バナーは出ない
- 既に会話が始まっている部屋でも、古い会話は時系列どおり前に入る (既存の行は不動)
- 直しようのないもの (ファイルが壊れている) は取り込みを試さず、バナーに残る
- 取り込みが失敗したらバナーに残る (黙って消えない)
"""
from __future__ import annotations

import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, BuildingMessage
from manager.initialization import InitializationMixin


class _Manager(InitializationMixin):
    """検算に必要な属性だけ持たせた最小の器。"""

    def __init__(self, session_local, home: Path, city_name: str, building_ids) -> None:
        self.SessionLocal = session_local
        self.saiverse_home = home
        self.city_name = city_name
        self.buildings = [SimpleNamespace(building_id=b) for b in building_ids]
        self.building_map = {b: SimpleNamespace(name=b) for b in building_ids}
        self.startup_alerts = []


class LegacyLogStartupRepairTests(unittest.TestCase):
    CITY = "city_a"

    def setUp(self) -> None:
        self._home_tmp = tempfile.TemporaryDirectory(prefix="saiverse_home_")
        self.home = Path(self._home_tmp.name)
        self.addCleanup(self._home_tmp.cleanup)

        self._db_tmp = tempfile.TemporaryDirectory(prefix="saiverse_db_")
        self.addCleanup(self._cleanup_db_dir)
        self.engine = create_engine(f"sqlite:///{Path(self._db_tmp.name) / 'saiverse.db'}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)

    def _cleanup_db_dir(self) -> None:
        try:
            self.engine.dispose()
        except Exception:
            pass
        gc.collect()
        try:
            self._db_tmp.cleanup()
        except PermissionError:
            pass

    def _write_log(self, building_id: str, messages) -> Path:
        b_dir = self.home / "cities" / self.CITY / "buildings" / building_id
        b_dir.mkdir(parents=True, exist_ok=True)
        path = b_dir / "log.json"
        if isinstance(messages, str):
            path.write_text(messages, encoding="utf-8")
        else:
            path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        return path

    def _msg(self, n: int, building_id: str, content: str) -> dict:
        return {
            "role": "user", "content": content, "seq": n,
            "message_id": f"{building_id}:old{n}",
            "timestamp": f"2026-05-0{n}T10:00:00", "heard_by": [],
        }

    def _rows(self):
        db = self.SessionLocal()
        try:
            return db.query(BuildingMessage).order_by(
                BuildingMessage.building_id, BuildingMessage.seq
            ).all()
        finally:
            db.close()

    def _run(self, building_ids):
        mgr = _Manager(self.SessionLocal, self.home, self.CITY, building_ids)
        mgr._check_legacy_building_log_import()
        return mgr.startup_alerts

    # ------------------------------------------------------------------

    def test_missing_history_is_imported_at_startup_without_a_banner(self) -> None:
        self._write_log("room1", [self._msg(1, "room1", "a"), self._msg(2, "room1", "b")])
        alerts = self._run(["room1"])
        self.assertEqual(alerts, [])
        rows = self._rows()
        self.assertEqual([r.content for r in rows], ["a", "b"])
        self.assertEqual([r.seq for r in rows], [-2, -1])

    def test_repair_is_idempotent_across_restarts(self) -> None:
        self._write_log("room1", [self._msg(1, "room1", "a")])
        self._run(["room1"])
        alerts = self._run(["room1"])  # 2 回目の起動
        self.assertEqual(alerts, [])
        self.assertEqual(len(self._rows()), 1)

    def test_history_is_prepended_when_the_room_already_has_messages(self) -> None:
        db = self.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id="room1", seq=1, role="user", content="新しい発言",
                timestamp="2026-06-01T10:00:00", heard_by="[]", ingested_by="[]",
                message_id="room1:1",
            ))
            db.commit()
        finally:
            db.close()
        self._write_log("room1", [self._msg(1, "room1", "古い発言")])

        alerts = self._run(["room1"])
        self.assertEqual(alerts, [])
        rows = self._rows()
        self.assertEqual([r.content for r in rows], ["古い発言", "新しい発言"])
        # 既存の行は動かない
        self.assertEqual(rows[1].seq, 1)
        self.assertEqual(rows[1].message_id, "room1:1")

    def test_unreadable_file_is_not_imported_and_stays_on_a_banner(self) -> None:
        self._write_log("room1", "{壊れている")
        with patch(
            "manager.initialization.InitializationMixin._repair_legacy_building_logs"
        ) as repair:
            alerts = self._run(["room1"])
        repair.assert_not_called()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["details"]["kind"], "unreadable")

    def test_failed_repair_stays_on_a_banner(self) -> None:
        """直そうとして直らなかったものは、黙って消えない。"""
        self._write_log("room1", [self._msg(1, "room1", "a")])
        with patch(
            "saiverse.legacy_log_import.import_building_logs",
            side_effect=RuntimeError("simulated import failure"),
        ):
            alerts = self._run(["room1"])
        self.assertEqual(len(self._rows()), 0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["details"]["kind"], "not_imported")


if __name__ == "__main__":
    unittest.main()
