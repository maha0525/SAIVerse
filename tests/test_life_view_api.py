"""暮らしビュー API に残った 1 本 (点クリップ) のテスト。

対象:
- GET /api/people/{id}/clips  (api/routes/people/life.py — 画面 C)

**退役した仲間の 404 も一緒に固定する** — 消したつもりのルートが別経路で生き
残っていないことの回帰:

- GET /api/people/{id}/profile-tree  (2026-08-21、目的の木が概念ごと消滅 — v3 §9-5)
- GET /api/people/{id}/day-plan      (2026-08-22 束 6c、読み手のライフビューと
                                      できごと UI を v0.3 で隠した — v3 §11)
- GET /api/episodes                  (同上。エピソードの行が退役 — v3 §7)

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
from database.models import AI, Base, Building, City, User

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

        # --- TestClient (people/life ルーターのみの薄いアプリ) ---
        from api.routes.people import life as life_route

        app = FastAPI()
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
                USERID=1, CITY_SLUG="city_a", UI_PORT=3001, API_PORT=8001,
                TIMEZONE=TZ_NAME,
            )
            city_b = City(
                USERID=1, CITY_SLUG="city_b", UI_PORT=3002, API_PORT=8002,
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
    # 退役したルートの 404 (消し忘れの回帰)
    # ------------------------------------------------------------------

    def test_retired_routes_are_gone(self):
        """profile-tree / day-plan / episodes はもうどこにも生えていない。

        いずれも供給する概念か読み手ごと退役した (docstring の一覧を参照)。
        ルートだけが生き残ると、動かない画面が「壊れている」顔で残る。
        """
        for path in (
            "/api/people/air/profile-tree",
            "/api/people/air/day-plan",
            "/api/episodes",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
