"""Metabolism 三水位の全体既定 (2026-09-03) — 三層解決・API・移行のテスト。

三層は 組み込み既定 < 全体設定 (user_settings.METABOLISM_*_CHARS) < モデル定義。
docs/concepts/metabolism.md。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saiverse import model_configs
from saiverse.model_configs import (
    BUILTIN_METABOLISM_HIGH_CHARS,
    BUILTIN_METABOLISM_LOW_CHARS,
    BUILTIN_METABOLISM_TARGET_CHARS,
    get_effective_metabolism_defaults,
    get_global_metabolism_defaults,
    get_metabolism_high_chars,
    get_metabolism_low_chars,
    get_metabolism_target_chars,
    set_global_metabolism_defaults,
)

MODEL_NO_KEYS = "wm-test-no-keys"
MODEL_EXPLICIT = "wm-test-explicit"
MODEL_NULL = "wm-test-null"


@pytest.fixture
def isolated_globals(monkeypatch: pytest.MonkeyPatch):
    """全体既定を空から始め、テスト後に元へ戻す。モデル定義は三種類だけの表に差し替える。

    実物の MODEL_CONFIGS (開発機の ~/.saiverse/user_data/models も混ざる) を使うと、
    全体既定の保存が「既存モデルと矛盾する」で弾かれてテストが環境依存になるため、
    表ごと差し替える (config.py は関数内 import で毎回モジュール属性を引くので効く)。
    """
    saved = get_global_metabolism_defaults()
    set_global_metabolism_defaults({})
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        MODEL_NO_KEYS: {"model": "x"},
        MODEL_EXPLICIT: {
            "model": "x", "metabolism_high_chars": 12_000, "metabolism_target_chars": 6_000,
            "metabolism_low_chars": 2_000,
        },
        MODEL_NULL: {"model": "x", "metabolism_high_chars": None, "metabolism_target_chars": None},
    })
    yield
    set_global_metabolism_defaults(saved)


# ── 解決層 ───────────────────────────────────────────────────────


def test_builtin_when_nothing_set(isolated_globals):
    assert get_metabolism_low_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_LOW_CHARS
    assert get_metabolism_target_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_TARGET_CHARS
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_HIGH_CHARS


def test_global_changes_model_without_keys(isolated_globals):
    set_global_metabolism_defaults({
        "metabolism_low_chars": 10_000,
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": 60_000,
    })
    assert get_metabolism_low_chars(MODEL_NO_KEYS) == 10_000
    assert get_metabolism_target_chars(MODEL_NO_KEYS) == 30_000
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == 60_000


def test_partial_global_fills_only_its_key(isolated_globals):
    set_global_metabolism_defaults({"metabolism_high_chars": 60_000})
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == 60_000
    assert get_metabolism_target_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_TARGET_CHARS
    assert get_metabolism_low_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_LOW_CHARS


def test_model_explicit_number_wins_over_global(isolated_globals):
    set_global_metabolism_defaults({"metabolism_high_chars": 60_000, "metabolism_target_chars": 30_000})
    assert get_metabolism_high_chars(MODEL_EXPLICIT) == 12_000
    assert get_metabolism_target_chars(MODEL_EXPLICIT) == 6_000
    assert get_metabolism_low_chars(MODEL_EXPLICIT) == 2_000


def test_model_null_still_means_no_watermark(isolated_globals):
    """モデル定義の null はモデル単位のオプトアウト — 全体設定では埋めない。"""
    set_global_metabolism_defaults({"metabolism_high_chars": 60_000, "metabolism_target_chars": 30_000})
    assert get_metabolism_high_chars(MODEL_NULL) is None
    assert get_metabolism_target_chars(MODEL_NULL) is None
    # low はキー無しなので全体 → 組み込みに落ちる
    assert get_metabolism_low_chars(MODEL_NULL) == BUILTIN_METABOLISM_LOW_CHARS


def test_set_ignores_invalid_values(isolated_globals):
    set_global_metabolism_defaults({
        "metabolism_low_chars": 0,
        "metabolism_target_chars": -5,
        "metabolism_high_chars": "abc",
        "unrelated": 1,
    })
    assert get_global_metabolism_defaults() == {
        "metabolism_low_chars": None,
        "metabolism_target_chars": None,
        "metabolism_high_chars": None,
    }
    assert "unrelated" not in model_configs._current_global_defaults()


def test_effective_defaults_composition(isolated_globals):
    assert get_effective_metabolism_defaults() == {
        "metabolism_low_chars": BUILTIN_METABOLISM_LOW_CHARS,
        "metabolism_target_chars": BUILTIN_METABOLISM_TARGET_CHARS,
        "metabolism_high_chars": BUILTIN_METABOLISM_HIGH_CHARS,
    }
    set_global_metabolism_defaults({"metabolism_target_chars": 30_000})
    assert get_effective_metabolism_defaults() == {
        "metabolism_low_chars": BUILTIN_METABOLISM_LOW_CHARS,
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": BUILTIN_METABOLISM_HIGH_CHARS,
    }


def test_session_lifecycle_watermarks_follow_global(isolated_globals):
    """消費側 (SessionLifecycle.get_metabolism_watermarks) は getter 経由なので追従する。"""
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle

    set_global_metabolism_defaults({
        "metabolism_low_chars": 10_000,
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": 60_000,
    })
    lifecycle = SessionLifecycle(SimpleNamespace(), None)
    wm = lifecycle.get_metabolism_watermarks(SimpleNamespace(model=MODEL_NO_KEYS))
    assert (wm.low, wm.target, wm.high) == (10_000, 30_000, 60_000)


# ── API (GET / PUT /api/config/metabolism-defaults) ─────────────


@pytest.fixture
def api_db(monkeypatch: pytest.MonkeyPatch, isolated_globals):
    """本物の SQLite (メモリ) を database.session.SessionLocal に差し込む。"""
    import database.session
    from database.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database.session, "SessionLocal", Session)
    yield Session
    engine.dispose()


def test_get_payload_shape(api_db):
    from api.routes import config

    payload = config.get_metabolism_defaults()
    assert payload == {
        "global": {"low": None, "target": None, "high": None},
        "effective": {
            "low": BUILTIN_METABOLISM_LOW_CHARS,
            "target": BUILTIN_METABOLISM_TARGET_CHARS,
            "high": BUILTIN_METABOLISM_HIGH_CHARS,
        },
        "builtin": {
            "low": BUILTIN_METABOLISM_LOW_CHARS,
            "target": BUILTIN_METABOLISM_TARGET_CHARS,
            "high": BUILTIN_METABOLISM_HIGH_CHARS,
        },
    }


def test_put_round_trip_persists_and_applies(api_db):
    from api.routes import config
    from database.models import UserSettings

    payload = config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_low_chars=10_000,
        metabolism_target_chars=30_000,
        metabolism_high_chars=60_000,
    ))
    assert payload["global"] == {"low": 10_000, "target": 30_000, "high": 60_000}
    assert payload["effective"] == {"low": 10_000, "target": 30_000, "high": 60_000}
    assert payload["builtin"]["high"] == BUILTIN_METABOLISM_HIGH_CHARS

    # DB 行 (USERID=1 を作って書く)
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert (row.METABOLISM_LOW_CHARS, row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) \
            == (10_000, 30_000, 60_000)
    # 再起動なしで解決層に効く
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == 60_000
    # GET も同じ
    assert config.get_metabolism_defaults() == payload


def test_put_partial_touches_only_given_keys(api_db):
    from api.routes import config

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_low_chars=10_000, metabolism_target_chars=30_000, metabolism_high_chars=60_000,
    ))
    payload = config.put_metabolism_defaults(
        config.MetabolismDefaultsRequest(metabolism_high_chars=80_000),
    )
    assert payload["global"] == {"low": 10_000, "target": 30_000, "high": 80_000}


def test_put_null_clears_back_to_builtin(api_db):
    from api.routes import config
    from database.models import UserSettings

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_low_chars=10_000, metabolism_target_chars=30_000, metabolism_high_chars=60_000,
    ))
    # 明示 null (= exclude_unset で「渡された None」として区別される)
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_high_chars": None})
    payload = config.put_metabolism_defaults(req)
    assert payload["global"] == {"low": 10_000, "target": 30_000, "high": None}
    assert payload["effective"]["high"] == BUILTIN_METABOLISM_HIGH_CHARS
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert row.METABOLISM_HIGH_CHARS is None
        assert row.METABOLISM_TARGET_CHARS == 30_000
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_HIGH_CHARS


def test_put_out_of_order_is_400_and_leaves_state(api_db):
    from api.routes import config

    # target=15万 だけ → 実効 high=組み込み20万 なので OK
    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_target_chars=150_000))
    # high=12万 → target 15万 > high で 400
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_high_chars=120_000))
    assert exc.value.status_code == 400
    assert config.get_metabolism_defaults()["global"] == {"low": None, "target": 150_000, "high": None}
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_HIGH_CHARS


def test_put_null_that_breaks_order_is_400(api_db):
    """既定に戻した結果が組み込み既定と組んで壊れるなら弾く (low=15万 のまま target を戻す)。"""
    from api.routes import config

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_low_chars=150_000, metabolism_target_chars=160_000, metabolism_high_chars=300_000,
    ))
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_target_chars": None})
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(req)
    assert exc.value.status_code == 400


def test_put_non_positive_is_400(api_db):
    from api.routes import config

    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_high_chars=0))
    assert exc.value.status_code == 400


# ── F1: 全体の変更が既存モデルの部分上書きを壊すなら 400 (Codex 指摘 2026-09-03) ──


MODEL_PARTIAL_LOW = "wm-test-partial-low"


def test_put_that_breaks_a_partially_overriding_model_is_400(api_db, monkeypatch):
    """low=15万 だけ書いたモデルは全体 target=16万 の下でだけ正しい。target を戻すと壊れる。"""
    from api.routes import config

    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, MODEL_PARTIAL_LOW,
        {"model": "x", "metabolism_low_chars": 150_000},
    )
    # モデルは low だけ → target は全体既定で埋まる。全体 target=16万 なら 15万 ≤ 16万 で通る。
    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=160_000, metabolism_high_chars=300_000,
    ))
    # target を既定 (組み込み 10万) に戻す → そのモデルは low 15万 > target 10万 で壊れる。
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_target_chars": None})
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(req)
    assert exc.value.status_code == 400
    assert MODEL_PARTIAL_LOW in exc.value.detail
    assert "モデル側" in exc.value.detail
    # 失敗した PUT は何も変えない (DB もキャッシュも)
    assert config.get_metabolism_defaults()["global"] == {
        "low": None, "target": 160_000, "high": 300_000,
    }
    # target を上げる分には通る
    payload = config.put_metabolism_defaults(
        config.MetabolismDefaultsRequest(metabolism_target_chars=170_000),
    )
    assert payload["global"]["target"] == 170_000
    assert model_configs.resolve_metabolism_watermarks(MODEL_PARTIAL_LOW) == (150_000, 170_000, 300_000)


def test_conflict_error_names_at_most_five_models(api_db, monkeypatch):
    from api.routes import config

    keys = [f"wm-test-many-{i}" for i in range(7)]
    for key in keys:
        monkeypatch.setitem(
            model_configs.MODEL_CONFIGS, key, {"model": "x", "metabolism_low_chars": 150_000},
        )
    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_target_chars=160_000))
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_target_chars": None})
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(req)
    named = [key for key in keys if key in exc.value.detail]
    assert len(named) == 5
    assert "ほか 2 件" in exc.value.detail


def test_model_with_null_watermark_never_conflicts(api_db):
    """null = その水位を持たない = 順序の対象外なので、全体をどう動かしても矛盾しない。"""
    from api.routes import config

    # MODEL_NULL (high/target が null、low はキー無し) は fixture で入っている
    payload = config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_low_chars=10_000, metabolism_target_chars=50_000, metabolism_high_chars=150_000,
    ))
    assert payload["global"] == {"low": 10_000, "target": 50_000, "high": 150_000}


# ── F2: PUT は直列化し、合成の土台は DB 行 (Codex 指摘 2026-09-03) ──────────


def test_sequential_single_field_puts_both_survive(api_db):
    from api.routes import config
    from database.models import UserSettings

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_low_chars=10_000))
    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_high_chars=150_000))
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert (row.METABOLISM_LOW_CHARS, row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) \
            == (10_000, None, 150_000)
    assert get_global_metabolism_defaults() == {
        "metabolism_low_chars": 10_000,
        "metabolism_target_chars": None,
        "metabolism_high_chars": 150_000,
    }


def test_merge_base_is_db_row_not_cache(api_db):
    """キャッシュが DB とずれていても、PUT は DB 行を土台に合成する。"""
    from api.routes import config
    from database.models import UserSettings

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_low_chars=10_000, metabolism_target_chars=50_000, metabolism_high_chars=150_000,
    ))
    # キャッシュだけを別の値にずらす (DB は 10k/50k/150k のまま)
    set_global_metabolism_defaults({"metabolism_low_chars": 20_000})
    payload = config.put_metabolism_defaults(
        config.MetabolismDefaultsRequest(metabolism_high_chars=180_000),
    )
    assert payload["global"] == {"low": 10_000, "target": 50_000, "high": 180_000}
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert (row.METABOLISM_LOW_CHARS, row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) \
            == (10_000, 50_000, 180_000)


def test_concurrent_single_field_puts_do_not_lose_updates(api_db):
    """三スレッドが別々の一項目を同時に PUT しても、三つとも残る (ロックで直列化)。"""
    import threading

    from api.routes import config
    from database.models import UserSettings

    # 各値は単独でも (組み込み既定と組んでも) 順序が成立する: 10k ≤ 100k、40k ≤ 50k ≤ 200k、100k ≤ 150k
    requests = [
        config.MetabolismDefaultsRequest(metabolism_low_chars=10_000),
        config.MetabolismDefaultsRequest(metabolism_target_chars=50_000),
        config.MetabolismDefaultsRequest(metabolism_high_chars=150_000),
    ]
    barrier = threading.Barrier(len(requests))
    errors: list[BaseException] = []

    def _run(req):
        try:
            barrier.wait(timeout=5)
            config.put_metabolism_defaults(req)
        except BaseException as exc:  # noqa: BLE001 — スレッド内の失敗を主スレッドで見せる
            errors.append(exc)

    threads = [threading.Thread(target=_run, args=(req,)) for req in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert (row.METABOLISM_LOW_CHARS, row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) \
            == (10_000, 50_000, 150_000)
    assert get_global_metabolism_defaults() == {
        "metabolism_low_chars": 10_000,
        "metabolism_target_chars": 50_000,
        "metabolism_high_chars": 150_000,
    }


# ── F3: 公開は不変写像の差し替え、解決は一枚から (Codex 指摘 2026-09-03) ────────


def test_resolve_reads_global_mapping_exactly_once(isolated_globals, monkeypatch):
    from saiverse.model_configs import resolve_metabolism_watermarks

    original = model_configs._current_global_defaults
    calls: list[int] = []

    def _counting():
        calls.append(1)
        return original()

    monkeypatch.setattr(model_configs, "_current_global_defaults", _counting)
    set_global_metabolism_defaults({
        "metabolism_low_chars": 10_000,
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": 60_000,
    })
    calls.clear()
    assert resolve_metabolism_watermarks(MODEL_NO_KEYS) == (10_000, 30_000, 60_000)
    assert len(calls) == 1
    calls.clear()
    assert resolve_metabolism_watermarks(MODEL_EXPLICIT) == (2_000, 6_000, 12_000)
    assert len(calls) == 1


def test_publish_replaces_mapping_identity_and_old_one_is_frozen(isolated_globals):
    from types import MappingProxyType

    set_global_metabolism_defaults({"metabolism_target_chars": 30_000})
    before = model_configs._current_global_defaults()
    assert isinstance(before, MappingProxyType)
    snapshot = dict(before)

    set_global_metabolism_defaults({"metabolism_target_chars": 70_000, "metabolism_high_chars": 90_000})
    after = model_configs._current_global_defaults()
    assert after is not before
    assert dict(before) == snapshot  # 古い一枚は変わらない
    assert after["metabolism_target_chars"] == 70_000
    with pytest.raises(TypeError):
        after["metabolism_target_chars"] = 1  # type: ignore[index]


def test_session_lifecycle_uses_single_snapshot_resolver(isolated_globals, monkeypatch):
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle

    seen: list[str] = []

    def _fake_resolve(model: str):
        seen.append(model)
        return (1_000, 2_000, 3_000)

    monkeypatch.setattr(model_configs, "resolve_metabolism_watermarks", _fake_resolve)
    lifecycle = SessionLifecycle(SimpleNamespace(), None)
    wm = lifecycle.get_metabolism_watermarks(SimpleNamespace(model=MODEL_NO_KEYS))
    assert seen == [MODEL_NO_KEYS]
    assert (wm.low, wm.target, wm.high) == (1_000, 2_000, 3_000)


# ── 移行 (user_settings への列追加は try_additive_migration の schema 差分で入る) ──


def test_additive_migration_adds_columns_and_is_idempotent(tmp_path):
    from database.migrate import needs_migration, try_additive_migration
    from database.models import Base, UserSettings

    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for col in ("METABOLISM_LOW_CHARS", "METABOLISM_TARGET_CHARS", "METABOLISM_HIGH_CHARS"):
            conn.execute(text(f'ALTER TABLE user_settings DROP COLUMN "{col}"'))
        conn.execute(text("INSERT INTO user (USERID, PASSWORD, USERNAME, LOGGED_IN) VALUES (1, 'p', 'u', 0)"))
        conn.execute(text(
            "INSERT INTO user_settings (USERID, TUTORIAL_COMPLETED, LAST_TUTORIAL_VERSION) "
            "VALUES (1, 0, 1)"
        ))
    engine.dispose()

    assert needs_migration(str(db_path))
    assert try_additive_migration(str(db_path))
    assert not needs_migration(str(db_path))

    engine = create_engine(f"sqlite:///{db_path}")
    cols = {c["name"] for c in inspect(engine).get_columns("user_settings")}
    assert {"METABOLISM_LOW_CHARS", "METABOLISM_TARGET_CHARS", "METABOLISM_HIGH_CHARS"} <= cols
    with sessionmaker(bind=engine)() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert row.METABOLISM_HIGH_CHARS is None  # 既存行は NULL = 未設定
    engine.dispose()

    # 二度目: 差分なし → True のまま、壊れない
    assert try_additive_migration(str(db_path))
    assert not needs_migration(str(db_path))
