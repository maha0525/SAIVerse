"""モデル保存 API の水位順序バリデーション (実効値で 残す量 ≤ 上限) のテスト。

キー無しは実行時に組み込み既定 (残す量4万/上限12万 — 2026-09-04 縮小。指摘当時は
10万/20万) へ解決されるため、入力された数値同士だけを比較すると
「high=3万だけ指定 → 実効 target=4万 > high」の壊れた順序が保存できてしまう
(Codex 指摘 2026-07-30)。
docs/issues/chat_options_metabolism_section_redesign.md
(旧三水位の低水位 `metabolism_low_chars` は 2026-09-04 廃止 — キーが残っていても
検証は黙って通す)
"""
import pytest
from fastapi import HTTPException

from api.routes.config import _validate_metabolism_watermarks


def test_partial_high_below_default_target_is_rejected():
    """high=3万だけ指定 → 実効 target=既定4万 > high で 400。"""
    with pytest.raises(HTTPException) as exc:
        _validate_metabolism_watermarks({"metabolism_high_chars": 30_000})
    assert exc.value.status_code == 400


def test_consistent_partial_spec_passes():
    _validate_metabolism_watermarks({
        "metabolism_high_chars": 50_000,
        "metabolism_target_chars": 40_000,
    })


def test_defaults_only_passes():
    _validate_metabolism_watermarks({})


def test_obsolete_low_key_is_silently_ignored():
    """廃止済み metabolism_low_chars が残っていてもエラーにしない (データ互換)。

    値がどれだけ大きくても順序制約に参加しない — 実行時に読まれないキーを
    保存の入口で咎めない。
    """
    _validate_metabolism_watermarks({
        "metabolism_low_chars": 999_999_999,
        "metabolism_target_chars": 40_000,
        "metabolism_high_chars": 50_000,
    })


def test_explicit_null_lifts_the_constraint():
    """null = その水位を持たない → 順序制約の対象外 (実行時の解釈と同じ)。"""
    _validate_metabolism_watermarks({
        "metabolism_high_chars": 50_000,
        "metabolism_target_chars": None,
    })


def test_zero_or_negative_means_disabled():
    """0 以下は実行時に「持たない」扱い (_metabolism_chars) — 検証も同じ解釈。"""
    _validate_metabolism_watermarks({
        "metabolism_high_chars": 50_000,
        "metabolism_target_chars": 0,
    })


def test_non_numeric_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_metabolism_watermarks({"metabolism_target_chars": "abc"})
    assert exc.value.status_code == 400


def test_bool_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_metabolism_watermarks({"metabolism_high_chars": True})
    assert exc.value.status_code == 400


# ── 全体設定の既定で埋める (2026-09-03) ─────────────────────────
#
# キー無しは「組み込み定数」ではなく「実効既定 = 全体設定があればそれ」で埋める。
# high だけ書いたモデルは、実行時に全体設定の target と組になるので、検証も
# その組で行う: 全体で target=3万 に下げた環境なら high=5万 は正しい順序で通り、
# 全体で target=15万 に上げた環境なら high=12万 は壊れた順序として弾く。


@pytest.fixture
def global_defaults():
    from saiverse import model_configs

    saved = model_configs.get_global_metabolism_defaults()
    yield model_configs.set_global_metabolism_defaults
    model_configs.set_global_metabolism_defaults(saved)


def test_high_below_global_target_is_rejected(global_defaults):
    """全体 target=15万 → high=12万 だけ書いたモデルは 400 (組み込み 10万 なら通っていた)。"""
    _validate_metabolism_watermarks({"metabolism_high_chars": 120_000})  # 組み込み基準では OK
    global_defaults({"metabolism_target_chars": 150_000})
    with pytest.raises(HTTPException) as exc:
        _validate_metabolism_watermarks({"metabolism_high_chars": 120_000})
    assert exc.value.status_code == 400
    assert "全体設定" in exc.value.detail


def test_high_above_lowered_global_target_passes(global_defaults):
    """全体 target=2万 → high=3万 だけ書いたモデルは通る (組み込み 4万 基準では 400 だった)。"""
    with pytest.raises(HTTPException):
        _validate_metabolism_watermarks({"metabolism_high_chars": 30_000})
    global_defaults({"metabolism_target_chars": 20_000})
    _validate_metabolism_watermarks({"metabolism_high_chars": 30_000})


def test_unset_global_falls_back_to_builtin(global_defaults):
    global_defaults({})
    with pytest.raises(HTTPException):
        _validate_metabolism_watermarks({"metabolism_high_chars": 30_000})


# ── clone も保存の入口 (2026-09-04) ─────────────────────────────
#
# clone_model_file は既存モデルの定義をそのまま user_data へ書き込む。ここが
# 検証を迂回すると、壊れた順序 (target=15万 だけ書かれ、実効 high=既定12万 を
# 超える) を持つ元モデルの複製が、不正な組のまま新モデルとして永続化される。


def _patch_clone_env(monkeypatch, tmp_path, configs):
    """clone_model_file をテスト内で完結させる: モデル表・書き込み先・再読込を差し替える。"""
    from saiverse import model_configs
    from api.routes import config as config_module

    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", configs)
    monkeypatch.setattr(model_configs, "reload_configs", lambda: None)
    monkeypatch.setattr(
        config_module, "_model_user_path", lambda key: tmp_path / f"{key}.json"
    )


def test_clone_of_inverted_source_is_rejected(monkeypatch, tmp_path):
    """target=15万 だけ書かれた元モデル (実効 high=既定12万 < target) の複製は 400。

    user_data にファイルが作られないこと (= 不正な組が永続化されないこと) も検証する。
    """
    from api.routes.config import ModelFileCloneRequest, clone_model_file

    _patch_clone_env(monkeypatch, tmp_path, {
        "broken_source": {"model": "x", "metabolism_target_chars": 150_000},
    })
    with pytest.raises(HTTPException) as exc:
        clone_model_file("broken_source", ModelFileCloneRequest(new_key="broken_copy"))
    assert exc.value.status_code == 400
    assert not (tmp_path / "broken_copy.json").exists()


def test_clone_of_valid_source_passes(monkeypatch, tmp_path):
    from api.routes.config import ModelFileCloneRequest, clone_model_file

    _patch_clone_env(monkeypatch, tmp_path, {"ok_source": {"model": "x"}})
    result = clone_model_file("ok_source", ModelFileCloneRequest(new_key="ok_copy"))
    assert result["key"] == "ok_copy"
    assert (tmp_path / "ok_copy.json").exists()
