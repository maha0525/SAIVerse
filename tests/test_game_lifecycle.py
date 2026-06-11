"""GameLifecycleService (phase 遷移 / 自動ポーズ・再開 / アーカイブ) のテスト。

manager は fake (必要属性のみ) + 実 sqlite で STATE_JSON 永続化も検証する。
アーカイブ先は saiverse.data_paths.USER_DATA_DIR をパッチして一時ディレクトリへ。
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import saiverse.data_paths as data_paths
from database.models import Base, Region as RegionModel
from saiverse.game_lifecycle import GameLifecycleService
from saiverse.regions import Region


class FakeBuilding:
    def __init__(self, building_id, region_id):
        self.building_id = building_id
        self.name = building_id
        self.description = ""
        self.base_system_instruction = ""
        self.region_id = region_id


class FakePersona:
    def __init__(self, current_building_id=None):
        self.current_building_id = current_building_id


class FakeTrack:
    def __init__(self, track_id, track_type):
        self.track_id = track_id
        self.track_type = track_type


class FakeTrackManager:
    def __init__(self):
        self.created = []
        self.completed = []
        self._tracks = {}

    def create(self, persona_id, track_type, title=None, intent=None,
               metadata=None, initial_status=None, **kwargs):
        track_id = f"t{len(self.created)}"
        self.created.append((persona_id, track_type, initial_status))
        self._tracks.setdefault(persona_id, []).append(FakeTrack(track_id, track_type))
        return track_id

    def list_for_persona(self, persona_id, statuses=None):
        return list(self._tracks.get(persona_id, []))

    def complete(self, track_id):
        self.completed.append(track_id)


class FakeManager:
    def __init__(self, session_factory, regions, personas=None, user_id=1, buildings=None):
        self.SessionLocal = session_factory
        self.regions = regions
        self.personas = personas or {}
        self.user_id = user_id
        self.state = SimpleNamespace(user_current_building_id=None)
        self.buildings = buildings or []
        self.building_map = {b.building_id: b for b in self.buildings}
        self.items_by_building = {}
        self.items = {}
        self.track_manager = FakeTrackManager()
        self.events = []  # add_building_event の記録 (building_id, msg, heard_by)
        self.moved = []   # move_user / summon_persona の記録 (entity_id, to_id)

    def add_building_event(self, building_id, msg, heard_by=None):
        self.events.append((building_id, msg, list(heard_by or [])))
        return dict(msg)

    def move_user(self, target_building_id):
        self.moved.append(("user", target_building_id))
        self.state.user_current_building_id = target_building_id
        return True, "ok"

    def summon_persona(self, persona_id, target_building_id=None):
        self.moved.append((persona_id, target_building_id))
        persona = self.personas.get(persona_id)
        if persona is not None:
            persona.current_building_id = target_building_id
        return True, None

    def _reload_regions(self):
        db = self.SessionLocal()
        try:
            for rid, region in self.regions.items():
                row = db.query(RegionModel).filter_by(REGION_ID=rid).first()
                if row is not None:
                    region.state = json.loads(row.STATE_JSON) if row.STATE_JSON else {}
        finally:
            db.close()

    def get_region(self, region_id):
        return self.regions.get(region_id)

    def get_subregions(self, region_id):
        return []

    def get_region_buildings(self, region_id, include_subregions=True):
        return [b for b in self.buildings if b.region_id == region_id]

    def get_top_region_of_building(self, building_id):
        for b in self.buildings:
            if b.building_id == building_id and b.region_id:
                return self.regions.get(b.region_id)
        return None


class GameLifecycleTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            db.add(RegionModel(
                REGION_ID="r1", CITYID=1, NAME="霧の谷", REGION_TYPE="game",
                RULER_ID="ruler1", LOBBY_BUILDING_ID="lobby",
            ))
            db.commit()
        finally:
            db.close()

        self.region = Region(
            region_id="r1", city_id=1, name="霧の谷", region_type="game",
            ruler_id="ruler1", lobby_building_id="lobby",
        )
        self.personas = {
            "ruler1": FakePersona("lobby"),
            "air": FakePersona("b1"),
        }
        self.buildings = [FakeBuilding("b1", "r1"), FakeBuilding("b2", "r1")]
        self.manager = FakeManager(
            self.SessionLocal, {"r1": self.region},
            personas=self.personas, user_id=1, buildings=self.buildings,
        )
        self.svc = GameLifecycleService(self.manager)

        self.archive_dir = Path(tempfile.mkdtemp())
        self._patcher = patch.object(data_paths, "USER_DATA_DIR", self.archive_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.archive_dir, ignore_errors=True)
        self.engine.dispose()
        os.unlink(self.db_path)

    def _start(self, participants=None):
        if participants is None:
            participants = ["air", "1"]
        return self.svc.start_game("r1", participants)

    # --- start ---

    def test_start_game(self):
        result = self._start()
        self.assertNotIn("Error", result)
        self.assertEqual(self.region.state["phase"], "playing")
        self.assertEqual(self.region.state["participants"], ["air", "1"])
        self.assertTrue(self.region.state["session_id"])
        # DB にも永続化されている
        db = self.SessionLocal()
        try:
            row = db.query(RegionModel).filter_by(REGION_ID="r1").first()
            self.assertEqual(json.loads(row.STATE_JSON)["phase"], "playing")
        finally:
            db.close()

    def test_start_requires_ruler(self):
        self.region.ruler_id = None
        self.assertIn("Error", self._start())

    def test_start_rejects_unknown_participant(self):
        self.assertIn("Error", self._start(["ghost"]))

    def test_start_rejects_empty_participants(self):
        self.assertIn("Error", self._start([]))

    def test_start_rejects_ruler_as_participant(self):
        self.assertIn("Error", self._start(["ruler1"]))

    def test_start_rejects_double_start(self):
        self._start()
        self.assertIn("Error", self._start())

    # --- pause / resume ---

    def test_pause_and_resume(self):
        self._start()
        result = self.svc.pause_game("r1", reason="test")
        self.assertNotIn("Error", result)
        self.assertEqual(self.region.state["phase"], "paused")

        # user (id=1) が Region 外に居ると再開できない
        self.manager.state.user_current_building_id = "outside"
        self.assertIn("Error", self.svc.resume_game("r1"))

        # 全員 Region 内なら再開できる
        self.manager.state.user_current_building_id = "b2"
        result = self.svc.resume_game("r1")
        self.assertNotIn("Error", result)
        self.assertEqual(self.region.state["phase"], "playing")

    def test_pause_requires_playing(self):
        self.assertIn("Error", self.svc.pause_game("r1"))

    # --- end / archive ---

    def test_end_game_archives_and_resets(self):
        self._start()
        session_id = self.region.state["session_id"]
        result = self.svc.end_game("r1", outcome="clear")
        self.assertNotIn("Error", result)

        archive_file = self.archive_dir / "game_sessions" / session_id / "session.json"
        self.assertTrue(archive_file.exists())
        snapshot = json.loads(archive_file.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["outcome"], "clear")
        self.assertEqual(snapshot["region"]["region_id"], "r1")
        self.assertEqual(len(snapshot["buildings"]), 2)
        self.assertEqual(snapshot["state"]["phase"], "playing")  # 終了直前の state

        # state はリセットされ再プレイ可能
        self.assertEqual(self.region.state["phase"], "setup")
        self.assertEqual(self.region.state["participants"], [])
        self.assertEqual(self.region.state["last_session_id"], session_id)
        self.assertEqual(self.region.state["last_outcome"], "clear")
        self.assertNotIn("session_id", self.region.state)

    def test_end_game_invalid_outcome(self):
        self._start()
        self.assertIn("Error", self.svc.end_game("r1", outcome="victory"))

    def test_end_game_requires_active_game(self):
        self.assertIn("Error", self.svc.end_game("r1"))

    def test_end_game_blocked_when_archive_fails(self):
        self._start()
        with patch.object(self.svc, "_archive_session", return_value=(None, "Error: disk full")):
            result = self.svc.end_game("r1")
        self.assertIn("Error", result)
        self.assertEqual(self.region.state["phase"], "playing")  # 閉じていない

    # --- 自動ポーズ / 再開 ---

    def test_auto_pause_on_participant_exit(self):
        self._start()
        self.svc.on_entity_moved("air", "b1", "outside")
        self.assertEqual(self.region.state["phase"], "paused")

    def test_no_pause_on_intra_region_move(self):
        self._start()
        self.svc.on_entity_moved("air", "b1", "b2")
        self.assertEqual(self.region.state["phase"], "playing")

    def test_no_pause_on_non_participant_exit(self):
        self._start()
        self.svc.on_entity_moved("ruler1", "b1", "outside")
        self.assertEqual(self.region.state["phase"], "playing")

    def test_auto_resume_when_all_return(self):
        self._start()
        self.manager.state.user_current_building_id = "b2"
        self.svc.on_entity_moved("air", "b1", "outside")
        self.assertEqual(self.region.state["phase"], "paused")

        # 参加者が戻る (current_building_id も Region 内へ)
        self.personas["air"].current_building_id = "b1"
        self.svc.on_entity_moved("air", "outside", "b1")
        self.assertEqual(self.region.state["phase"], "playing")

    # --- Track 拘束 ---

    def test_start_creates_game_session_track_for_personas_only(self):
        self._start()
        created = self.manager.track_manager.created
        # persona 'air' のみ。user '1' には作らない
        self.assertEqual(len(created), 1)
        persona_id, track_type, initial_status = created[0]
        self.assertEqual(persona_id, "air")
        self.assertEqual(track_type, "game_session")
        self.assertEqual(initial_status, "running")

    def test_end_completes_game_session_tracks(self):
        self._start()
        self.svc.end_game("r1", outcome="gameover")
        self.assertEqual(self.manager.track_manager.completed, ["t0"])

    def test_is_participating(self):
        self.assertFalse(self.svc.is_participating("air"))
        self._start()
        self.assertTrue(self.svc.is_participating("air"))
        self.assertFalse(self.svc.is_participating("ruler1"))
        self.svc.pause_game("r1", reason="test")
        self.assertTrue(self.svc.is_participating("air"))  # paused 中も拘束
        self.manager.state.user_current_building_id = "b2"
        self.svc.end_game("r1", outcome="aborted")
        self.assertFalse(self.svc.is_participating("air"))

    def test_no_resume_while_someone_still_outside(self):
        self._start()
        self.manager.state.user_current_building_id = "outside"
        self.svc.on_entity_moved("air", "b1", "outside")
        self.assertEqual(self.region.state["phase"], "paused")

        self.personas["air"].current_building_id = "b1"
        self.svc.on_entity_moved("air", "outside", "b1")
        self.assertEqual(self.region.state["phase"], "paused")  # user がまだ外

    # --- パーティー移動 ---

    def test_party_follows_user_move(self):
        self._start()
        self.manager.state.user_current_building_id = "b2"
        self.svc.on_entity_moved("1", "lobby", "b2")
        # air と ruler1 が b2 へ追従し、party_location が記録される
        self.assertIn(("air", "b2"), self.manager.moved)
        self.assertIn(("ruler1", "b2"), self.manager.moved)
        self.assertEqual(self.region.state["party_location"], "b2")

    def test_party_does_not_follow_persona_move(self):
        self._start()
        self.svc.on_entity_moved("air", "b1", "b2")
        self.assertEqual(self.manager.moved, [])
        self.assertNotIn("party_location", self.region.state)

    def test_user_return_reassembles_party_and_resumes(self):
        self._start()
        self.manager.state.user_current_building_id = "b1"
        # ユーザー離脱 → 自動ポーズ
        self.manager.state.user_current_building_id = "outside"
        self.svc.on_entity_moved("1", "b1", "outside")
        self.assertEqual(self.region.state["phase"], "paused")
        # ユーザー帰還 → 追従で全員集結 → 自動再開
        self.manager.state.user_current_building_id = "b1"
        self.svc.on_entity_moved("1", "outside", "b1")
        self.assertEqual(self.region.state["phase"], "playing")
        self.assertIn(("ruler1", "b1"), self.manager.moved)
        self.assertEqual(self.region.state["party_location"], "b1")

    def test_move_party_spell_moves_everyone(self):
        self._start()
        self.manager.state.user_current_building_id = "b1"
        result = self.svc.move_party("r1", "b2")
        self.assertNotIn("Error", result)
        self.assertIn(("user", "b2"), self.manager.moved)
        self.assertIn(("air", "b2"), self.manager.moved)
        self.assertIn(("ruler1", "b2"), self.manager.moved)
        self.assertEqual(self.region.state["party_location"], "b2")

    def test_move_party_rejects_foreign_building(self):
        self._start()
        self.assertIn("Error", self.svc.move_party("r1", "outside"))

    def test_move_party_requires_playing(self):
        self.assertIn("Error", self.svc.move_party("r1", "b1"))

    def test_end_game_returns_party_to_lobby(self):
        self._start()
        self.manager.state.user_current_building_id = "b2"
        self.personas["ruler1"].current_building_id = "b2"
        self.svc.end_game("r1", outcome="clear")
        self.assertIn(("air", "lobby"), self.manager.moved)
        self.assertIn(("ruler1", "lobby"), self.manager.moved)
        self.assertIn(("user", "lobby"), self.manager.moved)
        self.assertNotIn("party_location", self.region.state)

    def test_end_game_does_not_teleport_user_from_outside(self):
        self._start()
        self.manager.state.user_current_building_id = "outside"
        self.svc.end_game("r1", outcome="aborted")
        self.assertNotIn(("user", "lobby"), self.manager.moved)
        self.assertIn(("air", "lobby"), self.manager.moved)

    # --- ライフサイクルイベント通知 ---

    def _event_actions(self):
        return [
            msg["metadata"]["event"]["action"]
            for _, msg, _ in self.manager.events
        ]

    def test_lifecycle_events_emitted(self):
        self._start()
        self.assertIn("start", self._event_actions())
        self.svc.set_scene("r1", "battle")
        self.assertIn("scene_change", self._event_actions())
        self.svc.pause_game("r1", reason="test")
        self.assertIn("pause", self._event_actions())
        self.manager.state.user_current_building_id = "b1"
        self.svc.resume_game("r1")
        self.assertIn("resume", self._event_actions())
        self.svc.end_game("r1", outcome="clear")
        self.assertIn("end", self._event_actions())

    def test_event_heard_by_includes_party_and_ruler(self):
        self._start()
        _, _, heard_by = self.manager.events[0]
        self.assertIn("air", heard_by)
        self.assertIn("1", heard_by)
        self.assertIn("ruler1", heard_by)

    # --- active_game_for_user (UI ゲームモード判定) ---

    def test_active_game_none_before_start(self):
        self.manager.state.user_current_building_id = "b1"
        self.assertIsNone(self.svc.active_game_for_user())

    def test_active_game_when_user_inside_region(self):
        self._start()
        self.manager.state.user_current_building_id = "b1"
        info = self.svc.active_game_for_user()
        self.assertIsNotNone(info)
        self.assertEqual(info["region_id"], "r1")
        self.assertEqual(info["phase"], "playing")
        self.assertEqual(info["scene"], "exploration")

    def test_active_game_none_outside_region(self):
        self._start()
        # 控室 (lobby) は Region 外 = セッションログ対象外なのでゲームモードにしない
        self.manager.state.user_current_building_id = "lobby"
        self.assertIsNone(self.svc.active_game_for_user())

    def test_active_game_during_pause(self):
        self._start()
        self.manager.state.user_current_building_id = "b1"
        self.svc.pause_game("r1", reason="test")
        info = self.svc.active_game_for_user()
        self.assertIsNotNone(info)
        self.assertEqual(info["phase"], "paused")


if __name__ == "__main__":
    unittest.main()
