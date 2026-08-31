"""v0.3 の止め具 (autonomy_wiring.AUTONOMOUS_DRIVING_SHIPPED) の回帰。

v0.3 のリリース範囲は「形の層」で、自律の**運転** (時間割 + 判断点) は v0.4 の
管轄 (docs/intent/autonomous_behavior_v3.md §11)。ところが自律の ON/OFF は
ペルソナごとの ``AI.AUTONOMY_ENABLED`` (既定 True) しか無く、v0.3 では
その切り替え UI を隠したため、新しいユーザーの世界は「既定で自律 ON、OFF に
する手段が無い」状態になる。そこで定数一つの止め具を
``saiverse.autonomy_wiring`` の判定関数に置いた (2026-08-23 まはー裁定)。

ここで検証するのは **止め具が効いている状態** なので、``tests/conftest.py`` の
autouse 固定具 (テスト中は定数を True にして v0.4 の設計の振る舞いを検証し
続けるためのもの) を、各テストで明示的に外して False に戻す。

止めるもの: 判断点 / watchdog (見張り) / 起動時のコマ再予約 / 実イベントの
判断経由。止めないもの: 会話・スルース・手帳・沈黙タイマー・実イベントと
仲裁の**直接応答** (v0.2 と同じ経路)。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from saiverse import autonomy_wiring as wiring

PERSONA_ID = "alice"


@pytest.fixture
def gate_off(monkeypatch):
    """conftest の autouse 固定具を外し、本番と同じ「止め具あり」に戻す。"""
    monkeypatch.setattr(wiring, "AUTONOMOUS_DRIVING_SHIPPED", False)


def _manager(autonomy_enabled: bool = True) -> Any:
    """自律 ON のペルソナを 1 人だけ持つ最小の manager スタブ。"""
    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        current_building_id="alice_room",
        autonomy_enabled=autonomy_enabled,
    )
    return SimpleNamespace(personas={PERSONA_ID: persona})


def _record_fire(monkeypatch) -> List[Dict[str, Any]]:
    """fire_judgment_point の呼び出しを記録する (呼ばれないことの証拠用)。"""
    calls: List[Dict[str, Any]] = []

    def _fake(mgr, pid, kind, context=None, **kw):
        calls.append({"kind": kind, "context": context})
        return {"submitted": True}

    monkeypatch.setattr(wiring, "fire_judgment_point", _fake)
    return calls


# ---------------------------------------------------------------------------
# ① 判定関数そのもの
# ---------------------------------------------------------------------------


def test_is_autonomy_on_is_false_even_for_an_autonomy_enabled_persona(gate_off):
    """止め具が効いている間は、AUTONOMY_ENABLED=True のペルソナでも False。"""
    manager = _manager(autonomy_enabled=True)
    assert wiring.is_autonomy_on(manager, PERSONA_ID) is False


def test_is_autonomy_on_follows_the_persona_setting_once_shipped(monkeypatch):
    """止め具を外せば (v0.4)、判定はペルソナごとの設定値に戻る。"""
    monkeypatch.setattr(wiring, "AUTONOMOUS_DRIVING_SHIPPED", True)
    assert wiring.is_autonomy_on(_manager(autonomy_enabled=True), PERSONA_ID) is True
    assert wiring.is_autonomy_on(_manager(autonomy_enabled=False), PERSONA_ID) is False


def test_the_persona_setting_is_not_rewritten_by_the_gate(gate_off):
    """止め具は判定だけを止める — DB / in-memory の設定値は書き換えない。"""
    manager = _manager(autonomy_enabled=True)
    wiring.is_autonomy_on(manager, PERSONA_ID)
    assert manager.personas[PERSONA_ID].autonomy_enabled is True


# ---------------------------------------------------------------------------
# ② 実イベント: 判断点を撃たず、直接応対へ落ちる (v0.2 と同じ経路)
# ---------------------------------------------------------------------------


def test_external_event_skips_the_judgment_and_dispatches_directly(
    gate_off, monkeypatch,
):
    manager = _manager(autonomy_enabled=True)
    calls = _record_fire(monkeypatch)
    dispatched: List[str] = []

    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        dispatch_direct=lambda: dispatched.append("direct"),
    )

    assert route == wiring.ROUTE_DIRECT_AUTONOMY_DISABLED
    assert dispatched == ["direct"]  # イベントは落とさない
    assert calls == []               # 判断点は一度も撃たない


def test_user_utterance_conflict_engages_directly(gate_off, monkeypatch):
    """仲裁も判断を経ずに直接応答する (v0.2 と同じ挙動)。"""
    manager = _manager(autonomy_enabled=True)
    calls = _record_fire(monkeypatch)
    engaged: List[str] = []

    route = wiring.handle_user_utterance_conflict(
        manager, PERSONA_ID, "ねえ、いま話せる？",
        engage=lambda: engaged.append("engage"), user_id="1",
    )

    assert route == wiring.ROUTE_DIRECT_AUTONOMY_DISABLED
    assert engaged == ["engage"]
    assert calls == []


# ---------------------------------------------------------------------------
# ③ 見張り (watchdog) は何もしない
# ---------------------------------------------------------------------------


def test_watchdog_tick_does_nothing(gate_off, monkeypatch):
    manager = _manager(autonomy_enabled=True)
    calls = _record_fire(monkeypatch)

    out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out == {"action": "skip", "reason": "autonomy disabled"}
    assert calls == []


def test_fire_judgment_point_is_refused_at_the_gate(gate_off, monkeypatch):
    """判断点の共通入口も、止め具で submitted=False になる。"""
    manager = _manager(autonomy_enabled=True)
    result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open")
    assert result["submitted"] is False
    assert result["reason"] == "persona autonomy disabled"


# ---------------------------------------------------------------------------
# ④ AutonomyManager (見張りの器) が 1 本も立たない
# ---------------------------------------------------------------------------


def test_ensure_autonomy_for_does_not_start_a_tick(gate_off):
    from saiverse.saiverse_manager import SAIVerseManager

    manager = _manager(autonomy_enabled=True)
    manager._started = True
    manager._autonomy_managers = {}

    SAIVerseManager.ensure_autonomy_for(manager, PERSONA_ID)

    assert manager._autonomy_managers == {}


def test_startup_slot_rescheduling_goes_through_the_gate():
    """起動時のコマ再予約が、直接 autonomy_enabled を読まずゲートを通ること。"""
    from saiverse.saiverse_manager import SAIVerseManager

    source = inspect.getsource(SAIVerseManager._on_persona_registered)
    assert "is_autonomy_on" in source
    assert "autonomy_enabled" not in source


# ---------------------------------------------------------------------------
# ⑤ 起動時の INFO ログは一度だけ
# ---------------------------------------------------------------------------


def test_shipping_gate_logs_once_at_startup(gate_off, monkeypatch, caplog):
    monkeypatch.setattr(wiring, "_SHIPPING_GATE_LOGGED", False)

    with caplog.at_level("INFO", logger="saiverse.autonomy_wiring"):
        first = wiring.log_shipping_gate_once()
        second = wiring.log_shipping_gate_once()

    assert first is True
    assert second is False
    messages = [r.message for r in caplog.records if "自律の駆動" in r.message]
    assert len(messages) == 1
    assert "判断点" in messages[0]


def test_shipping_gate_is_silent_once_shipped(monkeypatch, caplog):
    """止め具を外した後 (v0.4) は何も出さない。"""
    monkeypatch.setattr(wiring, "AUTONOMOUS_DRIVING_SHIPPED", True)
    monkeypatch.setattr(wiring, "_SHIPPING_GATE_LOGGED", False)

    with caplog.at_level("INFO", logger="saiverse.autonomy_wiring"):
        assert wiring.log_shipping_gate_once() is False
    assert [r for r in caplog.records if "自律の駆動" in r.message] == []


def test_manager_start_announces_the_gate():
    """起動経路が止め具の告知を通ること (配線の回帰)。"""
    from saiverse.saiverse_manager import SAIVerseManager

    assert "log_shipping_gate_once" in inspect.getsource(SAIVerseManager.start)
