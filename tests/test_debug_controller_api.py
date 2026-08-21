"""自律稼働デバッグコントローラー API (api/routes/people/debug.py) の HTTP テスト。

対象は「切り上げ (wrap-up-conversation)」の発火条件。「いま会話中か」の真実は
開いている会話の出来事が持つ (life.md §7 案 Y)。開いている会話が無いまま撃つと、
別の会話あるいは存在しない会話を閉じることになる (2026-08-14 Codex 指摘 F5)。

会話終了判断は 2026-08-16 に退役したので (autonomous_behavior_v3.md §8/§13.3)、
``handle_conversation_end`` が返すのは判断の結果ではなく閉じた帳簿の事実
(``closed`` / ``episode_ref``) になった。

TestClient はワーカースレッドでルートを実行するため、DB は :memory: でなく
file sqlite + check_same_thread=False で共有する (test_life_settings_api.py と
同じ流儀)。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from database.models import AI, Base, City, User
from saiverse import episodes

PERSONA_ID = "alice"
BUILDING = "alice_room"


class WrapUpConversationApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        db_file = Path(self._tmp.name) / "saiverse_test.db"
        self.engine = create_engine(
            f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self._seed()

        self.fired = []
        self.manager = SimpleNamespace(
            SessionLocal=self.Session,
            personas={PERSONA_ID: SimpleNamespace(persona_id=PERSONA_ID)},
        )

        from api.routes.people import debug as debug_route

        # 発火は別スレッドなので、呼ばれたかどうかだけを同期的に記録する。
        self._orig_run_in_background = debug_route._run_in_background
        debug_route._run_in_background = (
            lambda fn, *a, **kw: self.fired.append((fn, a, kw))
        )
        self.addCleanup(
            setattr, debug_route, "_run_in_background", self._orig_run_in_background
        )

        app = FastAPI()
        app.include_router(debug_route.router, prefix="/api/people")
        app.dependency_overrides[get_manager] = lambda: self.manager
        self.client = TestClient(app)

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass  # Windows: sqlite ハンドル解放待ちの既知事情

    def _seed(self):
        db = self.Session()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
            db.flush()
            city = City(USERID=1, CITY_SLUG="city_a", UI_PORT=3001, API_PORT=8001)
            db.add(city)
            db.flush()
            db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="アリス"))
            db.commit()
        finally:
            db.close()

    def _post(self):
        return self.client.post(f"/api/people/{PERSONA_ID}/debug/wrap-up-conversation")

    # -- 撃つ側 ------------------------------------------------------------

    def test_fires_when_conversation_episode_is_open(self):
        ep = episodes.open_conversation_episode(
            self.manager, PERSONA_ID, building_id=BUILDING,
            participants=[PERSONA_ID, "1"],
        )
        res = self._post()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertEqual(len(self.fired), 1)
        # 検証した出来事そのものを背景処理へ渡す (TOCTOU の照合材料)。
        self.assertEqual(
            self.fired[0][2]["expected_episode_ref"], ep["episode_ref"],
        )

    def test_fire_time_check_skips_when_the_conversation_already_ended(self):
        """検証と背景発火の間に会話が閉じたら、発火側で撃たない。

        同期検証 → 背景発火の間に自然タイムアウトや別の切り上げが会話を閉じ、
        さらに新しい会話が開きうる。照合材料を渡さないと発火側は「いま開いている
        会話」を無条件に終わらせ、別の会話 / 存在しない会話を閉じる
        (2026-08-14 Codex 二巡目)。
        """
        from saiverse import autonomy_wiring
        ep = episodes.open_conversation_episode(
            self.manager, PERSONA_ID, building_id=BUILDING,
            participants=[PERSONA_ID, "1"],
        )
        self._post()
        # 背景処理が走る前に別経路が会話を閉じた
        episodes.close_conversation_episode(self.manager, PERSONA_ID)

        result = autonomy_wiring.handle_conversation_end(
            self.manager, PERSONA_ID,
            expected_episode_ref=ep["episode_ref"],
        )
        self.assertFalse(result["closed"])
        self.assertIn("no open conversation episode", result["reason"])

    def test_fire_time_close_targets_only_the_verified_episode(self):
        """照合と close は 1 手 (条件付き) —— 別の会話が開いていても閉じない。

        「読んで照合 → 閉じる」の形だと、その間に別経路が閉じて新しい会話を
        開いた場合に**別の会話を閉じてしまう** (2026-08-14 Codex 三巡目)。
        """
        from saiverse import autonomy_wiring
        first = episodes.open_conversation_episode(
            self.manager, PERSONA_ID, building_id=BUILDING,
            participants=[PERSONA_ID, "1"],
        )
        episodes.close_conversation_episode(self.manager, PERSONA_ID)
        second = episodes.open_conversation_episode(
            self.manager, PERSONA_ID, building_id=BUILDING,
            participants=[PERSONA_ID, "1"],
        )

        result = autonomy_wiring.handle_conversation_end(
            self.manager, PERSONA_ID,
            expected_episode_ref=first["episode_ref"],
        )
        self.assertFalse(result["closed"])
        # 新しい会話は開いたまま (巻き添えで閉じられていない)
        still_open = episodes.get_open_episode(
            self.manager, PERSONA_ID, kind=episodes.KIND_CONVERSATION,
        )
        self.assertEqual(still_open["episode_ref"], second["episode_ref"])

    # -- 撃たない側 --------------------------------------------------------

    def test_rejects_when_no_open_conversation_episode(self):
        """開いている会話が無ければ撃たない。"""
        res = self._post()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fired, [])

    def test_rejects_after_conversation_episode_closed(self):
        episodes.open_conversation_episode(
            self.manager, PERSONA_ID, building_id=BUILDING,
            participants=[PERSONA_ID, "1"],
        )
        episodes.close_conversation_episode(self.manager, PERSONA_ID)
        res = self._post()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fired, [])

    def test_non_conversation_episode_is_not_a_success(self):
        """会話以外の出来事 (作業中など) が開いているだけでは撃たない。"""
        episodes.open_episode(
            self.manager, PERSONA_ID, episodes.KIND_WORK_SESSION,
            building_id=BUILDING,
        )
        res = self._post()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fired, [])


if __name__ == "__main__":
    unittest.main()
