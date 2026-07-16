"""ライフ UI 4画面のバックエンド API テスト。

対象:
- GET /api/episodes                          (api/routes/episodes.py — 画面 A)
- GET /api/people/{id}/day-plan              (api/routes/people/life.py — 画面 B)
- GET /api/people/{id}/clips                (同上 — 画面 C)
- GET /api/people/{id}/profile-tree          (同上 — 画面 D)

一時 DB (temp dir の file sqlite) + 一時 persona dir を使い本番に触れない。
TestClient で HTTP 経由の検証 (クエリ検証・エラー系含む)。TestClient は
ワーカースレッドでルートを実行するため、DB は :memory: でなく file sqlite +
check_same_thread=False で共有する。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import AI, Base, Building, City, Episode, User

TZ_NAME = "Asia/Tokyo"
TZ = ZoneInfo(TZ_NAME)
TEST_DATE = "2026-07-06"


def _epoch(hh: int, mm: int = 0, day: str = TEST_DATE) -> int:
    """TEST_DATE (Asia/Tokyo) の hh:mm を epoch 秒にする。"""
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp())


class DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class LifeViewApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.addCleanup(self._cleanup_temp)

        # --- 一時 saiverse DB (file sqlite: TestClient のワーカースレッドと共有) ---
        db_file = tmp_path / "saiverse_test.db"
        self.engine = create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self._seed_world()

        # --- 一時 persona memory.db (marks 用) ---
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)
        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter

        persona_dir = tmp_path / "personas" / "air"
        persona_dir.mkdir(parents=True, exist_ok=True)
        self.adapter = SAIMemoryAdapter(
            "air", persona_dir=persona_dir, resource_id="air"
        )
        self.addCleanup(self.adapter.close)

        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas={"air": SimpleNamespace(sai_memory=self.adapter)},
        )

        # --- TestClient (episodes + people/life ルーターのみの薄いアプリ) ---
        from api.routes import episodes as episodes_route
        from api.routes.people import life as life_route

        app = FastAPI()
        app.include_router(episodes_route.router, prefix="/api/episodes")
        app.include_router(life_route.router, prefix="/api/people")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    def _seed_world(self):
        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city_a = City(
                USERID=1, CITYNAME="city_a", UI_PORT=3001, API_PORT=8001,
                TIMEZONE=TZ_NAME,
            )
            city_b = City(
                USERID=1, CITYNAME="city_b", UI_PORT=3002, API_PORT=8002,
                TIMEZONE="UTC",
            )
            db.add_all([city_a, city_b])
            db.flush()
            self.city_a_id = city_a.CITYID
            self.city_b_id = city_b.CITYID
            db.add(Building(
                CITYID=city_a.CITYID, BUILDINGID="cafe", BUILDINGNAME="カフェ",
            ))
            db.add_all([
                AI(AIID="air", HOME_CITYID=city_a.CITYID, AINAME="エア",
                   AVATAR_IMAGE="user_data/icons/air.png"),
                AI(AIID="quon", HOME_CITYID=city_a.CITYID, AINAME="クオン"),
                AI(AIID="stranger", HOME_CITYID=city_b.CITYID, AINAME="よその子"),
            ])
            db.commit()
        finally:
            db.close()

    def _seed_episodes(self):
        """TEST_DATE (Asia/Tokyo) の episodes を直接 INSERT する。"""
        rows = [
            # air: 午前のコマ実績 (closed, カフェ)
            Episode(
                EPISODE_ID="ep-air-1", PERSONA_ID="air", SHORT_ID=1, KIND="slot",
                STARTED_AT=_epoch(9), ENDED_AT=_epoch(10), BUILDING_ID="cafe",
                PARTICIPANTS_JSON=json.dumps(["air"]), ORIGIN_REF="day_plan:0",
                STATUS="closed", META_JSON=json.dumps({"note": "詩作"}),
            ),
            # air / quon: 同一の世界的できごと (occurrence で束ねる会話)
            Episode(
                EPISODE_ID="ep-air-2", PERSONA_ID="air", SHORT_ID=2,
                KIND="conversation", OCCURRENCE_ID="occ-1",
                STARTED_AT=_epoch(13), ENDED_AT=_epoch(13, 30),
                BUILDING_ID="cafe", STATUS="closed",
            ),
            Episode(
                EPISODE_ID="ep-quon-1", PERSONA_ID="quon", SHORT_ID=1,
                KIND="conversation", OCCURRENCE_ID="occ-1",
                STARTED_AT=_epoch(13, 5), ENDED_AT=_epoch(13, 30),
                BUILDING_ID="cafe", STATUS="closed",
            ),
            # air: 開きっぱなしのできごと (「いま」行。ended_at なし)
            Episode(
                EPISODE_ID="ep-air-3", PERSONA_ID="air", SHORT_ID=3,
                KIND="presence", STARTED_AT=_epoch(15), ENDED_AT=None,
                STATUS="open",
            ),
            # air: 前日のできごと (窓の外 → 出ない)
            Episode(
                EPISODE_ID="ep-air-old", PERSONA_ID="air", SHORT_ID=4,
                KIND="stroll", STARTED_AT=_epoch(9, 0, "2026-07-05"),
                ENDED_AT=_epoch(10, 0, "2026-07-05"), STATUS="closed",
            ),
            # 別 City のペルソナのできごと (city_a のタイムラインに出ない)
            Episode(
                EPISODE_ID="ep-stranger-1", PERSONA_ID="stranger", SHORT_ID=1,
                KIND="slot", STARTED_AT=_epoch(9), ENDED_AT=_epoch(10),
                STATUS="closed",
            ),
        ]
        db = self.Session()
        try:
            db.add_all(rows)
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # A: GET /api/episodes
    # ------------------------------------------------------------------

    def test_episodes_timeline(self):
        self._seed_episodes()
        resp = self.client.get(
            "/api/episodes", params={"city_id": self.city_a_id, "date": TEST_DATE},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["date"], TEST_DATE)
        self.assertEqual(body["timezone"], TZ_NAME)
        self.assertEqual(body["day_start"], _epoch(0))
        self.assertEqual(body["day_end"], _epoch(0, 0, "2026-07-07"))

        eps = body["episodes"]
        ids = [e["episode_id"] for e in eps]
        # 前日の行と別 City の行は出ない
        self.assertNotIn("ep-air-old", ids)
        self.assertNotIn("ep-stranger-1", ids)
        self.assertEqual(set(ids), {"ep-air-1", "ep-air-2", "ep-quon-1", "ep-air-3"})

        # 同一 occurrence の行は隣接する (grouping key = occurrence_id)
        occ_positions = [i for i, e in enumerate(eps) if e["group_key"] == "occ-1"]
        self.assertEqual(len(occ_positions), 2)
        self.assertEqual(occ_positions[1] - occ_positions[0], 1)

        by_id = {e["episode_id"]: e for e in eps}
        # 表示名解決
        self.assertEqual(by_id["ep-air-1"]["persona_name"], "エア")
        self.assertEqual(by_id["ep-air-1"]["building_name"], "カフェ")
        self.assertEqual(
            by_id["ep-air-1"]["persona_avatar_url"],
            "/api/static/user_icons/air.png",
        )
        self.assertEqual(by_id["ep-quon-1"]["persona_name"], "クオン")
        # occurrence 無し行の group_key は episode_id (単独グループ)
        self.assertEqual(by_id["ep-air-1"]["group_key"], "ep-air-1")
        # open 行 (「いま」行) も返り、ended_at は None
        self.assertEqual(by_id["ep-air-3"]["status"], "open")
        self.assertIsNone(by_id["ep-air-3"]["ended_at"])
        self.assertIsNone(by_id["ep-air-3"]["ended_at_iso"])
        # epoch と ISO の両方が返る (ISO は City タイムゾーン)
        self.assertEqual(by_id["ep-air-1"]["started_at"], _epoch(9))
        self.assertTrue(by_id["ep-air-1"]["started_at_iso"].startswith("2026-07-06T09:00"))

    def test_episodes_empty_day(self):
        resp = self.client.get(
            "/api/episodes", params={"city_id": self.city_a_id, "date": TEST_DATE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["episodes"], [])

    def test_episodes_invalid_params(self):
        # 不正な日付 → 400
        resp = self.client.get(
            "/api/episodes", params={"city_id": self.city_a_id, "date": "07/06"},
        )
        self.assertEqual(resp.status_code, 400)
        # 未知の City → 404
        resp = self.client.get("/api/episodes", params={"city_id": 9999})
        self.assertEqual(resp.status_code, 404)
        # city_id 欠落 → 422 (FastAPI のクエリ検証)
        resp = self.client.get("/api/episodes")
        self.assertEqual(resp.status_code, 422)

    # ------------------------------------------------------------------
    # B: GET /api/people/{id}/day-plan
    # ------------------------------------------------------------------

    def test_day_plan(self):
        from saiverse.day_plan import consume_budget, init_budget_ledger, save_day_plan

        save_day_plan(self.manager, "air", TEST_DATE, [
            {"start": "08:00", "kind": "作る", "ref": "none",
             "facility": "own_room", "budget_rounds": 4,
             "title": "詩を書く", "note": "朝の静けさで", "status": "done"},
            {"start": "12:00", "kind": "暮らし", "ref": "none",
             "facility": "cafe", "budget_rounds": 0, "title": "昼を過ごす",
             "note": "", "status": "done", "record_level": "presence_only"},
            {"start": "15:00", "kind": "知る", "ref": "none",
             "facility": "cafe", "budget_rounds": 4, "title": "調べ物",
             "note": "", "status": "skipped", "skip_reason": "budget_exhausted"},
        ])
        init_budget_ledger(self.manager, "air", TEST_DATE, 10)
        consume_budget(self.manager, "air", TEST_DATE, 3)

        resp = self.client.get(
            "/api/people/air/day-plan", params={"date": TEST_DATE},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["persona_id"], "air")
        self.assertEqual(body["date"], TEST_DATE)
        self.assertEqual(body["budget"], {"total": 10, "used": 3, "remaining": 7})

        slots = body["slots"]
        self.assertEqual(len(slots), 3)
        self.assertEqual(
            [s["index"] for s in slots], [0, 1, 2],
        )
        self.assertEqual(slots[0]["start"], "08:00")
        self.assertEqual(slots[0]["title"], "詩を書く")
        self.assertEqual(slots[0]["status"], "done")
        self.assertEqual(slots[0]["result_label"], "実行済み")
        # presence_only の done は「実行済み」と偽らない
        self.assertEqual(slots[1]["record_level"], "presence_only")
        self.assertEqual(slots[1]["result_label"], "時間を過ごした（詳細な記録なし）")
        # skipped はシステム都合が明示される
        self.assertEqual(slots[2]["skip_reason"], "budget_exhausted")
        self.assertIn("予算切れ", slots[2]["result_label"])

    def test_day_plan_empty_day(self):
        resp = self.client.get(
            "/api/people/air/day-plan", params={"date": "2026-01-01"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["slots"], [])
        self.assertIsNone(body["budget"])

    def test_day_plan_errors(self):
        resp = self.client.get(
            "/api/people/air/day-plan", params={"date": "not-a-date"},
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/people/nobody/day-plan")
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # B-2: lives / life_status (life.md §9.1/§9.2, Phase4 見せ方)
    # ------------------------------------------------------------------

    def test_day_plan_lives_and_life_status_in_life(self):
        from saiverse import clock
        from saiverse.day_plan import consume_life_pulse, save_day_plan, save_lives

        save_day_plan(self.manager, "air", TEST_DATE, [
            {"start": "08:00", "kind": "作る", "ref": "none", "facility": "own_room",
             "budget_rounds": 4, "title": "詩を書く", "note": ""},
            {"start": "15:00", "kind": "知る", "ref": "none", "facility": "cafe",
             "budget_rounds": 4, "title": "調べ物", "note": ""},
        ])
        save_lives(self.manager, "air", TEST_DATE, [
            {"start": "07:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
            {"start": "14:00", "end": "20:00", "budget_pulses": 8, "mode": "free"},
        ])
        consume_life_pulse(self.manager, "air", TEST_DATE, at_time="08:00")

        clock.enable_virtual(datetime(2026, 7, 6, 9, 30))  # ライフ0の区間内
        try:
            resp = self.client.get(
                "/api/people/air/day-plan", params={"date": TEST_DATE},
            )
        finally:
            clock.disable_virtual()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        lives = body["lives"]
        self.assertEqual(len(lives), 2)
        self.assertEqual(lives[0]["start"], "07:00")
        self.assertEqual(lives[0]["end"], "12:00")
        self.assertEqual(lives[0]["mode"], "free")
        self.assertEqual(lives[0]["budget_pulses"], 6)
        self.assertEqual(lives[0]["used_pulses"], 1)
        self.assertEqual(lives[0]["used_rounds"], 0)
        self.assertEqual(lives[0]["consumed"], 1.0)
        self.assertEqual(lives[0]["remaining"], 5.0)

        self.assertEqual(body["life_status"], {
            "lives_declared": True, "in_life": True, "life_index": 0,
        })

    def test_day_plan_life_status_in_valley(self):
        from saiverse import clock
        from saiverse.day_plan import save_day_plan, save_lives

        save_day_plan(self.manager, "air", TEST_DATE, [
            {"start": "08:00", "kind": "作る", "ref": "none", "facility": "own_room",
             "budget_rounds": 4, "title": "詩を書く", "note": ""},
            {"start": "15:00", "kind": "知る", "ref": "none", "facility": "cafe",
             "budget_rounds": 4, "title": "調べ物", "note": ""},
        ])
        save_lives(self.manager, "air", TEST_DATE, [
            {"start": "07:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
            {"start": "14:00", "end": "20:00", "budget_pulses": 8, "mode": "free"},
        ])

        clock.enable_virtual(datetime(2026, 7, 6, 13, 0))  # 二つのライフの間 (谷)
        try:
            resp = self.client.get(
                "/api/people/air/day-plan", params={"date": TEST_DATE},
            )
        finally:
            clock.disable_virtual()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["lives"]), 2)
        # 谷 = 宣言はあるが in_life=False (「未宣言」とは区別される)
        self.assertEqual(body["life_status"], {
            "lives_declared": True, "in_life": False, "life_index": None,
        })

    def test_day_plan_life_status_undeclared(self):
        """lives 未宣言の日は lives_declared=False (life_status 自体は省略しない)。"""
        from saiverse import clock

        clock.enable_virtual(datetime(2026, 7, 6, 9, 30))
        try:
            resp = self.client.get(
                "/api/people/air/day-plan", params={"date": TEST_DATE},
            )
        finally:
            clock.disable_virtual()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["lives"], [])
        self.assertEqual(body["life_status"], {
            "lives_declared": False, "in_life": False, "life_index": None,
        })

    def test_day_plan_life_status_none_for_non_current_date(self):
        """過去日を眺めているときは life_status 自体を返さない (嘘の「いま」を出さない)。"""
        from saiverse import clock

        clock.enable_virtual(datetime(2026, 7, 6, 9, 30))  # 「いま」= 2026-07-06
        try:
            resp = self.client.get(
                "/api/people/air/day-plan", params={"date": "2026-01-01"},
            )
        finally:
            clock.disable_virtual()
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["life_status"])

    # ------------------------------------------------------------------
    # C: GET /api/people/{id}/clips
    # ------------------------------------------------------------------

    def test_clips_batch(self):
        from sai_memory.clips import add_clip

        with self.adapter._db_lock:
            add_clip(self.adapter.conn, message_id="m1",
                      quote="言葉の標本", purpose_ref="task:3")
            add_clip(self.adapter.conn, message_id="m1", quote="夕方の音")
            add_clip(self.adapter.conn, message_id="m2",
                      quote="朝の光", purpose_ref=None)
            add_clip(self.adapter.conn, message_id="m9", quote="対象外")

        resp = self.client.get(
            "/api/people/air/clips", params={"message_ids": "m1, m2, missing"},
        )
        self.assertEqual(resp.status_code, 200)
        clips = resp.json()["clips"]
        self.assertEqual(len(clips), 3)  # m9 は要求外、missing は 0 件
        self.assertEqual({p["message_id"] for p in clips}, {"m1", "m2"})
        m1_clips = [p for p in clips if p["message_id"] == "m1"]
        self.assertEqual({p["quote"] for p in m1_clips}, {"言葉の標本", "夕方の音"})
        refs = {p["quote"]: p["purpose_ref"] for p in clips}
        self.assertEqual(refs["言葉の標本"], "task:3")
        self.assertIsNone(refs["夕方の音"])
        for p in clips:
            self.assertIn("clip_id", p)
            self.assertIsInstance(p["created_at"], int)

    def test_clips_empty_result(self):
        resp = self.client.get(
            "/api/people/air/clips", params={"message_ids": "no-such-id"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["clips"], [])

    def test_clips_errors(self):
        # 空の message_ids → 400
        resp = self.client.get(
            "/api/people/air/clips", params={"message_ids": " , ,"},
        )
        self.assertEqual(resp.status_code, 400)
        # 上限超過 (101 件) → 400
        too_many = ",".join(f"m{i}" for i in range(101))
        resp = self.client.get(
            "/api/people/air/clips", params={"message_ids": too_many},
        )
        self.assertEqual(resp.status_code, 400)
        # message_ids 欠落 → 422
        resp = self.client.get("/api/people/air/clips")
        self.assertEqual(resp.status_code, 422)
        # 未知ペルソナ → 404
        resp = self.client.get(
            "/api/people/nobody/clips", params={"message_ids": "m1"},
        )
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------------
    # D: GET /api/people/{id}/profile-tree
    # ------------------------------------------------------------------

    def test_profile_tree(self):
        from saiverse.persona_task_manager import PersonaTaskManager
        from saiverse.purpose_tree import create_candidate, name_theme
        from saiverse.track_manager import TrackManager

        tm = TrackManager(session_factory=self.Session)
        track_id = tm.create(
            "air", track_type="autonomous", title="言葉集め",
            intent="街の言葉を標本にする", initial_status="running",
        )

        # 候補 (やってみたいこと)。来歴 = 接地参照つき
        cand = create_candidate(
            self.manager, "air", "歌をつくってみたい", source_ref="episode:2",
        )
        # 命名テーマ = 親なし採用ノード (第一階層)
        theme = name_theme(self.manager, "air", "夕暮れの観察", [cand["ref"]])
        # アーカイブ済み候補は candidates から消える
        archived = create_candidate(
            self.manager, "air", "やめた候補", source_ref="episode:3",
        )
        PersonaTaskManager(self.Session).update_task_status(
            archived["id"], status="cancelled", actor="test", persona_id="air",
        )

        resp = self.client.get("/api/people/air/profile-tree")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["persona_id"], "air")

        first = {n["title"]: n for n in body["first_tier"]}
        # 第一階層 = 生きている Track + 親なし採用ノード (候補は含まれない)
        self.assertIn("言葉集め", first)
        self.assertIn("夕暮れの観察", first)
        self.assertNotIn("歌をつくってみたい", first)
        track_node = first["言葉集め"]
        self.assertEqual(track_node["node_kind"], "track")
        self.assertEqual(track_node["id"], track_id)
        self.assertIsNotNone(track_node["last_activity_at"])  # running 化で刻まれる
        theme_node = first["夕暮れの観察"]
        self.assertEqual(theme_node["node_kind"], "task")
        # 命名の来歴 (§15 自己所有感の担保)
        self.assertEqual(theme_node["promoted_from"], [cand["ref"]])
        self.assertEqual(theme_node["id"], theme["id"])

        cands = body["candidates"]
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["title"], "歌をつくってみたい")
        self.assertEqual(cands[0]["stage"], "candidate")
        self.assertEqual(cands[0]["source_ref"], "episode:2")
        self.assertEqual(cands[0]["ref"], cand["ref"])

    def test_profile_tree_empty(self):
        resp = self.client.get("/api/people/quon/profile-tree")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["first_tier"], [])
        self.assertEqual(body["candidates"], [])

    def test_profile_tree_unknown_persona(self):
        resp = self.client.get("/api/people/nobody/profile-tree")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
