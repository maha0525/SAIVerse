"""自律稼働デバッグコントローラー API (api/routes/people/debug.py) の HTTP テスト。

対象は「切り上げ (wrap-up-conversation)」の発火条件。「いま会話中か」の真実は
**メモリ内の会話状態**が持つ (autonomous_behavior_v3.md §7、束 6c)。開いている
会話が無いまま撃つと、別の会話あるいは存在しない会話を閉じることになる
(2026-08-14 Codex 指摘 F5)。

会話終了判断は 2026-08-16 に退役したので (v3 §8/§13.3)、``handle_conversation_end``
が返すのは判断の結果ではなく閉じた帳簿の事実 (``closed`` / ``conversation_id``)。

⚠ 「会話以外の出来事が開いているだけでは撃たない」の回帰は対象消滅した — 出来事の
行を作る書き手が全滅したので (v3 §7)、その状態を作り出す手段が無い。

TestClient はワーカースレッドでルートを実行するため、DB は :memory: でなく
file sqlite + check_same_thread=False で共有する。
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
from saiverse import user_conversation as uc

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
        # 会話状態は manager にぶら下がるので、テストごとに新しい manager =
        # 空の状態から始まる。退避先 (_FALLBACK_STATE) は使われないが念のため。
        uc._FALLBACK_STATE.clear()
        self.addCleanup(uc._FALLBACK_STATE.clear)

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

    def _open_conversation(self):
        return uc._set_open_conversation(
            self.manager, PERSONA_ID,
            building_id=BUILDING, participants=[PERSONA_ID, "1"],
        )

    def _post(self):
        return self.client.post(f"/api/people/{PERSONA_ID}/debug/wrap-up-conversation")

    # -- 撃つ側 ------------------------------------------------------------

    def test_fires_when_a_conversation_is_open(self):
        conv = self._open_conversation()
        res = self._post()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertEqual(len(self.fired), 1)
        # 検証した会話そのものを背景処理へ渡す (TOCTOU の照合材料)。
        self.assertEqual(
            self.fired[0][2]["expected_conversation_id"], conv["conversation_id"],
        )

    def test_fire_time_check_skips_when_the_conversation_already_ended(self):
        """検証と背景発火の間に会話が閉じたら、発火側で撃たない。

        同期検証 → 背景発火の間に自然タイムアウトや別の切り上げが会話を閉じ、
        さらに新しい会話が開きうる。照合材料を渡さないと発火側は「いま開いている
        会話」を無条件に終わらせ、別の会話 / 存在しない会話を閉じる
        (2026-08-14 Codex 二巡目)。
        """
        from saiverse import autonomy_wiring

        conv = self._open_conversation()
        self._post()
        # 背景処理が走る前に別経路が会話を閉じた
        uc.clear_open_conversation(self.manager, PERSONA_ID)

        result = autonomy_wiring.handle_conversation_end(
            self.manager, PERSONA_ID,
            expected_conversation_id=conv["conversation_id"],
        )
        self.assertFalse(result["closed"])
        self.assertIn("no open conversation", result["reason"])

    def test_fire_time_close_targets_only_the_verified_conversation(self):
        """照合と解除は 1 手 (条件付き) —— 別の会話が開いていても閉じない。

        「読んで照合 → 閉じる」の形だと、その間に別経路が閉じて新しい会話を
        開いた場合に**別の会話を閉じてしまう** (2026-08-14 Codex 三巡目)。
        """
        from saiverse import autonomy_wiring

        first = self._open_conversation()
        uc.clear_open_conversation(self.manager, PERSONA_ID)
        second = self._open_conversation()

        result = autonomy_wiring.handle_conversation_end(
            self.manager, PERSONA_ID,
            expected_conversation_id=first["conversation_id"],
        )
        self.assertFalse(result["closed"])
        # 新しい会話は開いたまま (巻き添えで閉じられていない)
        still_open = uc.get_open_conversation(self.manager, PERSONA_ID)
        self.assertEqual(
            still_open["conversation_id"], second["conversation_id"],
        )

    # -- 撃たない側 --------------------------------------------------------

    def test_rejects_when_no_conversation_is_open(self):
        """開いている会話が無ければ撃たない。"""
        res = self._post()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fired, [])

    def test_rejects_after_the_conversation_was_closed(self):
        self._open_conversation()
        uc.clear_open_conversation(self.manager, PERSONA_ID)
        res = self._post()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fired, [])


if __name__ == "__main__":
    unittest.main()
