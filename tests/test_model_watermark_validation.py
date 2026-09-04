"""モデル保存 API の水位順序バリデーション (実効値で 低 ≤ 目標 ≤ 高) のテスト。

キー無しは実行時に組み込み既定 (低4万/目標10万/高20万) へ解決されるため、
入力された数値同士だけを比較すると「high=5万だけ指定 → 実効 target=10万 > high」
の壊れた順序が保存できてしまう (Codex 指摘 2026-07-30)。
docs/issues/chat_options_metabolism_section_redesign.md
"""
import pytest
from fastapi import HTTPException

from api.routes.config import _validate_metabolism_watermarks


def test_partial_high_below_default_target_is_rejected():
    """high=5万だけ指定 → 実効 target=既定10万 > high で 400。"""
    with pytest.raises(HTTPException) as exc:
        _validate_metabolism_watermarks({"metabolism_high_chars": 50_000})
    assert exc.value.status_code == 400


def test_partial_target_below_default_low_is_rejected():
    """target=3万だけ指定 → 実効 low=既定4万 > target で 400。"""
    with pytest.raises(HTTPException) as exc:
        _validate_metabolism_watermarks({"metabolism_target_chars": 30_000})
    assert exc.value.status_code == 400


def test_consistent_partial_spec_passes():
    _validate_metabolism_watermarks({
        "metabolism_high_chars": 50_000,
        "metabolism_target_chars": 40_000,
        "metabolism_low_chars": 10_000,
    })


def test_defaults_only_passes():
    _validate_metabolism_watermarks({})


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
    """全体 target=3万 → high=5万 だけ書いたモデルは通る (組み込み 10万 基準では 400 だった)。"""
    with pytest.raises(HTTPException):
        _validate_metabolism_watermarks({"metabolism_high_chars": 50_000})
    global_defaults({"metabolism_low_chars": 10_000, "metabolism_target_chars": 30_000})
    _validate_metabolism_watermarks({"metabolism_high_chars": 50_000})


def test_unset_global_falls_back_to_builtin(global_defaults):
    global_defaults({})
    with pytest.raises(HTTPException):
        _validate_metabolism_watermarks({"metabolism_high_chars": 50_000})
