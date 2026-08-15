"""W10 (柱8) Spell 監査残の回帰テスト — auto_mode の正直化と realtime spell の gate。

Spell 監査 (2026-07-15) の残 finding 2 点を固定する:

1. auto_mode の正直化: リポジトリ全体で auto_mode=True を渡す箇所が存在せず、
   自律 Pulse でもスペル実行が「ユーザー起動」として下流 (確認ダイアログの
   自動承認 / Playbook の auto フィルタ) に伝わっていた。修正後は
   run_meta_user が pulse_type から導出し、state["_auto_mode"] で子まで運ぶ。
2. realtime spell の SPELL_ENABLED gate: persona の SPELL_ENABLED=false でも
   binding された realtime spell が毎 Pulse 自動実行されていた。修正後は
   state["_spell_enabled"] が False なら実行しない。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import sea.runtime_llm as runtime_llm
from sea.runtime import SEARuntime
from sea.runtime_graph import compile_with_langgraph
from tools.context import get_auto_mode


# ---------------------------------------------------------------------------
# 1. run_meta_user: pulse_type → auto_mode の導出
# ---------------------------------------------------------------------------

def _runtime_and_persona() -> tuple[SEARuntime, SimpleNamespace]:
    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(
        persona_name="p",
        persona_id="pid",
        model="m",
        llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()),
        execution_state={},
    )
    return runtime, persona


@pytest.mark.parametrize(
    "pulse_type,expected_auto_mode",
    [
        ("user", False),
        ("schedule", True),
        ("auto", True),
    ],
)
def test_run_meta_user_derives_auto_mode_from_pulse_type(pulse_type, expected_auto_mode):
    """user Pulse だけが auto_mode=False。schedule / auto は True で渡る。"""
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta/exec", start_node="exec", context_requirements=None)

    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._load_playbook_for = Mock(return_value=playbook)
    runtime._run_playbook = Mock(return_value=[])
    runtime.session_lifecycle = MagicMock()

    runtime.run_meta_user(
        persona, "hello", "b1",
        meta_playbook="meta/exec",
        pulse_type=pulse_type,
    )

    assert runtime._run_playbook.call_count == 1
    kwargs = runtime._run_playbook.call_args.kwargs
    assert kwargs["auto_mode"] is expected_auto_mode
    assert kwargs["pulse_type"] == pulse_type


# ---------------------------------------------------------------------------
# 2. compile_with_langgraph: state["_auto_mode"] の運搬 (単調 OR)
# ---------------------------------------------------------------------------

def _capture_initial_state(monkeypatch) -> dict:
    """compile_playbook を差し替え、graph 実行に渡る initial_state を捕獲する。"""
    captured: dict = {}

    def _fake_compile(*args, **kwargs):
        async def _fake_graph(initial_state, config):
            captured.update(initial_state)
            return initial_state
        return _fake_graph

    monkeypatch.setattr("sea.runtime_graph.compile_playbook", _fake_compile)
    return captured


def _minimal_playbook() -> SimpleNamespace:
    return SimpleNamespace(
        name="pb", start_node="n0", context_requirements=None,
        input_schema=[], output_schema=None,
    )


@pytest.mark.parametrize(
    "auto_mode,parent_auto,expected",
    [
        (True, False, True),    # 根元が auto
        (False, True, True),    # 親 Pulse が auto なら子も auto (単調)
        (False, False, False),  # user のまま
    ],
)
def test_initial_state_carries_auto_mode(monkeypatch, auto_mode, parent_auto, expected):
    captured = _capture_initial_state(monkeypatch)
    runtime = MagicMock()
    persona = SimpleNamespace(execution_state={})

    compile_with_langgraph(
        runtime,
        _minimal_playbook(),
        persona,
        "b1",
        None,
        auto_mode,
        base_messages=[],
        pulse_id="pulse-test",
        parent_state={"_auto_mode": parent_auto},
    )

    assert captured["_auto_mode"] is expected


# ---------------------------------------------------------------------------
# 3. スペル実行が state["_auto_mode"] を persona_context へ渡す
# ---------------------------------------------------------------------------

def test_spell_executor_passes_state_auto_mode_to_persona_context(monkeypatch):
    """自律 Pulse (_auto_mode=True) のスペルから get_auto_mode() が True で見える。

    これが False だと、確認ダイアログ (tools/confirmation.py) が誰も見ていない
    UI へ確認を出してブロックし、auto フィルタ (list_available_playbooks) が
    user 専用 Playbook を自律 Pulse に見せる。
    """
    seen: dict = {}

    def _fake_spell() -> str:
        seen["auto_mode"] = get_auto_mode()
        return "ok"

    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, "w10_fake_spell", _fake_spell)

    persona = SimpleNamespace(persona_id="pid", persona_log_path=None, manager_ref=None)
    state = {"_auto_mode": True, "_persona_obj": persona}

    result_text, _meta, ok = asyncio.run(
        runtime_llm._run_spell_tool_async(
            "w10_fake_spell", {}, persona, state, "pb", None, messages=[],
        )
    )

    assert ok is True
    assert result_text == "ok"
    assert seen["auto_mode"] is True


def test_spell_executor_defaults_to_user_mode_without_flag(monkeypatch):
    """_auto_mode が無い state (レガシー/直接呼び出し) は user 扱いに倒す。"""
    seen: dict = {}

    def _fake_spell() -> str:
        seen["auto_mode"] = get_auto_mode()
        return "ok"

    monkeypatch.setitem(runtime_llm.TOOL_REGISTRY, "w10_fake_spell", _fake_spell)

    persona = SimpleNamespace(persona_id="pid", persona_log_path=None, manager_ref=None)
    state = {"_persona_obj": persona}

    asyncio.run(
        runtime_llm._run_spell_tool_async(
            "w10_fake_spell", {}, persona, state, "pb", None, messages=[],
        )
    )

    assert seen["auto_mode"] is False


# ---------------------------------------------------------------------------
# 4. realtime spell は SPELL_ENABLED=false で実行されない
# ---------------------------------------------------------------------------

class _Boom(Exception):
    """realtime gate 通過直後 (status イベント送出) で node を止める番兵。"""


def _llm_node_state(spell_enabled: bool) -> dict:
    return {
        "_spell_enabled": spell_enabled,
        "_messages": [],
    }


def _run_llm_node_until_status(state: dict, monkeypatch) -> list:
    """lg_llm_node を realtime ブロック直後の status イベントまで走らせる。

    event_callback が _Boom を投げて LLM 呼び出し前に必ず停止するので、
    realtime spell の実行有無だけを観測できる。
    """
    calls: list = []

    async def _recorder(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(runtime_llm, "_execute_realtime_spells", _recorder)

    def _raising_callback(event):
        raise _Boom()

    # persona_id=None で node_with_persona_context の wrap を素通しし、
    # persona_context 依存なしで node 本体だけを走らせる。
    persona = SimpleNamespace(persona_id=None, persona_name="p")
    playbook = SimpleNamespace(name="pb")
    node_def = SimpleNamespace(id="llm")
    node = runtime_llm.lg_llm_node(
        MagicMock(), node_def, persona, "b1", playbook, _raising_callback,
    )

    with pytest.raises(_Boom):
        asyncio.run(node(state))
    return calls


def test_realtime_spells_skipped_when_spell_disabled(monkeypatch):
    state = _llm_node_state(spell_enabled=False)
    calls = _run_llm_node_until_status(state, monkeypatch)

    assert calls == []
    assert "_realtime_spells_executed" not in state


def test_realtime_spells_run_when_spell_enabled(monkeypatch):
    state = _llm_node_state(spell_enabled=True)
    calls = _run_llm_node_until_status(state, monkeypatch)

    assert len(calls) == 1
    assert state["_realtime_spells_executed"] is True
