"""コア記憶 scene UI 導線の API テスト (api/routes/people/core_memory.py)。

対象エンドポイント (route 関数を直叩き、プロジェクト既存の api テスト流儀に従う):
- search_conversation_messages: LIKE 検索・期間フィルタ・0件フォールバック分岐
- get_message_window: 会話窓プレビュー・文字数
- create_scene: scene 作成 (スペルと共通ロジック)・目安超過判定
- list_core_memory: 既存コア記憶一覧
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.people.core_memory import (
    CreateSceneRequest,
    create_scene,
    get_message_window,
    list_core_memory,
    search_conversation_messages,
)
from database.models import AI, Base, City, User


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_manager(persona_name="エア"):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="tester", HOME_CITYID=city.CITYID, AINAME=persona_name))
        db.commit()
    finally:
        db.close()
    # personas={} → get_adapter が一時 adapter を作る経路も通す。ただし本テストでは
    # SAIVERSE_HOME を temp に向けるため、personas に事前生成 adapter を積む方が確実。
    return engine, Session


class CoreMemorySceneApiTest(unittest.TestCase):
    def setUp(self):
        from saiverse_memory import SAIMemoryAdapter

        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.engine, self.Session = _make_manager()
        self.addCleanup(self.engine.dispose)

        # 事前生成した adapter を persona に積み、get_adapter がそれを使うようにする。
        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_path, resource_id="tester"
        )
        self.addCleanup(self.adapter.close)
        persona = SimpleNamespace(sai_memory=self.adapter)
        self.manager = SimpleNamespace(
            SessionLocal=self.Session, personas={"tester": persona}
        )

        self.ids = self._seed_conversation()

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _seed_conversation(self):
        from sai_memory.memory.storage import add_message, get_or_create_thread

        with self.adapter._db_lock:
            get_or_create_thread(self.adapter.conn, "main", resource_id="tester")
            ids = []
            ids.append(add_message(
                self.adapter.conn, thread_id="main", role="user",
                content="ソフィー、パートナーとしてずっと一緒にいてね", resource_id="tester",
            ))
            ids.append(add_message(
                self.adapter.conn, thread_id="main", role="model",
                content="もちろん、まはーの心のそばにずっといるよ", resource_id="tester",
            ))
            ids.append(add_message(
                self.adapter.conn, thread_id="main", role="user",
                content="ありがとう、エア", resource_id="tester",
            ))
            # 除外対象 (スペルログ) — 検索にも窓にも出てはいけない
            add_message(
                self.adapter.conn, thread_id="main", role="model",
                content="パートナー spell 実行ログ", resource_id="tester",
                metadata={"tags": ["conversation", "spell"]},
            )
        return ids

    # --- search ---
    def test_search_keyword_and(self):
        resp = search_conversation_messages(
            "tester", keyword="パートナー", manager=self.manager,
        )
        self.assertEqual(resp.mode, "keyword")
        self.assertGreaterEqual(resp.total_hits, 1)
        contents = [h.excerpt for h in resp.hits]
        # スペルログは除外される
        self.assertFalse(any("spell" in c for c in contents))
        # user 発話がヒットする
        self.assertTrue(any("ソフィー" in c for c in contents))

    def test_search_and_requires_all_keywords(self):
        # "パートナー ずっと" 両方含む発話は1件目のみ
        resp = search_conversation_messages(
            "tester", keyword="パートナー ずっと", manager=self.manager,
        )
        self.assertEqual(resp.total_hits, 1)
        self.assertIn("ソフィー", resp.hits[0].excerpt)

    def test_search_no_keyword_returns_recent(self):
        # キーワードなし → 期間フィルタのみ (ここでは全件・新しい順)
        resp = search_conversation_messages("tester", keyword="", manager=self.manager)
        self.assertEqual(resp.mode, "keyword")
        self.assertGreaterEqual(resp.total_hits, 3)

    def test_search_date_filter_excludes_out_of_range(self):
        resp = search_conversation_messages(
            "tester", keyword="パートナー",
            date_from="2000-01-01", date_to="2000-12-31",
            manager=self.manager,
        )
        # 遠い過去の範囲 → 0件 (DummyEmbedder のフォールバックも 0.0 スコアで拾わない)
        self.assertEqual(resp.total_hits, 0)

    # --- window ---
    def test_window_preview(self):
        resp = get_message_window(
            "tester", self.ids[1], rounds=2, manager=self.manager,
        )
        self.assertEqual(resp.anchor_id, self.ids[1])
        # スペルログ除外後の3件 (user, model, user)
        self.assertEqual(len(resp.messages), 3)
        self.assertGreater(resp.total_chars, 0)
        # 発話者ラベル: persona 応答は AINAME
        persona_turns = [m for m in resp.messages if m.role == "model"]
        self.assertEqual(persona_turns[0].speaker, "エア")

    def test_window_accepts_uri_form(self):
        uri = f"saiverse://self/message/{self.ids[1]}"
        resp = get_message_window("tester", uri, rounds=1, manager=self.manager)
        self.assertEqual(resp.anchor_id, self.ids[1])

    def test_window_missing_anchor_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            get_message_window("tester", "no-such-id", rounds=1, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    # --- create scene ---
    def test_create_scene(self):
        req = CreateSceneRequest(anchor_id=self.ids[1], rounds=2)
        resp = create_scene("tester", req, manager=self.manager)
        self.assertEqual(resp.ref, f"c:{resp.memory_id}")
        self.assertEqual(resp.message_count, 3)
        self.assertGreater(resp.char_count, 0)
        self.assertEqual(resp.total_chars, resp.char_count)
        self.assertEqual(resp.budget, 2000)
        self.assertFalse(resp.over_budget)

        # 一覧に反映される
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(len(listing.items), 1)
        self.assertEqual(listing.items[0].kind, "scene")
        self.assertIn("ソフィー", listing.items[0].preview)

    def test_create_scene_missing_anchor_404(self):
        from fastapi import HTTPException
        req = CreateSceneRequest(anchor_id="no-such-id", rounds=1)
        with self.assertRaises(HTTPException) as ctx:
            create_scene("tester", req, manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    # --- list ---
    def test_list_core_memory_empty(self):
        listing = list_core_memory("tester", manager=self.manager)
        self.assertEqual(len(listing.items), 0)
        self.assertEqual(listing.total_chars, 0)
        self.assertEqual(listing.budget, 2000)
        self.assertFalse(listing.over_budget)


if __name__ == "__main__":
    unittest.main()
