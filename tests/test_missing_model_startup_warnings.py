"""起動時の「設定されているのに定義が見つからないモデル」警告のテスト。

組み込みモデルの定義を削除すると、そのモデルを選んでいたユーザーの設定は
存在しないモデルを指したまま残る。標準モデルには起動時の警告 (と既定モデルへの
フォールバック) が前からあったが、軽量・Memory Weave・画像/音声/動画要約の
各モデルは画面に何も出なかった。ここでは次を押さえる:

- 同じ理由が当てはまる役割にも、ペルソナ単位とグローバル設定単位で一件ずつ警告が出る
- 判定が、各役割の値を実際に使う側と同じ引き方になっている
- ペルソナ単位の画像/音声/動画要約モデルは、値を読む箇所が無いので警告しない
- 検査の失敗がペルソナの読み込みや起動を止めない
- 標準モデルの既存の警告とフォールバックは変わらない

モデル定義は MODEL_CONFIGS を差し替えた偽物を使い、PersonaCore はスタブにする
(本物は SAIMemory や埋め込みモデルまで巻き込む)。LLM は呼ばない。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI as AIModel, Base, City as CityModel
from manager.initialization import InitializationMixin
from manager.persona import PersonaMixin
from saiverse import model_configs, model_defaults

PERSONA_ID = "air_city_a"
STANDARD_KEY = "test-standard-model"
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


@pytest.fixture(autouse=True)
def fake_model_definitions(monkeypatch):
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        STANDARD_KEY: {
            "model": "test-standard-api-name", "provider": "stub", "context_length": 1000,
        },
        DEFINED_KEY: {
            "model": DEFINED_API_NAME, "provider": "stub", "context_length": 1000,
        },
    })
    for key in ROLE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _raise(*_args, **_kwargs):
    raise RuntimeError("boom")


def _model_config_messages(svc):
    return [w["message"] for w in svc.startup_warnings if w["source"] == "model_config"]


# --- ペルソナ単位 ---------------------------------------------------------------


class _StubPersonaCore:
    """PersonaCore の代わり。受け取った引数をそのまま属性として持つ。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _persona_manager_stub(session_factory):
    svc = PersonaMixin.__new__(PersonaMixin)
    svc.SessionLocal = session_factory
    svc.city_id = 1
    svc.city_name = "city_a"
    svc.model = None
    svc._base_model = STANDARD_KEY
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
    svc.startup_warnings = []
    return svc


@pytest.fixture
def load_persona(monkeypatch):
    """AI 行を一つ作り、起動時と同じ入口 (_load_personas_from_db) で読み込む。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr("manager.persona.PersonaCore", _StubPersonaCore)

    def _load(**model_columns):
        columns = {"DEFAULT_MODEL": STANDARD_KEY, **model_columns}
        db = session_factory()
        try:
            db.add(CityModel(
                CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000,
            ))
            db.add(AIModel(AIID=PERSONA_ID, HOME_CITYID=1, AINAME="Air", **columns))
            db.commit()
        finally:
            db.close()
        svc = _persona_manager_stub(session_factory)
        svc._load_personas_from_db()
        return svc

    yield _load
    engine.dispose()


def test_persona_missing_lightweight_and_weave_each_warn(load_persona):
    svc = load_persona(
        LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
    )

    assert _model_config_messages(svc) == [
        f"ペルソナ '{PERSONA_ID}' の軽量モデル 'gone-lite' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
        f"ペルソナ '{PERSONA_ID}' のMemory Weaveモデル 'gone-weave' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT
        + "選び直すまで、このペルソナの記憶の整理は止まったままになります。",
    ]
    # 検査は知らせるだけ — 値は差し替えずにそのまま PersonaCore へ渡る
    persona = svc.personas[PERSONA_ID]
    assert persona.model == STANDARD_KEY
    assert persona.lightweight_model == "gone-lite"
    assert persona.memory_weave_model == "gone-weave"


def test_persona_media_summary_models_are_not_checked(load_persona):
    """ペルソナ単位の画像/音声/動画要約モデルは保存されるだけで、読む箇所が無い。
    選び直しても挙動が変わらない設定に「選び直して」と出さない。"""
    svc = load_persona(
        VISION_MODEL="gone-vision",
        AUDIO_MODEL="gone-audio",
        VIDEO_MODEL="gone-video",
    )

    assert _model_config_messages(svc) == []
    persona = svc.personas[PERSONA_ID]
    assert persona.vision_model == "gone-vision"
    assert persona.audio_model == "gone-audio"
    assert persona.video_model == "gone-video"


def test_persona_defined_values_do_not_warn(load_persona):
    svc = load_persona(
        LIGHTWEIGHT_MODEL=DEFINED_KEY,
        MEMORY_WEAVE_MODEL=DEFINED_API_NAME,
    )
    assert svc.startup_warnings == []
    assert PERSONA_ID in svc.personas


def test_persona_unset_and_empty_values_do_not_warn(load_persona):
    svc = load_persona(LIGHTWEIGHT_MODEL=None, MEMORY_WEAVE_MODEL="")

    assert svc.startup_warnings == []
    assert PERSONA_ID in svc.personas


def test_persona_lookup_follows_each_consumer(load_persona):
    """API モデル名は、Memory Weave (find_model_config) なら引けるが、
    軽量モデル (設定キーの完全一致) では引けない。"""
    svc = load_persona(
        LIGHTWEIGHT_MODEL=DEFINED_API_NAME,
        MEMORY_WEAVE_MODEL=DEFINED_API_NAME,
    )

    assert _model_config_messages(svc) == [
        f"ペルソナ '{PERSONA_ID}' の軽量モデル '{DEFINED_API_NAME}' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
    ]


def test_persona_failing_lookup_does_not_stop_loading(load_persona, monkeypatch):
    monkeypatch.setattr(model_configs, "find_model_config", _raise)

    svc = load_persona(LIGHTWEIGHT_MODEL="gone-lite", MEMORY_WEAVE_MODEL="gone-weave")

    assert PERSONA_ID in svc.personas
    assert not any(w["source"] == "persona_load" for w in svc.startup_warnings)
    # 引けなかった役割は飛ばし、残りの役割の検査は続く
    assert _model_config_messages(svc) == [
        f"ペルソナ '{PERSONA_ID}' の軽量モデル 'gone-lite' の設定ファイルが見つかりません。"
        + PERSONA_RESELECT,
    ]


def test_persona_failing_check_does_not_stop_loading(load_persona, monkeypatch):
    monkeypatch.setattr(model_defaults, "missing_model_warnings", _raise)

    svc = load_persona(LIGHTWEIGHT_MODEL="gone-lite")

    assert PERSONA_ID in svc.personas
    assert svc.startup_warnings == []


def test_persona_default_model_warning_and_fallback_unchanged(load_persona):
    svc = load_persona(DEFAULT_MODEL="gone-default")

    assert _model_config_messages(svc) == [
        f"ペルソナ '{PERSONA_ID}' のモデル 'gone-default' の設定ファイルが見つかりません。"
        f"デフォルトモデル '{STANDARD_KEY}' にフォールバックしました。",
    ]
    assert svc.personas[PERSONA_ID].model == STANDARD_KEY


# --- グローバル設定単位 ---------------------------------------------------------


def _init_global_model_config(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    svc = InitializationMixin.__new__(InitializationMixin)
    svc.city_name = "city_a"
    svc._init_model_config(STANDARD_KEY)
    return svc


def test_global_missing_values_warn(monkeypatch):
    svc = _init_global_model_config(
        monkeypatch,
        SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL="gone-lite",
        MEMORY_WEAVE_MODEL="gone-weave",
        SAIVERSE_IMAGE_SUMMARY_MODEL="gone-image",
        SAIVERSE_AUDIO_SUMMARY_MODEL="gone-audio",
        SAIVERSE_VIDEO_SUMMARY_MODEL="gone-video",
    )

    assert _model_config_messages(svc) == [
        "グローバル設定の軽量モデル 'gone-lite' の設定ファイルが見つかりません。" + GLOBAL_RESELECT,
        "グローバル設定のMemory Weaveモデル 'gone-weave' の設定ファイルが見つかりません。"
        + GLOBAL_RESELECT
        + "Memory Weaveモデルを個別に設定していないペルソナは、選び直すまで記憶の整理が止まったままになります。",
        "グローバル設定の画像要約モデル 'gone-image' の設定ファイルが見つかりません。" + GLOBAL_RESELECT,
        "グローバル設定の音声要約モデル 'gone-audio' の設定ファイルが見つかりません。" + GLOBAL_RESELECT,
        "グローバル設定の動画要約モデル 'gone-video' の設定ファイルが見つかりません。" + GLOBAL_RESELECT,
    ]
    assert svc._base_model == STANDARD_KEY


def test_global_unset_empty_and_defined_values_do_not_warn(monkeypatch):
    svc = _init_global_model_config(
        monkeypatch,
        SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL=DEFINED_KEY,
        MEMORY_WEAVE_MODEL=DEFINED_API_NAME,
        SAIVERSE_IMAGE_SUMMARY_MODEL="",
        # 音声・動画要約は未設定のまま
    )

    assert svc.startup_warnings == []


def test_global_failing_check_does_not_stop_startup(monkeypatch):
    monkeypatch.setattr(model_defaults, "missing_model_warnings", _raise)

    svc = _init_global_model_config(monkeypatch, MEMORY_WEAVE_MODEL="gone-weave")

    assert svc.startup_warnings == []
    assert svc._base_model == STANDARD_KEY
