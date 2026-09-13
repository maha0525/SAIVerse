"""ペルソナが話す標準モデルの決め方と、設定を変えたときの反映のテスト。

設計: docs/intent/persona_model_selection.md
実装: saiverse/persona_model_selection.py、api/routes/admin.py (write_env_updates)、
manager/admin.py (update_ai)、saiverse/saiverse_manager.py (set_model)、sea/runtime.py
(select_llm_client / run_meta_user)

ここで固定すること:

- 決める順番 (一時上書き → 個別 → グローバル → 組み込み) と、選ばれたモデルの定義が
  無ければ次へ進まず「使えない」になること
- 決め直しで一人の失敗がほかの人を止めず、切り替えられなかった人の名前が返ること
- 一時上書き中は上書きのまま、解除で新しい標準モデル、空で組み込み、個別を空で
  グローバルになること
- 定義の無い名前を保存しないこと (環境変数・ペルソナ設定・一時上書き)、同じ要求の
  ほかの値は保存されること、知らせが返ること、同時の保存で .env が欠けないこと
- モデル/プロバイダの設定の読み直しで決め直し、接続を捨てること
- 変更したその場の応答に、切り替えられなかった人の知らせ (notices) が載ること
  (一時上書きの設定、モデルの削除、プロバイダの保存。プロバイダの保存は二度決め直すので、
  後の決め直しの結果を知らせる)
- 書いている途中の返事は始めたときのモデルと接続を使い続けること (本物の runtime を
  通して、返事の途中で保存しても、その返事は前のモデル、次の返事から新しいモデル)。
  一時上書きのパラメータの変更も、書いている途中の返事の接続には載せないこと
- 返事の中の要約 (Stelis のクロニクル、ユーザーと最後に話してからの要約) も返事の
  軽量モデルの接続を使い、返事の外では返事と同じ決め方で軽量モデルを決め、使えなければ
  代わりのモデルで要約しないこと
- 使えないモデルを代わりのモデルで動かさず、チャット画面へ届く形で止めること
- Memory Weave モデルのグローバル設定を空で保存したら、組み込みの既定モデルになること

モデルの定義は MODEL_CONFIGS の差し替え、LLM クライアントは偽物。LLM は呼ばない。
定義ファイルを名前で探す先は一時フォルダに向け、~/.saiverse には触らない。
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI as AIModel, Base, City as CityModel, User as UserModel
from llm_clients.exceptions import ModelUnavailableError
from saiverse import data_paths, model_configs, model_defaults
from saiverse.persona_model_selection import (
    SOURCE_BUILTIN,
    SOURCE_GLOBAL,
    SOURCE_OVERRIDE,
    SOURCE_PERSONA,
    TIER_STANDARD,
    ReapplyResult,
    ReplyModelBinding,
    SpeakingModelChoice,
    UnswitchedPersona,
    apply_speaking_model,
    live_model_override,
    reapply_speaking_models,
    resolve_speaking_model,
)

FALLBACK_KEY = model_defaults.BUILTIN_DEFAULT_LITE_MODEL
MODEL_A = "test-model-a"
MODEL_B = "test-model-b"
OVERRIDE = "test-override-model"
LITE = "test-lite-model"
ROLE_ENV_KEYS = tuple(model_defaults.MODEL_ROLES.values())


def _definition(api_name: str) -> dict:
    return {"model": api_name, "provider": "stub", "context_length": 1000}


def _unswitched(name: str, model: str) -> str:
    return (
        f"{name}は新しい標準モデルに切り替えられなかったため、いまも '{model}' で話しています。"
        "もう一度保存し直すか、再起動すると切り替わります。"
    )


def _raise(*_args, **_kwargs):
    raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def isolated_model_definitions(monkeypatch, tmp_path):
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        FALLBACK_KEY: _definition("vendor/fallback"),
        MODEL_A: _definition("vendor/a"),
        MODEL_B: _definition("vendor/b"),
        OVERRIDE: _definition("vendor/override"),
        LITE: _definition("vendor/lite"),
    })
    # 定義ファイルを名前で探す引き方 (find_model_config) が本番の ~/.saiverse を見ないように
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "expansion_data")
    for key in ROLE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _persona(
    pid: str = "aoi",
    name: str = "アオイ",
    *,
    model: str = MODEL_A,
    lightweight_model=None,
    choice=None,
):
    """PersonaCore の接続の持ち方だけを本物で持つペルソナ (SAIMemory などは起こさない)。"""
    from persona.core import PersonaCore

    persona = PersonaCore.__new__(PersonaCore)
    persona.persona_id = pid
    persona.persona_name = name
    persona.model = model
    persona.provider = "stub"
    persona.context_length = 1000
    persona.model_supports_images = False
    persona.lightweight_model = lightweight_model
    persona.memory_weave_model = None
    persona._init_model_client_state()
    persona.speaking_model_choice = choice
    return persona


class _World:
    """一つの City のペルソナの DB 行と、決め直しが読む manager。"""

    def __init__(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        db = self.SessionLocal()
        try:
            db.add(CityModel(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
            db.commit()
        finally:
            db.close()
        self.manager = SimpleNamespace(
            personas={}, SessionLocal=self.SessionLocal, model=None, model_parameter_overrides={},
        )

    def add(self, pid, name, *, persona, default_model=None, lightweight_model=None, memory_weave_model=None):
        db = self.SessionLocal()
        try:
            db.add(AIModel(
                AIID=pid, HOME_CITYID=1, AINAME=name,
                DEFAULT_MODEL=default_model,
                LIGHTWEIGHT_MODEL=lightweight_model,
                MEMORY_WEAVE_MODEL=memory_weave_model,
            ))
            db.commit()
        finally:
            db.close()
        self.manager.personas[pid] = persona
        return persona

    def set_row(self, pid, **columns):
        db = self.SessionLocal()
        try:
            row = db.query(AIModel).filter_by(AIID=pid).one()
            for column, value in columns.items():
                setattr(row, column, value)
            db.commit()
        finally:
            db.close()


@pytest.fixture
def world():
    w = _World()
    yield w
    w.engine.dispose()


# ---------------------------------------------------------------------------
# 決める順番
# ---------------------------------------------------------------------------


def test_resolution_order_is_override_then_persona_then_global_then_builtin():
    assert resolve_speaking_model(
        override=OVERRIDE, persona_default=MODEL_A, global_default=MODEL_B,
    ) == SpeakingModelChoice(OVERRIDE, SOURCE_OVERRIDE, True)
    assert resolve_speaking_model(
        override=None, persona_default=MODEL_A, global_default=MODEL_B,
    ) == SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True)
    # 空・空白だけの値は未設定と同じ
    assert resolve_speaking_model(
        override="", persona_default="  ", global_default=MODEL_B,
    ) == SpeakingModelChoice(MODEL_B, SOURCE_GLOBAL, True)
    assert resolve_speaking_model(
        override=None, persona_default=None, global_default=None,
    ) == SpeakingModelChoice(FALLBACK_KEY, SOURCE_BUILTIN, True)


def test_a_chosen_model_without_a_definition_is_unavailable_and_does_not_fall_through():
    assert resolve_speaking_model(
        override=None, persona_default="gone-model", global_default=MODEL_B,
    ) == SpeakingModelChoice("gone-model", SOURCE_PERSONA, False)
    assert resolve_speaking_model(
        override="gone-override", persona_default=MODEL_A, global_default=None,
    ) == SpeakingModelChoice("gone-override", SOURCE_OVERRIDE, False)
    assert resolve_speaking_model(
        override=None, persona_default=None, global_default="gone-global",
    ) == SpeakingModelChoice("gone-global", SOURCE_GLOBAL, False)


# ---------------------------------------------------------------------------
# 決め直しと当てはめ
# ---------------------------------------------------------------------------


def test_a_new_global_default_switches_only_personas_without_their_own(world, monkeypatch):
    follower = world.add("aoi", "アオイ", persona=_persona("aoi", "アオイ", model=FALLBACK_KEY))
    keeper = world.add(
        "miku", "ミク", default_model=MODEL_A, persona=_persona("miku", "ミク", model=MODEL_A),
    )
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)

    result = reapply_speaking_models(world.manager)

    assert result.unswitched == []
    assert follower.model == MODEL_B
    assert follower.speaking_model_choice == SpeakingModelChoice(MODEL_B, SOURCE_GLOBAL, True)
    assert keeper.model == MODEL_A


def test_one_persona_failing_to_switch_does_not_stop_the_others(world, monkeypatch):
    broken = _persona("aoi", "アオイ", model=MODEL_A)
    broken.set_model = _raise
    world.add("aoi", "アオイ", persona=broken)
    other = world.add("miku", "ミク", persona=_persona("miku", "ミク", model=MODEL_A))
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)

    result = reapply_speaking_models(world.manager)

    assert other.model == MODEL_B
    assert broken.model == MODEL_A
    assert result.unswitched_names == ["アオイ"]
    assert result.notices() == [_unswitched("アオイ", MODEL_A)]


def test_nobody_is_switched_when_the_db_cannot_be_read(world, monkeypatch):
    aoi = world.add("aoi", "アオイ", persona=_persona("aoi", "アオイ", model=MODEL_A))
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)
    world.manager.SessionLocal = _raise

    result = reapply_speaking_models(world.manager)

    assert aoi.model == MODEL_A
    assert result.notices() == [_unswitched("アオイ", MODEL_A)]


def test_the_override_stays_while_active_and_is_released_to_the_current_default(world, monkeypatch):
    aoi = world.add("aoi", "アオイ", persona=_persona("aoi", "アオイ", model=FALLBACK_KEY))
    world.manager.model = OVERRIDE
    world.manager.model_parameter_overrides = {"temperature": 0.3}
    reapply_speaking_models(world.manager)
    assert aoi.model == OVERRIDE
    assert aoi._pending_parameter_overrides == {"temperature": 0.3}

    # 一時上書き中にグローバル設定を保存しても、上書きのまま
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)
    reapply_speaking_models(world.manager)
    assert aoi.model == OVERRIDE

    # 解除すると新しい標準モデル。上書きのパラメータは残らない
    world.manager.model = None
    world.manager.model_parameter_overrides = {}
    reapply_speaking_models(world.manager)
    assert aoi.model == MODEL_B
    assert aoi._pending_parameter_overrides is None

    # グローバル設定を空に戻すと組み込みの既定モデル
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", "")
    reapply_speaking_models(world.manager)
    assert aoi.speaking_model_choice == SpeakingModelChoice(FALLBACK_KEY, SOURCE_BUILTIN, True)


def test_clearing_the_personas_own_default_uses_the_global_default(world, monkeypatch):
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)
    aoi = world.add(
        "aoi", "アオイ", default_model=MODEL_A, persona=_persona("aoi", "アオイ", model=MODEL_A),
    )
    world.set_row("aoi", DEFAULT_MODEL=None)

    reapply_speaking_models(world.manager, persona_ids=["aoi"])

    assert aoi.speaking_model_choice == SpeakingModelChoice(MODEL_B, SOURCE_GLOBAL, True)


def test_switching_drops_the_old_connection_without_creating_a_new_one(world, monkeypatch):
    aoi = world.add("aoi", "アオイ", persona=_persona("aoi", "アオイ", model=MODEL_A))
    aoi._llm_client = object()
    created = []
    monkeypatch.setattr("persona.core.get_llm_client", lambda *a, **k: created.append(a) or object())
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)

    reapply_speaking_models(world.manager)

    assert aoi._llm_client is None
    assert created == []  # 新しい接続は次の返事を始めたあとに作る


def test_reapply_syncs_lightweight_and_memory_weave_and_resets_the_cold_sweep(world):
    aoi = world.add(
        "aoi", "アオイ", lightweight_model=LITE, memory_weave_model=MODEL_B,
        persona=_persona("aoi", "アオイ", model=FALLBACK_KEY),
    )
    aoi._lightweight_llm_client = object()
    aoi._lightweight_llm_client_initialized = True

    with patch("sea.session_lifecycle.invalidate_cold_sweep_fingerprints") as invalidate:
        reapply_speaking_models(world.manager)

    assert aoi.lightweight_model == LITE
    assert aoi.memory_weave_model == MODEL_B
    assert aoi._lightweight_llm_client is None
    invalidate.assert_called()


# ---------------------------------------------------------------------------
# モデル・プロバイダの設定の読み直し
# ---------------------------------------------------------------------------


def test_removing_a_model_definition_stops_the_persona_and_adding_it_back_restores_it(
    world, monkeypatch,
):
    import saiverse.app_state as app_state

    aoi = world.add(
        "aoi", "アオイ", default_model=MODEL_A,
        persona=_persona(
            "aoi", "アオイ", model=MODEL_A,
            choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True),
        ),
    )
    aoi._llm_client = object()
    monkeypatch.setattr(app_state, "manager", world.manager)

    without_a = {k: v for k, v in model_configs.MODEL_CONFIGS.items() if k != MODEL_A}
    monkeypatch.setattr(model_configs, "load_configs", lambda: dict(without_a))
    model_configs.reload_configs()

    assert aoi.speaking_model_choice == SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, False)
    assert aoi._llm_client is None
    with pytest.raises(ModelUnavailableError) as exc_info:
        ReplyModelBinding.capture(aoi).check_defined(TIER_STANDARD)
    assert exc_info.value.user_message == (
        "アオイが選んでいた標準モデル 'test-model-a' は SAIVerse にありません。"
        "ペルソナ設定で標準モデルを選び直すと、再起動しなくても話せるようになります。"
    )

    with_a = {**without_a, MODEL_A: _definition("vendor/a")}
    monkeypatch.setattr(model_configs, "load_configs", lambda: dict(with_a))
    model_configs.reload_configs()

    assert aoi.speaking_model_choice == SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True)
    ReplyModelBinding.capture(aoi).check_defined(TIER_STANDARD)


def test_a_provider_reload_drops_connections_even_when_the_model_name_is_unchanged(
    world, monkeypatch,
):
    import saiverse.app_state as app_state
    from saiverse import provider_configs

    aoi = world.add(
        "aoi", "アオイ", default_model=MODEL_A, lightweight_model=LITE,
        persona=_persona(
            "aoi", "アオイ", model=MODEL_A, lightweight_model=LITE,
            choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True),
        ),
    )
    aoi._llm_client = object()
    aoi._lightweight_llm_client = object()
    aoi._lightweight_llm_client_initialized = True
    monkeypatch.setattr(app_state, "manager", world.manager)
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", dict(provider_configs.PROVIDER_CONFIGS))
    monkeypatch.setattr(provider_configs, "load_configs", lambda: {})

    provider_configs.reload_configs()

    assert aoi.model == MODEL_A
    assert aoi._llm_client is None
    assert aoi._lightweight_llm_client is None


# ---------------------------------------------------------------------------
# 環境変数の保存 (グローバル設定)
# ---------------------------------------------------------------------------


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    from api.routes import admin

    path = tmp_path / ".env"
    monkeypatch.setattr(admin, "ENV_FILE_PATH", path)
    return path


def _env_lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_env_save_refuses_only_the_model_name_without_a_definition(world, env_file, monkeypatch):
    import saiverse.app_state as app_state
    from api.routes import admin

    monkeypatch.setattr(app_state, "manager", world.manager)
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_A)
    monkeypatch.setenv("SAIVERSE_IMAGE_DEFAULT_QUALITY", "low")
    aoi = world.add("aoi", "アオイ", persona=_persona("aoi", "アオイ", model=MODEL_A))

    result = admin.write_env_updates({
        "SAIVERSE_DEFAULT_MODEL": "gone-modle",
        "SAIVERSE_IMAGE_DEFAULT_QUALITY": "high",
    })

    assert result.rejected_keys == ["SAIVERSE_DEFAULT_MODEL"]
    assert result.notices == [
        "'gone-modle' というモデルは SAIVerse にないため、グローバル設定の標準モデルは保存しませんでした。"
        "個別の標準モデルを持たないペルソナは、いまも 'test-model-a' で話しています。"
    ]
    assert os.environ["SAIVERSE_DEFAULT_MODEL"] == MODEL_A
    assert os.environ["SAIVERSE_IMAGE_DEFAULT_QUALITY"] == "high"
    assert _env_lines(env_file) == ["SAIVERSE_IMAGE_DEFAULT_QUALITY=high"]
    assert aoi.model == MODEL_A


def test_env_route_returns_notices_with_success(env_file, monkeypatch):
    from api.routes import admin

    result = admin.update_env_vars(admin.EnvUpdateRequest(updates={
        "SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL": "gone-lite",
    }))

    assert result["success"] is True
    assert result["rejected_keys"] == ["SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL"]
    assert result["notices"] == [
        "'gone-lite' というモデルは SAIVerse にないため、グローバル設定の軽量モデルは保存しませんでした。"
        f"いまも組み込みの既定モデル '{FALLBACK_KEY}' を使っています。"
    ]
    assert "SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL" not in os.environ


@pytest.mark.parametrize("env_key,label", [
    ("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "軽量モデル"),
    ("MEMORY_WEAVE_MODEL", "Memory Weaveモデル"),
    ("SAIVERSE_IMAGE_SUMMARY_MODEL", "画像要約モデル"),
    ("SAIVERSE_AUDIO_SUMMARY_MODEL", "音声要約モデル"),
    ("SAIVERSE_VIDEO_SUMMARY_MODEL", "動画要約モデル"),
])
def test_env_save_refuses_undefined_names_for_every_model_role(env_file, monkeypatch, env_key, label):
    from api.routes import admin

    monkeypatch.setenv(env_key, MODEL_A)

    result = admin.write_env_updates({env_key: "gone-x"})

    assert result.rejected_keys == [env_key]
    assert result.notices == [
        f"'gone-x' というモデルは SAIVerse にないため、グローバル設定の{label}は保存しませんでした。"
        f"いまも '{MODEL_A}' を使っています。"
    ]
    assert os.environ[env_key] == MODEL_A


def test_env_save_accepts_empty_values_and_switches_personas_right_away(world, env_file, monkeypatch):
    import saiverse.app_state as app_state
    from api.routes import admin

    monkeypatch.setattr(app_state, "manager", world.manager)
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)
    aoi = world.add("aoi", "アオイ", persona=_persona("aoi", "アオイ", model=MODEL_B))

    cleared = admin.write_env_updates({"SAIVERSE_DEFAULT_MODEL": ""})
    assert (cleared.notices, cleared.rejected_keys) == ([], [])
    assert aoi.model == FALLBACK_KEY

    saved = admin.write_env_updates({"SAIVERSE_DEFAULT_MODEL": MODEL_A})
    assert saved.notices == []
    assert aoi.model == MODEL_A


def test_env_save_does_not_refuse_a_name_that_is_already_saved(env_file, monkeypatch):
    from api.routes import admin

    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", "gone-model")

    result = admin.write_env_updates({"SAIVERSE_DEFAULT_MODEL": "gone-model"})

    assert result.rejected_keys == []


def test_env_save_names_personas_that_could_not_be_switched(world, env_file, monkeypatch):
    import saiverse.app_state as app_state
    from api.routes import admin

    monkeypatch.setattr(app_state, "manager", world.manager)
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_A)
    broken = _persona("aoi", "アオイ", model=MODEL_A)
    broken.set_model = _raise
    world.add("aoi", "アオイ", persona=broken)
    other = world.add("miku", "ミク", persona=_persona("miku", "ミク", model=MODEL_A))

    result = admin.write_env_updates({"SAIVERSE_DEFAULT_MODEL": MODEL_B})

    assert other.model == MODEL_B
    assert result.notices == [_unswitched("アオイ", MODEL_A)]


def test_two_env_saves_at_the_same_time_keep_both_variables(env_file, monkeypatch):
    from api.routes import admin

    monkeypatch.setenv("SAIVERSE_TEST_FIRST", "old")
    monkeypatch.setenv("SAIVERSE_TEST_SECOND", "old")
    real_read = admin.read_env_file

    def slow_read():
        rows = real_read()
        # 読んでから書くまでの間に、もう一方の保存を割り込ませる
        time.sleep(0.2)
        return rows

    monkeypatch.setattr(admin, "read_env_file", slow_read)
    threads = [
        threading.Thread(target=admin.write_env_updates, args=({"SAIVERSE_TEST_FIRST": "1"},)),
        threading.Thread(target=admin.write_env_updates, args=({"SAIVERSE_TEST_SECOND": "2"},)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(_env_lines(env_file)) == ["SAIVERSE_TEST_FIRST=1", "SAIVERSE_TEST_SECOND=2"]


# ---------------------------------------------------------------------------
# ペルソナ設定の保存
# ---------------------------------------------------------------------------


class _AdminWorld:
    def __init__(self):
        from manager.admin import AdminService

        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        db = self.SessionLocal()
        try:
            db.add(UserModel(USERID=1, PASSWORD="x", USERNAME="u"))
            db.flush()
            db.add(CityModel(CITYID=1, USERID=1, CITY_SLUG="city_a", UI_PORT=3000, API_PORT=8000))
            db.add(AIModel(
                AIID="aoi", HOME_CITYID=1, AINAME="アオイ",
                DEFAULT_MODEL=MODEL_A, LIGHTWEIGHT_MODEL=LITE, MEMORY_WEAVE_MODEL=MODEL_B,
            ))
            db.commit()
        finally:
            db.close()
        self.persona = _persona(
            "aoi", "アオイ", model=MODEL_A, lightweight_model=LITE,
            choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True),
        )
        admin = AdminService.__new__(AdminService)
        admin.SessionLocal = self.SessionLocal
        admin.building_map = {}
        admin.state = SimpleNamespace(model=None, city_id=1)
        admin.manager = SimpleNamespace(
            model=None, model_parameter_overrides={}, ensure_autonomy_for=lambda ai_id: None,
        )
        admin.personas = {"aoi": self.persona}
        admin._set_persona_avatar = lambda ai_id, value: None
        self.admin = admin

    def update(self, **overrides):
        kwargs = dict(
            name="アオイ", description="desc", system_prompt="prompt", home_city_id=1,
            default_model=MODEL_A, lightweight_model=LITE, memory_weave_model=MODEL_B,
            autonomy_enabled=True, avatar_path=None, avatar_upload=None,
        )
        kwargs.update(overrides)
        return self.admin.update_ai("aoi", **kwargs)

    def row(self):
        db = self.SessionLocal()
        try:
            return db.query(AIModel).filter_by(AIID="aoi").one()
        finally:
            db.close()


@pytest.fixture
def admin_world():
    w = _AdminWorld()
    yield w
    w.engine.dispose()


def test_persona_save_refuses_undefined_model_names_and_saves_the_rest(admin_world):
    with patch("sea.session_lifecycle.invalidate_cold_sweep_fingerprints") as invalidate:
        result = admin_world.update(
            default_model="gone-default",
            lightweight_model="gone-lite",
            memory_weave_model="gone-weave",
            description="新しい説明",
        )

    assert "[WARNING:LLM]" in result
    assert result.split("[WARNING:LLM]", 1)[1].strip().splitlines() == [
        "'gone-default' というモデルは SAIVerse にないため、アオイの標準モデルは保存しませんでした。"
        "アオイはいまも 'test-model-a' で話しています。",
        "'gone-lite' というモデルは SAIVerse にないため、アオイの軽量モデルは保存しませんでした。"
        "いまの設定 'test-lite-model' のままです。",
        "'gone-weave' というモデルは SAIVerse にないため、アオイのMemory Weaveモデルは保存しませんでした。"
        "いまの設定 'test-model-b' のままです。",
    ]
    row = admin_world.row()
    assert (row.DEFAULT_MODEL, row.LIGHTWEIGHT_MODEL, row.MEMORY_WEAVE_MODEL) == (MODEL_A, LITE, MODEL_B)
    assert row.DESCRIPTION == "新しい説明"
    assert admin_world.persona.model == MODEL_A
    invalidate.assert_called()


def test_persona_save_applies_the_model_right_away_and_empty_means_the_global_default(
    admin_world, monkeypatch,
):
    result = admin_world.update(default_model=MODEL_B)
    assert "[WARNING:LLM]" not in result
    assert admin_world.persona.model == MODEL_B

    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", OVERRIDE)
    admin_world.update(default_model="")

    assert admin_world.row().DEFAULT_MODEL is None
    assert admin_world.persona.speaking_model_choice == SpeakingModelChoice(OVERRIDE, SOURCE_GLOBAL, True)


def test_persona_save_keeps_the_override_while_it_is_active(admin_world):
    admin_world.admin.manager.model = OVERRIDE

    admin_world.update(default_model=MODEL_B)

    assert admin_world.row().DEFAULT_MODEL == MODEL_B
    assert admin_world.persona.model == OVERRIDE


def test_a_memory_weave_reselection_resets_the_cold_sweep(admin_world):
    with patch("sea.session_lifecycle.invalidate_cold_sweep_fingerprints") as invalidate:
        admin_world.update(memory_weave_model=MODEL_A)

    assert admin_world.row().MEMORY_WEAVE_MODEL == MODEL_A
    assert admin_world.persona.memory_weave_model == MODEL_A
    invalidate.assert_called()


# ---------------------------------------------------------------------------
# チャット画面の一時上書き
# ---------------------------------------------------------------------------


def test_the_override_route_refuses_a_model_without_a_definition():
    from api.routes import config as config_route

    manager = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        config_route.set_model(config_route.UpdateModelRequest(model="gone-model"), manager=manager)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "'gone-model' というモデルは SAIVerse にないため、チャット画面のモデル一時上書きには"
        "使えません。モデル管理の画面にあるモデルから選び直してください。"
    )
    manager.set_model.assert_not_called()


def test_setting_and_clearing_the_override_decides_everyone_again(world, monkeypatch):
    from saiverse.saiverse_manager import SAIVerseManager

    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)
    keeper = world.add(
        "miku", "ミク", default_model=MODEL_A, persona=_persona("miku", "ミク", model=MODEL_A),
    )
    follower = world.add("aoi", "アオイ", persona=_persona("aoi", "アオイ", model=MODEL_B))
    host = world.manager
    host.state = SimpleNamespace(model=None, context_length=0, provider="")
    host.context_length = 0
    host.provider = ""

    result = SAIVerseManager.set_model(host, OVERRIDE, {"temperature": 0.1})
    assert result.unswitched == []
    assert keeper.model == follower.model == OVERRIDE
    assert host.state.model == OVERRIDE

    SAIVerseManager.set_model(host, "", None)
    assert keeper.model == MODEL_A
    assert follower.model == MODEL_B
    assert host.model is None

    with pytest.raises(ValueError):
        SAIVerseManager.set_model(host, "gone-model", None)
    assert host.model is None


# ---------------------------------------------------------------------------
# 起動時の写しを使わない (旧 update_default_model の契約の置き換え)
# ---------------------------------------------------------------------------


def test_admin_service_reads_the_live_override_instead_of_a_startup_copy():
    from manager.admin import AdminService
    from saiverse.saiverse_manager import SAIVerseManager

    manager = MagicMock()
    manager.model = OVERRIDE
    manager.model_parameter_overrides = {"temperature": 0.5}
    state = MagicMock()
    state.model = None  # 起動時の写しは「上書きなし」のまま
    svc = AdminService(manager, MagicMock(), state)

    assert not hasattr(svc, "_base_model")
    assert not hasattr(SAIVerseManager, "update_default_model")
    assert live_model_override(svc) == (OVERRIDE, {"temperature": 0.5})


# ---------------------------------------------------------------------------
# 書いている途中の返事は始めたときのモデルと接続で書く
# ---------------------------------------------------------------------------


def _node():
    return SimpleNamespace(id="node", memorize=None, speak=False)


def test_a_reply_keeps_its_model_and_connection_when_the_settings_change_midway(monkeypatch):
    from sea.pulse_context import resolve_execution_context
    from sea.runtime import SEARuntime

    def _fake_client(model, provider, context_length, *rest):
        return SimpleNamespace(model=model)

    monkeypatch.setattr("persona.core.get_llm_client", _fake_client)
    monkeypatch.setattr("llm_clients.get_llm_client", _fake_client)
    persona = _persona(model=MODEL_A, choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True))
    state = {"_model_binding": ReplyModelBinding.capture(persona)}
    runtime = SEARuntime(SimpleNamespace(building_histories={}))

    first, model = runtime.select_llm_client(_node(), persona, state=state)
    assert (first.model, model) == (MODEL_A, MODEL_A)

    # 返事の途中で標準モデルが B に変わる (保存・一時上書き・読み直しのどれでも同じ当てはめ)
    apply_speaking_model(persona, SpeakingModelChoice(MODEL_B, SOURCE_PERSONA, True), "stub", 1000)
    assert persona._llm_client is None

    again, model = runtime.select_llm_client(_node(), persona, state=state)
    assert again is first
    assert model == MODEL_A
    assert resolve_execution_context(persona, None, state=state).model_key == MODEL_A

    # 次の返事から新しい設定
    next_state = {"_model_binding": ReplyModelBinding.capture(persona)}
    following, model = runtime.select_llm_client(_node(), persona, state=next_state)
    assert (following.model, model) == (MODEL_B, MODEL_B)


def test_a_reply_that_had_not_connected_yet_uses_the_definition_it_started_with(monkeypatch):
    calls = []

    def _fake_client(model, provider, context_length, *rest):
        calls.append((model, provider, context_length, rest))
        return SimpleNamespace(model=model)

    monkeypatch.setattr("persona.core.get_llm_client", _fake_client)
    monkeypatch.setattr("llm_clients.get_llm_client", _fake_client)
    persona = _persona(model=MODEL_A, choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True))
    binding = ReplyModelBinding.capture(persona)
    snapshot = model_configs.MODEL_CONFIGS[MODEL_A]

    # 始めた直後に A の定義が読み直しで消え、ペルソナは B に切り替わる
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        key: value for key, value in model_configs.MODEL_CONFIGS.items() if key != MODEL_A
    })
    apply_speaking_model(persona, SpeakingModelChoice(MODEL_B, SOURCE_PERSONA, True), "stub", 1000)

    client = binding.client_for(TIER_STANDARD)

    assert client.model == MODEL_A
    assert calls == [(MODEL_A, "stub", 1000, (snapshot,))]
    # この返事専用の接続を、B に切り替わったペルソナには持たせない
    assert persona._llm_client is None


def test_a_connection_made_for_old_settings_is_not_kept_by_the_persona():
    persona = _persona(model=MODEL_A)
    token = persona._model_settings_token
    persona.set_model(MODEL_B, 1000, "stub")

    stale = object()
    assert persona._install_client_if_current("standard", token, stale) is stale
    assert persona._llm_client is None


def _meta_runtime():
    from sea.runtime import SEARuntime

    runtime = SEARuntime(SimpleNamespace(building_histories={"b1": []}))
    runtime._choose_playbook = Mock(return_value=SimpleNamespace(
        name="meta_user/exec", start_node="exec", context_requirements=None,
    ))
    runtime._prepare_context = Mock(return_value=[])
    runtime._run_playbook = Mock(return_value=["ok"])
    lifecycle = runtime.session_lifecycle
    lifecycle.maybe_run_metabolism = Mock()
    lifecycle.maybe_run_emergency_precompaction = Mock(return_value="skip")
    lifecycle.maybe_run_window_refill = Mock(return_value="skip")
    lifecycle.ensure_window_floor = Mock(return_value="skip")
    return runtime


def _stub_persona(**overrides):
    base = dict(
        persona_name="アオイ", persona_id="aoi", model="m", llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock(), add_to_persona_only=Mock()),
        execution_state={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_whole_reply_uses_the_models_decided_at_its_start():
    runtime = _meta_runtime()
    persona = _stub_persona()

    def _playbook_changes_the_setting(*_args, **_kwargs):
        persona.model = "m2"  # 返事の途中で標準モデルが変わった
        return ["ok"]

    runtime._run_playbook.side_effect = _playbook_changes_the_setting

    runtime.run_meta_user(persona=persona, user_input="hello", building_id="b1")

    binding = runtime._run_playbook.call_args.kwargs["model_binding"]
    assert isinstance(binding, ReplyModelBinding)
    assert binding.model_for(TIER_STANDARD) == "m"
    assert runtime.session_lifecycle.maybe_run_window_refill.call_args.kwargs["model_key"] == "m"
    # 返事の後の記憶の整理も、この返事のモデルの提示コンテキストを扱う
    assert runtime.session_lifecycle.maybe_run_metabolism.call_args.kwargs["model_key"] == "m"


def test_an_isolated_sub_playbook_inherits_the_reply_binding(monkeypatch):
    from sea import runtime_graph
    from sea.pulse_context import PulseContext

    captured = {}

    async def compiled(state, config):
        captured["state"] = state
        return state

    monkeypatch.setattr(runtime_graph, "compile_playbook", lambda playbook, **kwargs: compiled)
    runtime = MagicMock()
    runtime._is_spell_enabled_for_persona.return_value = False
    persona = SimpleNamespace(persona_id="aoi", sai_memory=None)
    playbook = SimpleNamespace(
        name="sub", start_node="n", input_schema=[], output_schema=None, report_template=None,
    )
    binding = ReplyModelBinding.capture(_persona())
    parent_ctx = PulseContext(pulse_id="p1")

    runtime_graph.compile_with_langgraph(
        runtime, playbook, persona, "b1", None, False, [], "p1",
        parent_state={"_model_binding": binding, "_pulse_context": parent_ctx, "_pulse_id": "p1"},
        isolate_pulse_context=True, line="sub",
    )

    state = captured["state"]
    assert state["_model_binding"] is binding
    assert state["_pulse_context"] is not parent_ctx
    assert state["_pulse_context"].model_binding is binding


# ---------------------------------------------------------------------------
# 代わりのモデルで動かさない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source,message", [
    (
        SOURCE_PERSONA,
        "アオイが選んでいた標準モデル 'gone-default' は SAIVerse にありません。"
        "ペルソナ設定で標準モデルを選び直すと、再起動しなくても話せるようになります。",
    ),
    (
        SOURCE_GLOBAL,
        "アオイが使うグローバル設定の標準モデル 'gone-default' は SAIVerse にありません。"
        "グローバル設定の「モデルロール」で標準モデルを選び直すと、再起動しなくても話せるようになります。",
    ),
])
def test_a_persona_whose_default_model_is_gone_stops_before_the_reply_starts(source, message):
    runtime = _meta_runtime()
    persona = _stub_persona(
        model="gone-default",
        speaking_model_choice=SpeakingModelChoice("gone-default", source, False),
    )

    with pytest.raises(ModelUnavailableError) as exc_info:
        runtime.run_meta_user(persona=persona, user_input="hello", building_id="b1")

    runtime.session_lifecycle.maybe_run_window_refill.assert_not_called()
    runtime._run_playbook.assert_not_called()
    persona.history_manager.add_message.assert_not_called()
    err = exc_info.value
    assert err.user_message == message
    assert err.to_dict()["type"] == "error"
    assert err.to_dict()["error_code"] == "model_unavailable"
    assert err.to_dict()["content"] == message


def test_an_autonomous_reply_stops_when_the_lightweight_model_is_gone(monkeypatch):
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    runtime = _meta_runtime()

    with pytest.raises(ModelUnavailableError) as exc_info:
        runtime.run_meta_user(
            persona=_stub_persona(), user_input="", building_id="b1", pulse_type="auto",
        )

    runtime.session_lifecycle.maybe_run_window_refill.assert_not_called()
    assert exc_info.value.user_message == (
        "アオイの軽量モデル 'gone-lite' は SAIVerse にないため、返事の途中の作業ができませんでした。"
        "グローバル設定の「モデルロール」で軽量モデルを選び直すと、再起動しなくても続けられます。"
    )


def test_an_unreachable_lightweight_model_is_not_replaced_by_the_standard_model(monkeypatch):
    from sea.pulse_context import Aspect, PulseContext, resolve_execution_context
    from sea.runtime import SEARuntime

    monkeypatch.setattr("persona.core.get_llm_client", _raise)
    monkeypatch.setattr("llm_clients.get_llm_client", _raise)
    persona = _persona(model=MODEL_A, lightweight_model=LITE)
    persona._llm_client = object()
    ctx = PulseContext(pulse_id="p")
    ctx.push_line(aspect=Aspect.WORKER)
    state = {"_pulse_context": ctx}
    runtime = SEARuntime(SimpleNamespace(building_histories={}))
    ec = resolve_execution_context(persona, ctx, state=state)

    with pytest.raises(ModelUnavailableError) as exc_info:
        runtime.select_llm_client(_node(), persona, execution_context=ec, state=state)

    err = exc_info.value
    assert (err.role, err.reason, err.model) == ("lightweight_model", "unreachable", LITE)
    assert err.user_message == (
        "アオイの軽量モデル 'test-lite-model' に繋げなかったため、返事の途中の作業ができませんでした。"
        "API キーなどの接続の設定を確かめるか、軽量モデルを選び直してください。再起動は要りません。"
    )


def test_the_structured_output_route_does_not_fall_back_to_the_standard_model(monkeypatch):
    from sea.runtime import SEARuntime

    model_configs.MODEL_CONFIGS[MODEL_A]["supports_structured_output"] = False
    monkeypatch.setattr("llm_clients.get_llm_client", _raise)
    persona = _persona(model=MODEL_A)
    persona._llm_client = object()
    runtime = SEARuntime(SimpleNamespace(building_histories={}))

    with pytest.raises(ModelUnavailableError) as exc_info:
        runtime.select_llm_client(_node(), persona, needs_structured_output=True)

    assert (exc_info.value.role, exc_info.value.reason) == ("lightweight_model", "unreachable")


def test_the_error_reaches_the_chat_exit_through_the_dispatcher():
    """会話の受け口が包んだ ModelUnavailableError は、ディスパッチャが元の例外として投げ直す
    (manager/runtime.py の backend_worker が LLMError を to_dict() でエラーイベントにする)。"""
    from saiverse.pulse_dispatcher import PulseDispatcher
    from saiverse.user_conversation import UserUtteranceError

    err = ModelUnavailableError(
        "default_model 'gone' is missing", role="default_model", reason="missing",
        user_message="アオイが選んでいた標準モデル 'gone' は SAIVerse にありません。",
    )

    def _utterance(*_args, **_kwargs):
        try:
            raise err
        except ModelUnavailableError as exc:
            raise UserUtteranceError(
                "direct response failed", stage="direct_response",
                side_effects_done=True, fallback_safe=False,
            ) from exc

    invoke_main_line = Mock()
    with patch("saiverse.user_conversation.on_user_utterance", _utterance):
        with pytest.raises(ModelUnavailableError) as exc_info:
            PulseDispatcher(SimpleNamespace()).dispatch_user_utterance(
                persona_id="aoi", user_id="1", event={}, invoke_main_line=invoke_main_line,
            )

    assert exc_info.value is err
    invoke_main_line.assert_not_called()


# ---------------------------------------------------------------------------
# /run_playbook スペル: サブラインは返事のモデルを引き継ぎ、止まったことを結果に畳まない
# ---------------------------------------------------------------------------


def _run_playbook_tool():
    path = Path(__file__).resolve().parent.parent / "builtin_data" / "tools" / "run_playbook.py"
    spec = importlib.util.spec_from_file_location("run_playbook_tool_for_model_selection", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spell_env(module, run_playbook_side_effect):
    from sea.runtime import SEARuntime

    binding = ReplyModelBinding.capture(_persona())
    pulse_ctx = SimpleNamespace(_line_stack=[object()], pulse_id="p1", model_binding=binding)
    sea_runtime = MagicMock()
    sea_runtime.decide_playbook_permission = (
        lambda *args, **kwargs: SEARuntime.decide_playbook_permission(sea_runtime, *args, **kwargs)
    )
    sea_runtime._load_playbook_for = MagicMock(
        return_value=SimpleNamespace(name="memory_research", router_callable=True),
    )
    sea_runtime._run_playbook = MagicMock(side_effect=run_playbook_side_effect)
    manager = SimpleNamespace(
        sea_runtime=sea_runtime, personas={"aoi": SimpleNamespace(current_building_id="b1")},
    )
    patches = [
        patch.object(module, "get_active_persona_id", return_value="aoi"),
        patch.object(module, "get_active_manager", return_value=manager),
        patch.object(module, "get_active_pulse_context", return_value=pulse_ctx),
    ]
    return binding, sea_runtime, patches


def test_run_playbook_hands_the_reply_binding_to_the_sub_line():
    module = _run_playbook_tool()
    binding, sea_runtime, patches = _spell_env(
        module, lambda *a, **kw: kw["parent_state"].update({"report_to_parent": "ok"}),
    )
    with patches[0], patches[1], patches[2]:
        module.run_playbook(name="memory_research")

    assert sea_runtime._run_playbook.call_args.kwargs["parent_state"]["_model_binding"] is binding


def test_run_playbook_does_not_turn_an_unavailable_model_into_a_spell_result():
    module = _run_playbook_tool()

    def _unavailable(*_args, **_kwargs):
        raise ModelUnavailableError("lightweight missing", role="lightweight_model", reason="missing")

    _binding, _sea_runtime, patches = _spell_env(module, _unavailable)
    with patches[0], patches[1], patches[2]:
        with pytest.raises(ModelUnavailableError):
            module.run_playbook(name="memory_research")


# ---------------------------------------------------------------------------
# 返事の途中の保存: 本物の runtime を通す
# ---------------------------------------------------------------------------


class _PulseClient:
    """LLM ノードと要約が呼ぶ口だけを持つ偽の接続。呼ばれたモデルを順に記録する。"""

    def __init__(self, model, calls, on_first_generate=None):
        self.model = model
        self._calls = calls
        self._on_first_generate = on_first_generate

    def generate(self, messages, **_kwargs):
        self._calls.append(self.model)
        hook, self._on_first_generate = self._on_first_generate, None
        if hook is not None:
            hook()
        return f"reply by {self.model}"

    def consume_usage(self):
        return None

    def consume_reasoning(self):
        return []

    def consume_reasoning_details(self):
        return None


def test_a_setting_saved_in_the_middle_of_a_real_reply_reaches_only_the_next_reply(world, monkeypatch):
    """run_meta_user → _run_playbook → langgraph → LLM ノード → select_llm_client を本物で通す。

    偽物にしているのは、LLM の接続を作る口 (get_llm_client)、Playbook の選択
    (_choose_playbook)、送る内容の組み立て (_prepare_context。受け取った model_key は見る)、
    応答の前後の記憶の手当て (session_lifecycle の四つ)。
    """
    from sea.playbook_models import PlaybookSchema
    from sea.runtime import SEARuntime

    calls = []

    def _save_a_new_global_default():
        # 返事の最初の LLM 呼び出しの最中に、グローバル設定の標準モデルが B で保存される
        monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_B)
        reapply_speaking_models(world.manager)

    hooks = {MODEL_A: _save_a_new_global_default}

    def _fake_client(model, provider, context_length, *rest):
        return _PulseClient(model, calls, hooks.pop(model, None))

    monkeypatch.setattr("persona.core.get_llm_client", _fake_client)
    monkeypatch.setattr("llm_clients.get_llm_client", _fake_client)
    monkeypatch.setenv("SAIVERSE_DEFAULT_MODEL", MODEL_A)
    persona = world.add("aoi", "アオイ", persona=_persona(
        model=MODEL_A, choice=SpeakingModelChoice(MODEL_A, SOURCE_GLOBAL, True),
    ))
    persona.history_manager = SimpleNamespace(add_message=Mock(), add_to_persona_only=Mock())
    persona.execution_state = {}
    persona.sai_memory = None

    runtime = SEARuntime(SimpleNamespace(
        building_histories={"b1": []}, SessionLocal=world.SessionLocal,
        # Beat 分割 (develop b866d0b9) で発言の帰属先を在室表 (manager.occupants)
        # から引くようになった。空なら渡した building_id がそのまま使われる。
        occupants={},
    ))
    runtime._choose_playbook = Mock(return_value=PlaybookSchema(
        name="model_pin_probe", description="t", input_schema=[], start_node="first",
        nodes=[
            {"id": "first", "type": "llm", "action": "一つ目", "next": "second"},
            {"id": "second", "type": "llm", "action": "二つ目", "next": None},
        ],
    ))
    runtime._prepare_context = Mock(return_value=[])
    lifecycle = runtime.session_lifecycle
    lifecycle.maybe_run_metabolism = Mock()
    lifecycle.maybe_run_emergency_precompaction = Mock(return_value="skip")
    lifecycle.maybe_run_window_refill = Mock(return_value="skip")
    lifecycle.ensure_window_floor = Mock(return_value="skip")

    runtime.run_meta_user(persona=persona, user_input="hello", building_id="b1")

    # 保存は返事の途中でペルソナに当てはまったが、その返事は二つの LLM ノードとも A で書いた
    assert persona.model == MODEL_B
    assert calls == [MODEL_A, MODEL_A]
    assert runtime._prepare_context.call_args.kwargs["model_key"] == MODEL_A
    assert lifecycle.maybe_run_metabolism.call_args.kwargs["model_key"] == MODEL_A

    runtime.run_meta_user(persona=persona, user_input="hello again", building_id="b1")

    # 次の返事から B
    assert calls == [MODEL_A, MODEL_A, MODEL_B, MODEL_B]
    assert runtime._prepare_context.call_args.kwargs["model_key"] == MODEL_B


def test_a_parameter_change_in_the_middle_of_a_reply_reaches_only_the_next_reply(monkeypatch):
    """チャット画面の一時上書きのパラメータを返事の途中で変えても、その返事の接続は設定し直さない。"""
    configured = []

    class _ConfigurableClient(_PulseClient):
        def configure_parameters(self, parameters):
            configured.append((id(self), dict(parameters)))

    def _fake_client(model, provider, context_length, *rest):
        return _ConfigurableClient(model, [])

    monkeypatch.setattr("persona.core.get_llm_client", _fake_client)
    monkeypatch.setattr("llm_clients.get_llm_client", _fake_client)
    monkeypatch.setitem(model_configs.MODEL_CONFIGS, OVERRIDE, {
        **_definition("vendor/override"), "parameters": {"temperature": {"default": 1.0}},
    })
    persona = _persona(model=OVERRIDE, choice=SpeakingModelChoice(OVERRIDE, SOURCE_OVERRIDE, True))
    persona.apply_parameter_overrides({"temperature": 0.2})

    binding = ReplyModelBinding.capture(persona)
    in_reply = binding.client_for(TIER_STANDARD)
    assert configured == [(id(in_reply), {"temperature": 0.2})]

    persona.apply_parameter_overrides({"temperature": 0.9})  # 返事の途中で変えた

    # 書いている途中の返事は同じ接続のまま、設定し直さない
    assert binding.client_for(TIER_STANDARD) is in_reply
    assert configured == [(id(in_reply), {"temperature": 0.2})]

    # 次の返事は新しいパラメータで作った接続
    next_reply = ReplyModelBinding.capture(persona).client_for(TIER_STANDARD)
    assert next_reply is not in_reply
    assert configured[-1] == (id(next_reply), {"temperature": 0.9})

    persona.apply_parameter_overrides({})  # 空で呼ばれたら、パラメータの一時上書きを外す
    assert persona._pending_parameter_overrides is None


# ---------------------------------------------------------------------------
# 変更したその場の知らせ (notices): チャット画面の一時上書き、モデル、プロバイダ
# ---------------------------------------------------------------------------


def test_the_override_route_returns_notices_when_it_sets_an_override():
    from api.routes import config as config_route

    manager = MagicMock()
    manager.set_model.return_value = ReapplyResult([UnswitchedPersona("アオイ", MODEL_A)])
    manager.model = OVERRIDE
    manager.model_parameter_overrides = {}

    result = config_route.set_model(config_route.UpdateModelRequest(model=OVERRIDE), manager=manager)

    assert result["current_model"] == OVERRIDE
    assert result["notices"] == [_unswitched("アオイ", MODEL_A)]


def test_the_world_editor_route_returns_the_persona_settings_warning():
    """ワールドエディタからのペルソナ設定の保存も、ペルソナ設定の画面と同じく warning を返す。"""
    from api.routes import world as world_route

    manager = MagicMock()
    manager.update_ai.return_value = "Updated AI 'アオイ'.[WARNING:LLM]保存しませんでした"
    assert world_route.update_ai("aoi", MagicMock(), manager=manager) == {
        "message": "Updated AI 'アオイ'.", "warning": "保存しませんでした",
    }

    manager.update_ai.return_value = "Updated AI 'アオイ'."
    assert world_route.update_ai("aoi", MagicMock(), manager=manager) == {
        "message": "Updated AI 'アオイ'.",
    }


def _world_with_a_persona_that_cannot_switch(world, monkeypatch):
    import saiverse.app_state as app_state

    broken = _persona(
        "aoi", "アオイ", model=MODEL_A, choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True),
    )
    broken.drop_llm_clients = _raise  # 読み直しで接続を捨てるところで失敗する
    world.add("aoi", "アオイ", default_model=MODEL_A, persona=broken)
    monkeypatch.setattr(app_state, "manager", world.manager)
    return broken


def test_deleting_a_model_returns_200_with_the_personas_that_could_not_switch(
    world, monkeypatch, tmp_path,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import config as config_route

    _world_with_a_persona_that_cannot_switch(world, monkeypatch)
    remaining = dict(model_configs.MODEL_CONFIGS)
    monkeypatch.setattr(model_configs, "load_configs", lambda: dict(remaining))
    user_file = tmp_path / "user-model.json"
    user_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_route, "_model_user_path", lambda key: user_file)
    app = FastAPI()
    app.include_router(config_route.router, prefix="/api/config")

    response = TestClient(app).delete("/api/config/models/user-model")

    assert response.status_code == 200
    assert response.json() == {"notices": [_unswitched("アオイ", MODEL_A)]}
    assert not user_file.exists()


_PROVIDER_PAYLOAD = {
    "id": "notice-probe", "display_name": "Notice Probe", "protocol": "openai_compat",
    "base_url": "https://api.moonshot.cn/v1", "api_key_env": "KIMI_API_KEY",
}


def _post_a_provider(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import providers as providers_route
    from saiverse import provider_configs

    existing = dict(provider_configs.PROVIDER_CONFIGS)
    monkeypatch.setattr(provider_configs, "PROVIDER_CONFIGS", existing)
    monkeypatch.setattr(provider_configs, "load_configs", lambda: {
        **existing,
        _PROVIDER_PAYLOAD["id"]: {**_PROVIDER_PAYLOAD, "source": provider_configs.SOURCE_USER_DATA},
    })
    monkeypatch.setattr(provider_configs, "USER_DATA_DIR", tmp_path)
    models = dict(model_configs.MODEL_CONFIGS)
    monkeypatch.setattr(model_configs, "load_configs", lambda: dict(models))
    app = FastAPI()
    app.include_router(providers_route.router, prefix="/api/providers")
    response = TestClient(app).post("/api/providers", json=_PROVIDER_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


def test_saving_a_provider_names_the_personas_that_could_not_switch(world, monkeypatch, tmp_path):
    _world_with_a_persona_that_cannot_switch(world, monkeypatch)

    body = _post_a_provider(monkeypatch, tmp_path)

    assert body["id"] == "notice-probe"
    # プロバイダとモデルの二度の読み直しで二度とも切り替えられなかった。知らせは後の結果の一件
    assert body["notices"] == [_unswitched("アオイ", MODEL_A)]


def test_saving_a_provider_does_not_name_a_persona_that_the_later_reload_switched(
    world, monkeypatch, tmp_path,
):
    broken = _world_with_a_persona_that_cannot_switch(world, monkeypatch)
    drops = []

    def _fail_only_the_first_time():
        drops.append(1)
        if len(drops) == 1:
            raise RuntimeError("boom")

    broken.drop_llm_clients = _fail_only_the_first_time

    body = _post_a_provider(monkeypatch, tmp_path)

    # プロバイダの読み直しでは切り替えられなかったが、後のモデルの読み直しで切り替わった
    assert len(drops) == 2
    assert body["notices"] == []


def test_a_provider_save_reloads_providers_and_models_in_one_section_of_the_settings_lock(
    monkeypatch, tmp_path,
):
    """二つの読み直しの間に、別の設定の保存や決め直しが割り込めない。"""
    from saiverse import provider_configs

    events = []

    class _RecordingLock:
        def __enter__(self):
            events.append("lock")
            return self

        def __exit__(self, *_exc):
            events.append("unlock")
            return False

    monkeypatch.setattr("saiverse.persona_model_selection.MODEL_SETTINGS_LOCK", _RecordingLock())
    monkeypatch.setattr(provider_configs, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(provider_configs, "reload_configs", lambda: events.append("providers"))
    final = ReapplyResult([UnswitchedPersona("アオイ", MODEL_A)])
    monkeypatch.setattr(
        provider_configs, "reload_models_after_provider_change",
        lambda: events.append("models") or final,
    )

    saved = provider_configs.save_provider("lock-probe", {"protocol": "openai_compat"})
    deleted = provider_configs.delete_provider("lock-probe")

    assert events == ["lock", "providers", "models", "unlock"] * 2
    assert saved is final and deleted is final


# ---------------------------------------------------------------------------
# 返事の中の要約も返事の軽量モデルで行い、代わりのモデルでは要約しない
# ---------------------------------------------------------------------------


class _StelisMemory:
    """スレッドの中身の読み出しとスレッドの終わりだけを持つ記憶。"""

    def __init__(self):
        self.ended = []

    def get_thread_messages(self, thread_id, page=0, page_size=1000):
        return [{"role": "assistant", "content": "資料を三つ読んだ"}]

    def get_stelis_info(self, thread_id):
        return SimpleNamespace(chronicle_prompt=None)

    def end_stelis_thread(self, thread_id, status, chronicle_summary):
        self.ended.append((thread_id, status, chronicle_summary))
        return True

    def set_active_thread(self, thread_id):
        self.active_thread = thread_id


def _recording_clients(monkeypatch):
    """ペルソナの接続と返事専用の接続の作り口を、作ったモデルを記録する偽物にする。"""
    created = []

    def _fake_client(model, provider, context_length, *rest):
        created.append(model)
        return _PulseClient(model, [])

    monkeypatch.setattr("persona.core.get_llm_client", _fake_client)
    monkeypatch.setattr("llm_clients.get_llm_client", _fake_client)
    return created


def test_the_stelis_chronicle_uses_the_lightweight_connection_decided_at_the_reply_start(monkeypatch):
    from sea.runtime import SEARuntime

    created = _recording_clients(monkeypatch)
    persona = _persona(
        model=MODEL_A, lightweight_model=LITE,
        choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True),
    )
    persona.sai_memory = _StelisMemory()
    state = {"_model_binding": ReplyModelBinding.capture(persona)}
    persona.set_lightweight_model(MODEL_B)  # 返事の途中で軽量モデルが選び直された

    summary = SEARuntime(SimpleNamespace(building_histories={}))._generate_stelis_chronicle(
        persona, "stelis-1", None, state=state,
    )

    assert summary == f"reply by {LITE}"
    assert created == [LITE]  # 選び直した B の接続も、ほかの一時的な接続も作らない


def test_the_stelis_chronicle_is_skipped_and_the_thread_still_ends_when_the_lightweight_model_is_gone(
    monkeypatch,
):
    from sea.pulse_context import PulseContext
    from sea.runtime import SEARuntime

    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    created = _recording_clients(monkeypatch)
    persona = _persona(model=MODEL_A, choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True))
    memory = _StelisMemory()
    persona.sai_memory = memory
    pulse_ctx = PulseContext(pulse_id="p1")
    pulse_ctx.model_binding = ReplyModelBinding.capture(persona)

    chronicle = SEARuntime(SimpleNamespace(building_histories={}))._end_subagent_thread(
        persona, "stelis-1", "parent-1", generate_chronicle=True, pulse_context=pulse_ctx,
    )

    assert chronicle is None
    assert memory.ended == [("stelis-1", "completed", None)]
    assert created == []  # 標準モデルにも組み込みの既定モデルにも回さない


def test_outside_a_reply_the_stelis_chronicle_decides_the_lightweight_model_like_a_reply(
    monkeypatch,
):
    from sea.runtime import SEARuntime

    created = _recording_clients(monkeypatch)
    runtime = SEARuntime(SimpleNamespace(building_histories={}))
    with_own = _persona(model=MODEL_A, lightweight_model=LITE)
    with_own.sai_memory = _StelisMemory()
    without_own = _persona("miku", "ミク", model=MODEL_A)
    without_own.sai_memory = _StelisMemory()

    # 個別の軽量モデル → (グローバル設定が無いので) 組み込みの既定モデル
    assert runtime._generate_stelis_chronicle(with_own, "stelis-1") == f"reply by {LITE}"
    assert runtime._generate_stelis_chronicle(without_own, "stelis-2") == f"reply by {FALLBACK_KEY}"
    assert created == [LITE, FALLBACK_KEY]

    # グローバル設定の軽量モデルが無いモデルなら、組み込みの既定モデルにも回さず作らない
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "gone-lite")
    assert runtime._generate_stelis_chronicle(without_own, "stelis-3") is None
    assert created == [LITE, FALLBACK_KEY]


def _since_last_user_tool():
    path = (
        Path(__file__).resolve().parent.parent
        / "builtin_data" / "tools" / "get_since_last_user_conversation.py"
    )
    spec = importlib.util.spec_from_file_location("since_last_user_tool_for_model_selection", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SINCE_LAST_USER_MESSAGES = [
    {"role": "assistant", "content": "散歩に出た", "metadata": {}},
    {"role": "assistant", "content": "待つ", "metadata": {"tags": ["wait"]}},
]


def test_the_since_last_user_summary_uses_the_lightweight_connection_decided_at_the_reply_start(
    monkeypatch,
):
    module = _since_last_user_tool()
    created = _recording_clients(monkeypatch)
    persona = _persona(
        model=MODEL_A, lightweight_model=LITE,
        choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True),
    )
    pulse_ctx = SimpleNamespace(model_binding=ReplyModelBinding.capture(persona))
    persona.set_lightweight_model(MODEL_B)

    with patch.object(module, "get_active_pulse_context", return_value=pulse_ctx):
        summary = module._generate_summary(persona, _SINCE_LAST_USER_MESSAGES, "uuid-1")

    assert summary == f"reply by {LITE}"
    assert created == [LITE]


def test_the_since_last_user_summary_does_not_pick_another_model_by_a_partial_name(monkeypatch):
    """名前の一部一致 (find_model_config) で別のモデルを拾わず、LLM を呼ばない件数の要約にする。"""
    module = _since_last_user_tool()
    monkeypatch.setitem(model_configs.MODEL_CONFIGS, "vendor/lite-pro", _definition("vendor/lite-pro"))
    monkeypatch.setenv("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL", "lite-pr")
    # 前の引き方 (find_model_config) なら、この名前で別のモデルを拾っていた
    assert model_configs.find_model_config("lite-pr")[0] == "vendor/lite-pro"
    created = _recording_clients(monkeypatch)
    persona = _persona(model=MODEL_A, choice=SpeakingModelChoice(MODEL_A, SOURCE_PERSONA, True))
    pulse_ctx = SimpleNamespace(model_binding=ReplyModelBinding.capture(persona))

    with patch.object(module, "get_active_pulse_context", return_value=pulse_ctx):
        summary = module._generate_summary(persona, _SINCE_LAST_USER_MESSAGES, "uuid-1")

    assert summary == "待機1回、その他のアクティビティ1件がありました。"
    assert created == []


def test_outside_a_reply_the_since_last_user_summary_decides_the_lightweight_model_like_a_reply(
    monkeypatch,
):
    module = _since_last_user_tool()
    created = _recording_clients(monkeypatch)
    persona = _persona(model=MODEL_A)  # 個別の軽量モデルを持たない

    with patch.object(module, "get_active_pulse_context", return_value=None):
        summary = module._generate_summary(persona, _SINCE_LAST_USER_MESSAGES, "uuid-1")

    assert summary == f"reply by {FALLBACK_KEY}"
    assert created == [FALLBACK_KEY]


# ---------------------------------------------------------------------------
# グローバル設定を空で保存したら組み込みの既定モデル (Memory Weave モデル)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("saved", ["", "   "])
def test_an_empty_global_memory_weave_setting_means_the_builtin_model(monkeypatch, saved):
    from saiverse.memory_weave_llm import (
        SOURCE_BUILTIN as WEAVE_SOURCE_BUILTIN,
        resolve_global_memory_weave_model,
        resolve_memory_weave_model,
    )

    monkeypatch.setenv("MEMORY_WEAVE_MODEL", saved)

    assert resolve_global_memory_weave_model() == (FALLBACK_KEY, WEAVE_SOURCE_BUILTIN)
    assert resolve_memory_weave_model(SimpleNamespace(memory_weave_model=None)) == (
        FALLBACK_KEY, WEAVE_SOURCE_BUILTIN,
    )


@pytest.mark.parametrize("job", ["generate", "build_from_logs"])
def test_memopedia_jobs_use_the_builtin_model_when_the_global_setting_is_saved_empty(
    world, monkeypatch, tmp_path, job,
):
    import database.session as db_session
    from api.routes.people import memopedia as memopedia_route

    monkeypatch.setenv("MEMORY_WEAVE_MODEL", "")
    persona_dir = tmp_path / "personas" / "aoi"
    persona_dir.mkdir(parents=True)
    (persona_dir / "memory.db").touch()
    monkeypatch.setattr(memopedia_route, "get_personas_dir", lambda: tmp_path / "personas")
    # 本番の DB を読まない: ペルソナの行は隔離した DB から引く (この行は無い = 個別の値なし)
    monkeypatch.setattr(db_session, "SessionLocal", world.SessionLocal)
    looked_up = []
    monkeypatch.setattr(
        model_configs, "find_model_config", lambda name: looked_up.append(name) or ("", {}),
    )

    if job == "generate":
        memopedia_route._run_memopedia_generation(
            "job-1", "aoi", "keyword", None, None, 1, 1, False, None,
        )
    else:
        memopedia_route._run_build_memopedia_from_logs("job-1", "aoi", 10, 10, 0.0, None)

    assert looked_up == [FALLBACK_KEY]


def test_the_chronicle_cost_estimate_uses_the_builtin_model_when_the_global_setting_is_blank(
    monkeypatch,
):
    from api.routes.people import arasuji as arasuji_route

    monkeypatch.setenv("MEMORY_WEAVE_MODEL", "   ")
    monkeypatch.setattr(arasuji_route, "_get_arasuji_db", lambda persona_id: MagicMock())
    monkeypatch.setattr(
        "sea.session_lifecycle.collect_folded_chronicle_entry_ids",
        lambda manager, persona_id: [],
    )
    monkeypatch.setattr("sai_memory.arasuji.absorption.is_repair_incomplete", lambda conn: False)
    estimated = {}

    def _estimate(conn, *, model_name, **_kwargs):
        estimated["model_name"] = model_name
        return SimpleNamespace(
            total_messages=0, processed_messages=0, unprocessed_messages=0,
            estimated_llm_calls=0, estimated_cost_usd=0.0, model_name=model_name,
            is_free_tier=False, currency="USD", consolidation_calls=0,
        )

    monkeypatch.setattr("sai_memory.arasuji.estimate.estimate_chronicle_generation_cost", _estimate)

    result = arasuji_route.estimate_chronicle_cost("aoi", manager=SimpleNamespace(personas={}))

    assert estimated["model_name"] == FALLBACK_KEY
    assert result.model_name == FALLBACK_KEY
