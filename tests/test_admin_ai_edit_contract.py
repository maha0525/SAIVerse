"""ペルソナ編集 (`AdminService.get_ai_details` / `update_ai`) の契約。

ワールドエディタとペルソナ設定モーダルの保存経路は、API (`api/routes/world.py` /
`api/routes/people/config.py`) → `SAIVerseManager` → `AdminService` の一本しかない。
それにもかかわらず、2026-08-12 まで同名メソッドの複製が `PersonaMixin` にも残っていて、
新しい設定項目が片方にだけ足される状態が続いていた (経緯:
`docs/issues/archive/persona_mixin_ai_edit_dead_duplicate.md`)。複製を撤去した上で、
本物の契約——どの項目が DB へ往復するか、省略した項目は既存値を触らないこと、
拒否すべき更新は DB を変えずに断ること——をここで固定する。

`AdminService.__new__` でインスタンスを組むのは、`update_ai` が実際に読む属性だけを
注入して LLM クライアント再生成やアバター画像変換の経路を踏まないため。`personas` を
空 dict にすると、インメモリのペルソナへ反映するブロックごと通らない。
"""
import json
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from manager.admin import AdminService
from manager.persona import PersonaMixin

AI_ID = "air_city_a"


def _make_admin(session_local):
    """`update_ai` / `get_ai_details` が読む属性だけを持つ AdminService。"""
    admin = AdminService.__new__(AdminService)
    admin.SessionLocal = session_local
    admin.personas = {}
    admin.building_map = {}
    admin.state = SimpleNamespace(model=None, city_id=1)
    admin.avatar_calls = []
    admin._set_persona_avatar = lambda ai_id, value: admin.avatar_calls.append(
        (ai_id, value)
    )
    return admin


class AdminAiEditContractTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)

        db = self.SessionLocal()
        try:
            db.add(User(USERID=1, PASSWORD="x", USERNAME="u"))
            db.flush()
            db.add(City(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
            db.add(City(CITYID=2, USERID=1, CITY_SLUG="city_b", UI_PORT=3001, API_PORT=8001))
            db.add(AI(AIID=AI_ID, HOME_CITYID=1, AINAME="Air"))
            db.commit()
        finally:
            db.close()

        self.admin = _make_admin(self.SessionLocal)

    # --- helpers ---

    def _update(self, ai_id=AI_ID, **overrides):
        """編集フォームが常に送る項目を埋めた `update_ai` 呼び出し。"""
        kwargs = dict(
            name="Air",
            description="desc",
            system_prompt="prompt",
            home_city_id=1,
            default_model="gemini-2.0-flash",
            lightweight_model="gemini-2.0-flash-lite",
            autonomy_enabled=True,
            avatar_path=None,
            avatar_upload=None,
        )
        kwargs.update(overrides)
        return self.admin.update_ai(ai_id, **kwargs)

    def _row(self, ai_id=AI_ID):
        db = self.SessionLocal()
        try:
            return db.query(AI).filter(AI.AIID == ai_id).first()
        finally:
            db.close()

    # --- 永続化 ---

    def test_update_persists_every_field_the_edit_form_sends(self):
        result = self._update(
            name="エア",
            description="姉妹",
            system_prompt="you are Air",
            avatar_path="  /icons/air.webp  ",
            autonomy_enabled=False,
            vision_model="vision-1",
            audio_model="audio-1",
            video_model="video-1",
            memory_weave_model="weave-1",
            appearance_image_path="  /appearance/air.png  ",
            chronicle_enabled=False,
            autonomous_chronicle_enabled=False,
            auto_recall_enabled=False,
            memory_weave_context=False,
            memopedia_index_enabled=True,
            core_memory_char_budget=1234,
            chronicle_char_budget=45000,
            spell_enabled=False,
            realtime_info_enabled=False,
            meta_judgment_config={"max_retries": 5},
            user_conv_timeout_minutes=45,
        )
        self.assertNotIn("Error", result)

        details = self.admin.get_ai_details(AI_ID)
        self.assertEqual(details["AINAME"], "エア")
        self.assertEqual(details["DESCRIPTION"], "姉妹")
        self.assertEqual(details["SYSTEMPROMPT"], "you are Air")
        self.assertEqual(details["AVATAR_IMAGE"], "/icons/air.webp")
        self.assertEqual(details["APPEARANCE_IMAGE_PATH"], "/appearance/air.png")
        self.assertEqual(details["DEFAULT_MODEL"], "gemini-2.0-flash")
        self.assertEqual(details["LIGHTWEIGHT_MODEL"], "gemini-2.0-flash-lite")
        self.assertEqual(details["VISION_MODEL"], "vision-1")
        self.assertEqual(details["AUDIO_MODEL"], "audio-1")
        self.assertEqual(details["VIDEO_MODEL"], "video-1")
        self.assertEqual(details["MEMORY_WEAVE_MODEL"], "weave-1")
        self.assertFalse(details["AUTONOMY_ENABLED"])
        self.assertFalse(details["CHRONICLE_ENABLED"])
        self.assertFalse(details["AUTONOMOUS_CHRONICLE_ENABLED"])
        self.assertFalse(details["AUTO_RECALL_ENABLED"])
        self.assertFalse(details["MEMORY_WEAVE_CONTEXT"])
        self.assertTrue(details["MEMOPEDIA_INDEX_ENABLED"])
        self.assertEqual(details["CORE_MEMORY_CHAR_BUDGET"], 1234)
        self.assertEqual(details["CHRONICLE_CHAR_BUDGET"], 45000)
        self.assertFalse(details["SPELL_ENABLED"])
        self.assertFalse(details["REALTIME_INFO_ENABLED"])
        self.assertEqual(json.loads(details["META_JUDGMENT_CONFIG"]), {"max_retries": 5})
        self.assertEqual(details["USER_CONV_TIMEOUT_MINUTES"], 45)

        # アバターはインメモリのキャッシュにも同じ値で伝わる
        self.assertEqual(self.admin.avatar_calls, [(AI_ID, "/icons/air.webp")])

    def test_blank_model_selection_clears_the_column(self):
        """「指定なし」を選ぶと空文字が届く。空文字は NULL に倒す (= 既定に従う)。"""
        self._update(vision_model="vision-1", memory_weave_model="weave-1")
        self._update(
            default_model="",
            lightweight_model="",
            vision_model="",
            audio_model="",
            video_model="",
            memory_weave_model="",
            appearance_image_path="",
        )

        row = self._row()
        self.assertIsNone(row.DEFAULT_MODEL)
        self.assertIsNone(row.LIGHTWEIGHT_MODEL)
        self.assertIsNone(row.VISION_MODEL)
        self.assertIsNone(row.AUDIO_MODEL)
        self.assertIsNone(row.VIDEO_MODEL)
        self.assertIsNone(row.MEMORY_WEAVE_MODEL)
        self.assertIsNone(row.APPEARANCE_IMAGE_PATH)

    def test_omitted_toggles_keep_their_stored_values(self):
        """トグルを送らない呼び出し元 (ワールドエディタ) が、設定モーダルの値を消さない。"""
        self._update(
            chronicle_enabled=False,
            autonomous_chronicle_enabled=False,
            auto_recall_enabled=False,
            memory_weave_context=False,
            memopedia_index_enabled=True,
            spell_enabled=False,
            realtime_info_enabled=False,
            core_memory_char_budget=1234,
            meta_judgment_config={"max_retries": 5},
            user_conv_timeout_minutes=45,
        )
        self._update(name="Air2")  # トグル類を一切渡さない

        row = self._row()
        self.assertEqual(row.AINAME, "Air2")
        self.assertFalse(row.CHRONICLE_ENABLED)
        self.assertFalse(row.AUTONOMOUS_CHRONICLE_ENABLED)
        self.assertFalse(row.AUTO_RECALL_ENABLED)
        self.assertFalse(row.MEMORY_WEAVE_CONTEXT)
        self.assertTrue(row.MEMOPEDIA_INDEX_ENABLED)
        self.assertFalse(row.SPELL_ENABLED)
        self.assertFalse(row.REALTIME_INFO_ENABLED)
        self.assertEqual(row.CORE_MEMORY_CHAR_BUDGET, 1234)
        self.assertEqual(json.loads(row.META_JUDGMENT_CONFIG), {"max_retries": 5})
        self.assertEqual(row.USER_CONV_TIMEOUT_MINUTES, 45)

    def test_zero_values_fall_back_to_the_builtin_defaults(self):
        """0 / 負値は「既定値運用に戻す」の意味なので NULL に倒す。"""
        self._update(
            core_memory_char_budget=1234, chronicle_char_budget=45000,
            user_conv_timeout_minutes=45,
        )
        self._update(
            core_memory_char_budget=0, chronicle_char_budget=-1,
            user_conv_timeout_minutes=-1,
        )

        row = self._row()
        self.assertIsNone(row.CORE_MEMORY_CHAR_BUDGET)
        self.assertIsNone(row.CHRONICLE_CHAR_BUDGET)
        self.assertIsNone(row.USER_CONV_TIMEOUT_MINUTES)

    def test_empty_meta_judgment_config_falls_back_to_the_builtin_defaults(self):
        self._update(meta_judgment_config={"max_retries": 5})
        self._update(meta_judgment_config={})

        self.assertIsNone(self._row().META_JUDGMENT_CONFIG)

    # --- 拒否する更新 ---

    def test_unknown_ai_is_rejected_without_touching_the_database(self):
        result = self._update(ai_id="nobody_city_a", name="Nobody")

        self.assertIn("not found", result)
        self.assertEqual(self._row().AINAME, "Air")
        self.assertEqual(self.admin.avatar_calls, [])

    def test_dispatched_persona_cannot_change_home_city(self):
        db = self.SessionLocal()
        try:
            db.query(AI).filter(AI.AIID == AI_ID).first().IS_DISPATCHED = True
            db.commit()
        finally:
            db.close()

        result = self._update(name="エア", home_city_id=2)

        self.assertIn("Cannot change the home city", result)
        row = self._row()
        self.assertEqual(row.HOME_CITYID, 1)
        self.assertEqual(row.AINAME, "Air")  # 拒否時は他の項目も書かない
        self.assertEqual(self.admin.avatar_calls, [])

    def test_get_ai_details_returns_none_for_unknown_ai(self):
        self.assertIsNone(self.admin.get_ai_details("nobody_city_a"))

    # --- 複製の再発防止 ---

    def test_persona_mixin_does_not_redefine_the_edit_methods(self):
        """`PersonaMixin` に同名メソッドが戻ると、`RuntimeService` 側だけ古い実装になる。

        `AdminService` は `PersonaMixin` を継承しつつ自前定義で上書きするので、複製が
        あっても API 経路は壊れない。壊れないまま両方が保守され続けるのが元の負債。
        """
        for name in ("get_ai_details", "update_ai"):
            self.assertNotIn(
                name,
                vars(PersonaMixin),
                f"PersonaMixin.{name} が再び定義されています。ペルソナ編集の実装は "
                "AdminService だけが持ちます (docs/issues/archive/"
                "persona_mixin_ai_edit_dead_duplicate.md)。",
            )


if __name__ == "__main__":
    unittest.main()
