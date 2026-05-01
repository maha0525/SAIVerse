"""Phase 3 後段: `/run_playbook` Spell の単体テスト。

仕様: docs/intent/persona_cognition/nested_subline_spell.md (v0.1)

検証範囲 (この時点では context-less ユニットテストに絞る):
- エラーパス (active persona / manager / pulse_context が無い場合のメッセージ)
- 深さ制限の判定 (line_stack 長さに応じた拒否)
- router_callable=false の Playbook 拒否
- Playbook 名不正時のメッセージ
- 正常系: sea_runtime._run_playbook が line='sub' で呼ばれること

実 Playbook を起動するエンドツーエンド経路は、`/run_playbook` Spell 実装の
段階的展開 (track_user_conversation の 1-LLM + Spell 構成書き換え) で実機検証する。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Tool は importlib で動的ロードしているのでパスから直接読み込む
TOOL_PATH = (
    Path(__file__).resolve().parent.parent
    / "builtin_data" / "tools" / "run_playbook.py"
)
spec = importlib.util.spec_from_file_location("run_playbook_tool_under_test", str(TOOL_PATH))
run_playbook_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_playbook_module)

run_playbook = run_playbook_module.run_playbook
_MAX_LINE_STACK_DEPTH = run_playbook_module._max_depth()


# ---------------------------------------------------------------------------
# エラーパス: コンテキストが揃ってない
# ---------------------------------------------------------------------------


def test_run_playbook_returns_error_when_persona_id_missing():
    """contextvar に persona_id がセットされてない時はエラー文字列を返す。"""
    with patch.object(run_playbook_module, "get_active_persona_id", return_value=None):
        result = run_playbook(name="memory_research")
    assert "Active persona context" in result


def test_run_playbook_returns_error_when_manager_missing():
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=None):
        result = run_playbook(name="memory_research")
    assert "Manager reference" in result


def test_run_playbook_returns_error_when_pulse_context_missing():
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=MagicMock()), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=None):
        result = run_playbook(name="memory_research")
    assert "No active PulseContext" in result


# ---------------------------------------------------------------------------
# 深さ制限
# ---------------------------------------------------------------------------


def test_run_playbook_rejects_when_line_stack_at_max():
    """line_stack が最大深さに達していると起動を拒否する。"""
    pulse_ctx = SimpleNamespace(
        _line_stack=[object()] * _MAX_LINE_STACK_DEPTH,
        pulse_id="p1",
    )
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=MagicMock()), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=pulse_ctx):
        result = run_playbook(name="memory_research")
    assert "Subline depth limit" in result
    assert "memory_research" in result


def test_run_playbook_allows_when_line_stack_below_max():
    """深さ未達なら起動経路に入る (sea_runtime._run_playbook が呼ばれる)。"""
    pulse_ctx = SimpleNamespace(
        _line_stack=[object()] * (_MAX_LINE_STACK_DEPTH - 1),
        pulse_id="p1",
    )
    sea_runtime = MagicMock()
    sea_runtime._load_playbook_for = MagicMock(
        return_value=SimpleNamespace(name="memory_research", router_callable=True)
    )
    sea_runtime._run_playbook = MagicMock(side_effect=lambda *a, **kw: kw["parent_state"].update({"report_to_parent": "ok"}))
    manager = SimpleNamespace(
        sea_runtime=sea_runtime,
        personas={"air_city_a": SimpleNamespace(current_building_id="b1")},
    )
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=manager), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=pulse_ctx):
        result = run_playbook(name="memory_research")
    # New return shape: (report_text, metadata). No sub-playbook media here so
    # metadata is empty {}.
    assert result == ("ok", {})
    sea_runtime._run_playbook.assert_called_once()


# ---------------------------------------------------------------------------
# router_callable チェック
# ---------------------------------------------------------------------------


def test_run_playbook_rejects_non_router_callable():
    pulse_ctx = SimpleNamespace(_line_stack=[object()], pulse_id="p1")
    sea_runtime = MagicMock()
    sea_runtime._load_playbook_for = MagicMock(
        return_value=SimpleNamespace(name="source_web", router_callable=False)
    )
    manager = SimpleNamespace(
        sea_runtime=sea_runtime,
        personas={"air_city_a": SimpleNamespace(current_building_id="b1")},
    )
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=manager), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=pulse_ctx):
        result = run_playbook(name="source_web")
    assert "not callable from spell" in result
    assert "router_callable=false" in result


# ---------------------------------------------------------------------------
# Playbook 名不正
# ---------------------------------------------------------------------------


def test_run_playbook_returns_error_when_playbook_not_found():
    pulse_ctx = SimpleNamespace(_line_stack=[object()], pulse_id="p1")
    sea_runtime = MagicMock()
    sea_runtime._load_playbook_for = MagicMock(return_value=None)
    sea_runtime._playbook_cache = {}
    manager = SimpleNamespace(
        sea_runtime=sea_runtime,
        personas={"air_city_a": SimpleNamespace(current_building_id="b1")},
    )
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=manager), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=pulse_ctx):
        result = run_playbook(name="nonexistent_playbook")
    assert "not found" in result
    assert "nonexistent_playbook" in result


# ---------------------------------------------------------------------------
# 正常系: sub-line で _run_playbook が呼ばれる
# ---------------------------------------------------------------------------


def test_run_playbook_invokes_sub_line_with_correct_arguments():
    pulse_ctx = SimpleNamespace(_line_stack=[object()], pulse_id="pulse-xyz")
    captured: dict = {}

    def fake_run(playbook, persona_obj, building_id, **kwargs):
        captured["playbook"] = playbook
        captured["persona_obj"] = persona_obj
        captured["building_id"] = building_id
        captured["kwargs"] = kwargs
        # サブ Playbook が report_to_parent を書く挙動を mock
        kwargs["parent_state"]["report_to_parent"] = "Search complete: found 3 entries"

    sea_runtime = MagicMock()
    sea_runtime._load_playbook_for = MagicMock(
        return_value=SimpleNamespace(name="memory_research", router_callable=True)
    )
    sea_runtime._run_playbook = MagicMock(side_effect=fake_run)
    persona_obj = SimpleNamespace(current_building_id="building_air")
    manager = SimpleNamespace(
        sea_runtime=sea_runtime,
        personas={"air_city_a": persona_obj},
    )
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=manager), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=pulse_ctx):
        result = run_playbook(name="memory_research")
    # New return shape: (report_text, metadata). No sub-playbook media here so
    # metadata is empty {}.
    assert result == ("Search complete: found 3 entries", {})
    # line='sub' / pulse_context 共有 / parent_state に _pulse_id / _messages 空
    kwargs = captured["kwargs"]
    assert kwargs["line"] == "sub"
    assert kwargs["isolate_pulse_context"] is False
    parent_state = kwargs["parent_state"]
    assert parent_state["_pulse_id"] == "pulse-xyz"
    assert parent_state["_pulse_context"] is pulse_ctx
    assert parent_state["_messages"] == []


def test_run_playbook_warns_when_no_report_to_parent():
    """report_to_parent が無い場合、エラーは出さず警告メッセージを返す。"""
    pulse_ctx = SimpleNamespace(_line_stack=[object()], pulse_id="p1")
    sea_runtime = MagicMock()
    sea_runtime._load_playbook_for = MagicMock(
        return_value=SimpleNamespace(name="memory_research", router_callable=True)
    )
    sea_runtime._run_playbook = MagicMock(side_effect=lambda *a, **kw: None)
    manager = SimpleNamespace(
        sea_runtime=sea_runtime,
        personas={"air_city_a": SimpleNamespace(current_building_id="b1")},
    )
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=manager), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=pulse_ctx):
        result = run_playbook(name="memory_research")
    assert "no report_to_parent" in result
    assert "memory_research" in result


def test_run_playbook_returns_error_on_subline_exception():
    pulse_ctx = SimpleNamespace(_line_stack=[object()], pulse_id="p1")
    sea_runtime = MagicMock()
    sea_runtime._load_playbook_for = MagicMock(
        return_value=SimpleNamespace(name="memory_research", router_callable=True)
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("LLM provider unreachable")

    sea_runtime._run_playbook = MagicMock(side_effect=_explode)
    manager = SimpleNamespace(
        sea_runtime=sea_runtime,
        personas={"air_city_a": SimpleNamespace(current_building_id="b1")},
    )
    with patch.object(run_playbook_module, "get_active_persona_id", return_value="air_city_a"), \
         patch.object(run_playbook_module, "get_active_manager", return_value=manager), \
         patch.object(run_playbook_module, "get_active_pulse_context", return_value=pulse_ctx):
        result = run_playbook(name="memory_research")
    assert "Sub-line failed" in result
    assert "LLM provider unreachable" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
