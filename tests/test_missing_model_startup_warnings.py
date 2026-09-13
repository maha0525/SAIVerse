"""モデル設定の警告のテスト。

画面はページを開くたびに GET /api/config/startup-warnings を読んでこの警告を出す。
警告は起動時に積まず、取りに来るたびにいまの設定と各ペルソナの状態から作る
(manager/initialization.py の ``current_model_setting_warnings``)。
設計: docs/intent/persona_model_selection.md (決まったこと 7・8、実装が守ること)。

ここで押さえること:

- 標準・軽量・Memory Weave・画像/音声/動画要約モデルの定義が見つからないとき、
  代わりのモデルで動いているとは言わず「止まっています」と、どこで選び直せば
  再起動しなくても使えるかを伝える。ペルソナは ID ではなく表示名で呼ぶ
- グローバル設定の標準・軽量・Memory Weave モデルは、その値を使っている
  (個別の値を持たない) ペルソナの名前を並べる
- チャット画面のモデル一時上書き中は「上書きを解除すると止まる」と伝える
- 起動時も、定義の無いモデルを代わりのモデルへ差し替えずに読み込む
- 決め方が指すモデルと、ペルソナが実際に使っているモデルが食い違う人がいるときだけ、
  その名前つきで「切り替えられなかった」と知らせる
- 判定が、各役割の値を実際に使う側と同じ引き方になっている
- 画面のルートは保存済みの警告の後ろに計算分を足し、計算が失敗しても保存済みは返す

モデル定義は MODEL_CONFIGS を差し替えた偽物を使い (読み直しのテストだけは本物の
reload_configs を一時フォルダ相手に通す)、PersonaCore はスタブにする。LLM は呼ばない。
定義ファイルを名前で探す先は一時フォルダに向け、~/.saiverse には触らない。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes import config as config_route
from database.models import AI as AIModel, Base, City as CityModel
from manager.initialization import InitializationMixin
from manager.persona import PersonaMixin
from saiverse import data_paths, model_configs, model_defaults
from saiverse.persona_model_selection import (
    SOURCE_GLOBAL,
    SpeakingModelChoice,
    reapply_speaking_models,
)

PERSONA_ID = "air_city_a"
NAME = "アイ"
SECOND_ID = "miku_city_a"
SECOND_NAME = "ミク"
FALLBACK_KEY = model_defaults.BUILTIN_DEFAULT_LITE_MODEL
DEFINED_KEY = "test-defined-model"
#: DEFINED_KEY の定義が持つ API モデル名。設定キーとしては存在しない。
DEFINED_API_NAME = "vendor/test-defined-api-name"
OTHER_KEY = "test-other-model"

ROLE_ENV_KEYS = tuple(model_defaults.MODEL_ROLES.values())

PERSONA_RESELECT = "ペルソナ設定で選び直すと"
GLOBAL_RESELECT = "グローバル設定の「モデルロール」で選び直すと"
LIGHT_WORK = "軽量モデルを使う作業（返事の途中の作業や、自分から動く判断）ができず、止まっています。"


def _persona_default(name: str, value: str) -> str:
    return (
        f"{name}の標準モデル '{value}' は SAIVerse にないため、{name}は止まっています。"
        f"{PERSONA_RESELECT}、再起動しなくても話せるようになります。"
    )


def _persona_lite(name: str, value: str) -> str:
    return (
        f"{name}の軽量モデル '{value}' は SAIVerse にないため、{name}は{LIGHT_WORK}"
        f"{PERSONA_RESELECT}、再起動しなくても続けられるようになります。"
    )


def _persona_weave(name: str, value: str) -> str:
    return (
        f"{name}のMemory Weaveモデル '{value}' は SAIVerse にないため、{name}の記憶の整理は止まっています。"
        f"{PERSONA_RESELECT}、再起動しなくても整理が再開します。"
    )


def _names(names) -> str:
    return f" ({'、'.join(names)})" if names else ""


def _global_default(value: str, names) -> str:
    if names:
        return (
            f"グローバル設定の標準モデル '{value}' は SAIVerse にないため、"
            f"個別の標準モデルを持たないペルソナ{_names(names)}は止まっています。"
            f"{GLOBAL_RESELECT}、再起動しなくても話せるようになります。"
        )
    return (
        f"グローバル設定の標準モデル '{value}' は SAIVerse にありません。"
        "個別の標準モデルを持たないペルソナは、選び直すまで止まります。"
        f"{GLOBAL_RESELECT}、再起動しなくても話せるようになります。"
    )


def _global_lite(value: str, names=()) -> str:
    return (
        f"グローバル設定の軽量モデル '{value}' は SAIVerse にないため、"
        f"個別の軽量モデルを持たないペルソナ{_names(names)}は{LIGHT_WORK}"
        f"{GLOBAL_RESELECT}、再起動しなくても続けられるようになります。"
    )


def _global_weave(value: str, names=()) -> str:
    return (
        f"グローバル設定のMemory Weaveモデル '{value}' は SAIVerse にないため、"
        f"Memory Weaveモデルを個別に設定していないペルソナ{_names(names)}の記憶の整理は止まっています。"
        f"{GLOBAL_RESELECT}、再起動しなくても整理が再開します。"
    )


def _global_summary(label: str, value: str) -> str:
    return (
        f"グローバル設定の{label} '{value}' は SAIVerse にないため、要約は止まっています。"
        f"{GLOBAL_RESELECT}、再起動しなくても要約されるようになります。"
    )


def _unswitched(name: str, model: str) -> str:
    return (
        f"{name}は新しい標準モデルに切り替えられなかったため、いまも '{model}' で話しています。"
        "もう一度保存し直すか、再起動すると切り替わります。"
    )


def _definition(api_name: str) -> dict:
    return {"model": api_name, "provider": "stub", "context_length": 1000}


@pytest.fixture(autouse=True)
def fake_model_definitions(monkeypatch, tmp_path):
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        FALLBACK_KEY: _definition("test-fallback-api-name"),
        DEFINED_KEY: _definition(DEFINED_API_NAME),
        OTHER_KEY: _definition("vendor/test-other"),
    })
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "no_expansion")
    for key in ROLE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _raise(*_args, **_kwargs):
    raise RuntimeError("boom")


def _set_env(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _messages(warnings):
    assert all(w["source"] == "model_config" for w in warnings), warnings
    return [w["message"] for w in warnings]


class _StubPersonaCore:
    """PersonaCore の代わり。受け取った引数をそのまま属性として持ち、当てはめを受け付ける。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def set_model(self, model, context_length, provider, parameter_overrides=None):
        self.model = model
        self.context_length = context_length
        self.provider = provider
        self._pending_parameter_overrides = dict(parameter_overrides) if parameter_overrides else None

    def set_lightweight_model(self, value):
        self.lightweight_model = value or None

    def drop_llm_clients(self):
        pass


class _Manager(InitializationMixin, PersonaMixin):
    """起動時にモデル設定を決める二つの入口 (_init_model_config と
    _load_personas_from_db) と、警告の計算を持つ最小の manager。"""


class _World:
    """一つの City とペルソナの DB 行。起動は start() で行う。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def add_persona(self, persona_id=PERSONA_ID, name=NAME, **model_columns):
        db = self.session_factory()
        try:
            db.add(AIModel(AIID=persona_id, HOME_CITYID=1, AINAME=name, **model_columns))
            db.commit()
        finally:
            db.close()

    def set_persona_models(self, persona_id=PERSONA_ID, **model_columns):
        """ペルソナの DB 行を、保存の処理 (update_ai) を通さずに書き換える。"""
        db = self.session_factory()
        try:
            ai = db.query(AIModel).filter_by(AIID=persona_id).one()
            for column, value in model_columns.items():
                setattr(ai, column, value)
            db.commit()
        finally:
            db.close()

    def start(self) -> _Manager:
        """SAIVerseManager.__init__ と同じ順で、モデル設定の決定とペルソナの読み込みを行う。

        呼ぶたびに同じ DB から起動し直すので、二度呼べば再起動になる。"""
        svc = _Manager.__new__(_Manager)
        svc.SessionLocal = self.session_factory
        svc.city_id = 1
        svc.city_name = "city_a"
        svc.default_avatar = "avatar.png"
        svc.user_room_id = "user_room_city_a"
        svc.timezone_info = None
        svc.timezone_name = "UTC"
        svc.buildings = []
        svc.building_map = {}
        svc.building_histories = {}
        svc.occupants = {}
        svc.personas = {}
        svc.avatar_map = {}
        svc.id_to_name_map = {}
        svc.items = {}
        svc.items_by_persona = {}
        svc.get_persona_pending_events = lambda *a, **k: []
        svc.archive_persona_events = lambda *a, **k: None
        svc._init_model_config()
        svc._load_personas_from_db()
        return svc


@pytest.fixture
def world(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr("manager.persona.PersonaCore", _StubPersonaCore)
    db = session_factory()
    try:
        db.add(CityModel(
            CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000,
        ))
        db.commit()
    finally:
        db.close()
    yield _World(session_factory)
    engine.dispose()


# --- ペルソナ単位 ---------------------------------------------------------------


def test_persona_missing_models_each_warn_by_display_name(world):
    world.add_persona(
        DEFAULT_MODEL="gone-default",
        LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
    )
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        _persona_default(NAME, "gone-default"),
        _persona_lite(NAME, "gone-lite"),
        _persona_weave(NAME, "gone-weave"),
    ]


@pytest.mark.parametrize("columns", [
    pytest.param(
        {
            "DEFAULT_MODEL": DEFINED_KEY,
            "LIGHTWEIGHT_MODEL": DEFINED_KEY,
            "MEMORY_WEAVE_MODEL": DEFINED_API_NAME,
        },
        id="defined",
    ),
    pytest.param(
        {"DEFAULT_MODEL": None, "LIGHTWEIGHT_MODEL": None, "MEMORY_WEAVE_MODEL": None},
        id="unset",
    ),
    pytest.param(
        {"DEFAULT_MODEL": "", "LIGHTWEIGHT_MODEL": "", "MEMORY_WEAVE_MODEL": ""},
        id="empty",
    ),
])
def test_persona_defined_unset_and_empty_values_do_not_warn(world, columns):
    world.add_persona(**columns)
    svc = world.start()

    assert PERSONA_ID in svc.personas
    assert svc.current_model_setting_warnings() == []


def test_persona_media_summary_models_are_not_checked(world):
    """ペルソナ単位の画像/音声/動画要約モデルは保存されるだけで、読む箇所が無い。"""
    world.add_persona(
        VISION_MODEL="gone-vision",
        AUDIO_MODEL="gone-audio",
        VIDEO_MODEL="gone-video",
    )
    svc = world.start()

    assert svc.current_model_setting_warnings() == []


def test_persona_lookup_follows_each_consumer(world):
    """API モデル名は、Memory Weave (find_model_config) なら引けるが、
    標準・軽量モデル (設定キーの完全一致) では引けない。"""
    world.add_persona(
        DEFAULT_MODEL=DEFINED_API_NAME,
        LIGHTWEIGHT_MODEL=DEFINED_API_NAME,
        MEMORY_WEAVE_MODEL=DEFINED_API_NAME,
    )
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        _persona_default(NAME, DEFINED_API_NAME),
        _persona_lite(NAME, DEFINED_API_NAME),
    ]
    # 標準モデルは読み込む側でも引けず、代わりのモデルへは差し替えない
    persona = svc.personas[PERSONA_ID]
    assert persona.model == DEFINED_API_NAME
    assert persona.speaking_model_choice.defined is False


def test_a_persona_that_could_not_be_loaded_still_gets_the_setting_warning(world, monkeypatch):
    world.add_persona(DEFAULT_MODEL="gone-default")
    monkeypatch.setattr("manager.persona.PersonaCore", _raise)
    svc = world.start()

    assert PERSONA_ID not in svc.personas
    assert _messages(svc.current_model_setting_warnings()) == [
        _persona_default(NAME, "gone-default"),
    ]


def test_failing_lookup_skips_only_that_role(world, monkeypatch):
    world.add_persona(LIGHTWEIGHT_MODEL="gone-lite", MEMORY_WEAVE_MODEL="gone-weave")
    svc = world.start()
    monkeypatch.setattr(model_configs, "find_model_config", _raise)

    # 引けなかった役割 (Memory Weave) は飛ばし、残りの役割の検査は続く
    assert _messages(svc.current_model_setting_warnings()) == [
        _persona_lite(NAME, "gone-lite"),
    ]


# --- グローバル設定単位 ---------------------------------------------------------


def test_global_missing_values_each_warn_and_name_the_personas_using_them(world, monkeypatch):
    _set_env(
        monkeypatch,
        SAIVERSE_DEFAULT_MODEL="gone-default",
        SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
        SAIVERSE_IMAGE_SUMMARY_MODEL="gone-image",
        SAIVERSE_AUDIO_SUMMARY_MODEL="gone-audio",
        SAIVERSE_VIDEO_SUMMARY_MODEL="gone-video",
    )
    world.add_persona()
    world.add_persona(SECOND_ID, SECOND_NAME)
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        _global_default("gone-default", [NAME, SECOND_NAME]),
        _global_lite("gone-lite", [NAME, SECOND_NAME]),
        _global_weave("gone-weave", [NAME, SECOND_NAME]),
        _global_summary("画像要約モデル", "gone-image"),
        _global_summary("音声要約モデル", "gone-audio"),
        _global_summary("動画要約モデル", "gone-video"),
    ]
    # 代わりのモデルでは読み込まない
    persona = svc.personas[PERSONA_ID]
    assert persona.model == "gone-default"
    assert persona.speaking_model_choice == SpeakingModelChoice("gone-default", SOURCE_GLOBAL, False)
    assert svc.provider == ""


def test_global_default_missing_when_every_persona_has_its_own(world, monkeypatch):
    _set_env(monkeypatch, SAIVERSE_DEFAULT_MODEL="gone-default")
    world.add_persona(DEFAULT_MODEL=DEFINED_KEY)
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        _global_default("gone-default", []),
    ]


def test_global_defined_unset_and_empty_values_do_not_warn(world, monkeypatch):
    _set_env(
        monkeypatch,
        SAIVERSE_DEFAULT_MODEL=DEFINED_KEY,
        SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL=DEFINED_KEY,
        MEMORY_WEAVE_MODEL=DEFINED_API_NAME,
        SAIVERSE_IMAGE_SUMMARY_MODEL="",
        # 音声・動画要約は未設定のまま
    )
    svc = world.start()

    assert svc.provider == "stub"
    assert svc.current_model_setting_warnings() == []


def test_global_lookup_follows_each_consumer(world, monkeypatch):
    """API モデル名は、Memory Weave・画像/音声/動画要約 (find_model_config) なら
    引けるが、標準・軽量モデル (設定キーの完全一致) では引けない。"""
    _set_env(monkeypatch, **{key: DEFINED_API_NAME for key in ROLE_ENV_KEYS})
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        _global_default(DEFINED_API_NAME, []),
        _global_lite(DEFINED_API_NAME),
    ]


# --- チャット画面のモデル一時上書き ----------------------------------------------


def test_while_the_override_is_active_the_default_model_warnings_say_it_stops_when_released(
    world, monkeypatch,
):
    world.add_persona(DEFAULT_MODEL="gone-default")
    world.add_persona(SECOND_ID, SECOND_NAME)
    _set_env(monkeypatch, SAIVERSE_DEFAULT_MODEL="gone-global")
    svc = world.start()
    svc.model = OTHER_KEY
    reapply_speaking_models(svc)

    assert _messages(svc.current_model_setting_warnings()) == [
        "グローバル設定の標準モデル 'gone-global' は SAIVerse にありません。"
        f"いまはチャット画面のモデル一時上書き '{OTHER_KEY}' で話していますが、"
        f"上書きを解除すると、個別の標準モデルを持たないペルソナ ({SECOND_NAME})は止まります。"
        f"{GLOBAL_RESELECT}、上書きを解除しても再起動せずに話し続けられます。",
        f"{NAME}の標準モデル 'gone-default' は SAIVerse にありません。"
        f"いまはチャット画面のモデル一時上書き '{OTHER_KEY}' で話していますが、"
        f"上書きを解除すると{NAME}は止まります。"
        f"{PERSONA_RESELECT}、上書きを解除しても再起動せずに話し続けられます。",
    ]


def test_an_override_model_without_a_definition_is_warned(world):
    svc = world.start()
    svc.model = "gone-override"

    assert _messages(svc.current_model_setting_warnings()) == [
        "チャット画面のモデル一時上書き 'gone-override' は SAIVerse にないため、ペルソナは止まっています。"
        "チャット画面でモデルを選び直すか一時上書きを解除すると、再起動しなくても話せるようになります。",
    ]


# --- 起動時 ---------------------------------------------------------------------


def test_startup_records_no_model_setting_warnings_and_does_not_substitute(world, monkeypatch):
    _set_env(monkeypatch, **{key: "gone-global" for key in ROLE_ENV_KEYS})
    world.add_persona(
        DEFAULT_MODEL="gone-default",
        LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
    )
    svc = world.start()

    # 同じ事実を二重に出さないよう、起動時には積まない
    assert svc.startup_warnings == []
    # 組み込みの既定モデルへ差し替えない
    assert svc.provider == ""
    persona = svc.personas[PERSONA_ID]
    assert persona.model == "gone-default"
    assert persona.provider == ""
    assert persona.lightweight_model == "gone-lite"
    assert persona.memory_weave_model == "gone-weave"
    # 積まなかった分は、取りに来たときに作られる (グローバル 6 件 + ペルソナ 3 件)
    assert len(svc.current_model_setting_warnings()) == 9


# --- 切り替えられなかったペルソナ -------------------------------------------------


def test_the_unswitched_notice_names_only_personas_still_speaking_the_old_model(world, monkeypatch):
    world.add_persona()  # グローバル設定に従う
    world.add_persona(SECOND_ID, SECOND_NAME, DEFAULT_MODEL=DEFINED_KEY)
    svc = world.start()
    assert svc.personas[PERSONA_ID].model == FALLBACK_KEY

    # 決め直しを通らずに設定だけ変わった状態 (保存の途中で当てはめに失敗したときと同じ)
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", OTHER_KEY)
    assert _messages(svc.current_model_setting_warnings()) == [
        _unswitched(NAME, FALLBACK_KEY),
    ]

    reapply_speaking_models(svc)
    assert svc.personas[PERSONA_ID].model == OTHER_KEY
    assert svc.current_model_setting_warnings() == []


def test_clearing_the_global_default_is_noticed_until_the_personas_are_switched(world, monkeypatch):
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", DEFINED_KEY)
    world.add_persona()
    svc = world.start()
    assert svc.personas[PERSONA_ID].model == DEFINED_KEY

    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", "")
    assert _messages(svc.current_model_setting_warnings()) == [
        _unswitched(NAME, DEFINED_KEY),
    ]

    reapply_speaking_models(svc)
    assert svc.personas[PERSONA_ID].model == FALLBACK_KEY
    assert svc.current_model_setting_warnings() == []


# --- 起動後の変更 ---------------------------------------------------------------


def test_fixing_the_persona_row_clears_the_missing_warnings(world):
    world.add_persona(
        DEFAULT_MODEL="gone-default",
        LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
    )
    svc = world.start()
    assert len(svc.current_model_setting_warnings()) == 3

    world.set_persona_models(
        DEFAULT_MODEL=DEFINED_KEY,
        LIGHTWEIGHT_MODEL=DEFINED_KEY,
        MEMORY_WEAVE_MODEL=DEFINED_API_NAME,
    )
    # 保存の処理を通していないので、ペルソナはまだ前のモデルのまま
    assert _messages(svc.current_model_setting_warnings()) == [
        _unswitched(NAME, "gone-default"),
    ]
    reapply_speaking_models(svc)
    assert svc.current_model_setting_warnings() == []


def test_adding_a_definition_after_startup_clears_warnings(world, monkeypatch):
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "added-later")
    world.add_persona(DEFAULT_MODEL="added-later", MEMORY_WEAVE_MODEL="added-later")
    svc = world.start()
    assert len(svc.current_model_setting_warnings()) == 3

    # 読み直し (model_configs.reload_configs) は MODEL_CONFIGS を新しい辞書へ差し替える
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        **model_configs.MODEL_CONFIGS,
        "added-later": _definition("vendor/added-later"),
    })
    assert svc.current_model_setting_warnings() == []


def test_removing_a_definition_after_startup_raises_warnings(world, monkeypatch):
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", DEFINED_KEY)
    world.add_persona(DEFAULT_MODEL=DEFINED_KEY, MEMORY_WEAVE_MODEL=DEFINED_KEY)
    svc = world.start()
    assert svc.current_model_setting_warnings() == []

    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        key: value
        for key, value in model_configs.MODEL_CONFIGS.items()
        if key != DEFINED_KEY
    })
    assert _messages(svc.current_model_setting_warnings()) == [
        # アイは個別の軽量モデルを持たないので、グローバル設定の軽量モデルを使っている
        _global_lite(DEFINED_KEY, [NAME]),
        _persona_default(NAME, DEFINED_KEY),
        _persona_weave(NAME, DEFINED_KEY),
    ]


def test_real_reload_is_reflected(world, monkeypatch, tmp_path):
    """モデル定義を作る・消すルートは最後に model_configs.reload_configs() を呼ぶ。
    その本物を、一時フォルダの user_data と組み込み定義 (builtin_data) だけを相手に通す。"""
    user_data = tmp_path / "user_data"
    models_dir = user_data / "models"
    models_dir.mkdir(parents=True)

    world.add_persona(LIGHTWEIGHT_MODEL="test-reloaded-model")
    svc = world.start()
    assert len(svc.current_model_setting_warnings()) == 1

    definition_file = models_dir / "test-reloaded-model.json"
    definition_file.write_text(
        json.dumps(_definition("vendor/test-reloaded-model")), encoding="utf-8",
    )
    model_configs.reload_configs()
    assert svc.current_model_setting_warnings() == []

    definition_file.unlink()
    model_configs.reload_configs()
    assert _messages(svc.current_model_setting_warnings()) == [
        _persona_lite(NAME, "test-reloaded-model"),
    ]


# --- 読み出し・引き当ての失敗 -----------------------------------------------------

UNREAD_PERSONAS = "ペルソナごとのモデル設定を読み出せなかったため、ペルソナ単位の確認はできていません。"


class _QueryFailingSession:
    """query で失敗するセッション。close が呼ばれたかを記録する。"""

    def __init__(self):
        self.closed = False

    def query(self, *_args, **_kwargs):
        raise RuntimeError("boom")

    def close(self):
        self.closed = True


def test_session_creation_failure_keeps_global_warnings(world, monkeypatch):
    """DB のセッションが作れなくても、グローバル設定の警告は消えず、
    ペルソナ単位の確認ができていないことが一件の警告で伝わる。"""
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    world.add_persona(LIGHTWEIGHT_MODEL="gone-persona-lite")
    svc = world.start()
    svc.SessionLocal = _raise

    assert _messages(svc.current_model_setting_warnings()) == [
        _global_lite("gone-lite"),
        UNREAD_PERSONAS,
    ]


def test_query_failure_keeps_global_warnings_and_closes_the_session(world, monkeypatch):
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    world.add_persona(LIGHTWEIGHT_MODEL="gone-persona-lite")
    svc = world.start()
    session = _QueryFailingSession()
    svc.SessionLocal = lambda: session

    assert _messages(svc.current_model_setting_warnings()) == [
        _global_lite("gone-lite"),
        UNREAD_PERSONAS,
    ]
    assert session.closed


def test_route_shows_global_warnings_when_persona_rows_cannot_be_read(world, monkeypatch):
    """画面のルートまで通しても、DB の失敗で警告が消えて正常に見えることはない。"""
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    svc = world.start()
    svc.SessionLocal = _raise

    assert config_route.get_startup_warnings(manager=svc) == {"warnings": [
        {"source": "model_config", "message": _global_lite("gone-lite")},
        {"source": "model_config", "message": UNREAD_PERSONAS},
    ]}


def test_media_summary_does_not_substitute_the_builtin_default(monkeypatch):
    """要約モデルの定義が無いとき、組み込みの既定モデルの定義があっても要約しない。"""
    from saiverse import media_summary

    monkeypatch.setattr("llm_clients.factory.get_llm_client", _raise)

    assert media_summary._resolve_client_for_model("gone-image", "image") is None


# --- 画面のルート---------------------------------------------------------------

RECORDED = {"source": "persona_load", "message": "Failed to load persona 'eris_city_a': boom"}


def test_route_returns_recorded_warnings_then_current_ones(world):
    world.add_persona(LIGHTWEIGHT_MODEL="gone-lite")
    svc = world.start()
    svc.startup_warnings.append(RECORDED)
    lite_warning = {"source": "model_config", "message": _persona_lite(NAME, "gone-lite")}

    assert config_route.get_startup_warnings(manager=svc) == {
        "warnings": [RECORDED, lite_warning],
    }
    # 計算分は保存済みの側へ溜まらない — 画面を開き直しても増えない
    assert svc.startup_warnings == [RECORDED]
    assert config_route.get_startup_warnings(manager=svc) == {
        "warnings": [RECORDED, lite_warning],
    }

    # 起動後に選び直すと、次に画面が取りに来たときには消えている
    world.set_persona_models(LIGHTWEIGHT_MODEL=DEFINED_KEY)
    assert config_route.get_startup_warnings(manager=svc) == {"warnings": [RECORDED]}


def test_route_returns_recorded_warnings_when_computation_fails(world, monkeypatch):
    world.add_persona(LIGHTWEIGHT_MODEL="gone-lite")
    svc = world.start()
    svc.startup_warnings.append(RECORDED)
    monkeypatch.setattr(svc, "current_model_setting_warnings", _raise)

    assert config_route.get_startup_warnings(manager=svc) == {"warnings": [RECORDED]}
    assert svc.startup_warnings == [RECORDED]
