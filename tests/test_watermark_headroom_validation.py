"""水位の保存時検査「整理を始める量 − 残す量 > 知覚の上限 + 余裕」のテスト。

docs/issues/watermarks_unsatisfiable_when_perception_is_large.md 裁定 4。

残す量 (会話の行だけを数える) と整理を始める量 (実際に送る合計を数える) は主語が
違うので、その差より知覚の上限が大きいと、会話をどれだけ畳んでも合計が上限を
下回らない。設定の時点でその状態を作れないようにするのがこの検査で、保存の入口
(モデルの作成 / 更新 / 複製 / チャットから保存、全体設定の PUT) すべてが通る。

余裕の分は `saiverse.model_configs.WATERMARK_HEADROOM_CHARS` の一箇所。数字が
変わってもこのファイルが追随するよう、値は定数から取って組み立てる。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.routes import config as config_module
from api.routes.config import _validate_watermarks
from saiverse import model_configs
from saiverse.model_configs import (
    BUILTIN_PERCEPTION_HIGH_CHARS,
    WATERMARK_HEADROOM_CHARS,
)


@pytest.fixture
def global_defaults():
    """全体既定を空から始め、テスト後に元へ戻す。"""
    saved = model_configs.get_global_watermark_defaults()
    model_configs.set_global_watermark_defaults({})
    yield model_configs.set_global_watermark_defaults
    model_configs.set_global_watermark_defaults(saved)


# ── モデル保存の入口 ─────────────────────────────────────────────


def test_gap_smaller_than_perception_cap_is_rejected(global_defaults):
    """差 3万 < 知覚の上限 6万 + 余裕 → 400、文言に理由と直し方が入る。"""
    with pytest.raises(HTTPException) as exc:
        _validate_watermarks({
            "metabolism_target_chars": 30_000, "metabolism_high_chars": 60_000,
        })
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert "部屋の様子" in detail
    assert f"{BUILTIN_PERCEPTION_HIGH_CHARS:,}" in detail
    assert f"{WATERMARK_HEADROOM_CHARS:,}" in detail
    assert "整理をはじめる文字数を増やす" in detail


def test_gap_equal_to_needed_is_rejected(global_defaults):
    """等号ぎりぎりは通さない — 畳み残しの端数があるので > で組む。"""
    needed = BUILTIN_PERCEPTION_HIGH_CHARS + WATERMARK_HEADROOM_CHARS
    with pytest.raises(HTTPException) as exc:
        _validate_watermarks({
            "metabolism_target_chars": 40_000,
            "metabolism_high_chars": 40_000 + needed,
        })
    assert exc.value.status_code == 400


def test_gap_one_char_over_needed_passes(global_defaults):
    needed = BUILTIN_PERCEPTION_HIGH_CHARS + WATERMARK_HEADROOM_CHARS
    _validate_watermarks({
        "metabolism_target_chars": 40_000,
        "metabolism_high_chars": 40_000 + needed + 1,
    })


def test_builtin_defaults_satisfy_the_check(global_defaults):
    """組み込み既定 (4万 / 12万 / 知覚 6万) はこの検査を満たす。"""
    _validate_watermarks({})


def test_lowering_perception_cap_makes_a_narrow_window_savable(global_defaults):
    """差 3万 でも、そのモデルの知覚の水位を 1万 まで下げれば通る。

    下ろす到達点 (下の水位) も一緒に下げる必要がある — 上限だけ下げると
    「省略した後に残す量 (既定 4万) > 省略をはじめる量 (1万)」の順序違反になる
    (Metabolism 側で上限だけ下げたときと同じ扱い)。
    """
    narrow = {"metabolism_target_chars": 30_000, "metabolism_high_chars": 60_000}
    with pytest.raises(HTTPException):
        _validate_watermarks(narrow)
    with pytest.raises(HTTPException) as exc:
        _validate_watermarks({**narrow, "perception_high_chars": 10_000})
    assert "以下にしてください" in exc.value.detail
    _validate_watermarks({
        **narrow, "perception_high_chars": 10_000, "perception_target_chars": 8_000,
    })


def test_perception_opt_out_waives_the_check(global_defaults):
    """知覚の上限を明示 null にしたモデルは検査しない (合計は伸びるに任せる選択)。

    「上限を下回る」約束を意図して捨てた設定なので、入口で咎めない — 実行時は
    裁定 7 と同じく旗を立てて超過を許す。
    """
    _validate_watermarks({
        "metabolism_target_chars": 30_000, "metabolism_high_chars": 60_000,
        "perception_high_chars": None,
    })


def test_metabolism_high_opt_out_waives_the_check(global_defaults):
    """文字数で発火しないモデル (high=null) には「上限を下回る」という約束が無い。"""
    _validate_watermarks({
        "metabolism_target_chars": 30_000, "metabolism_high_chars": None,
    })


# ── 空欄は実効値 (全体設定 → 組み込み) で埋めて比べる ────────────


def test_blank_metabolism_is_filled_from_global(global_defaults):
    """知覚の上限だけ書いたモデルは、全体設定の Metabolism 水位と比べられる。"""
    only_perception = {"perception_high_chars": 100_000}
    # 全体が組み込み (差 8万) のままなら 10万 + 余裕 に足りず 400
    with pytest.raises(HTTPException) as exc:
        _validate_watermarks(only_perception)
    assert "全体設定の既定値で数えます" in exc.value.detail
    # 全体の上限を上げれば同じモデル定義が通る
    global_defaults({"metabolism_high_chars": 200_000})
    _validate_watermarks(only_perception)


def test_blank_perception_is_filled_from_global(global_defaults):
    """Metabolism だけ書いたモデルは、全体設定の知覚の上限と比べられる。"""
    narrow = {"metabolism_target_chars": 30_000, "metabolism_high_chars": 60_000}
    with pytest.raises(HTTPException):
        _validate_watermarks(narrow)
    global_defaults({"perception_target_chars": 8_000, "perception_high_chars": 10_000})
    _validate_watermarks(narrow)


# ── 全体設定の PUT の入口 ────────────────────────────────────────


@pytest.fixture
def api_db(monkeypatch: pytest.MonkeyPatch, global_defaults):
    """本物の SQLite (メモリ) を database.session.SessionLocal に差し込む。

    モデル表も水位を持たない一つだけに差し替える — 開発機の user_data の
    モデル定義が混ざると、全体既定の PUT が環境依存で弾かれる。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import database.session
    from database.models import Base

    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {"wm-plain": {"model": "x"}})
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database.session, "SessionLocal", Session)
    yield Session
    engine.dispose()


def test_put_that_breaks_the_headroom_is_400(api_db):
    """全体設定でも同じ式で弾き、同じ理由を返す。"""
    with pytest.raises(HTTPException) as exc:
        config_module.put_metabolism_defaults(config_module.MetabolismDefaultsRequest(
            metabolism_target_chars=30_000, metabolism_high_chars=60_000,
        ))
    assert exc.value.status_code == 400
    assert "部屋の様子" in exc.value.detail
    # 失敗した PUT は何も残さない
    assert config_module.get_metabolism_defaults()["global"]["high"] is None


def test_put_of_perception_cap_alone_is_checked_against_effective_metabolism(api_db):
    """知覚の上限だけを上げる PUT も、実効の Metabolism 水位 (組み込み) と比べる。"""
    with pytest.raises(HTTPException) as exc:
        config_module.put_metabolism_defaults(
            config_module.MetabolismDefaultsRequest(perception_high_chars=100_000),
        )
    assert exc.value.status_code == 400
    assert "組み込み既定で数えます" in exc.value.detail


def test_put_lowering_both_together_passes(api_db):
    """二族を同じリクエストで動かせば、狭い窓の設定にも到達できる。"""
    payload = config_module.put_metabolism_defaults(config_module.MetabolismDefaultsRequest(
        metabolism_target_chars=30_000, metabolism_high_chars=60_000,
        perception_target_chars=8_000, perception_high_chars=10_000,
    ))
    assert payload["global"]["perception_high"] == 10_000


def test_put_that_breaks_an_existing_model_names_it(api_db, monkeypatch):
    """全体の知覚上限を上げた結果、部分上書きしているモデルが成立しなくなるなら 400。

    全体の組そのもの (検査 1) は通す — 上限を 30万 に上げてあるので差は十分ある。
    落ちるのは自分で水位を書いているモデル (差 9万) だけで、名前が出る。
    """
    monkeypatch.setitem(
        model_configs.MODEL_CONFIGS, "wm-narrow",
        {"model": "x", "metabolism_target_chars": 30_000, "metabolism_high_chars": 120_000},
    )
    with pytest.raises(HTTPException) as exc:
        config_module.put_metabolism_defaults(config_module.MetabolismDefaultsRequest(
            metabolism_high_chars=300_000,
            perception_target_chars=80_000, perception_high_chars=85_000,
        ))
    assert exc.value.status_code == 400
    assert "wm-narrow" in exc.value.detail
    assert "モデル側" in exc.value.detail


# ── 複製とチャットからの保存も同じ入口 ──────────────────────────


def _patch_model_file_env(monkeypatch, tmp_path, configs):
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", configs)
    monkeypatch.setattr(model_configs, "reload_configs", lambda: None)
    monkeypatch.setattr(
        config_module, "_model_user_path", lambda key: tmp_path / f"{key}.json",
    )


def test_clone_of_unsatisfiable_source_is_rejected(global_defaults, monkeypatch, tmp_path):
    from api.routes.config import ModelFileCloneRequest, clone_model_file

    _patch_model_file_env(monkeypatch, tmp_path, {
        "narrow_source": {
            "model": "x", "metabolism_target_chars": 30_000, "metabolism_high_chars": 60_000,
        },
    })
    with pytest.raises(HTTPException) as exc:
        clone_model_file("narrow_source", ModelFileCloneRequest(new_key="narrow_copy"))
    assert exc.value.status_code == 400
    assert not (tmp_path / "narrow_copy.json").exists()


def test_save_from_chat_with_unsatisfiable_watermarks_is_rejected(
    global_defaults, monkeypatch, tmp_path,
):
    from api.routes.config import SaveModelFromChatRequest, save_model_from_chat

    _patch_model_file_env(monkeypatch, tmp_path, {"src": {"model": "x"}})
    with pytest.raises(HTTPException) as exc:
        save_model_from_chat(SaveModelFromChatRequest(
            source_model="src", target_key="dst", display_name="Dst",
            metabolism_target_chars=30_000, metabolism_high_chars=60_000,
        ))
    assert exc.value.status_code == 400
    assert not (tmp_path / "dst.json").exists()


def test_create_and_update_go_through_the_same_check(global_defaults, monkeypatch, tmp_path):
    from api.routes.config import (
        ModelFileCreateRequest,
        ModelFileUpdateRequest,
        create_model_file,
        update_model_file,
    )

    _patch_model_file_env(monkeypatch, tmp_path, {"existing": {"model": "x"}})
    monkeypatch.setattr(config_module, "_validate_model_connection", lambda key, cfg: None)
    narrow = {
        "model": "x", "metabolism_target_chars": 30_000, "metabolism_high_chars": 60_000,
    }
    with pytest.raises(HTTPException) as exc:
        create_model_file(ModelFileCreateRequest(key="fresh", config=dict(narrow)))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        update_model_file("existing", ModelFileUpdateRequest(config=dict(narrow)))
    assert exc.value.status_code == 400
    assert not (tmp_path / "fresh.json").exists()
    assert not (tmp_path / "existing.json").exists()
