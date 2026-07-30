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
