"""「設定されているのに定義が見つからないモデル」の警告のテスト。

組み込みモデルの定義を削除すると、そのモデルを選んでいたユーザーの設定は
存在しないモデルを指したまま残る。画面はページを開くたびに
GET /api/config/startup-warnings を読んでこの警告を出す。警告は起動時に積まず、
取りに来るたびにいまの設定から作る (manager/initialization.py の
``current_model_setting_warnings``)。ここでは次を押さえる:

- 標準・軽量・Memory Weave・画像/音声/動画要約モデルが、ペルソナ単位と
  グローバル設定単位で一件ずつ警告になる。ペルソナ単位の要約モデルは、値を
  読む箇所が無いので警告しない
- 判定が、各役割の値を実際に使う側と同じ引き方になっている
- 起動後に設定を直す・モデル定義を足す/消す (読み直しを含む) と、再起動せずに
  次の計算へ反映される
- 起動時には警告を積まないが、標準モデルを代わりのモデルで読み込むことは今までどおり
- グローバル設定の標準モデルを起動後に変えても、個別の標準モデルを持たないペルソナは
  再起動まで前のモデルで動く。そのあいだはそのことを知らせ、起動し直す・チュートリアルの
  自動設定 (update_default_model) で揃うと消える
- 画面のルートは保存済みの警告の後ろに計算分を足し、計算が失敗しても保存済みは返す

モデル定義は MODEL_CONFIGS を差し替えた偽物を使い (読み直しのテストだけは本物の
reload_configs を一時フォルダ相手に通す)、PersonaCore はスタブにする (本物は
SAIMemory や埋め込みモデルまで巻き込む)。LLM は呼ばない。
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

PERSONA_ID = "air_city_a"
#: 標準モデルの定義が見つからないとき、起動時に代わりに読み込まれるモデル
FALLBACK_KEY = model_defaults.BUILTIN_DEFAULT_LITE_MODEL
DEFINED_KEY = "test-defined-model"
#: DEFINED_KEY の定義が持つ API モデル名。設定キーとしては存在しない。
DEFINED_API_NAME = "vendor/test-defined-api-name"

ROLE_ENV_KEYS = (
    "SAIVERSE_DEFAULT_MODEL",
    "SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL",
    "MEMORY_WEAVE_MODEL",
    "SAIVERSE_IMAGE_SUMMARY_MODEL",
    "SAIVERSE_AUDIO_SUMMARY_MODEL",
    "SAIVERSE_VIDEO_SUMMARY_MODEL",
)

PERSONA_RESELECT = "ペルソナ設定から選び直してください。"
GLOBAL_RESELECT = "グローバル設定の「モデルロール」から選び直してください。"
#: 画像・音声・動画要約モデルの定義が見つからないときに、グローバル設定の警告へ付く一文
#: (saiverse/media_summary.py は組み込みの既定モデルへ切り替えて要約を続ける)
MEDIA_SUBSTITUTE = f"いまは組み込みの既定モデル '{FALLBACK_KEY}' に切り替えて要約を続けようとしています。"


def _changed_notice(value: str, *, running: str) -> str:
    """グローバル設定の標準モデルが定義のある別の値に変わったときの知らせ。"""
    return (
        f"グローバル設定の標準モデルは '{value}' に変わっていますが、"
        "個別の標準モデルを持たないペルソナには再起動するまで反映されません。"
        f"いまは '{running}' で動いています。"
    )


def _cleared_notice(*, running: str) -> str:
    """グローバル設定の標準モデルが空・未設定になったときの知らせ。"""
    return (
        "グローバル設定の標準モデルが未設定になりましたが、"
        f"個別の標準モデルを持たないペルソナは再起動するまで '{running}' で動き続けます。"
        f"再起動後は組み込みの既定モデル '{FALLBACK_KEY}' になります。"
    )


def _definition(api_name: str) -> dict:
    return {"model": api_name, "provider": "stub", "context_length": 1000}


@pytest.fixture(autouse=True)
def fake_model_definitions(monkeypatch):
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        FALLBACK_KEY: _definition("test-fallback-api-name"),
        DEFINED_KEY: _definition(DEFINED_API_NAME),
    })
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
    """PersonaCore の代わり。受け取った引数をそのまま属性として持つ。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Manager(InitializationMixin, PersonaMixin):
    """起動時にモデル設定を解決する二つの入口 (_init_model_config と
    _load_personas_from_db) と、警告の計算を持つ最小の manager。"""


class _World:
    """一つの City と一人のペルソナの DB 行。起動は start() で行う。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def add_persona(self, **model_columns):
        db = self.session_factory()
        try:
            db.add(AIModel(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Air", **model_columns))
            db.commit()
        finally:
            db.close()

    def set_persona_models(self, **model_columns):
        """ペルソナ設定の保存 (manager/admin.py の update_ai) が DB 行を書き換えるのと同じ変更。"""
        db = self.session_factory()
        try:
            ai = db.query(AIModel).filter_by(AIID=PERSONA_ID).one()
            for column, value in model_columns.items():
                setattr(ai, column, value)
            db.commit()
        finally:
            db.close()

    def start(self, model=None) -> _Manager:
        """SAIVerseManager.__init__ と同じ順で、モデル設定の解決とペルソナの読み込みを行う。

        ``model`` は SAIVerseManager.__init__ の同名の引数。呼ぶたびに同じ DB から
        起動し直すので、二度呼べば再起動になる。"""
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
        svc._init_model_config(model)  # main.py は model を渡さない
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


def test_persona_missing_models_each_warn(world):
    world.add_persona(
        DEFAULT_MODEL="gone-default",
        LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
    )
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        f"ペルソナ '{PERSONA_ID}' の標準モデル 'gone-default' の設定ファイルが見つかりません。"
        f"いまはモデル '{FALLBACK_KEY}' で代わりに動いています。"
        "ペルソナ設定から選び直してください。",
        f"ペルソナ '{PERSONA_ID}' の軽量モデル 'gone-lite' の設定ファイルが見つかりません。"
        "ペルソナ設定から選び直してください。",
        f"ペルソナ '{PERSONA_ID}' のMemory Weaveモデル 'gone-weave' の設定ファイルが見つかりません。"
        "ペルソナ設定から選び直してください。"
        "選び直すまで、このペルソナの記憶の整理は止まったままになります。",
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
    """ペルソナ単位の画像/音声/動画要約モデルは保存されるだけで、読む箇所が無い。
    選び直しても挙動が変わらない設定に「選び直して」と出さない。"""
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
        f"ペルソナ '{PERSONA_ID}' の標準モデル '{DEFINED_API_NAME}' の設定ファイルが見つかりません。"
        f"いまはモデル '{FALLBACK_KEY}' で代わりに動いています。" + PERSONA_RESELECT,
        f"ペルソナ '{PERSONA_ID}' の軽量モデル '{DEFINED_API_NAME}' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
    ]
    # 標準モデルは読み込む側でも引けず、代わりのモデルで読み込まれている
    assert svc.personas[PERSONA_ID].model == FALLBACK_KEY


def test_persona_not_loaded_omits_substitute_sentence(world, monkeypatch):
    """読み込めなかったペルソナは、何で動いているとも言えない。"""
    world.add_persona(DEFAULT_MODEL="gone-default")
    monkeypatch.setattr("manager.persona.PersonaCore", _raise)
    svc = world.start()

    assert PERSONA_ID not in svc.personas
    assert _messages(svc.current_model_setting_warnings()) == [
        f"ペルソナ '{PERSONA_ID}' の標準モデル 'gone-default' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
    ]


def test_persona_running_under_the_missing_name_omits_substitute_sentence(world):
    """ペルソナ設定の保存 (manager/admin.py の update_ai) は、定義を引く前にメモリ上の
    モデル名を書き換える。設定値と同じ名前を「代わりに動いている」とは言えない。"""
    world.add_persona(DEFAULT_MODEL="gone-default")
    svc = world.start()
    svc.personas[PERSONA_ID].model = "gone-default"

    assert _messages(svc.current_model_setting_warnings()) == [
        f"ペルソナ '{PERSONA_ID}' の標準モデル 'gone-default' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
    ]


def test_failing_lookup_skips_only_that_role(world, monkeypatch):
    world.add_persona(LIGHTWEIGHT_MODEL="gone-lite", MEMORY_WEAVE_MODEL="gone-weave")
    svc = world.start()
    monkeypatch.setattr(model_configs, "find_model_config", _raise)

    # 引けなかった役割 (Memory Weave) は飛ばし、残りの役割の検査は続く
    assert _messages(svc.current_model_setting_warnings()) == [
        f"ペルソナ '{PERSONA_ID}' の軽量モデル 'gone-lite' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
    ]


# --- グローバル設定単位 ---------------------------------------------------------


def test_global_missing_values_each_warn(world, monkeypatch):
    _set_env(
        monkeypatch,
        SAIVERSE_DEFAULT_MODEL="gone-default",
        SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
        SAIVERSE_IMAGE_SUMMARY_MODEL="gone-image",
        SAIVERSE_AUDIO_SUMMARY_MODEL="gone-audio",
        SAIVERSE_VIDEO_SUMMARY_MODEL="gone-video",
    )
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        "グローバル設定の標準モデル 'gone-default' の設定ファイルが見つかりません。"
        f"いまはモデル '{FALLBACK_KEY}' で代わりに動いています。"
        "グローバル設定の「モデルロール」から選び直してください。",
        "グローバル設定の軽量モデル 'gone-lite' の設定ファイルが見つかりません。" + GLOBAL_RESELECT,
        "グローバル設定のMemory Weaveモデル 'gone-weave' の設定ファイルが見つかりません。"
        + GLOBAL_RESELECT
        + "Memory Weaveモデルを個別に設定していないペルソナは、選び直すまで記憶の整理が止まったままになります。",
        "グローバル設定の画像要約モデル 'gone-image' の設定ファイルが見つかりません。"
        + MEDIA_SUBSTITUTE + GLOBAL_RESELECT,
        "グローバル設定の音声要約モデル 'gone-audio' の設定ファイルが見つかりません。"
        + MEDIA_SUBSTITUTE + GLOBAL_RESELECT,
        "グローバル設定の動画要約モデル 'gone-video' の設定ファイルが見つかりません。"
        + MEDIA_SUBSTITUTE + GLOBAL_RESELECT,
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

    assert svc._base_model == DEFINED_KEY
    assert svc.current_model_setting_warnings() == []


def test_global_lookup_follows_each_consumer(world, monkeypatch):
    """API モデル名は、Memory Weave・画像/音声/動画要約 (find_model_config) なら
    引けるが、標準・軽量モデル (設定キーの完全一致) では引けない。"""
    _set_env(monkeypatch, **{key: DEFINED_API_NAME for key in ROLE_ENV_KEYS})
    svc = world.start()

    assert _messages(svc.current_model_setting_warnings()) == [
        f"グローバル設定の標準モデル '{DEFINED_API_NAME}' の設定ファイルが見つかりません。"
        f"いまはモデル '{FALLBACK_KEY}' で代わりに動いています。" + GLOBAL_RESELECT,
        f"グローバル設定の軽量モデル '{DEFINED_API_NAME}' の設定ファイルが見つかりません。"
        + GLOBAL_RESELECT,
    ]
    # 標準モデルは起動時の読み込みでも引けず、代わりのモデルで動いている
    assert svc._base_model == FALLBACK_KEY


def test_global_running_under_the_missing_name_omits_substitute_sentence(world, monkeypatch):
    """チュートリアルの自動設定が呼ぶ update_default_model は、定義を引く前に
    _base_model を書き換える。設定値と同じ名前を「代わりに動いている」とは言えない。"""
    svc = world.start()
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", "gone-default")
    svc._base_model = "gone-default"

    assert _messages(svc.current_model_setting_warnings()) == [
        "グローバル設定の標準モデル 'gone-default' の設定ファイルが見つかりません。" + GLOBAL_RESELECT,
    ]


# --- 起動時 ---------------------------------------------------------------------


def test_startup_records_no_model_setting_warnings_but_still_falls_back(world, monkeypatch):
    _set_env(monkeypatch, **{key: "gone-global" for key in ROLE_ENV_KEYS})
    world.add_persona(
        DEFAULT_MODEL="gone-default",
        LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
    )
    svc = world.start()

    # 同じ事実を二重に出さないよう、起動時には積まない
    assert svc.startup_warnings == []
    # 標準モデルを代わりのモデルで読み込むことは今までどおり
    assert svc._base_model == FALLBACK_KEY
    assert svc.provider == "stub"
    persona = svc.personas[PERSONA_ID]
    assert persona.model == FALLBACK_KEY
    assert persona.provider == "stub"
    # 標準モデル以外は、値を差し替えずにそのまま渡る
    assert persona.lightweight_model == "gone-lite"
    assert persona.memory_weave_model == "gone-weave"
    # 積まなかった分は、取りに来たときに作られる (グローバル 6 件 + ペルソナ 3 件)
    assert len(svc.current_model_setting_warnings()) == 9


# --- 起動後の変更 ---------------------------------------------------------------


def test_fixing_persona_row_after_startup_clears_warnings(world):
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
    assert svc.current_model_setting_warnings() == []


def test_fixing_global_env_after_startup_clears_warnings(world, monkeypatch):
    _set_env(monkeypatch, **{key: "gone-global" for key in ROLE_ENV_KEYS})
    svc = world.start()
    assert len(svc.current_model_setting_warnings()) == 6

    # グローバル設定のモデルロールの保存も、チュートリアルの自動設定も、
    # write_env_updates (api/routes/admin.py) で os.environ を書き換える
    _set_env(monkeypatch, **{key: DEFINED_KEY for key in ROLE_ENV_KEYS})
    # 定義が見つからない警告は消える。標準モデルだけは、起動時に代わりに読み込んだ
    # モデルのまま再起動まで動くので、そのことの知らせが残る。
    assert _messages(svc.current_model_setting_warnings()) == [
        _changed_notice(DEFINED_KEY, running=FALLBACK_KEY),
    ]


def test_adding_definition_after_startup_clears_warnings(world, monkeypatch):
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


def test_removing_definition_after_startup_raises_warnings(world, monkeypatch):
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
        f"グローバル設定の軽量モデル '{DEFINED_KEY}' の設定ファイルが見つかりません。"
        + GLOBAL_RESELECT,
        # ペルソナは起動時に読み込んだ名前のまま動いているので「代わりに」の一文は付かない
        f"ペルソナ '{PERSONA_ID}' の標準モデル '{DEFINED_KEY}' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
        f"ペルソナ '{PERSONA_ID}' のMemory Weaveモデル '{DEFINED_KEY}' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT
        + "選び直すまで、このペルソナの記憶の整理は止まったままになります。",
    ]


def test_real_reload_is_reflected(world, monkeypatch, tmp_path):
    """モデル定義を作る・消すルートは最後に model_configs.reload_configs() を呼ぶ。
    その本物を、一時フォルダの user_data と組み込み定義 (builtin_data) だけを相手に通す。"""
    user_data = tmp_path / "user_data"
    models_dir = user_data / "models"
    models_dir.mkdir(parents=True)
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", user_data)
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "no_expansion")

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
        f"ペルソナ '{PERSONA_ID}' の軽量モデル 'test-reloaded-model' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
    ]


# --- 標準モデルの変更が再起動まで反映されないことの知らせ ------------------------


def test_changing_global_default_model_notices_until_restart(world, monkeypatch):
    """グローバル設定の保存は os.environ を書き換えるだけで、個別の標準モデルを持たない
    ペルソナのモデルは変えない。そのあいだは知らせ、起動し直すとその値で動いて消える。"""
    world.add_persona()  # 個別の標準モデルを持たない
    svc = world.start()
    assert svc.personas[PERSONA_ID].model == FALLBACK_KEY

    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", DEFINED_KEY)
    assert svc.current_model_setting_warnings() == [
        {"source": "model_config", "message": _changed_notice(DEFINED_KEY, running=FALLBACK_KEY)},
    ]
    # 知らせのとおり、ペルソナは前のモデルのまま
    assert svc.personas[PERSONA_ID].model == FALLBACK_KEY

    restarted = world.start()
    assert restarted._base_model == DEFINED_KEY
    assert restarted.personas[PERSONA_ID].model == DEFINED_KEY
    assert restarted.current_model_setting_warnings() == []


@pytest.mark.parametrize("cleared", [
    pytest.param("", id="empty"),
    pytest.param(None, id="unset"),
])
def test_clearing_global_default_model_notices_until_restart(world, monkeypatch, cleared):
    """グローバル設定で標準モデルを空にしても、再起動までは前のモデルで動く。
    起動し直すと組み込みの既定モデルで動き、知らせは消える。"""
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", DEFINED_KEY)
    world.add_persona()
    svc = world.start()
    assert svc.personas[PERSONA_ID].model == DEFINED_KEY

    if cleared is None:
        monkeypatch.delenv("SAIVERSE_DEFAULT_MODEL")
    else:
        monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", cleared)
    assert svc.current_model_setting_warnings() == [
        {"source": "model_config", "message": _cleared_notice(running=DEFINED_KEY)},
    ]

    restarted = world.start()
    assert restarted._base_model == FALLBACK_KEY
    assert restarted.personas[PERSONA_ID].model == FALLBACK_KEY
    assert restarted.current_model_setting_warnings() == []


@pytest.mark.parametrize("startup_value", [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param(DEFINED_KEY, id="defined"),
    pytest.param(FALLBACK_KEY, id="builtin-default-by-name"),
])
def test_unchanged_global_default_model_does_not_notice(world, monkeypatch, startup_value):
    if startup_value is not None:
        monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", startup_value)
    svc = world.start()

    assert svc.current_model_setting_warnings() == []


def test_update_default_model_clears_the_notice(world, monkeypatch):
    """チュートリアルの自動設定は、環境変数を書いたあと update_default_model で
    _base_model を同じ値に揃える (api/routes/tutorial.py の auto_configure_models)。
    揃ったあとは知らせない。"""
    from saiverse.saiverse_manager import SAIVerseManager

    svc = world.start()
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", DEFINED_KEY)
    assert _messages(svc.current_model_setting_warnings()) == [
        _changed_notice(DEFINED_KEY, running=FALLBACK_KEY),
    ]

    SAIVerseManager.update_default_model(svc, DEFINED_KEY)

    assert svc._base_model == DEFINED_KEY
    assert svc.current_model_setting_warnings() == []


def test_changing_to_an_undefined_value_is_left_to_the_missing_definition_warning(
    world, monkeypatch,
):
    """定義の無い値に変えたときは「設定ファイルが見つかりません」の警告だけを出す。
    再起動しても組み込みの既定モデルへ落ちるので、その値に変わるとは言えない。"""
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", DEFINED_KEY)
    svc = world.start()

    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", "gone-default")
    assert _messages(svc.current_model_setting_warnings()) == [
        "グローバル設定の標準モデル 'gone-default' の設定ファイルが見つかりません。"
        f"いまはモデル '{DEFINED_KEY}' で代わりに動いています。" + GLOBAL_RESELECT,
    ]


def test_startup_with_a_model_argument_does_not_notice(world, monkeypatch):
    """SAIVerseManager.__init__ は標準モデルを引数 model でも受け取る。いまの起動口
    (main.py ほか) は渡さないが、渡された起動では環境変数が標準モデルを決めていないので、
    環境変数との食い違いを「再起動するまで反映されない」とは言えない。"""
    svc = world.start(model=DEFINED_KEY)
    assert svc._base_model == DEFINED_KEY
    # 環境変数は未設定のまま。引数を見ずに比べると「未設定になりましたが」を出してしまう
    assert svc.current_model_setting_warnings() == []

    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", FALLBACK_KEY)
    assert svc.current_model_setting_warnings() == []


# --- 読み出し・引き当ての失敗 -----------------------------------------------------

UNREAD_PERSONAS = "ペルソナごとのモデル設定を読み出せなかったため、ペルソナ単位の確認はできていません。"
GLOBAL_LITE_WARNING = (
    "グローバル設定の軽量モデル 'gone-lite' の設定ファイルが見つかりません。" + GLOBAL_RESELECT
)


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
        GLOBAL_LITE_WARNING,
        UNREAD_PERSONAS,
    ]


def test_query_failure_keeps_global_warnings_and_closes_the_session(world, monkeypatch):
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    world.add_persona(LIGHTWEIGHT_MODEL="gone-persona-lite")
    svc = world.start()
    session = _QueryFailingSession()
    svc.SessionLocal = lambda: session

    assert _messages(svc.current_model_setting_warnings()) == [
        GLOBAL_LITE_WARNING,
        UNREAD_PERSONAS,
    ]
    assert session.closed


def test_route_shows_global_warnings_when_persona_rows_cannot_be_read(world, monkeypatch):
    """画面のルートまで通しても、DB の失敗で警告が消えて正常に見えることはない。"""
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    svc = world.start()
    svc.SessionLocal = _raise

    assert config_route.get_startup_warnings(manager=svc) == {"warnings": [
        {"source": "model_config", "message": GLOBAL_LITE_WARNING},
        {"source": "model_config", "message": UNREAD_PERSONAS},
    ]}


def test_media_summary_substitute_sentence_needs_the_fallback_definition(
    world, monkeypatch, tmp_path,
):
    """要約側は、組み込みの既定モデルの定義も引けないと要約しない
    (saiverse/media_summary.py の _resolve_client_for_model)。そのときは
    「代わりに要約しています」と言わない。

    要約側は組み込みの既定モデルの名前を import 時に束縛しているので、定数ではなく
    定義の側を消す。find_model_config は MODEL_CONFIGS に無い名前を定義フォルダの
    ファイルからも探す (組み込みの既定モデルの JSON は builtin_data/models/ にある) ので、
    三層の定義フォルダも空の一時フォルダへ向ける。"""
    from saiverse import media_summary

    monkeypatch.setenv("SAIVERSE_IMAGE_SUMMARY_MODEL", "gone-image")
    svc = world.start()
    # 起動の後で消す。起動時は標準モデルの代わりにもこの定義が使われる。
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        key: value
        for key, value in model_configs.MODEL_CONFIGS.items()
        if key != FALLBACK_KEY
    })
    for name in ("USER_DATA_DIR", "EXPANSION_DATA_DIR", "BUILTIN_DATA_DIR"):
        monkeypatch.setattr(data_paths, name, tmp_path / name)

    assert _messages(svc.current_model_setting_warnings()) == [
        "グローバル設定の画像要約モデル 'gone-image' の設定ファイルが見つかりません。"
        + GLOBAL_RESELECT,
    ]
    # 同じ状態で、要約側も代わりのモデルへ切り替えられずに諦める
    assert media_summary._resolve_client_for_model("gone-image", "image") is None


# --- 画面のルート---------------------------------------------------------------

RECORDED = {"source": "persona_load", "message": "Failed to load persona 'eris_city_a': boom"}


def test_route_returns_recorded_warnings_then_current_ones(world):
    world.add_persona(LIGHTWEIGHT_MODEL="gone-lite")
    svc = world.start()
    svc.startup_warnings.append(RECORDED)
    lite_warning = {
        "source": "model_config",
        "message": (
            f"ペルソナ '{PERSONA_ID}' の軽量モデル 'gone-lite' の設定ファイルが見つかりません。"
            + PERSONA_RESELECT
        ),
    }

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


def test_global_default_model_messages_name_the_override_while_it_is_active(world, monkeypatch):
    """チャット画面のモデル一時上書き (manager.model、set_model が立てる) が有効な間は、
    ペルソナはその上書きのモデルで動いている。グローバル設定の標準モデルの警告と知らせが
    「いまは …」で名指すのも、その上書き。食い違いの判定そのものは _base_model と比べる。"""
    override = "test-override-model"
    model_configs.MODEL_CONFIGS[override] = _definition("vendor/test-override")
    svc = world.start()  # 環境変数は未設定 → 組み込みの既定モデルで起動
    svc.model = override

    _set_env(monkeypatch, SAIVERSE_DEFAULT_MODEL="gone-default")
    assert _messages(svc.current_model_setting_warnings()) == [
        "グローバル設定の標準モデル 'gone-default' の設定ファイルが見つかりません。"
        f"いまはモデル '{override}' で代わりに動いています。"
        + GLOBAL_RESELECT,
    ]

    _set_env(monkeypatch, SAIVERSE_DEFAULT_MODEL=DEFINED_KEY)
    assert _messages(svc.current_model_setting_warnings()) == [
        _changed_notice(DEFINED_KEY, running=override),
    ]
