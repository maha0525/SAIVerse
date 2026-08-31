"""Shared pytest fixtures and helpers for SAIVerse test suite."""

import sys
from pathlib import Path

import pytest  # noqa: F401

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Re-export for backward compatibility
from tool_loader import load_builtin_tool  # noqa: E402, F401


@pytest.fixture(autouse=True)
def _autonomous_driving_shipped(monkeypatch):
    """テスト中だけ v0.3 の止め具を外す (自律の駆動を「出荷済み」にする)。

    ``saiverse.autonomy_wiring.AUTONOMOUS_DRIVING_SHIPPED`` は v0.3 のリリース
    範囲を「形の層」に留めるための止め具で、本番では False (= 判断点・watchdog・
    コマの再予約が発火しない)。一方で既存の自律系テストは **v0.4 で配線する運転の
    設計そのもの**を固定している資産なので、止め具に引きずられて全部「何もしない」
    を検証するテストに化けてはいけない。だからテスト中は True にして、設計の
    振る舞いを検証し続ける。

    止め具そのものの回帰は ``tests/test_v03_autonomy_gate.py`` が、この固定具を
    明示的に外して (定数を False に戻して) 検証する。
    """
    from saiverse import autonomy_wiring

    monkeypatch.setattr(autonomy_wiring, "AUTONOMOUS_DRIVING_SHIPPED", True)
