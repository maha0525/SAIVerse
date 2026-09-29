"""消した建物の ID を空けること: 残る会話などを特殊な ID へ付け替える (saiverse/building_retirement.py)。

docs/issues/building_delete_leaves_contents.md の「3. ID の使い回し」と「5. 既にある残骸」。
ここで固定するのは:

- 建物の削除の場で、残る参照 (部屋を指す欄・JSON の欄・メッセージ ID・アドオンの
  メタデータ)・ペルソナの記憶の印 (複製を取ってから。本文は変えない)・フォルダを
  特殊な ID へ移し、記録を「完了」にする
- 記憶の印の書き換えは、adapter と同じ錠前の中で行う
- 記憶の印が書き換えられなくても削除は成功し、記録は「予定」のまま、その間は元の
  ID を作成の口が使わず、次の起動が続きを終えると空く
- 同じ DB を別の SAIVerse が使っている間は、記憶とフォルダを次の起動へ回す
- ``deleted_`` で始まる ID は作れない (手で付けた ID・名前から作った ID)
- 起動時: 昔消した建物の残骸を付け替える / 二度目は何もしない / 多重起動では見送る /
  削除が巻き戻った予定・同じ ID が作られてしまった予定は触らずに閉じる
- 部屋 ID の付け替え (building_id_repair) は、この記録の要素を読み飛ばす

すべて一時フォルダの SAIVERSE_HOME と一時ファイルの SQLite で動かす (本番は触らない)。
"""
from __future__ import annotations

import gc
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.models import (
    AI,
    AddonMessageMetadata,
    Base,
    Building,
    BuildingMessage,
    City,
    Episode,
    LLMUsageLog,
    PersonaBuildingState,
    PersonaDayPlan,
    PersonaPulseCursor,
    Playbook,
    Region,
    User,
)
from manager.admin import AdminService
from sai_memory.memory.storage import init_db
from sai_memory.perception_buffer import init_perception_buffer_table, push_perception
from sai_memory.room_state import room_key
from saiverse import building_id_repair as repair
from saiverse import building_retirement as retirement

CITY = "city_a"
CITY_ID = 1
PERSONA = "p1_city_a"
NOW = datetime(2026, 9, 29, 18, 28, 0)
_OWNS_DB = "saiverse.runtime_marker.another_running_process_owns_db"


class _RetirementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="saiverse_retire_")
        self.addCleanup(self._cleanup_temp)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        env = patch.dict(os.environ, {"SAIVERSE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

        self.db_path = Path(self._tmp.name) / "saiverse.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self._add(
            User(USERID=1, PASSWORD="x", USERNAME="tester"),
            City(CITYID=CITY_ID, USERID=1, CITY_SLUG=CITY, UI_PORT=3000, API_PORT=8000),
        )

        self.svc = AdminService.__new__(AdminService)
        self.svc.SessionLocal = self.Session
        self.svc.saiverse_home = self.home
        self.svc.db_path = str(self.db_path)
        self.svc.manager = SimpleNamespace(_load_items_from_db=lambda: None)

    def _cleanup_temp(self) -> None:
        try:
            self.engine.dispose()
        except Exception:
            pass
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    # -- 組み立て ------------------------------------------------------------

    def _add(self, *objects) -> None:
        db = self.Session()
        try:
            for obj in objects:
                db.add(obj)
                db.flush()
            db.commit()
        finally:
            db.close()

    def _create(self, name: str, building_id=None) -> str:
        return self.svc.create_building(name, "", 5, "", CITY_ID, building_id)

    @staticmethod
    def _message(building_id: str, seq: int, content: str = "こんにちは") -> BuildingMessage:
        return BuildingMessage(
            building_id=building_id, seq=seq, role="user", content=content,
            timestamp="2026-09-29T10:00:00", heard_by="[]", ingested_by="[]",
            message_id=f"{building_id}:{seq}",
        )

    @property
    def buildings_root(self) -> Path:
        return self.home / "cities" / CITY / "buildings"

    @property
    def record_path(self) -> Path:
        return self.home / "cities" / CITY / repair.RENAMES_FILENAME

    def _memory_path(self, persona_id: str = PERSONA) -> Path:
        return self.home / "personas" / persona_id / "memory.db"

    def _seed_memory(self, building_id: str, persona_id: str = PERSONA) -> None:
        """記憶のファイルに、部屋の会話を写した目印と、移動の知らせ (部屋の鍵つき) を入れる。"""
        conn = init_db(str(self._memory_path(persona_id)))
        try:
            init_perception_buffer_table(conn)
            push_perception(
                conn, "building_changed", "現在地が「道具店」に変わりました",
                reduce_key=room_key(building_id),
                metadata=json.dumps({"to_id": building_id, "to_name": "道具店"}, ensure_ascii=False),
            )
            conn.execute(
                "INSERT INTO messages (id, thread_id, role, content, resource_id, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("m1", f"{persona_id}:main", "user", f"{building_id} の話をしよう", persona_id, 50,
                 json.dumps({"building_msg_ref": f"{building_id}:{building_id}:1",
                             "note": building_id}, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()

    def _make_folder(self, building_id: str) -> Path:
        folder = self.buildings_root / building_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "log.json").write_text("[]", encoding="utf-8")
        return folder

    # -- 読み出し ------------------------------------------------------------

    def _scalar(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def _record(self) -> dict:
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def _retire_entries(self, old_id: str):
        if not self.record_path.exists():
            return []
        return [
            e for e in self._record()["renames"]
            if e.get("kind") == "retire" and e.get("old_id") == old_id
        ]

    def _memory_rows(self, persona_id: str = PERSONA) -> dict:
        conn = sqlite3.connect(str(self._memory_path(persona_id)))
        try:
            return {
                "buffer": conn.execute(
                    "SELECT content, reduce_key, metadata FROM perception_buffer ORDER BY id"
                ).fetchall(),
                "messages": conn.execute(
                    "SELECT id, content, metadata FROM messages ORDER BY id"
                ).fetchall(),
            }
        finally:
            conn.close()

    def _memory_backups(self, persona_id: str = PERSONA):
        return sorted(
            (self.home / "backups" / retirement.BACKUP_KIND).glob(f"*/{persona_id}_memory*.db")
        )

    def _message_count(self, building_id: str) -> int:
        return self._scalar(
            "SELECT COUNT(*) FROM building_messages WHERE building_id = :b", b=building_id,
        )

    def _startup(self, **kwargs):
        return retirement.retire_deleted_buildings(
            session_factory=self.Session,
            db_path=str(self.db_path),
            saiverse_home=self.home,
            city_slug=CITY,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 建物を消すとき
# ---------------------------------------------------------------------------

class LiveRetirementTests(_RetirementTestCase):
    def _seed_room_with_everything(self) -> str:
        created = self._create("鉄腕の道具店")
        self.assertIn("(ID: building_1_city_a)", created)
        room = "building_1_city_a"
        self._add(
            Building(CITYID=CITY_ID, BUILDINGID="inn_city_a", BUILDINGNAME="宿"),
            AI(AIID=PERSONA, HOME_CITYID=CITY_ID, AINAME="P1", PRIVATE_ROOM_ID=room),
            Region(REGION_ID="r1_city_a", CITYID=CITY_ID, NAME="R", ENTRANCE_BUILDING_ID=room),
            self._message(room, 1, content=f"{room} の話をしよう"),
            self._message("inn_city_a", 1),
            AddonMessageMetadata(message_id=f"{room}:1", addon_name="tts", key="audio", value="a.wav"),
            PersonaPulseCursor(PERSONA_ID=PERSONA, BUILDING_ID=room, CURSOR_SEQ=1, ENTRY_MARKER_SEQ=1),
            PersonaBuildingState(PERSONA_ID=PERSONA, BUILDING_ID=room, BASELINE_JSON="{}"),
            Episode(EPISODE_ID="ep1", PERSONA_ID=PERSONA, KIND="presence", STARTED_AT=1, BUILDING_ID=room),
            LLMUsageLog(PERSONA_ID=PERSONA, BUILDING_ID=room, MODEL_ID="m", INPUT_TOKENS=1, OUTPUT_TOKENS=1),
            Playbook(name="room_playbook", scope="building", building_id=room, schema_json="{}", nodes_json="[]"),
            PersonaDayPlan(
                persona_id=PERSONA, plan_date="2026-09-29",
                slots_json=json.dumps([{"start": "10:00", "facility": room, "note": f"{room} に行く"}],
                                      ensure_ascii=False),
                created_at=NOW, updated_at=NOW,
            ),
        )
        self._seed_memory(room)
        self._make_folder(room)
        return room

    def test_delete_moves_every_remaining_reference_memory_marks_and_folder(self) -> None:
        room = self._seed_room_with_everything()
        memory_before = self._memory_rows()

        result = self.svc.delete_building(room)

        self.assertFalse(result.startswith("Error"), result)
        self.assertEqual(result, "Building '鉄腕の道具店' deleted successfully.")  # 文面は変えない
        [entry] = self._retire_entries(room)
        tomb = entry["new_id"]
        self.assertRegex(tomb, r"^deleted_building_1_city_a_\d{14}$")
        self.assertEqual((entry["status"], entry["source"]), ("done", "delete"))
        for key in ("planned_at", "db_renamed_at", "done_at"):
            self.assertIn(key, entry)

        # 部屋を指す欄は全部、特殊な ID へ
        for sql in (
            'SELECT "PRIVATE_ROOM_ID" FROM "ai"',
            'SELECT "ENTRANCE_BUILDING_ID" FROM "region"',
            'SELECT "BUILDING_ID" FROM persona_pulse_cursor',
            'SELECT "BUILDING_ID" FROM persona_building_state',
            'SELECT "BUILDING_ID" FROM episodes',
            'SELECT "BUILDING_ID" FROM llm_usage_log',
            "SELECT building_id FROM playbooks",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self._scalar(sql), tomb)
        # 会話: 行は特殊な ID の下、メッセージ ID もその形。元の ID は legacy_message_id に写る
        self.assertEqual(self._message_count(room), 0)
        self.assertEqual(
            self._scalar(
                "SELECT message_id || '|' || legacy_message_id || '|' || content "
                "FROM building_messages WHERE building_id = :b", b=tomb,
            ),
            f"{tomb}:1|{room}:1|{room} の話をしよう",
        )
        self.assertEqual(self._message_count("inn_city_a"), 1)  # 別の部屋は触らない
        self.assertEqual(self._scalar("SELECT message_id FROM addon_message_metadata"), f"{tomb}:1")
        # JSON の欄は値の完全一致だけ (文章の中の ID は変えない)
        self.assertEqual(
            json.loads(self._scalar("SELECT slots_json FROM persona_day_plan")),
            [{"start": "10:00", "facility": tomb, "note": f"{room} に行く"}],
        )

        # 記憶: 印だけ特殊な ID へ。本文・知覚の文面は変えない。複製は書き換える前の中身
        after = self._memory_rows()
        self.assertEqual([r[0] for r in after["buffer"]], [r[0] for r in memory_before["buffer"]])
        self.assertEqual(after["buffer"][0][1], room_key(tomb))
        self.assertEqual(json.loads(after["buffer"][0][2])["to_id"], tomb)
        self.assertEqual([r[:2] for r in after["messages"]], [r[:2] for r in memory_before["messages"]])
        self.assertEqual(
            json.loads(after["messages"][0][2]),
            {"building_msg_ref": f"{tomb}:{tomb}:1", "note": room},
        )
        [backup] = self._memory_backups()
        conn = sqlite3.connect(str(backup))
        try:
            self.assertEqual(
                conn.execute("SELECT metadata FROM messages").fetchone()[0],
                memory_before["messages"][0][2],
            )
        finally:
            conn.close()

        # フォルダは特殊な ID の名前へ移る
        self.assertFalse((self.buildings_root / room).exists())
        self.assertTrue((self.buildings_root / tomb / "log.json").is_file())

        # 元の ID は空いている: 同じ番号の新しい建物に何も戻らない
        self.assertIn(f"(ID: {room})", self._create("霧雨の宿亭"))
        self.assertEqual(self._message_count(room), 0)

    def test_memory_marks_are_written_inside_the_adapter_lock(self) -> None:
        """同じプロセスの記憶の書き手 (adapter の _db_lock) と同じ錠前の中で書く。"""
        from sai_memory.db_locks import lock_for_path

        room = self._seed_room_with_everything()
        lock = lock_for_path(str(self._memory_path()))
        held = []
        real_apply = repair.apply_memory_rewrite

        def checking_apply(conn, updates):
            held.append(lock._is_owned())
            return real_apply(conn, updates)

        with patch("saiverse.building_id_repair.apply_memory_rewrite", side_effect=checking_apply):
            self.assertFalse(self.svc.delete_building(room).startswith("Error"))
        self.assertEqual(held, [True])


class FailureAndReservationTests(_RetirementTestCase):
    ROOM = "building_1_city_a"

    def _seed(self) -> None:
        self.assertIn(f"(ID: {self.ROOM})", self._create("鉄腕の道具店"))
        self._add(
            AI(AIID=PERSONA, HOME_CITYID=CITY_ID, AINAME="P1"),
            self._message(self.ROOM, 1, content="道具店での会話"),
        )
        self._seed_memory(self.ROOM)
        self._make_folder(self.ROOM)

    def test_memory_failure_keeps_the_deletion_reserves_the_id_and_startup_finishes(self) -> None:
        self._seed()
        memory_before = self._memory_rows()

        # 記憶のファイルが別の書き手に握られていて書けない
        with patch(
            "saiverse.building_id_repair.apply_memory_rewrite",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            result = self.svc.delete_building(self.ROOM)

        self.assertFalse(result.startswith("Error"), result)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM "building"'), 0)
        [entry] = self._retire_entries(self.ROOM)
        tomb = entry["new_id"]
        self.assertEqual(entry["status"], "planned")
        self.assertIn("db_renamed_at", entry)
        self.assertEqual(self._message_count(tomb), 1)  # DB は削除と一緒に済んでいる
        self.assertEqual(self._memory_rows(), memory_before)  # 記憶は書き換える前のまま

        # 済むまで元の ID は使わない: 手で付けた ID は理由つきで断り、連番は次の番号へ
        refused = self._create("手作りの部屋", building_id=self.ROOM)
        self.assertTrue(refused.startswith("Error"), refused)
        self.assertIn("has not finished", refused)
        self.assertIn("(ID: building_2_city_a)", self._create("霧雨の宿亭"))

        # 次の起動が続きを終える
        self.assertEqual(self._startup(), [])
        [entry] = self._retire_entries(self.ROOM)
        self.assertEqual(entry["status"], "done")
        self.assertEqual(
            json.loads(self._memory_rows()["messages"][0][2])["building_msg_ref"],
            f"{tomb}:{tomb}:1",
        )
        self.assertTrue((self.buildings_root / tomb / "log.json").is_file())

        # 空いたので、次の日本語名の建物は歯抜けにならず 1 番を使う
        self.assertIn(f"(ID: {self.ROOM})", self._create("星見の塔"))
        self.assertEqual(self._message_count(self.ROOM), 0)

    def test_another_process_on_the_same_db_defers_memory_and_folder_to_startup(self) -> None:
        self._seed()
        memory_before = self._memory_rows()

        with patch(_OWNS_DB, return_value=(True, "pid 1234")):
            self.assertFalse(self.svc.delete_building(self.ROOM).startswith("Error"))

        [entry] = self._retire_entries(self.ROOM)
        self.assertEqual(entry["status"], "planned")
        self.assertEqual(self._memory_rows(), memory_before)
        self.assertTrue((self.buildings_root / self.ROOM).is_dir())
        self.assertEqual(self._memory_backups(), [])

        self.assertEqual(self._startup(), [])
        [entry] = self._retire_entries(self.ROOM)
        self.assertEqual(entry["status"], "done")
        self.assertFalse((self.buildings_root / self.ROOM).exists())
        self.assertTrue((self.buildings_root / entry["new_id"]).is_dir())

    def test_record_that_cannot_be_written_stops_the_deletion(self) -> None:
        self._seed()
        with patch("saiverse.building_retirement._save_record", side_effect=OSError("disk full")):
            result = self.svc.delete_building(self.ROOM)
        self.assertTrue(result.startswith("Error"), result)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM "building"'), 1)
        self.assertEqual(self._message_count(self.ROOM), 1)


class TombstonePrefixTests(_RetirementTestCase):
    def test_custom_ids_starting_with_deleted_are_refused(self) -> None:
        for custom in ("deleted_foo", "Deleted_Foo", "DELETED_x_20260929182800"):
            with self.subTest(custom=custom):
                result = self._create(f"部屋 {custom}", building_id=custom)
                self.assertTrue(result.startswith("Error"), result)
                self.assertIn("reserved", result)
        self.assertEqual(self._scalar('SELECT COUNT(*) FROM "building"'), 0)

    def test_names_whose_slug_starts_with_deleted_get_the_numbered_form(self) -> None:
        self.assertIn("(ID: building_1_city_a)", self._create("Deleted Room"))
        # 「Deleted」だけでも、町の名前と繋ぐと deleted_city_a になるので連番へ
        self.assertIn("(ID: building_2_city_a)", self._create("Deleted"))
        # 途中に deleted を含むだけの名前は、そのまま
        self.assertIn("(ID: not_deleted_city_a)", self._create("Not Deleted"))

    def test_tombstone_id_gets_a_suffix_when_taken(self) -> None:
        taken = {"deleted_building_1_city_a_20260929182800"}
        self.assertEqual(
            retirement.choose_tombstone_id("building_1_city_a", now=NOW, taken=taken, folder_roots=[]),
            "deleted_building_1_city_a_20260929182800_2",
        )
        # 旧い形の ID (「/」入り) の残骸は、フォルダ名に使える形にする
        self.assertEqual(
            retirement.choose_tombstone_id("2/28_city_a", now=NOW, taken=set(), folder_roots=[]),
            "deleted_2_28_city_a_20260929182800",
        )


# ---------------------------------------------------------------------------
# 起動時
# ---------------------------------------------------------------------------

class StartupTests(_RetirementTestCase):
    GONE = "building_9_city_a"

    def _seed_leftovers(self) -> None:
        """この仕組みより前に消した建物の残骸と、触ってはいけない行を並べる。"""
        self._add(
            AI(AIID=PERSONA, HOME_CITYID=CITY_ID, AINAME="P1"),
            Building(CITYID=CITY_ID, BUILDINGID="inn_city_a", BUILDINGNAME="宿"),
            self._message(self.GONE, 1, content="昔の会話"),
            self._message("inn_city_a", 1),
            # 前に付け替え済みの特殊な ID は残骸ではない
            self._message("deleted_building_8_city_a_20260101000000", 1),
            PersonaPulseCursor(PERSONA_ID=PERSONA, BUILDING_ID=self.GONE, CURSOR_SEQ=1),
        )
        self._seed_memory(self.GONE)
        self._make_folder(self.GONE)

    def test_past_leftovers_are_retired_and_a_second_run_does_nothing(self) -> None:
        self._seed_leftovers()
        tomb = f"deleted_{self.GONE}_20260929182800"

        with patch(_OWNS_DB, return_value=(False, "")):
            self.assertEqual(self._startup(now=NOW), [])

        self.assertEqual(self._message_count(self.GONE), 0)
        self.assertEqual(self._message_count(tomb), 1)
        self.assertEqual(self._message_count("inn_city_a"), 1)
        self.assertEqual(self._message_count("deleted_building_8_city_a_20260101000000"), 1)
        self.assertEqual(self._scalar('SELECT "BUILDING_ID" FROM persona_pulse_cursor'), tomb)
        [entry] = self._retire_entries(self.GONE)
        self.assertEqual((entry["new_id"], entry["status"], entry["source"]), (tomb, "done", "leftover"))
        self.assertEqual(
            json.loads(self._memory_rows()["messages"][0][2])["building_msg_ref"],
            f"{tomb}:{tomb}:1",
        )
        self.assertTrue((self.buildings_root / tomb / "log.json").is_file())
        db_backups = list(self.db_path.parent.glob("saiverse.db_backup_building_retirement_*.bak"))
        self.assertEqual(len(db_backups), 1)

        record_before = self._record()
        with patch(_OWNS_DB, return_value=(False, "")):
            self.assertEqual(self._startup(now=datetime(2026, 9, 30)), [])
        self.assertEqual(self._record(), record_before)
        self.assertEqual(
            len(list(self.db_path.parent.glob("saiverse.db_backup_building_retirement_*.bak"))), 1,
        )
        self.assertEqual(len(self._memory_backups()), 1)

    def test_skipped_while_another_process_owns_the_db(self) -> None:
        self._seed_leftovers()
        with patch(_OWNS_DB, return_value=(True, "pid 1234")):
            self.assertEqual(self._startup(now=NOW), [])
        self.assertEqual(self._message_count(self.GONE), 1)
        self.assertFalse(self.record_path.exists())

    def test_planned_entry_whose_building_still_exists_is_closed_untouched(self) -> None:
        """削除が巻き戻った (記録は書いたが commit されなかった) 予定。"""
        self._add(
            AI(AIID=PERSONA, HOME_CITYID=CITY_ID, AINAME="P1"),
            Building(CITYID=CITY_ID, BUILDINGID="shop_city_a", BUILDINGNAME="店"),
            self._message("shop_city_a", 1),
        )
        self._seed_memory("shop_city_a")
        memory_before = self._memory_rows()
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        tomb = "deleted_shop_city_a_20260929182800"
        self.record_path.write_text(json.dumps({"format_version": 1, "renames": [{
            "kind": "retire", "source": "delete", "old_id": "shop_city_a", "new_id": tomb,
            "status": "planned", "planned_at": "2026-09-29T18:28:00+09:00",
        }]}), encoding="utf-8")

        self.assertEqual(self._startup(), [])

        [entry] = self._retire_entries("shop_city_a")
        self.assertEqual((entry["status"], entry["note"]), ("done", "building_kept"))
        self.assertEqual(self._message_count("shop_city_a"), 1)
        self.assertEqual(self._memory_rows(), memory_before)
        self.assertEqual(retirement.pending_retirement_ids(self.home), set())

    def test_planned_entry_whose_id_was_reused_is_closed_with_an_alert(self) -> None:
        """記憶の印が済む前に同じ ID の建物が (作成の口の外から) 作られていた予定。"""
        self._add(
            AI(AIID=PERSONA, HOME_CITYID=CITY_ID, AINAME="P1"),
            Building(CITYID=CITY_ID, BUILDINGID="shop_city_a", BUILDINGNAME="新しい店"),
            self._message("shop_city_a", 1, content="新しい店の会話"),
        )
        self._seed_memory("shop_city_a")
        memory_before = self._memory_rows()
        self._make_folder("shop_city_a")
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        tomb = "deleted_shop_city_a_20260929182800"
        self.record_path.write_text(json.dumps({"format_version": 1, "renames": [{
            "kind": "retire", "source": "delete", "old_id": "shop_city_a", "new_id": tomb,
            "status": "planned", "planned_at": "2026-09-29T18:28:00+09:00",
            "db_renamed_at": "2026-09-29T18:28:01+09:00",
        }]}), encoding="utf-8")

        alerts = self._startup()

        self.assertEqual([a["details"]["reason"] for a in alerts], ["id_reused"])
        [entry] = self._retire_entries("shop_city_a")
        self.assertEqual((entry["status"], entry["note"]), ("done", "id_reused"))
        # 新しい建物のものは動かさない
        self.assertEqual(self._message_count("shop_city_a"), 1)
        self.assertEqual(self._memory_rows(), memory_before)
        self.assertTrue((self.buildings_root / "shop_city_a").is_dir())

    def test_unsafe_id_repair_ignores_retire_entries(self) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        planned = {
            "kind": "retire", "source": "delete", "old_id": "shop_city_a",
            "new_id": "deleted_shop_city_a_20260929182800", "status": "planned",
            "planned_at": "2026-09-29T18:28:00+09:00", "db_renamed_at": "2026-09-29T18:28:01+09:00",
        }
        self.record_path.write_text(
            json.dumps({"format_version": 1, "renames": [planned]}), encoding="utf-8",
        )
        alerts = repair.repair_unsafe_building_ids(
            session_factory=self.Session,
            db_path=str(self.db_path),
            saiverse_home=self.home,
            city_id=CITY_ID,
            city_slug=CITY,
            environ={repair.DISCORD_CHANNEL_MAP_ENV: json.dumps(
                [{"channel_id": "1", "building_id": "shop_city_a"}],
            )},
        )
        self.assertEqual(alerts, [])
        self.assertEqual(self._record()["renames"], [planned])
        self.assertEqual(retirement.pending_retirement_ids(self.home), {"shop_city_a"})


if __name__ == "__main__":
    unittest.main()
