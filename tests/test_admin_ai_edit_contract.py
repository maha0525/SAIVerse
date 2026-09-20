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


#: 編集フォームが送るモデル名の定義。設定ファイルの無いモデル名は保存されない
#: (docs/intent/persona_model_selection.md 決まったこと 6) ので、ここで往復を
#: 検べる名前は合成の定義として置いておく。
_MODEL_DEFINITIONS = {
    name: {"model": f"vendor/{name}", "provider": "stub", "context_length": 1000}
    for name in ("gemini-2.0-flash", "gemini-2.0-flash-lite", "weave-1", "reflex-1")
}


class AdminAiEditContractTest(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        from saiverse import model_configs

        definitions = patch.dict(model_configs.MODEL_CONFIGS, _MODEL_DEFINITIONS)
        definitions.start()
        self.addCleanup(definitions.stop)

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
            reflex_judgment_model="reflex-1",
            appearance_image_path="  /appearance/air.png  ",
            chronicle_enabled=False,
            autonomous_chronicle_enabled=False,
            auto_recall_enabled=False,
            auto_recall_enhanced=True,
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
        self.assertEqual(details["REFLEX_JUDGMENT_MODEL"], "reflex-1")
        self.assertFalse(details["AUTONOMY_ENABLED"])
        self.assertFalse(details["CHRONICLE_ENABLED"])
        self.assertFalse(details["AUTONOMOUS_CHRONICLE_ENABLED"])
        self.assertFalse(details["AUTO_RECALL_ENABLED"])
        self.assertTrue(details["AUTO_RECALL_ENHANCED"])
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
        self._update(
            vision_model="vision-1", memory_weave_model="weave-1",
            reflex_judgment_model="reflex-1",
        )
        self._update(
            default_model="",
            lightweight_model="",
            vision_model="",
            audio_model="",
            video_model="",
            memory_weave_model="",
            reflex_judgment_model="",
            appearance_image_path="",
        )

        row = self._row()
        self.assertIsNone(row.DEFAULT_MODEL)
        self.assertIsNone(row.LIGHTWEIGHT_MODEL)
        self.assertIsNone(row.VISION_MODEL)
        self.assertIsNone(row.AUDIO_MODEL)
        self.assertIsNone(row.VIDEO_MODEL)
        self.assertIsNone(row.MEMORY_WEAVE_MODEL)
        self.assertIsNone(row.REFLEX_JUDGMENT_MODEL)
        self.assertIsNone(row.APPEARANCE_IMAGE_PATH)

    def test_omitted_toggles_keep_their_stored_values(self):
        """トグルを送らない呼び出し元 (ワールドエディタ) が、設定モーダルの値を消さない。"""
        self._update(
            chronicle_enabled=False,
            autonomous_chronicle_enabled=False,
            auto_recall_enabled=False,
            auto_recall_enhanced=True,
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
        self.assertTrue(row.AUTO_RECALL_ENHANCED)
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

    def test_undefined_reflex_judgment_model_is_not_saved(self):
        """反射判断の個別の上書きも、設定ファイルの無い名前は保存しない。

        判定は関所そのもの (saiverse/model_defaults.py の role_model_save_rejection)
        を実物のまま通す — モックに差し替えると「関所を呼んでいるか」ではなく
        「モックを呼んでいるか」を検べることになる。断った回は前の設定が残り、
        断ったこと自体は画面の知らせ ([WARNING:LLM]) で届く。
        """
        self._update(reflex_judgment_model="reflex-1")
        result = self._update(reflex_judgment_model="no-such-model")

        self.assertIn("[WARNING:LLM]", result)
        self.assertIn("no-such-model", result)
        self.assertEqual(self._row().REFLEX_JUDGMENT_MODEL, "reflex-1")

    def test_ordinary_model_is_accepted_for_the_reflex_judgment_role(self):
        """反射判断の役割には、jev 互換でない普通のモデルも保存できる。

        会話の役割へ反射判断専用の宛先を保存するのは断るが、逆向き (反射判断の
        役割に普通の LLM) は断たない — 第 2 段で合法になるので、いま断ると将来の
        正しい設定まで拒むことになる (docs/intent/reflex_judgment.md §6-4)。
        """
        result = self._update(reflex_judgment_model="reflex-1")

        self.assertNotIn("[WARNING:LLM]", result)
        self.assertEqual(self._row().REFLEX_JUDGMENT_MODEL, "reflex-1")

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

    def test_saiverse_manager_delegation_accepts_every_admin_parameter(self):
        """API 経路は SAIVerseManager.update_ai (委譲) を経由する。

        AdminService.update_ai に引数を足して委譲側を忘れると、テストは
        AdminService 直呼びで緑のまま、実経路だけが TypeError → 500 になる
        (2026-09-01 実害: chronicle_char_budget の追加漏れで v0.3.1 の
        ペルソナ設定保存が全ユーザーで失敗した)。委譲は全引数を素通しする
        契約なので、両者のシグネチャ一致を機械検査する。
        """
        import inspect

        from saiverse.saiverse_manager import SAIVerseManager

        admin_params = set(inspect.signature(AdminService.update_ai).parameters)
        manager_params = set(inspect.signature(SAIVerseManager.update_ai).parameters)
        missing = admin_params - manager_params
        self.assertFalse(
            missing,
            "SAIVerseManager.update_ai (委譲側) に足りない引数があります: "
            f"{sorted(missing)}。AdminService.update_ai に引数を足したら、"
            "saiverse_manager.py の委譲メソッドにも同じ引数と受け渡しを足すこと。",
        )
        # 受け取るだけで admin へ渡し忘れるケースも捕まえる: 委譲本体のソースに
        # 引数名が (仮引数以外で) 登場するかを検査する。
        src = inspect.getsource(SAIVerseManager.update_ai)
        body = src.split('"""ワールドエディタ', 1)[-1]  # docstring 以降 = 委譲呼び出し部
        for name in admin_params - {"self"}:
            self.assertIn(
                name,
                body,
                f"SAIVerseManager.update_ai が引数 '{name}' を admin へ渡していません。",
            )

    def test_omitted_model_fields_keep_their_stored_values(self):
        """送られてこなかったモデル欄は触らない (既定は UNSET の印)。

        既定を None にすると「知らない画面が項目ごと送らない」と「空欄にして外す」が
        潰れて、ワールドエディタの保存が設定モーダルの個別モデルを黙って NULL へ戻す
        (2026-09-20 の敵対レビュー 1 巡目、high)。入口が現在値を読んで詰め直す形は、
        読みと書きの間の並行保存を古い値で巻き戻すので採らない (同 2 巡目、high) —
        「触らない」は受け側が錠の中で保証する。
        """
        self._update(
            vision_model="vision-1", audio_model="vision-1", video_model="vision-1",
            memory_weave_model="weave-1", reflex_judgment_model="reflex-1",
        )
        # モデル欄を一つも送らない保存 (= ワールドエディタの形)。
        self._update()

        row = self._row()
        self.assertEqual(row.VISION_MODEL, "vision-1")
        self.assertEqual(row.AUDIO_MODEL, "vision-1")
        self.assertEqual(row.VIDEO_MODEL, "vision-1")
        self.assertEqual(row.MEMORY_WEAVE_MODEL, "weave-1")
        self.assertEqual(row.REFLEX_JUDGMENT_MODEL, "reflex-1")

    def test_the_unset_sentinel_also_works_for_the_required_model_fields(self):
        """標準・軽量は必須引数だが、印を値として渡せば同じく触らない。

        ペルソナ設定の PATCH は欄が省かれた回にこの形で渡す (api/routes/people/config.py)。
        """
        from manager.admin import UNSET

        self._update()  # 標準 = gemini-2.0-flash / 軽量 = gemini-2.0-flash-lite を保存
        self._update(default_model=UNSET, lightweight_model=UNSET)

        row = self._row()
        self.assertEqual(row.DEFAULT_MODEL, "gemini-2.0-flash")
        self.assertEqual(row.LIGHTWEIGHT_MODEL, "gemini-2.0-flash-lite")

    def test_world_editor_route_does_not_send_the_hidden_model_fields(self):
        """ワールドエディタの入口は、画面が扱わないモデル欄を渡さない。

        入口が現在値を読んで詰め直す形に戻すと、読みと書きの間に挟まった別の画面の
        保存を古い値で巻き戻す (docs/issues/archive/
        world_editor_save_wipes_persona_model_overrides.md)。「触らない」は受け側の
        UNSET の既定に任せるのが契約。
        """
        from api.routes import world as world_routes

        captured = {}

        class _Manager:
            def update_ai(self, *args, **kwargs):
                captured.update(kwargs)
                return "更新しました"

        request = world_routes.AIUpdate(
            name="アイ", description="d", system_prompt="s", home_city_id=1,
            default_model=None, lightweight_model=None,
            autonomy_enabled=True, avatar_path=None,
        )
        world_routes.update_ai("ai_city_a", request, manager=_Manager())

        for field in (
            "vision_model", "audio_model", "video_model",
            "memory_weave_model", "reflex_judgment_model",
        ):
            self.assertNotIn(field, captured)

    def test_persona_config_route_sends_unset_for_absent_model_fields(self):
        """ペルソナ設定の PATCH は、リクエストに無いモデル欄を UNSET の印で渡す。

        以前は現在値を読んで詰め直していた — それだと読みと書きの間に挟まった別の
        保存を古い値で巻き戻す (ワールドエディタと同じ形の穴)。空文字は「個別設定を
        外す」としてそのまま渡す。
        """
        from api.routes.people import config as people_config
        from manager.admin import UNSET

        captured = {}

        class _Manager:
            def get_ai_details(self, ai_id):
                return {
                    "AINAME": "アイ", "DESCRIPTION": "d", "SYSTEMPROMPT": "s",
                    "HOME_CITYID": 1, "AUTONOMY_ENABLED": True,
                    "AVATAR_IMAGE": None, "APPEARANCE_IMAGE_PATH": None,
                    "DEFAULT_MODEL": "gemini-2.0-flash",
                }

            def update_ai(self, **kwargs):
                captured.update(kwargs)
                return "更新しました"

        request = people_config.UpdateAIConfigRequest(
            reflex_judgment_model="",  # 明示の空 = 個別設定を外す
        )
        people_config.update_persona_config("ai_city_a", request, manager=_Manager())

        self.assertEqual(captured.get("reflex_judgment_model"), "")
        for field in ("memory_weave_model", "vision_model", "audio_model", "video_model", "lightweight_model", "default_model"):
            self.assertIs(captured.get(field), UNSET)

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
