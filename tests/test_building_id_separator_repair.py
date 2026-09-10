"""部屋 ID に区切り記号 (/ \\) を含む部屋を、起動時に付け替えること。

docs/issues/building_id_contains_path_separator.md。ここで固定するのは:

- 新しい ID の決め方 (置き換え・大文字小文字・既存の行・既存のフォルダ・安全でない ID)
- JSON の欄は値の完全一致だけを置き換える (文章中の ID は変えない、辞書のキーも対象)
- 部屋を指す欄・メッセージ ID・アドオンのメタデータを書き換え、legacy_message_id は変えない
- 手順 1・2・3 のそれぞれの後で止まった状態から、次の起動で完了できる
- 多重起動・バックアップの失敗で見送り、DB の失敗では元に戻す
- Discord の対応表に旧 ID が残っていれば知らせる
- N さんの形 (部屋「2/28」) で、付け替えの後に過去ログが警告なしで移る
- 確認処理が取り込み処理と同じ関数で場所を決め、「/」入りの ID を check_failed にする

すべて一時フォルダの SAIVERSE_HOME と一時ファイルの SQLite で動かす (本番は触らない)。
"""
from __future__ import annotations

import gc
import json
import os
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
    AddonConfig,
    AddonMessageMetadata,
    AddonPersonaConfig,
    Base,
    Building,
    BuildingMessage,
    BuildingOccupancyLog,
    BuildingToolLink,
    Episode,
    ExecutionLedgerEntry,
    ExecutionOutboxItem,
    Fixture,
    ItemLocation,
    LLMUsageLog,
    PersonaBuildingState,
    PersonaDayPlan,
    PersonaEventLog,
    PersonaPulseCursor,
    PersonaSchedule,
    PersonaTimetableTemplate,
    PhenomenonRule,
    Playbook,
    RealtimeSpellBinding,
    Region,
    User,
)
from manager.ids import is_safe_path_component
from manager.initialization import InitializationMixin
from saiverse import building_id_repair as repair
from saiverse.legacy_log_import import find_log_files, scan_legacy_log_deficits

CITY = "city_a"
CITY_ID = 1
OLD = "2/28_city_a"
NEW = "2_28_city_a"
NOW = datetime(2026, 9, 1, 10, 0, 0)


class _Manager(InitializationMixin):
    """起動の Phase 1 に必要な属性だけを持たせた最小の器。"""

    def __init__(self, session_local, db_path: Path) -> None:
        self.SessionLocal = session_local
        self.db_path = str(db_path)
        self.city_id = CITY_ID
        self.city_name = CITY
        self.startup_alerts = []

    def load_buildings_like_startup(self, home: Path) -> None:
        """_init_buildings / _init_file_paths の代わりに、DB から部屋を読み直す。"""
        db = self.SessionLocal()
        try:
            rows = db.query(Building).filter(Building.CITYID == self.city_id).all()
            self.buildings = [SimpleNamespace(building_id=r.BUILDINGID) for r in rows]
            self.building_map = {
                r.BUILDINGID: SimpleNamespace(name=r.BUILDINGNAME) for r in rows
            }
        finally:
            db.close()
        self.saiverse_home = home


class _RepairTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._home_tmp = tempfile.TemporaryDirectory(prefix="saiverse_home_")
        self.home = Path(self._home_tmp.name)
        self.addCleanup(self._home_tmp.cleanup)
        # get_saiverse_home と多重起動の確認が読む先を一時フォルダへ向ける
        env = patch.dict(os.environ, {"SAIVERSE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(repair.DISCORD_CHANNEL_MAP_ENV, None)

        self._db_tmp = tempfile.TemporaryDirectory(prefix="saiverse_db_")
        self.addCleanup(self._cleanup_db_dir)
        self.db_path = Path(self._db_tmp.name) / "saiverse.db"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
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

    # -- 置き場所 ------------------------------------------------------------

    @property
    def buildings_root(self) -> Path:
        return self.home / "cities" / CITY / "buildings"

    @property
    def oldest_buildings_root(self) -> Path:
        """もっと古い形の置き場 (~/.saiverse/buildings)。"""
        return self.home / "buildings"

    @property
    def record_path(self) -> Path:
        return self.home / "cities" / CITY / repair.RENAMES_FILENAME

    # -- 組み立て ------------------------------------------------------------

    def _add(self, *objects) -> None:
        db = self.SessionLocal()
        try:
            db.add_all(objects)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _building(building_id: str, name: str, city_id: int = CITY_ID) -> Building:
        return Building(CITYID=city_id, BUILDINGID=building_id, BUILDINGNAME=name)

    @staticmethod
    def _message(building_id: str, seq: int, message_id: str, content: str = "こんにちは", **extra):
        return BuildingMessage(
            building_id=building_id, seq=seq, role="user", content=content,
            timestamp="2026-09-01T10:00:00", heard_by="[]", ingested_by="[]",
            message_id=message_id, **extra,
        )

    def _make_folder(self, root: Path, building_id: str, files=("log.json",)) -> Path:
        # 旧 ID の区切り記号は、この OS のフォルダの段の区切りになる (v0.2 と同じ組み立て方)
        folder = root / building_id
        folder.mkdir(parents=True, exist_ok=True)
        for name in files:
            (folder / name).write_text("[]", encoding="utf-8")
        return folder

    def _write_record(self, *entries: dict) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        self.record_path.write_text(
            json.dumps({"format_version": 1, "renames": list(entries)}, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _planned(new_id: str = NEW, **extra) -> dict:
        entry = {
            "old_id": OLD, "new_id": new_id, "building_name": "2/28",
            "status": "planned", "planned_at": "2026-09-11T10:00:00+09:00",
        }
        entry.update(extra)
        return entry

    # -- 読み出し ------------------------------------------------------------

    def _scalar(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def _all(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).fetchall()

    def _building_ids(self):
        return sorted(r[0] for r in self._all('SELECT "BUILDINGID" FROM "building"'))

    def _record(self) -> dict:
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def _run(self, environ=None):
        return repair.repair_building_ids_with_path_separators(
            session_factory=self.SessionLocal,
            db_path=str(self.db_path),
            saiverse_home=self.home,
            city_id=CITY_ID,
            city_slug=CITY,
            environ={} if environ is None else environ,
        )


# ---------------------------------------------------------------------------
# 部品
# ---------------------------------------------------------------------------

class ChooseNewIdTests(unittest.TestCase):
    def test_separators_become_underscores(self) -> None:
        self.assertEqual(repair.choose_new_building_id(OLD, taken=set(), folder_roots=[]), NEW)
        self.assertEqual(
            repair.choose_new_building_id("a\\b/c_city_a", taken=set(), folder_roots=[]),
            "a_b_c_city_a",
        )

    def test_taken_ids_get_a_numbered_suffix(self) -> None:
        self.assertEqual(
            repair.choose_new_building_id(OLD, taken={NEW, f"{NEW}_2"}, folder_roots=[]),
            f"{NEW}_3",
        )

    def test_existing_folder_gets_a_numbered_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / NEW).mkdir()
            self.assertEqual(
                repair.choose_new_building_id(OLD, taken=set(), folder_roots=[root]),
                f"{NEW}_2",
            )

    def test_recorded_id_is_kept_only_while_it_is_free(self) -> None:
        self.assertEqual(
            repair.choose_new_building_id(OLD, taken=set(), folder_roots=[], preferred=f"{NEW}_2"),
            f"{NEW}_2",
        )
        self.assertEqual(
            repair.choose_new_building_id(
                OLD, taken={f"{NEW}_2"}, folder_roots=[], preferred=f"{NEW}_2",
            ),
            NEW,
        )

    def test_unsafe_result_is_returned_for_the_caller_to_reject(self) -> None:
        new_id = repair.choose_new_building_id("a/b.", taken=set(), folder_roots=[])
        self.assertEqual(new_id, "a_b.")
        self.assertFalse(is_safe_path_component(new_id))

    def test_legacy_folder_parts(self) -> None:
        self.assertEqual(repair.legacy_folder_parts(OLD), ["2", "28_city_a"])
        # 素の結合が別の部屋のフォルダや buildings の外を指しうる形は、場所を決めない
        for bad in ("a//b", "a/", "/a", "a/../b", "./a"):
            with self.subTest(bad=bad):
                self.assertIsNone(repair.legacy_folder_parts(bad))


class ReplaceExactStringsTests(unittest.TestCase):
    REPLACEMENTS = {OLD: NEW, f"{OLD}:5": f"{NEW}:5"}

    def test_exact_values_are_replaced(self) -> None:
        value = {"facility": OLD, "ref": f"{OLD}:5", "rooms": [OLD, "salon_city_a"]}
        new, changed = repair.replace_exact_strings(value, self.REPLACEMENTS)
        self.assertTrue(changed)
        self.assertEqual(
            new, {"facility": NEW, "ref": f"{NEW}:5", "rooms": [NEW, "salon_city_a"]},
        )

    def test_ids_inside_sentences_and_unmapped_ids_are_not_touched(self) -> None:
        value = {"note": f"{OLD} に行く", "ref": f"{OLD}:6"}
        new, changed = repair.replace_exact_strings(value, self.REPLACEMENTS)
        self.assertFalse(changed)
        self.assertIs(new, value)

    def test_dictionary_keys_are_replaced(self) -> None:
        new, changed = repair.replace_exact_strings({OLD: {"to": [OLD]}}, self.REPLACEMENTS)
        self.assertTrue(changed)
        self.assertEqual(new, {NEW: {"to": [NEW]}})

    def test_key_is_kept_when_the_target_key_already_exists(self) -> None:
        value = {OLD: 1, NEW: 2}
        new, changed = repair.replace_exact_strings(value, self.REPLACEMENTS)
        self.assertFalse(changed)
        self.assertEqual(new, {OLD: 1, NEW: 2})

    def test_non_string_values_are_left_alone(self) -> None:
        new, changed = repair.replace_exact_strings([1, 2.5, None, True], self.REPLACEMENTS)
        self.assertFalse(changed)
        self.assertEqual(new, [1, 2.5, None, True])


# ---------------------------------------------------------------------------
# DB とフォルダの付け替え
# ---------------------------------------------------------------------------

class RenameTests(_RepairTestCase):
    def _seed_everything(self) -> None:
        self._add(
            self._building(OLD, "2/28"),
            self._building("salon_city_a", "サロン"),
            User(USERID=1, PASSWORD="x", USERNAME="まはー", CURRENT_BUILDINGID=OLD),
            AI(AIID="p1_city_a", HOME_CITYID=CITY_ID, AINAME="P1", PRIVATE_ROOM_ID=OLD),
            Region(
                REGION_ID="r1_city_a", CITYID=CITY_ID, NAME="R", ENTRANCE_BUILDING_ID=OLD,
                STATE_JSON=json.dumps({"scene": OLD, "note": f"{OLD} で集合"}, ensure_ascii=False),
                CONFIG_JSON=json.dumps({"adjacency": {OLD: ["salon_city_a"]}}),
            ),
            ItemLocation(ITEM_ID="item_in_room", OWNER_KIND="building", OWNER_ID=OLD, SLOT_NUMBER=1),
            ItemLocation(ITEM_ID="item_of_persona", OWNER_KIND="persona", OWNER_ID=OLD, SLOT_NUMBER=1),
            Fixture(FIXTURE_ID="fx1", BUILDING_ID=OLD, NAME="時計"),
            RealtimeSpellBinding(OWNER_KIND="building", OWNER_ID=OLD, SPELL_NAME="weather"),
            RealtimeSpellBinding(OWNER_KIND="persona", OWNER_ID=OLD, SPELL_NAME="weather"),
            self._message(OLD, 1, f"{OLD}:1", content=f"{OLD} の話をしよう"),
            self._message(OLD, -1, f"{OLD}:-1", legacy_seq=1, legacy_message_id=f"{OLD}:old1"),
            BuildingMessage(
                building_id="salon_city_a", seq=1, role="host", content="移動しました",
                timestamp="2026-09-01T10:00:00", heard_by="[]", ingested_by="[]",
                event_type="occupancy",
                event_data=json.dumps({"from_building_id": OLD, "to_building_id": "salon_city_a"}),
                message_id="salon_city_a:1",
            ),
            AddonMessageMetadata(message_id=f"{OLD}:1", addon_name="tts", key="audio", value="a.wav"),
            AddonMessageMetadata(message_id=f"{OLD}:old1", addon_name="tts", key="audio", value="b.wav"),
            PersonaPulseCursor(PERSONA_ID="p1_city_a", BUILDING_ID=OLD, CURSOR_SEQ=1, ENTRY_MARKER_SEQ=1),
            PersonaBuildingState(PERSONA_ID="p1_city_a", BUILDING_ID=OLD, BASELINE_JSON="{}"),
            BuildingOccupancyLog(CITYID=CITY_ID, BUILDINGID=OLD, AIID="p1_city_a", ENTRY_TIMESTAMP=NOW),
            Episode(EPISODE_ID="ep1", PERSONA_ID="p1_city_a", KIND="presence", STARTED_AT=1, BUILDING_ID=OLD),
            LLMUsageLog(PERSONA_ID="p1_city_a", BUILDING_ID=OLD, MODEL_ID="m", INPUT_TOKENS=1, OUTPUT_TOKENS=1),
            Playbook(name="room_playbook", scope="building", building_id=OLD, schema_json="{}", nodes_json="[]"),
            BuildingToolLink(BUILDINGID=OLD, TOOLID=1),
            PersonaDayPlan(
                persona_id="p1_city_a", plan_date="2026-09-01",
                slots_json=json.dumps(
                    [{"start": "10:00", "facility": OLD, "note": f"{OLD} に行く"}], ensure_ascii=False,
                ),
                created_at=NOW, updated_at=NOW,
            ),
            PersonaTimetableTemplate(
                PERSONA_ID="p1_city_a", SLOTS_JSON=json.dumps([{"start": "09:00", "facility": OLD}]),
                CREATED_AT=NOW, UPDATED_AT=NOW,
            ),
            PhenomenonRule(
                TRIGGER_TYPE="persona_move", CONDITION_JSON=json.dumps({"to_building": OLD}),
                PHENOMENON_NAME="chime",
            ),
            PersonaSchedule(
                PERSONA_ID="p1_city_a", SCHEDULE_TYPE="oneshot", META_PLAYBOOK="meta_auto",
                PLAYBOOK_PARAMS=json.dumps({"building_id": OLD}),
            ),
            ExecutionLedgerEntry(EXECUTION_ID="ex1", KIND="building.ingest", STATUS="applied", CREATED_AT=1, UPDATED_AT=1),
            ExecutionOutboxItem(
                EXECUTION_ID="ex1", TARGET="building.ingest_mark", PERSONA_ID="p1_city_a",
                PAYLOAD_JSON=json.dumps({"building_id": OLD, "message_id": f"{OLD}:1"}),
                STATUS="pending", CREATED_AT=1,
            ),
            ExecutionOutboxItem(
                EXECUTION_ID="ex1", TARGET="building.ingest_mark", PERSONA_ID="p1_city_a",
                PAYLOAD_JSON=json.dumps({"building_id": OLD}), STATUS="delivered", CREATED_AT=1,
            ),
            PersonaEventLog(PERSONA_ID="p1_city_a", CONTENT="届いた", PAYLOAD=json.dumps({"building": OLD})),
            AddonConfig(addon_name="addon1", params_json=json.dumps({"rooms": [OLD]})),
            AddonPersonaConfig(addon_name="addon1", persona_id="p1_city_a", params_json=json.dumps({"home": OLD})),
        )

    def test_every_reference_to_the_room_is_rewritten(self) -> None:
        self._seed_everything()
        self._make_folder(self.buildings_root, OLD, files=("log.json", "log.json.corrupted_20260426"))
        self._make_folder(self.oldest_buildings_root, OLD)

        alerts = self._run()

        self.assertEqual(alerts, [])
        self.assertEqual(self._building_ids(), sorted([NEW, "salon_city_a"]))
        # 表示名は変えない
        self.assertEqual(
            self._scalar('SELECT "BUILDINGNAME" FROM "building" WHERE "BUILDINGID" = :b', b=NEW),
            "2/28",
        )

        for sql in (
            'SELECT "CURRENT_BUILDINGID" FROM "user"',
            'SELECT "PRIVATE_ROOM_ID" FROM "ai"',
            'SELECT "ENTRANCE_BUILDING_ID" FROM "region"',
            "SELECT \"OWNER_ID\" FROM item_location WHERE \"ITEM_ID\" = 'item_in_room'",
            'SELECT "BUILDING_ID" FROM fixture',
            "SELECT \"OWNER_ID\" FROM realtime_spell_binding WHERE \"OWNER_KIND\" = 'building'",
            'SELECT "BUILDING_ID" FROM persona_pulse_cursor',
            'SELECT "BUILDING_ID" FROM persona_building_state',
            'SELECT "BUILDINGID" FROM building_occupancy_log',
            'SELECT "BUILDING_ID" FROM episodes',
            'SELECT "BUILDING_ID" FROM llm_usage_log',
            "SELECT building_id FROM playbooks",
            'SELECT "BUILDINGID" FROM building_tool_link',
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self._scalar(sql), NEW)
        # 部屋以外の持ち主は変えない
        self.assertEqual(
            self._scalar("SELECT \"OWNER_ID\" FROM item_location WHERE \"ITEM_ID\" = 'item_of_persona'"),
            OLD,
        )
        self.assertEqual(
            self._scalar("SELECT \"OWNER_ID\" FROM realtime_spell_binding WHERE \"OWNER_KIND\" = 'persona'"),
            OLD,
        )

        # 部屋の会話: メッセージ ID は新しい部屋 ID の形。legacy_message_id と本文は元のまま
        rows = self._all(
            "SELECT seq, message_id, legacy_message_id, content FROM building_messages "
            "WHERE building_id = :b ORDER BY seq",
            b=NEW,
        )
        self.assertEqual(
            [(r.seq, r.message_id, r.legacy_message_id) for r in rows],
            [(-1, f"{NEW}:-1", f"{OLD}:old1"), (1, f"{NEW}:1", None)],
        )
        self.assertEqual(rows[1].content, f"{OLD} の話をしよう")
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM building_messages WHERE building_id = :b", b=OLD), 0,
        )
        self.assertEqual(
            json.loads(self._scalar(
                "SELECT event_data FROM building_messages WHERE building_id = 'salon_city_a'"
            )),
            {"from_building_id": NEW, "to_building_id": "salon_city_a"},
        )

        # アドオンのメタデータ: 付け替えたメッセージ ID に合わせる。対応表に無い値は変えない
        self.assertEqual(
            sorted(r[0] for r in self._all("SELECT message_id FROM addon_message_metadata")),
            sorted([f"{NEW}:1", f"{OLD}:old1"]),
        )

        # JSON の欄: 完全一致だけを置き換える
        def loads(sql):
            return json.loads(self._scalar(sql))

        self.assertEqual(loads('SELECT "STATE_JSON" FROM region'), {"scene": NEW, "note": f"{OLD} で集合"})
        self.assertEqual(loads('SELECT "CONFIG_JSON" FROM region'), {"adjacency": {NEW: ["salon_city_a"]}})
        self.assertEqual(
            loads("SELECT slots_json FROM persona_day_plan"),
            [{"start": "10:00", "facility": NEW, "note": f"{OLD} に行く"}],
        )
        self.assertEqual(
            loads('SELECT "SLOTS_JSON" FROM persona_timetable_template'),
            [{"start": "09:00", "facility": NEW}],
        )
        self.assertEqual(loads('SELECT "CONDITION_JSON" FROM phenomenon_rule'), {"to_building": NEW})
        self.assertEqual(loads('SELECT "PLAYBOOK_PARAMS" FROM persona_schedule'), {"building_id": NEW})
        self.assertEqual(
            loads("SELECT \"PAYLOAD_JSON\" FROM execution_outbox WHERE \"STATUS\" = 'pending'"),
            {"building_id": NEW, "message_id": f"{NEW}:1"},
        )
        # 配達済みの記録は実行時点で凍結されたまま
        self.assertEqual(
            loads("SELECT \"PAYLOAD_JSON\" FROM execution_outbox WHERE \"STATUS\" = 'delivered'"),
            {"building_id": OLD},
        )
        self.assertEqual(loads('SELECT "PAYLOAD" FROM persona_event_log'), {"building": NEW})
        self.assertEqual(loads("SELECT params_json FROM addon_config"), {"rooms": [NEW]})
        self.assertEqual(loads("SELECT params_json FROM addon_persona_config"), {"home": NEW})

        # フォルダ: 1 段に戻り、脇のファイルも一緒に移り、空になった途中のフォルダは消える
        self.assertTrue((self.buildings_root / NEW / "log.json").is_file())
        self.assertTrue((self.buildings_root / NEW / "log.json.corrupted_20260426").is_file())
        self.assertFalse((self.buildings_root / "2").exists())
        self.assertTrue((self.oldest_buildings_root / NEW / "log.json").is_file())
        self.assertFalse((self.oldest_buildings_root / "2").exists())

        # 記録は「完了」で残る
        [entry] = self._record()["renames"]
        self.assertEqual((entry["old_id"], entry["new_id"], entry["status"]), (OLD, NEW, "done"))
        for key in ("planned_at", "db_renamed_at", "done_at"):
            self.assertIn(key, entry)

        # 付け替えの前に控えを取った
        backups = list(self.db_path.parent.glob("saiverse.db_backup_building_id_repair_*.bak"))
        self.assertEqual(len(backups), 1)

    def test_existing_room_id_that_matches_ignoring_case_gets_a_suffix(self) -> None:
        self._add(self._building(OLD, "2/28"), self._building("2_28_City_A", "別の部屋"))
        self.assertEqual(self._run(), [])
        self.assertEqual(self._building_ids(), sorted(["2_28_City_A", f"{NEW}_2"]))

    def test_leftover_rows_of_a_deleted_room_get_a_suffix(self) -> None:
        """削除済みの部屋の会話が残っている ID には付け替えない (その会話が混ざるため)。"""
        self._add(
            self._building(OLD, "2/28"),
            self._message(NEW, 1, f"{NEW}:1", content="消えた部屋の会話"),
        )
        self.assertEqual(self._run(), [])
        self.assertEqual(self._building_ids(), [f"{NEW}_2"])
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM building_messages WHERE building_id = :b", b=NEW), 1,
        )

    def test_existing_folder_gets_a_suffix(self) -> None:
        self._add(self._building(OLD, "2/28"))
        self._make_folder(self.buildings_root, NEW)
        self.assertEqual(self._run(), [])
        self.assertEqual(self._building_ids(), [f"{NEW}_2"])

    def test_unsafe_new_id_is_not_renamed_and_is_reported(self) -> None:
        self._add(self._building("a/b.", "a/b."))
        alerts = self._run()
        self.assertEqual([a["details"]["reason"] for a in alerts], ["unsafe_new_id"])
        self.assertEqual(self._building_ids(), ["a/b."])
        self.assertFalse(self.record_path.exists())

    def test_other_cities_and_normal_rooms_are_not_touched(self) -> None:
        self._add(
            self._building("x/y_city_b", "x/y", city_id=2),
            self._building("salon_city_a", "サロン"),
        )
        self.assertEqual(self._run(), [])
        self.assertEqual(self._building_ids(), sorted(["salon_city_a", "x/y_city_b"]))
        self.assertFalse(self.record_path.exists())

    def test_backslash_is_replaced_too(self) -> None:
        old = "a\\b_city_a"
        self._add(self._building(old, "a\\b"))
        self._make_folder(self.buildings_root, old)
        self.assertEqual(self._run(), [])
        self.assertEqual(self._building_ids(), ["a_b_city_a"])
        self.assertTrue((self.buildings_root / "a_b_city_a" / "log.json").is_file())
        self.assertFalse((self.buildings_root / old).exists())

    def test_second_startup_does_nothing(self) -> None:
        self._add(self._building(OLD, "2/28"))
        self._make_folder(self.buildings_root, OLD)
        self.assertEqual(self._run(), [])
        with patch("database.backup.backup_saiverse_db") as backup:
            self.assertEqual(self._run(), [])
        backup.assert_not_called()
        self.assertEqual(len(self._record()["renames"]), 1)


class ResumeTests(_RepairTestCase):
    def test_resumes_from_the_database_after_stopping_at_step_1(self) -> None:
        self._add(self._building(OLD, "2/28"), self._message(OLD, 1, f"{OLD}:1"))
        self._make_folder(self.buildings_root, OLD)
        self._write_record(self._planned())

        self.assertEqual(self._run(), [])

        self.assertEqual(self._building_ids(), [NEW])
        self.assertEqual(self._scalar("SELECT message_id FROM building_messages"), f"{NEW}:1")
        self.assertTrue((self.buildings_root / NEW / "log.json").is_file())
        [entry] = self._record()["renames"]
        self.assertEqual(entry["status"], "done")

    def test_moves_the_folders_after_stopping_at_step_2(self) -> None:
        self._add(self._building(NEW, "2/28"), self._message(NEW, 1, f"{NEW}:1"))
        self._make_folder(self.buildings_root, OLD)
        self._write_record(self._planned())

        with patch("database.backup.backup_saiverse_db") as backup:
            self.assertEqual(self._run(), [])

        backup.assert_not_called()  # DB はもう書き換えない
        self.assertEqual(self._building_ids(), [NEW])
        self.assertEqual(self._scalar("SELECT message_id FROM building_messages"), f"{NEW}:1")
        self.assertTrue((self.buildings_root / NEW / "log.json").is_file())
        self.assertFalse((self.buildings_root / "2").exists())
        [entry] = self._record()["renames"]
        self.assertEqual(entry["status"], "done")
        self.assertIn("db_renamed_at", entry)

    def test_moves_the_rest_after_stopping_in_the_middle_of_step_3(self) -> None:
        self._add(self._building(NEW, "2/28"))
        self._make_folder(self.buildings_root, NEW)  # 一つ目は移し終わった
        (self.buildings_root / "2").mkdir()  # 途中のフォルダを消す前に止まった
        self._make_folder(self.oldest_buildings_root, OLD)  # 二つ目はまだ
        self._write_record(self._planned())

        self.assertEqual(self._run(), [])

        self.assertTrue((self.buildings_root / NEW / "log.json").is_file())
        self.assertFalse((self.buildings_root / "2").exists())
        self.assertTrue((self.oldest_buildings_root / NEW / "log.json").is_file())
        self.assertFalse((self.oldest_buildings_root / "2").exists())
        self.assertEqual(self._record()["renames"][0]["status"], "done")

    def test_recorded_id_that_is_no_longer_free_is_decided_again(self) -> None:
        self._add(self._building(OLD, "2/28"), self._building(NEW, "後から作られた部屋"))
        self._write_record(self._planned())

        self.assertEqual(self._run(), [])

        self.assertEqual(self._building_ids(), sorted([NEW, f"{NEW}_2"]))
        [entry] = self._record()["renames"]
        self.assertEqual((entry["new_id"], entry["status"]), (f"{NEW}_2", "done"))

    def test_same_new_id_is_reused_when_only_the_database_was_restored(self) -> None:
        """控えから DB だけを戻すと、部屋は旧 ID に戻るがフォルダは新しい場所にある。"""
        self._add(self._building(OLD, "2/28"))
        self._make_folder(self.buildings_root, NEW)
        self._write_record(self._planned(status="done", db_renamed_at="x", done_at="x"))

        self.assertEqual(self._run(), [])

        self.assertEqual(self._building_ids(), [NEW])
        self.assertTrue((self.buildings_root / NEW / "log.json").is_file())
        self.assertEqual(
            [(e["new_id"], e["status"]) for e in self._record()["renames"]],
            [(NEW, "done"), (NEW, "done")],
        )


class SkipAndFailureTests(_RepairTestCase):
    def test_skips_while_another_process_owns_the_database(self) -> None:
        self._add(self._building(OLD, "2/28"))
        self._make_folder(self.buildings_root, OLD)
        with patch(
            "saiverse.runtime_marker.another_running_process_owns_db",
            return_value=(True, "verified SAIVerse City 'city_a' process pid 1"),
        ) as owns:
            alerts = self._run()

        owns.assert_called_once_with(str(self.db_path))
        self.assertEqual([a["id"] for a in alerts], ["building_id_repair_skipped_running"])
        self.assertIn("「2/28」", alerts[0]["message"])
        self.assertEqual(self._building_ids(), [OLD])
        self.assertTrue((self.buildings_root / OLD / "log.json").is_file())
        self.assertFalse(self.record_path.exists())

    def test_skips_when_the_backup_cannot_be_made(self) -> None:
        self._add(self._building(OLD, "2/28"))
        with patch("database.backup.backup_saiverse_db", side_effect=RuntimeError("disk full")):
            alerts = self._run()
        self.assertEqual([a["id"] for a in alerts], ["building_id_repair_skipped_backup"])
        self.assertEqual(self._building_ids(), [OLD])
        self.assertFalse(self.record_path.exists())

    def test_database_failure_rolls_everything_back(self) -> None:
        """一意制約にぶつかったら、この回の付け替えをまるごと諦めて元に戻す。"""
        self._add(
            self._building(OLD, "2/28"),
            self._message(OLD, 1, f"{OLD}:1"),
            AddonMessageMetadata(message_id=f"{OLD}:1", addon_name="tts", key="audio", value="a.wav"),
            # 会話の行は無いのに、新しいメッセージ ID を指すメタデータだけが残っている
            AddonMessageMetadata(message_id=f"{NEW}:1", addon_name="tts", key="audio", value="stale.wav"),
        )
        self._make_folder(self.buildings_root, OLD)

        alerts = self._run()

        self.assertEqual([a["id"] for a in alerts], ["building_id_repair_failed"])
        self.assertEqual(self._building_ids(), [OLD])
        self.assertEqual(self._scalar("SELECT building_id FROM building_messages"), OLD)
        self.assertEqual(self._scalar("SELECT message_id FROM building_messages"), f"{OLD}:1")
        self.assertEqual(
            sorted(r[0] for r in self._all("SELECT message_id FROM addon_message_metadata")),
            sorted([f"{OLD}:1", f"{NEW}:1"]),
        )
        self.assertTrue((self.buildings_root / OLD / "log.json").is_file())
        [entry] = self._record()["renames"]
        self.assertEqual(entry["status"], "planned")  # 次の起動でやり直す

    def test_folder_that_cannot_be_moved_keeps_the_plan_open(self) -> None:
        """移す先に同じ名前のフォルダがあったら、どちらも消さず、予定のまま残して知らせる。"""
        self._add(self._building(NEW, "2/28"))
        self._make_folder(self.buildings_root, OLD)
        self._make_folder(self.buildings_root, NEW, files=("other.txt",))
        self._write_record(self._planned())

        alerts = self._run()

        self.assertEqual([a["details"]["reason"] for a in alerts], ["folder_move_failed"])
        self.assertTrue((self.buildings_root / OLD / "log.json").is_file())
        self.assertTrue((self.buildings_root / NEW / "other.txt").is_file())
        self.assertEqual(self._record()["renames"][0]["status"], "planned")

    def test_startup_continues_when_the_repair_itself_raises(self) -> None:
        mgr = _Manager(self.SessionLocal, self.db_path)
        with patch(
            "saiverse.building_id_repair.repair_building_ids_with_path_separators",
            side_effect=RuntimeError("boom"),
        ):
            mgr._repair_building_ids_with_path_separators()
        self.assertEqual([a["id"] for a in mgr.startup_alerts], ["building_id_repair_failed"])


class DiscordMappingTests(_RepairTestCase):
    @staticmethod
    def _env(payload) -> dict:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return {repair.DISCORD_CHANNEL_MAP_ENV: raw}

    @staticmethod
    def _entry(building_id: str) -> dict:
        return {"channel_id": "123", "city_id": CITY, "building_id": building_id, "host_user_id": "9"}

    def test_old_id_left_in_the_mapping_is_reported_on_every_startup(self) -> None:
        self._add(self._building(OLD, "2/28"))
        env = self._env([self._entry(OLD)])

        alerts = self._run(environ=env)

        self.assertEqual([a["id"] for a in alerts], ["building_id_repair_discord_mapping"])
        self.assertIn(f"`{OLD}` を `{NEW}` に書き換えてください", alerts[0]["message"])
        # 付け替えるものが無い次の起動でも、書き換えるまで知らせ続ける
        self.assertEqual(
            [a["id"] for a in self._run(environ=env)], ["building_id_repair_discord_mapping"],
        )

    def test_dictionary_form_of_the_mapping_is_read_too(self) -> None:
        self._add(self._building(OLD, "2/28"))
        entry = self._entry(OLD)
        entry.pop("channel_id")
        alerts = self._run(environ=self._env({"123": entry}))
        self.assertEqual([a["id"] for a in alerts], ["building_id_repair_discord_mapping"])

    def test_mapping_that_already_uses_the_new_id_is_not_reported(self) -> None:
        self._add(self._building(OLD, "2/28"))
        self.assertEqual(self._run(environ=self._env([self._entry(NEW)])), [])

    def test_broken_mapping_is_skipped_without_a_banner(self) -> None:
        self._add(self._building(OLD, "2/28"))
        self.assertEqual(self._run(environ=self._env("{壊れている")), [])


# ---------------------------------------------------------------------------
# 起動の経路を一続きに通す
# ---------------------------------------------------------------------------

class NSanRoomEndToEndTests(_RepairTestCase):
    """N さんの形: 部屋「2/28」、2 段のフォルダに 23 行の log.json (同じ message_id が 2 組)、DB の会話 0 行。"""

    @staticmethod
    def _n_san_log() -> list:
        numbers = list(range(1, 22))  # 21 種類の message_id
        numbers = numbers[:5] + [5] + numbers[5:12] + [12] + numbers[12:]  # 5 と 12 が 2 回 → 23 行
        return [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"古い発言 {i}",
                "seq": n,
                "message_id": f"{OLD}:{n}",
                "timestamp": f"2025-02-28T10:{i:02d}:00",
                "heard_by": [],
            }
            for i, n in enumerate(numbers)
        ]

    def _startup(self) -> _Manager:
        mgr = _Manager(self.SessionLocal, self.db_path)
        mgr._repair_building_ids_with_path_separators()
        mgr.load_buildings_like_startup(self.home)
        mgr._check_legacy_building_log_import()
        return mgr

    def test_room_is_renamed_and_its_history_is_imported_without_a_banner(self) -> None:
        self._add(self._building(OLD, "2/28"), self._building("user_room_city_a", "自室"))
        log = self._n_san_log()
        self.assertEqual(len(log), 23)
        self.assertEqual(len({m["message_id"] for m in log}), 21)
        folder = self.buildings_root / OLD
        folder.mkdir(parents=True)
        (folder / "log.json").write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")

        mgr = self._startup()

        self.assertEqual(mgr.startup_alerts, [])
        rows = self._all(
            "SELECT building_id, seq, message_id, legacy_message_id, content "
            "FROM building_messages ORDER BY seq"
        )
        self.assertEqual(len(rows), 23)
        self.assertEqual({r.building_id for r in rows}, {NEW})
        self.assertEqual([r.content for r in rows], [m["content"] for m in log])
        self.assertTrue(all(r.message_id.startswith(f"{NEW}:") for r in rows))
        self.assertEqual([r.legacy_message_id for r in rows], [m["message_id"] for m in log])
        self.assertFalse((self.buildings_root / "2").exists())
        self.assertTrue((self.buildings_root / NEW / "log.json").is_file())
        [entry] = self._record()["renames"]
        self.assertEqual((entry["old_id"], entry["new_id"], entry["status"]), (OLD, NEW, "done"))

        # 次の起動: 付け替えも取り込みも起きず、警告も出ない
        again = self._startup()
        self.assertEqual(again.startup_alerts, [])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM building_messages"), 23)


class CheckAndImportUseTheSameFolderRuleTests(_RepairTestCase):
    def _nested_log(self) -> None:
        folder = self.buildings_root / OLD
        folder.mkdir(parents=True)
        (folder / "log.json").write_text(
            json.dumps([{
                "role": "user", "content": "a", "seq": 1,
                "message_id": f"{OLD}:1", "timestamp": "2025-02-28T10:00:00",
            }]),
            encoding="utf-8",
        )

    def test_check_reports_check_failed_for_an_id_with_a_separator(self) -> None:
        self._nested_log()
        db = self.SessionLocal()
        try:
            deficits = scan_legacy_log_deficits(db, self.home, CITY, [OLD])
        finally:
            db.close()
        self.assertEqual(len(deficits), 1)
        self.assertEqual(deficits[0]["kind"], "check_failed")
        self.assertIsNone(deficits[0]["missing"])
        self.assertIsNone(deficits[0]["path"])
        # 取り込み処理も同じ名前を拒む — 二つの結果が食い違わない
        self.assertEqual(find_log_files(self.home, city_filter=CITY, building_filter=OLD), [])

    def test_skipped_repair_shows_check_failed_not_an_unfixable_deficit(self) -> None:
        """付け替えを見送った起動では「移せませんでした」ではなく「確認できませんでした」が出る。"""
        self._add(self._building(OLD, "2/28"))
        self._nested_log()
        mgr = _Manager(self.SessionLocal, self.db_path)
        with patch(
            "saiverse.runtime_marker.another_running_process_owns_db", return_value=(True, "pid 1"),
        ):
            mgr._repair_building_ids_with_path_separators()
        mgr.load_buildings_like_startup(self.home)
        mgr._check_legacy_building_log_import()

        self.assertEqual(
            [(a["id"], (a.get("details") or {}).get("kind")) for a in mgr.startup_alerts],
            [
                ("building_id_repair_skipped_running", None),
                (f"legacy_log_deficit_{OLD}", "check_failed"),
            ],
        )
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM building_messages"), 0)


if __name__ == "__main__":
    unittest.main()
