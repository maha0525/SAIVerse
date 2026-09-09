"""水位の全体既定 (2026-09-03) — 三層解決・プリセット・API・移行のテスト。

三層は 組み込み既定 < 全体設定 (user_settings.METABOLISM_*_CHARS) < モデル定義。
docs/concepts/metabolism.md。廃止済みのキー (`metabolism_low_chars` 2026-09-04 /
`perception_*_chars` 2026-09-09) は黙って無視される (データ互換)。

2026-09-09 にしきい値は一系統 (残す量 / 上限) になり、組み込み既定は 2万 / 6万へ
下がった (docs/intent/presented_context_reduction.md 設計 3)。保存時検査も型と順序
だけになったので、ここの数字は「残す量 ≤ 上限」を満たすだけでよい。検査そのものの
テストは test_model_watermark_validation.py。
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
    BUILTIN_METABOLISM_TARGET_CHARS,
    METABOLISM_PRESETS,
    get_effective_watermark_defaults,
    get_global_watermark_defaults,
    get_metabolism_high_chars,
    get_metabolism_target_chars,
    set_global_watermark_defaults,
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
    MODEL_EXPLICIT には廃止済みの metabolism_low_chars を残してある — 旧モデル
    JSON との互換 (黙って無視) の検証を兼ねる。
    """
    saved = get_global_watermark_defaults()
    set_global_watermark_defaults({})
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {
        MODEL_NO_KEYS: {"model": "x"},
        MODEL_EXPLICIT: {
            "model": "x", "metabolism_high_chars": 90_000, "metabolism_target_chars": 6_000,
            "metabolism_low_chars": 2_000,  # 廃止済みキーの残骸 (無視される)
        },
        MODEL_NULL: {"model": "x", "metabolism_high_chars": None, "metabolism_target_chars": None},
    })
    yield
    set_global_watermark_defaults(saved)


# ── 解決層 ───────────────────────────────────────────────────────


def test_builtin_when_nothing_set(isolated_globals):
    assert get_metabolism_target_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_TARGET_CHARS
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_HIGH_CHARS


def test_global_changes_model_without_keys(isolated_globals):
    set_global_watermark_defaults({
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": 60_000,
    })
    assert get_metabolism_target_chars(MODEL_NO_KEYS) == 30_000
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == 60_000


def test_partial_global_fills_only_its_key(isolated_globals):
    set_global_watermark_defaults({"metabolism_high_chars": 60_000})
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == 60_000
    assert get_metabolism_target_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_TARGET_CHARS


def test_model_explicit_number_wins_over_global(isolated_globals):
    set_global_watermark_defaults({"metabolism_high_chars": 60_000, "metabolism_target_chars": 30_000})
    assert get_metabolism_high_chars(MODEL_EXPLICIT) == 90_000
    assert get_metabolism_target_chars(MODEL_EXPLICIT) == 6_000


def test_model_null_still_means_no_watermark(isolated_globals):
    """モデル定義の null はモデル単位のオプトアウト — 全体設定では埋めない。"""
    set_global_watermark_defaults({"metabolism_high_chars": 60_000, "metabolism_target_chars": 30_000})
    assert get_metabolism_high_chars(MODEL_NULL) is None
    assert get_metabolism_target_chars(MODEL_NULL) is None


def test_obsolete_low_key_in_model_config_is_ignored(isolated_globals):
    """廃止済み metabolism_low_chars はモデル定義に残っていても解決に現れない。"""
    assert model_configs.resolve_metabolism_watermarks(MODEL_EXPLICIT) == (6_000, 90_000)


def test_set_ignores_invalid_values(isolated_globals):
    set_global_watermark_defaults({
        "metabolism_target_chars": -5,
        "metabolism_high_chars": "abc",
        "metabolism_low_chars": 10_000,  # 廃止済みキー (旧 DB 互換で無視)
        "unrelated": 1,
    })
    assert get_global_watermark_defaults() == {
        "metabolism_target_chars": None,
        "metabolism_high_chars": None,
    }
    assert "unrelated" not in model_configs._current_global_defaults()
    assert "metabolism_low_chars" not in model_configs._current_global_defaults()


def test_set_ignores_obsolete_perception_keys(isolated_globals):
    """旧 DB / 旧クライアントの知覚の二水位が混ざっても黙って落とす (2026-09-09 廃止)。"""
    set_global_watermark_defaults({
        "metabolism_target_chars": 30_000,
        "perception_target_chars": 12_000,
        "perception_high_chars": 20_000,
    })
    assert get_global_watermark_defaults() == {
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": None,
    }
    assert "perception_high_chars" not in model_configs._current_global_defaults()


def test_effective_defaults_composition(isolated_globals):
    assert get_effective_watermark_defaults() == {
        "metabolism_target_chars": BUILTIN_METABOLISM_TARGET_CHARS,
        "metabolism_high_chars": BUILTIN_METABOLISM_HIGH_CHARS,
    }
    set_global_watermark_defaults({"metabolism_target_chars": 30_000})
    assert get_effective_watermark_defaults() == {
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": BUILTIN_METABOLISM_HIGH_CHARS,
    }


def test_obsolete_perception_keys_in_model_config_are_ignored(isolated_globals, monkeypatch):
    """モデル定義に残る perception_*_chars は解決に一切現れない (2026-09-09 廃止)。"""
    model_key = "wm-test-legacy-perception"
    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, model_key,
        {
            "model": "x", "metabolism_target_chars": 30_000,
            "metabolism_high_chars": 90_000,
            "perception_target_chars": 12_000, "perception_high_chars": 20_000,
        },
    )
    assert model_configs.resolve_metabolism_watermarks(model_key) == (30_000, 90_000)
    assert not hasattr(model_configs, "resolve_perception_watermarks")


# ── プリセット (2026-09-09 まはー裁定) ───────────────────────────


def test_presets_are_three_steps_with_one_to_three_ratio():
    """多い / デフォルト / 少ない の三段。どの段も 残す量 : 上限 = 1 : 3。"""
    assert [p["id"] for p in METABOLISM_PRESETS] == ["large", "default", "small"]
    assert [p["label"] for p in METABOLISM_PRESETS] == ["多い", "デフォルト", "少ない"]
    for preset in METABOLISM_PRESETS:
        assert preset["metabolism_high_chars"] == preset["metabolism_target_chars"] * 3


def test_default_preset_is_the_builtin_default():
    """「デフォルト」の段と組み込み既定は同じ数字 — 二枚目の真実を作らない。"""
    default = next(p for p in METABOLISM_PRESETS if p["id"] == "default")
    assert default["metabolism_target_chars"] == BUILTIN_METABOLISM_TARGET_CHARS
    assert default["metabolism_high_chars"] == BUILTIN_METABOLISM_HIGH_CHARS


def test_builtin_defaults_are_the_decided_numbers():
    """2026-09-09 まはー裁定の数字 (2万 / 6万) を固定する。"""
    assert BUILTIN_METABOLISM_TARGET_CHARS == 20_000
    assert BUILTIN_METABOLISM_HIGH_CHARS == 60_000


def test_session_lifecycle_watermarks_follow_global(isolated_globals):
    """消費側 (SessionLifecycle.get_metabolism_watermarks) は getter 経由なので追従する。"""
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle

    set_global_watermark_defaults({
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": 60_000,
    })
    lifecycle = SessionLifecycle(SimpleNamespace(), None)
    wm = lifecycle.get_metabolism_watermarks(SimpleNamespace(model=MODEL_NO_KEYS))
    assert (wm.target, wm.high) == (30_000, 60_000)


# ── 実行時クランプ: 既存データの「残す量 > 上限」逆転 (Codex 指摘 2026-09-04) ──
#
# 保存 API の順序検証 (残す量 ≤ 上限) は既に保存済みのデータには効かない。
# 例: 旧既定 (上限 20万) の下で target=15万 だけ書いた model JSON は、新既定
# (上限 6万) と合成されると「残す量 15万 > 上限 6万」に逆転する。逆転のまま
# 走ると退場計画が空になり「知覚の供給過多」の間違った旗が立つので、実行時の
# 解決点 (SessionLifecycle.get_metabolism_watermarks) が上限を残す量まで
# 引き上げ、WARNING を (persona, model) ごと 1 度だけ出す。


def test_inverted_target_only_model_clamps_high_and_warns_once(
    isolated_globals, monkeypatch, caplog,
):
    """target=15万 だけの model は組み込み上限 6万 と逆転 → high を 15万 に引き上げ。"""
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle

    model_key = "wm-test-inverted-target"
    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, model_key,
        {"model": "x", "metabolism_target_chars": 150_000},
    )
    lifecycle = SessionLifecycle(SimpleNamespace(), None)
    persona = SimpleNamespace(persona_id="p1", model=model_key)

    with caplog.at_level("WARNING", logger="sea.session_lifecycle"):
        wm = lifecycle.get_metabolism_watermarks(persona)
    assert (wm.target, wm.high) == (150_000, 150_000)
    warnings = [r for r in caplog.records if "inverted" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert model_key in message
    assert "150000" in message and str(BUILTIN_METABOLISM_HIGH_CHARS) in message

    # 二度目の呼び出しは同じ実効値のまま、WARNING を重ねない
    caplog.clear()
    with caplog.at_level("WARNING", logger="sea.session_lifecycle"):
        wm2 = lifecycle.get_metabolism_watermarks(persona)
    assert (wm2.target, wm2.high) == (150_000, 150_000)
    assert [r for r in caplog.records if "inverted" in r.getMessage()] == []


def test_inverted_high_only_model_clamps_to_builtin_target(
    isolated_globals, monkeypatch, caplog,
):
    """high=1万5千 だけの model は組み込み残す量 2万 と逆転 → high を 2万 に引き上げ。"""
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle

    model_key = "wm-test-inverted-high"
    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, model_key,
        {"model": "x", "metabolism_high_chars": 15_000},
    )
    lifecycle = SessionLifecycle(SimpleNamespace(), None)
    persona = SimpleNamespace(persona_id="p1", model=model_key)

    with caplog.at_level("WARNING", logger="sea.session_lifecycle"):
        wm = lifecycle.get_metabolism_watermarks(persona)
    assert (wm.target, wm.high) == (
        BUILTIN_METABOLISM_TARGET_CHARS, BUILTIN_METABOLISM_TARGET_CHARS,
    )
    warnings = [r for r in caplog.records if "inverted" in r.getMessage()]
    assert len(warnings) == 1
    assert model_key in warnings[0].getMessage()


def test_inversion_warning_uses_explicit_persona_id_when_persona_is_none(
    isolated_globals, monkeypatch, caplog,
):
    """persona=None (未ロード) でも persona_id 明示で抑止キーが潰れない。

    context-status は未ロードのペルソナで persona=None のまま呼ぶ。抑止キーの
    persona 側が "?" に潰れると、未ロードのペルソナ同士が同じキーで抑止し合う —
    persona_id を明示すればペルソナごとに別々に 1 度ずつ警告される。
    """
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle

    model_key = "wm-test-inverted-target"
    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, model_key,
        {"model": "x", "metabolism_target_chars": 150_000},
    )
    lifecycle = SessionLifecycle(SimpleNamespace(), None)

    with caplog.at_level("WARNING", logger="sea.session_lifecycle"):
        wm = lifecycle.get_metabolism_watermarks(None, model_key, persona_id="p-a")
    assert (wm.target, wm.high) == (150_000, 150_000)
    warnings = [r for r in caplog.records if "inverted" in r.getMessage()]
    assert len(warnings) == 1
    assert "p-a" in warnings[0].getMessage()
    assert ("p-a", model_key) in lifecycle._watermark_inversion_warned

    # 別ペルソナは別キー → もう 1 度警告される (同一 "?" キーに潰れていない証拠)
    caplog.clear()
    with caplog.at_level("WARNING", logger="sea.session_lifecycle"):
        lifecycle.get_metabolism_watermarks(None, model_key, persona_id="p-b")
    warnings = [r for r in caplog.records if "inverted" in r.getMessage()]
    assert len(warnings) == 1
    assert "p-b" in warnings[0].getMessage()

    # 同じペルソナの二度目は抑止される
    caplog.clear()
    with caplog.at_level("WARNING", logger="sea.session_lifecycle"):
        lifecycle.get_metabolism_watermarks(None, model_key, persona_id="p-a")
    assert [r for r in caplog.records if "inverted" in r.getMessage()] == []


def test_inversion_warning_is_exactly_once_under_concurrency(
    isolated_globals, monkeypatch, caplog,
):
    """同じ (persona, model) を複数スレッドが同時に呼んでも警告はちょうど 1 件。

    既出集合への「確認して追加」は素のままだと原子的でない。_warn_lock で
    判定+追加を原子化したので、この結果は決定的 (Codex 指摘 2026-09-04)。
    """
    import threading
    from types import SimpleNamespace

    from sea.session_lifecycle import SessionLifecycle

    model_key = "wm-test-inverted-target"
    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, model_key,
        {"model": "x", "metabolism_target_chars": 150_000},
    )
    lifecycle = SessionLifecycle(SimpleNamespace(), None)
    persona = SimpleNamespace(persona_id="p1", model=model_key)

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def _run():
        try:
            barrier.wait(timeout=5)
            lifecycle.get_metabolism_watermarks(persona)
        except BaseException as exc:  # noqa: BLE001 — スレッド内の失敗を主スレッドで見せる
            errors.append(exc)

    with caplog.at_level("WARNING", logger="sea.session_lifecycle"):
        threads = [threading.Thread(target=_run) for _ in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    assert errors == []
    warnings = [r for r in caplog.records if "inverted" in r.getMessage()]
    assert len(warnings) == 1


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


def _short(target, high):
    return {"target": target, "high": high}


def _preset_payload():
    return [
        {
            "id": preset["id"], "label": preset["label"],
            "target": preset["metabolism_target_chars"],
            "high": preset["metabolism_high_chars"],
        }
        for preset in METABOLISM_PRESETS
    ]


def test_get_payload_shape(api_db):
    from api.routes import config

    payload = config.get_metabolism_defaults()
    assert payload == {
        "global": _short(None, None),
        "effective": _short(
            BUILTIN_METABOLISM_TARGET_CHARS, BUILTIN_METABOLISM_HIGH_CHARS,
        ),
        "builtin": _short(
            BUILTIN_METABOLISM_TARGET_CHARS, BUILTIN_METABOLISM_HIGH_CHARS,
        ),
        "presets": _preset_payload(),
    }


def test_get_payload_carries_the_presets_for_the_screen(api_db):
    """画面はプリセットの数字を書き写さず、この応答から受け取る。"""
    from api.routes import config

    presets = config.get_metabolism_defaults()["presets"]
    assert [p["id"] for p in presets] == ["large", "default", "small"]
    default = next(p for p in presets if p["id"] == "default")
    assert (default["target"], default["high"]) == (
        BUILTIN_METABOLISM_TARGET_CHARS, BUILTIN_METABOLISM_HIGH_CHARS,
    )


def test_put_round_trip_persists_and_applies(api_db):
    from api.routes import config
    from database.models import UserSettings

    payload = config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=30_000,
        metabolism_high_chars=150_000,
    ))
    assert payload["global"] == _short(30_000, 150_000)
    assert payload["effective"] == _short(30_000, 150_000)
    assert payload["builtin"]["high"] == BUILTIN_METABOLISM_HIGH_CHARS

    # DB 行 (USERID=1 を作って書く)
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert (row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) == (30_000, 150_000)
    # 再起動なしで解決層に効く
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == 150_000
    # GET も同じ
    assert config.get_metabolism_defaults() == payload


def test_put_obsolete_perception_keys_are_silently_dropped(api_db):
    """旧クライアントが知覚の二水位を送っても pydantic が黙って落とす (2026-09-09 廃止)。"""
    from api.routes import config

    req = config.MetabolismDefaultsRequest.model_validate({
        "metabolism_target_chars": 30_000,
        "perception_target_chars": 12_000,
        "perception_high_chars": 20_000,
    })
    payload = config.put_metabolism_defaults(req)
    assert payload["global"] == _short(30_000, None)
    assert "perception_high" not in payload["global"]


def test_put_partial_touches_only_given_keys(api_db):
    from api.routes import config

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=30_000, metabolism_high_chars=150_000,
    ))
    payload = config.put_metabolism_defaults(
        config.MetabolismDefaultsRequest(metabolism_high_chars=180_000),
    )
    assert payload["global"] == _short(30_000, 180_000)


def test_put_null_clears_back_to_builtin(api_db):
    from api.routes import config
    from database.models import UserSettings

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=30_000, metabolism_high_chars=150_000,
    ))
    # 明示 null (= exclude_unset で「渡された None」として区別される)
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_high_chars": None})
    payload = config.put_metabolism_defaults(req)
    assert payload["global"] == _short(30_000, None)
    assert payload["effective"]["high"] == BUILTIN_METABOLISM_HIGH_CHARS
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert row.METABOLISM_HIGH_CHARS is None
        assert row.METABOLISM_TARGET_CHARS == 30_000
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == BUILTIN_METABOLISM_HIGH_CHARS


def test_put_obsolete_low_key_is_silently_dropped(api_db):
    """旧クライアントが metabolism_low_chars を送っても pydantic が黙って落とす。"""
    from api.routes import config

    req = config.MetabolismDefaultsRequest.model_validate({
        "metabolism_low_chars": 10_000,
        "metabolism_target_chars": 30_000,
    })
    payload = config.put_metabolism_defaults(req)
    assert payload["global"] == _short(30_000, None)


def test_put_out_of_order_is_400_and_leaves_state(api_db):
    from api.routes import config

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=100_000, metabolism_high_chars=200_000,
    ))
    # high=8万 → target 10万 > high で 400
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_high_chars=80_000))
    assert exc.value.status_code == 400
    assert config.get_metabolism_defaults()["global"] == _short(100_000, 200_000)
    assert get_metabolism_high_chars(MODEL_NO_KEYS) == 200_000


def test_put_null_that_breaks_order_is_400(api_db):
    """既定に戻した結果が組み込み既定と組んで壊れるなら弾く (target=25万 のまま high を戻す)。"""
    from api.routes import config

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=250_000, metabolism_high_chars=400_000,
    ))
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_high_chars": None})
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(req)
    assert exc.value.status_code == 400


def test_put_non_positive_is_400(api_db):
    from api.routes import config

    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_high_chars=0))
    assert exc.value.status_code == 400


# ── F1: 全体の変更が既存モデルの部分上書きを壊すなら 400 (Codex 指摘 2026-09-03) ──


MODEL_PARTIAL_TARGET = "wm-test-partial-target"


def test_put_that_breaks_a_partially_overriding_model_is_400(api_db, monkeypatch):
    """target=25万 だけ書いたモデルは全体 high=30万 の下でだけ正しい。high を戻すと壊れる。"""
    from api.routes import config

    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, MODEL_PARTIAL_TARGET,
        {"model": "x", "metabolism_target_chars": 250_000},
    )
    # モデルは target だけ → high は全体既定で埋まる。全体 high=40万 なら 25万 ≤ 40万 で通る。
    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_high_chars=400_000,
    ))
    # high を既定 (組み込み 6万) に戻す → そのモデルは target 25万 > high 6万 で壊れる。
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_high_chars": None})
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(req)
    assert exc.value.status_code == 400
    assert MODEL_PARTIAL_TARGET in exc.value.detail
    assert "モデル側" in exc.value.detail
    # 失敗した PUT は何も変えない (DB もキャッシュも)
    assert config.get_metabolism_defaults()["global"] == _short(None, 400_000)
    # high を下げても順序が保たれる分には通る
    payload = config.put_metabolism_defaults(
        config.MetabolismDefaultsRequest(metabolism_high_chars=330_000),
    )
    assert payload["global"]["high"] == 330_000
    assert model_configs.resolve_metabolism_watermarks(MODEL_PARTIAL_TARGET) == (250_000, 330_000)


def test_conflict_error_names_at_most_five_models(api_db, monkeypatch):
    from api.routes import config

    keys = [f"wm-test-many-{i}" for i in range(7)]
    for key in keys:
        monkeypatch.setitem(
            model_configs.MODEL_CONFIGS, key, {"model": "x", "metabolism_target_chars": 250_000},
        )
    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_high_chars=400_000))
    req = config.MetabolismDefaultsRequest.model_validate({"metabolism_high_chars": None})
    with pytest.raises(HTTPException) as exc:
        config.put_metabolism_defaults(req)
    named = [key for key in keys if key in exc.value.detail]
    assert len(named) == 5
    assert "ほか 2 件" in exc.value.detail


def test_model_with_null_watermark_never_conflicts(api_db):
    """null = その水位を持たない = 順序の対象外なので、全体をどう動かしても矛盾しない。"""
    from api.routes import config

    # MODEL_NULL (high/target が null) は fixture で入っている
    payload = config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=50_000, metabolism_high_chars=150_000,
    ))
    assert payload["global"] == _short(50_000, 150_000)


# ── F2: PUT は直列化し、合成の土台は DB 行 (Codex 指摘 2026-09-03) ──────────


def test_sequential_single_field_puts_both_survive(api_db):
    from api.routes import config
    from database.models import UserSettings

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_target_chars=30_000))
    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(metabolism_high_chars=150_000))
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert (row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) == (30_000, 150_000)
    assert get_global_watermark_defaults() == {
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": 150_000,
    }


def test_merge_base_is_db_row_not_cache(api_db):
    """キャッシュが DB とずれていても、PUT は DB 行を土台に合成する。"""
    from api.routes import config
    from database.models import UserSettings

    config.put_metabolism_defaults(config.MetabolismDefaultsRequest(
        metabolism_target_chars=50_000, metabolism_high_chars=150_000,
    ))
    # キャッシュだけを別の値にずらす (DB は 50k/150k のまま)
    set_global_watermark_defaults({"metabolism_target_chars": 20_000})
    payload = config.put_metabolism_defaults(
        config.MetabolismDefaultsRequest(metabolism_high_chars=180_000),
    )
    assert payload["global"] == _short(50_000, 180_000)
    with api_db() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert (row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) == (50_000, 180_000)


def test_concurrent_single_field_puts_do_not_lose_updates(api_db):
    """二スレッドが別々の一項目を同時に PUT しても、二つとも残る (ロックで直列化)。"""
    import threading

    from api.routes import config
    from database.models import UserSettings

    # 各値は単独でも (組み込み既定と組んでも) 順序検査を通る:
    # target=3万 なら上限は組み込み 6万、high=15万 なら残す量は組み込み 2万。
    requests = [
        config.MetabolismDefaultsRequest(metabolism_target_chars=30_000),
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
        assert (row.METABOLISM_TARGET_CHARS, row.METABOLISM_HIGH_CHARS) == (30_000, 150_000)
    assert get_global_watermark_defaults() == {
        "metabolism_target_chars": 30_000,
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
    set_global_watermark_defaults({
        "metabolism_target_chars": 30_000,
        "metabolism_high_chars": 60_000,
    })
    calls.clear()
    assert resolve_metabolism_watermarks(MODEL_NO_KEYS) == (30_000, 60_000)
    assert len(calls) == 1
    calls.clear()
    assert resolve_metabolism_watermarks(MODEL_EXPLICIT) == (6_000, 90_000)
    assert len(calls) == 1


def test_publish_replaces_mapping_identity_and_old_one_is_frozen(isolated_globals):
    from types import MappingProxyType

    set_global_watermark_defaults({"metabolism_target_chars": 30_000})
    before = model_configs._current_global_defaults()
    assert isinstance(before, MappingProxyType)
    snapshot = dict(before)

    set_global_watermark_defaults({"metabolism_target_chars": 70_000, "metabolism_high_chars": 90_000})
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
        return (2_000, 3_000)

    monkeypatch.setattr(model_configs, "resolve_metabolism_watermarks", _fake_resolve)
    lifecycle = SessionLifecycle(SimpleNamespace(), None)
    wm = lifecycle.get_metabolism_watermarks(SimpleNamespace(model=MODEL_NO_KEYS))
    assert seen == [MODEL_NO_KEYS]
    assert (wm.target, wm.high) == (2_000, 3_000)


# ── 移行 (user_settings への列追加は try_additive_migration の schema 差分で入る) ──


def test_additive_migration_adds_columns_and_is_idempotent(tmp_path):
    from database.migrate import needs_migration, try_additive_migration
    from database.models import Base, UserSettings

    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    watermark_columns = ("METABOLISM_TARGET_CHARS", "METABOLISM_HIGH_CHARS")
    with engine.begin() as conn:
        for col in watermark_columns:
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
    assert set(watermark_columns) <= cols
    with sessionmaker(bind=engine)() as db:
        row = db.query(UserSettings).filter(UserSettings.USERID == 1).one()
        assert row.METABOLISM_HIGH_CHARS is None  # 既存行は NULL = 未設定
    engine.dispose()

    # 二度目: 差分なし → True のまま、壊れない
    assert try_additive_migration(str(db_path))
    assert not needs_migration(str(db_path))


def test_obsolete_low_column_triggers_full_rewrite_migration(tmp_path):
    """旧 METABOLISM_LOW_CHARS 列が残った DB は additive では解消できず全書換に回る。

    低水位の廃止 (2026-09-04) で列は models.py から消えた。DB にだけ残る「余分な
    列」は needs_migration が検出し、try_additive_migration は False (列削除は
    全書換の領分) を返す — 起動時の全書換が新スキーマへコピーして列を落とす。
    """
    from database.migrate import needs_migration, try_additive_migration
    from database.models import Base

    db_path = tmp_path / "with_low.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            'ALTER TABLE user_settings ADD COLUMN "METABOLISM_LOW_CHARS" INTEGER'
        ))
    engine.dispose()

    assert needs_migration(str(db_path))
    assert not try_additive_migration(str(db_path))


def test_obsolete_perception_columns_are_dropped_in_place(tmp_path):
    """旧 PERCEPTION_*_CHARS 列は KNOWN_COLUMN_DROPS が ALTER で落とす (2026-09-09)。

    列を消すだけの差分で全書換 (ファイル move) に落ちると、生きた DB では
    Windows のファイルロックを踏む。落とす列を名指ししてあるので、追加系の
    パスがその場で解消し、他の値 (既に保存されている残す量 / 上限) は残る。
    """
    from database.migrate import needs_migration, try_additive_migration
    from database.models import Base

    db_path = tmp_path / "with_perception.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for col in ("PERCEPTION_TARGET_CHARS", "PERCEPTION_HIGH_CHARS"):
            conn.execute(text(
                f'ALTER TABLE user_settings ADD COLUMN "{col}" INTEGER'
            ))
        conn.execute(text(
            "INSERT INTO user (USERID, PASSWORD, USERNAME, LOGGED_IN) "
            "VALUES (1, 'p', 'u', 0)"
        ))
        conn.execute(text(
            "INSERT INTO user_settings "
            "(USERID, TUTORIAL_COMPLETED, LAST_TUTORIAL_VERSION, "
            "METABOLISM_TARGET_CHARS, PERCEPTION_HIGH_CHARS) "
            "VALUES (1, 0, 1, 30000, 60000)"
        ))
    engine.dispose()

    assert needs_migration(str(db_path))
    assert try_additive_migration(str(db_path))  # 全書換に落ちない
    assert not needs_migration(str(db_path))

    engine = create_engine(f"sqlite:///{db_path}")
    cols = {c["name"] for c in inspect(engine).get_columns("user_settings")}
    assert "PERCEPTION_HIGH_CHARS" not in cols
    assert "PERCEPTION_TARGET_CHARS" not in cols
    with engine.begin() as conn:
        kept = conn.execute(text(
            "SELECT METABOLISM_TARGET_CHARS FROM user_settings WHERE USERID = 1"
        )).scalar()
    assert kept == 30_000  # 残す量は無傷
    engine.dispose()

    # 二度目は何もすることが無い (冪等)
    assert try_additive_migration(str(db_path))
    assert not needs_migration(str(db_path))


def test_column_drops_are_all_or_nothing(tmp_path, monkeypatch):
    """廃止列の削除は 1 トランザクション — 途中で失敗したら部分適用を残さない。

    列ごとに commit すると「片方だけ消えた」DB が生まれ、呼び出し側
    (try_additive_migration) の「追加系は部分適用しない」という契約とも食い違う
    (ローカルレビュー指摘 2026-09-10)。SQLite は索引の張られた列を DROP できない
    ので、二列目にそれを置いて失敗を作る。
    """
    from database import migrate
    from database.models import Base

    db_path = tmp_path / "partial_drop.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for col in ("ZZ_PLAIN", "ZZ_INDEXED"):
            conn.execute(text(
                f'ALTER TABLE user_settings ADD COLUMN "{col}" INTEGER'
            ))
        conn.execute(text(
            "CREATE INDEX ix_zz_indexed ON user_settings (ZZ_INDEXED)"
        ))
    engine.dispose()

    monkeypatch.setattr(
        migrate, "KNOWN_COLUMN_DROPS",
        {"user_settings": ("ZZ_PLAIN", "ZZ_INDEXED")},
    )
    migrate.apply_known_column_drops(str(db_path))  # 例外は投げず警告で続行

    engine = create_engine(f"sqlite:///{db_path}")
    cols = {c["name"] for c in inspect(engine).get_columns("user_settings")}
    engine.dispose()
    # 二列目の失敗で一列目も戻っている (全部落ちるか、一つも落ちないか)。
    assert "ZZ_PLAIN" in cols
    assert "ZZ_INDEXED" in cols

    # 対照: 一列目だけなら落ちる — つまり上で残っていたのは「落とせなかった」の
    # ではなく、二列目の失敗で**戻した**結果 (テストが素通りしていない証拠)。
    monkeypatch.setattr(
        migrate, "KNOWN_COLUMN_DROPS", {"user_settings": ("ZZ_PLAIN",)},
    )
    migrate.apply_known_column_drops(str(db_path))
    engine = create_engine(f"sqlite:///{db_path}")
    cols = {c["name"] for c in inspect(engine).get_columns("user_settings")}
    engine.dispose()
    assert "ZZ_PLAIN" not in cols
