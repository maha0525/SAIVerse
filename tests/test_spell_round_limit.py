"""スペル連続実行回数の上限・グローバル設定・ペルソナへの提示の回帰テスト。

docs/issues/spell_round_limit_redesign.md:
- 既定上限は 5 回 (SAIVERSE_SPELL_MAX_ROUNDS 未設定時)
- 環境変数 / グローバル設定 API (/api/config/spell-max-rounds) で再起動なしに変更可能
- 各ラウンドのスペル結果に残り実行回数（最終ラウンドは次応答で不発となる予告）を添える
- /quick_spell の正常終端では残り回数注釈を添えない
- それでも上限超過して最終応答にスペルが含まれた場合:
  - final_continuation 本体は生のまま保つ
  - 締めの BeatSegment には <user_only> の失敗ブロックを載せる
  - _finalize_beat が締め発言 (assistant) の直後に [Spell Limit] 通知 (system) を記録する
"""
import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple
from unittest.mock import patch

from api.routes.config import (
    SpellMaxRoundsRequest,
    get_spell_max_rounds_setting,
    set_spell_max_rounds_setting,
)
from sea import runtime_llm


SPELL_NAME = "note_add"


class _FakeRuntime:
    def __init__(self):
        self.stored: List[Tuple[str, str]] = []  # (role, text)
        self.manager = SimpleNamespace()
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def _effective_building_id(self, persona, fallback):
        return fallback

    def _store_memory(self, persona, text, **kwargs):
        role = kwargs.get("role", "assistant")
        self.stored.append((role, text))
        return f"msg-{len(self.stored)}" if kwargs.get("return_message_id") else True

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, *args, **kwargs):
        return None

    def _accumulate_usage(self, *args, **kwargs):
        return None


class _ScriptedClient:
    def __init__(self, responses: List[str]):
        self.responses = list(responses)
        self.calls: List[list] = []

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("_ScriptedClient: no scripted responses left")
        return self.responses.pop(0)

    def consume_usage(self):
        return None

    def consume_reasoning(self):
        return []

    def consume_reasoning_details(self):
        return None


def _make_fake_spell(calls: List[Tuple[str, dict]]):
    async def _fake(name: str, args: dict, *a: Any, **kw: Any):
        calls.append((name, args))
        return f"ok:{args.get('step', '')}", {}, True

    return _fake


def test_get_spell_max_rounds_default_and_clamping(monkeypatch):
    monkeypatch.delenv("SAIVERSE_SPELL_MAX_ROUNDS", raising=False)
    assert runtime_llm.get_spell_max_rounds() == 5

    monkeypatch.setenv("SAIVERSE_SPELL_MAX_ROUNDS", "10")
    assert runtime_llm.get_spell_max_rounds() == 10

    monkeypatch.setenv("SAIVERSE_SPELL_MAX_ROUNDS", "0")
    assert runtime_llm.get_spell_max_rounds() == runtime_llm.SPELL_MAX_ROUNDS_MIN

    monkeypatch.setenv("SAIVERSE_SPELL_MAX_ROUNDS", "999")
    assert runtime_llm.get_spell_max_rounds() == runtime_llm.SPELL_MAX_ROUNDS_MAX

    monkeypatch.setenv("SAIVERSE_SPELL_MAX_ROUNDS", "invalid")
    assert runtime_llm.get_spell_max_rounds() == runtime_llm.SPELL_MAX_ROUNDS_DEFAULT


def test_spell_round_budget_notes_and_final_warning(monkeypatch):
    monkeypatch.setenv("SAIVERSE_SPELL_MAX_ROUNDS", "3")
    calls: List[Tuple[str, dict]] = []
    client = _ScriptedClient([
        "/spell name='note_add' args={'step': 2}",
        "/spell name='note_add' args={'step': 3}",
        "全部終わったよ！",
    ])
    runtime = _FakeRuntime()
    persona = SimpleNamespace(persona_id="p1")
    state = {"_pulse_id": "pulse-1", "_pulse_context": None, "_cancellation_token": None}
    node_def = SimpleNamespace(memorize=True, speak=False)
    playbook = SimpleNamespace(name="test_playbook")
    messages: List[dict] = []

    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=_make_fake_spell(calls)):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text="/spell name='note_add' args={'step': 1}",
            spell_enabled=True,
            llm_client=client,
            runtime=runtime,
            persona=persona,
            building_id="b1",
            state=state,
            messages=messages,
            playbook=playbook,
            event_callback=None,
            node_def=node_def,
        ))

    assert result.loop_count == 3
    assert result.final_continuation == "全部終わったよ！"
    assert result.limit_exceeded_notice is None
    assert "_spell_limit_exceeded_notice" not in state
    assert len(calls) == 3

    system_results = [text for role, text in runtime.stored if role == "system"]
    assert len(system_results) == 3
    assert "あと 2 回（上限 3 回）" in system_results[0]
    assert "あと 1 回（上限 3 回）" in system_results[1]
    assert "上限（3 回）に達しました" in system_results[2]
    assert "次の応答ではスペルを実行できません" in system_results[2]


def test_quick_spell_terminal_omits_round_budget_note(monkeypatch):
    monkeypatch.setenv("SAIVERSE_SPELL_MAX_ROUNDS", "5")
    calls: List[Tuple[str, dict]] = []
    client = _ScriptedClient([])
    runtime = _FakeRuntime()
    persona = SimpleNamespace(persona_id="p1")
    state = {"_pulse_id": "pulse-1", "_pulse_context": None, "_cancellation_token": None}
    node_def = SimpleNamespace(memorize=True, speak=False)
    playbook = SimpleNamespace(name="test_playbook")

    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=_make_fake_spell(calls)):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text="/quick_spell name='note_add' args={'step': 1}",
            spell_enabled=True,
            llm_client=client,
            runtime=runtime,
            persona=persona,
            building_id="b1",
            state=state,
            messages=[],
            playbook=playbook,
            event_callback=None,
            node_def=node_def,
        ))

    assert result.loop_count == 1
    assert result.final_continuation == ""
    system_results = [text for role, text in runtime.stored if role == "system"]
    assert len(system_results) == 1
    assert "[Spell Rounds]" not in system_results[0]


def test_spell_limit_exceeded_formats_segment_and_records_notice_after_closing(monkeypatch):
    monkeypatch.setenv("SAIVERSE_SPELL_MAX_ROUNDS", "2")
    calls: List[Tuple[str, dict]] = []
    closing_raw = (
        "手帳に書くね！\n"
        "/spell name='note_add' args={'step': 3}\n"
        "それから部屋にも戻るよ。\n"
        "/spell name='note_add' args={'step': 4}\n"
        "お待たせ！"
    )
    client = _ScriptedClient([
        "/spell name='note_add' args={'step': 2}",
        closing_raw,
    ])
    runtime = _FakeRuntime()
    persona = SimpleNamespace(persona_id="p1")
    state: dict = {"_pulse_id": "pulse-1", "_pulse_context": None, "_cancellation_token": None}
    node_def = SimpleNamespace(id="llm_1", memorize=True, speak=False, important=False)
    playbook = SimpleNamespace(name="test_playbook", display_name="test_playbook")
    messages: List[dict] = []

    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=_make_fake_spell(calls)):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text="/spell name='note_add' args={'step': 1}",
            spell_enabled=True,
            llm_client=client,
            runtime=runtime,
            persona=persona,
            building_id="b1",
            state=state,
            messages=messages,
            playbook=playbook,
            event_callback=None,
            node_def=node_def,
        ))

    # 3 回目は実行されず、final_continuation は生のまま保たれる
    assert result.loop_count == 2
    assert len(calls) == 2
    assert result.final_continuation == closing_raw
    assert result.limit_exceeded_notice is not None
    assert "[Spell Limit]" in result.limit_exceeded_notice
    assert "上限（2 回）" in result.limit_exceeded_notice

    # チャット表示用の締め BeatSegment には失敗ブロックが入り、前・間・後ろの本文も全て保持される
    closing_seg_text = result.segments[-1].text
    assert "手帳に書くね！" in closing_seg_text
    assert "それから部屋にも戻るよ。" in closing_seg_text
    assert "お待たせ！" in closing_seg_text
    assert closing_seg_text.count("spellResultError") == 2
    assert "上限（2 回）に達したため、実行されませんでした。" in closing_seg_text
    assert (
        closing_seg_text.index("手帳に書くね！")
        < closing_seg_text.index("それから部屋にも戻るよ。")
        < closing_seg_text.index("お待たせ！")
    )

    # _finalize_beat を通すと、締め発言 (assistant) の直後にシステム通知が保存される
    beat = runtime_llm.BeatExecution.from_node_locals(
        persona=persona,
        node_def=node_def,
        node_id="llm_1",
        playbook=playbook,
        state=state,
        event_callback=None,
        messages=messages,
        prompt="",
        continuation=result.final_continuation,
        schema_consumed=False,
    )
    runtime_llm._finalize_beat(runtime, beat)

    assert "_spell_limit_exceeded_notice" not in state
    assert runtime.stored[-2] == ("assistant", closing_raw)
    assert runtime.stored[-1][0] == "system"
    assert "[Spell Limit]" in runtime.stored[-1][1]
    assert state["_messages"][-1]["role"] == "system"
    assert "[Spell Limit]" in state["_messages"][-1]["content"]


def test_spell_max_rounds_config_endpoints(monkeypatch):
    monkeypatch.delenv("SAIVERSE_SPELL_MAX_ROUNDS", raising=False)
    initial = get_spell_max_rounds_setting()
    assert initial == {
        "rounds": 5,
        "default": 5,
        "min": 1,
        "max": 20,
    }

    written: dict = {}

    def _fake_write_env_updates(updates: dict):
        written.update(updates)
        for k, v in updates.items():
            monkeypatch.setenv(k, v)

    with patch("api.routes.admin.write_env_updates", side_effect=_fake_write_env_updates):
        res = set_spell_max_rounds_setting(SpellMaxRoundsRequest(rounds=8))
        assert res == {"success": True, "rounds": 8}
        assert written["SAIVERSE_SPELL_MAX_ROUNDS"] == "8"
        assert get_spell_max_rounds_setting()["rounds"] == 8

        # 範囲外は丸められる
        res_clamped = set_spell_max_rounds_setting(SpellMaxRoundsRequest(rounds=99))
        assert res_clamped == {"success": True, "rounds": 20}
        assert get_spell_max_rounds_setting()["rounds"] == 20
