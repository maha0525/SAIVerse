"""/quick_spell — スペルループ終端宣言の回帰 (docs/intent/quick_spell.md)。

契約の核: 成功したら手放せる、失敗したら起こされる。
- 全行 quick + 全成功 → LLM 再呼び出しなしでループ終端 (final_continuation="")
- 混在 (通常 /spell が 1 行でも) → 従来どおり再呼び出し、quick の結果も同じ注入に含む
- 失敗 (機械的 ok=False / metadata "error": true / unknown) → 説明文付き注入で昇格
- 説明文は注入のみで SAIMemory へ永続化しない (不変条件 5)
"""
import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import patch

import pytest

from sea import runtime_llm

SPELL_NAME = "note_add"


class SpellLoopRuntime:
    """_run_spell_loop が触る SEARuntime の最小フェイク (test_beat_gate と同型)。"""

    def __init__(self):
        self.stored: List[str] = []
        self.manager = SimpleNamespace()
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def _store_memory(self, persona, text, **kwargs):
        self.stored.append(text)
        return "msg-1" if kwargs.get("return_message_id") else True

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, *args, **kwargs):
        return None

    def _accumulate_usage(self, *args, **kwargs):
        return None


class ScriptedClient:
    """スクリプト化された retry 応答を順に返す mock LLM クライアント。"""

    def __init__(self, responses: List[str]):
        self.responses = list(responses)
        self.calls: List[list] = []

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("ScriptedClient: no scripted responses left")
        return self.responses.pop(0)

    def consume_usage(self):
        return None


def _run_loop(text: str, client: ScriptedClient, fake_spell, messages: Optional[list] = None):
    runtime = SpellLoopRuntime()
    persona = SimpleNamespace(persona_id="p1")
    state = {"_pulse_id": "pulse-1", "_pulse_context": None, "_cancellation_token": None}
    node_def = SimpleNamespace(memorize=None, speak=False)
    playbook = SimpleNamespace(name="test_playbook")
    msgs = messages if messages is not None else []
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=fake_spell):
        merged, continuation, rounds = asyncio.run(runtime_llm._run_spell_loop(
            text=text,
            spell_enabled=True,
            llm_client=client,
            runtime=runtime,
            persona=persona,
            building_id="b1",
            state=state,
            messages=msgs,
            playbook=playbook,
            event_callback=None,
            node_def=node_def,
        ))
    return merged, continuation, rounds, runtime, msgs


def _ok_spell(result: str = "記録しました", meta: Optional[dict] = None):
    async def fake(tool_name, tool_args, persona, state, playbook_name,
                   event_callback, messages=None):
        return (result, meta, True)
    return fake


# ---------------------------------------------------------------------------
# パーサ (quick フラグの伝搬)
# ---------------------------------------------------------------------------


def test_parse_quick_canonical():
    parsed = runtime_llm._parse_spell_lines(
        "/quick_spell name='note_add' args={\"text\": \"メモ\"}"
    )
    assert len(parsed) == 1
    assert parsed[0].quick is True
    # 正規化行は /quick_spell 動詞を保つ (本人の宣言を書き換えない)
    assert parsed[0].norm.startswith("/quick_spell name='note_add'")


def test_parse_normal_spell_is_not_quick():
    parsed = runtime_llm._parse_spell_lines(
        "/spell name='note_add' args={\"text\": \"メモ\"}"
    )
    assert len(parsed) == 1
    assert parsed[0].quick is False
    assert parsed[0].norm.startswith("/spell name=")


def test_parse_quick_fuzzy_form():
    parsed = runtime_llm._parse_spell_lines("/quick_spell note_add text='メモ'")
    assert len(parsed) == 1
    assert parsed[0].quick is True
    assert parsed[0].name == "note_add"


def test_parse_quick_multiline_rescue_keeps_quick():
    text = '/quick_spell name=\'note_add\' args={"text": "1行目\n2行目"}'
    parsed = runtime_llm._parse_spell_lines(text)
    assert len(parsed) == 1
    assert parsed[0].quick is True
    assert parsed[0].args["text"] == "1行目\n2行目"


# ---------------------------------------------------------------------------
# 終端 (全 quick + 全成功)
# ---------------------------------------------------------------------------


def test_all_quick_success_terminates_without_llm_call():
    client = ScriptedClient([])  # 呼ばれたら AssertionError
    merged, continuation, rounds, runtime, msgs = _run_loop(
        "メモしておこう。\n/quick_spell name='note_add' args={\"text\": \"メモ\"}",
        client, _ok_spell(),
    )
    assert rounds == 1
    assert client.calls == []          # LLM 再呼び出しなし
    assert continuation == ""          # 空応答 = 正規終端
    # 発話本文と結果ブロックは merged (UI/建物履歴) に残る
    assert "メモしておこう。" in merged
    assert "記録しました" in merged
    # SAIMemory には発話 (assistant) と結果 (system) が記録されている
    assert any("quick_spell name='note_add'" in s for s in runtime.stored)
    assert any("記録しました" in s for s in runtime.stored)


def test_mixed_round_reinvokes_and_includes_quick_result():
    client = ScriptedClient(["結果を見て続けます。"])
    text = (
        "両方やる。\n"
        "/quick_spell name='note_add' args={\"text\": \"帳簿\"}\n"
        "/spell name='note_add' args={\"text\": \"検索\"}"
    )
    merged, continuation, rounds, runtime, msgs = _run_loop(text, client, _ok_spell())
    assert rounds == 1
    assert len(client.calls) == 1      # 従来どおり再呼び出し
    assert continuation == "結果を見て続けます。"
    # quick の結果も同じ注入メッセージに含まれる (不変条件 7)
    injected = [m for m in msgs if m.get("role") == "user" and "[Spell Result" in m.get("content", "")]
    assert len(injected) == 1


# ---------------------------------------------------------------------------
# 昇格 (失敗を沈黙で飲み込まない — 不変条件 2)
# ---------------------------------------------------------------------------

_ESCALATION_NOTE = "/quick_spell で唱えたスペルに失敗があったため"


def _assert_escalated(client, runtime, msgs):
    assert len(client.calls) == 1
    injected = [m for m in msgs if _ESCALATION_NOTE in m.get("content", "")]
    assert len(injected) == 1, "昇格説明文が注入されている"
    # 説明文は注入のみ — SAIMemory へ記録される combined_results には混ぜない
    assert not any(_ESCALATION_NOTE in s for s in runtime.stored)


def test_quick_mechanical_failure_escalates():
    async def failing(tool_name, tool_args, persona, state, playbook_name,
                      event_callback, messages=None):
        return ("Spell error (note_add): RuntimeError: boom", None, False)

    client = ScriptedClient(["失敗を確認しました。"])
    merged, continuation, rounds, runtime, msgs = _run_loop(
        "/quick_spell name='note_add' args={}", client, failing,
    )
    assert continuation == "失敗を確認しました。"
    _assert_escalated(client, runtime, msgs)
    # 機械的失敗は [Spell Error] ラベル (Phase 1)
    assert any("[Spell Error: note_add]" in s for s in runtime.stored)


def test_quick_metadata_error_escalates():
    """論理的失敗: ツールが metadata {"error": true} で宣言するケース (§3.3)。"""
    client = ScriptedClient(["見つからなかったようです。"])
    merged, continuation, rounds, runtime, msgs = _run_loop(
        "/quick_spell name='note_add' args={}",
        client, _ok_spell("該当するメモが見つかりませんでした", {"error": True}),
    )
    assert continuation == "見つからなかったようです。"
    _assert_escalated(client, runtime, msgs)


def test_quick_unknown_spell_escalates():
    """未登録スペルを quick で唱えても沈黙せず、エラーを見せて再試行機会を返す。"""
    client = ScriptedClient(["書式を直します。"])
    merged, continuation, rounds, runtime, msgs = _run_loop(
        "/quick_spell name='no_such_spell' args={}", client, _ok_spell(),
    )
    assert continuation == "書式を直します。"
    _assert_escalated(client, runtime, msgs)


# ---------------------------------------------------------------------------
# 回帰: quick 不使用時の従来挙動
# ---------------------------------------------------------------------------


def test_normal_spell_round_unchanged():
    client = ScriptedClient(["続けて話します。"])
    merged, continuation, rounds, runtime, msgs = _run_loop(
        "/spell name='note_add' args={}", client, _ok_spell(),
    )
    assert rounds == 1
    assert len(client.calls) == 1
    assert continuation == "続けて話します。"
    assert not any(_ESCALATION_NOTE in m.get("content", "") for m in msgs)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
