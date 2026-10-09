"""From supplied syntax through feedback, correction and tool execution."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sea import runtime_llm
from sea.head_pipeline import LineHeadInput
from sea.head_pipeline.sections.common_prompt import CommonPromptSection
from sea.head_pipeline.sections.spell_list import (
    SpellEntry,
    SpellListSection,
    SpellListSnapshot,
)
from sea.head_pipeline.types import SnapshotStaleError
from test_quick_spell import ScriptedClient, SpellLoopRuntime

NAME = "fixture-addon__gesture"
GOOD = f"/spell name='{NAME}' args={{\"intent\": \"friendly_wave\"}}"


@pytest.mark.parametrize("verb", ["spell", "quick_spell"])
def test_hyphen_shorthand_unwraps_the_complete_args_object(verb):
    text = f'/{verb} {NAME} args={{"intent": "friendly_wave", "nested": {{"x": 1}}}}'
    malformed = []
    parsed = runtime_llm._parse_spell_lines(text, malformed_out=malformed)
    assert malformed == []
    assert len(parsed) == 1
    assert parsed[0].args == {"intent": "friendly_wave", "nested": {"x": 1}}
    assert parsed[0].quick == (verb == "quick_spell")
    assert parsed[0].norm.startswith(f"/{verb} name='{NAME}' args=")


@pytest.mark.parametrize("text", [
    "/spell",
    f"/spell name='{NAME}'",
    f'/spell name="{NAME}" args={{}}',
    f"/spell name={NAME} args={{}}",
    f"  /spell name='{NAME}' args={{}}",
    f"/spell {NAME} args={{bad",
    f"/spell {NAME} intent='unterminated",
    f"/spell {NAME} ignored intent='wave'",
    f"/spell {NAME} intent='wave' ignored",
    f"/quick_spell name='{NAME}'",
    f"/spell(name='{NAME}')",
    "/quick_spell()",
])
def test_each_unreadable_invocation_has_one_failure(text):
    malformed = []
    assert runtime_llm._parse_spell_lines(text, malformed_out=malformed) == []
    assert len(malformed) == 1
    assert text[malformed[0][2].start():malformed[0][2].end()] == text


@pytest.mark.parametrize("text", [
    "普通の発言です。", "/spelling is an ordinary word", "説明中の /spell はコマンドではない",
])
def test_ordinary_speech_is_not_an_invocation(text):
    malformed = []
    assert runtime_llm._parse_spell_lines(text, malformed_out=malformed) == []
    assert malformed == []


def test_malformed_unquoted_name_keeps_the_intended_name_in_the_hint():
    malformed = []
    runtime_llm._parse_spell_lines(f"/spell name={NAME} args={{}}", malformed_out=malformed)
    assert malformed[0][0] == NAME


def _entry():
    return SpellEntry(
        name=NAME, display_name="合成ジェスチャー", description="テスト用",
        parameters_json=json.dumps({
            "type": "object", "required": ["intent"],
            "properties": {"intent": {"type": "string", "enum": ["friendly_wave"]}},
        }), addon_key="fixture-addon", visible=True,
    )


def _render_examples():
    section = SpellListSection()
    old = SpellListSnapshot(enabled=True, entries=(), addon_manifests=(), registered_names=frozenset())
    new = SpellListSnapshot(enabled=True, entries=(_entry(),), addon_manifests=(), registered_names=frozenset({NAME}))
    head = section.render(new).text
    notification = section.diff_to_notifications(old, new)[0].label
    marker = "呼び出し例（引数の値は用途に合わせて変更）: "
    examples = []
    for rendered in (head, notification):
        examples.append(next(line.split(marker, 1)[1] for line in rendered.splitlines() if marker in line))
    assert examples[0] == examples[1]
    return examples


def test_common_uri_example_is_canonical_and_preserves_array_args():
    repo = Path(runtime_llm.__file__).resolve().parent.parent
    common = (repo / "builtin_data/prompts/common.txt").read_text(encoding="utf-8")
    lines = [line for line in common.splitlines() if line.startswith("/spell")]
    assert lines
    for line in lines:
        assert line.startswith("/spell name='")
        parsed = runtime_llm._parse_spell_lines(line)
        assert len(parsed) == 1
        assert parsed[0].name == "resolve_uri"
        assert parsed[0].args == {"uris": ["saiverse://self/message/msg/recent?depth=5"]}


def test_saved_common_prompt_requests_recapture_after_the_template_update(tmp_path, monkeypatch):
    repo = Path(runtime_llm.__file__).resolve().parent.parent
    template = (repo / "builtin_data/prompts/common.txt").read_text(encoding="utf-8")
    canonical = "/spell name='resolve_uri' args={'uris': ['saiverse://self/message/msg/recent?depth=5']}"
    bare = "/spell resolve_uri uris=['saiverse://self/message/msg/recent?depth=5']"
    old = template.replace(canonical, bare)
    path = tmp_path / "common.txt"
    path.write_text(old, encoding="utf-8")
    monkeypatch.setattr("saiverse.data_paths.find_file", lambda subdir, filename: path)
    monkeypatch.setattr("sea.head_pipeline.spell_gate.resolve_spell_enabled", lambda ctx: True)
    persona = SimpleNamespace(common_prompt=old, persona_id="synthetic_only",
                              persona_name="合成", linked_user_name="合成利用者")
    ctx = LineHeadInput(persona_id="synthetic_only", persona=persona, manager=SimpleNamespace(),
                        line_id="main", line_role="main_line", model_key="fake-model",
                        current_building_id="synthetic_building")
    section = CommonPromptSection()
    saved = section.serialize_snapshot(section.capture(ctx))
    path.write_text(template, encoding="utf-8")
    with pytest.raises(SnapshotStaleError):
        section.deserialize_snapshot(saved)
    persona.common_prompt = template
    new_text = section.render(section.capture(ctx)).text
    assert canonical in new_text
    assert bare not in new_text


def test_saved_spell_list_gets_current_examples_without_changing_its_entries(monkeypatch):
    monkeypatch.setattr("tools.SPELL_TOOL_SCHEMAS", {NAME: {}})
    section = SpellListSection()
    snapshot = SpellListSnapshot(enabled=True, entries=(_entry(),), addon_manifests=(),
                                 registered_names=frozenset({NAME}))
    restored = section.deserialize_snapshot(section.serialize_snapshot(snapshot))
    assert restored.entries == snapshot.entries
    assert GOOD in section.render(restored).text


def test_hidden_spell_help_is_also_shown_in_canonical_form():
    section = SpellListSection()
    # SpellEntry is a frozen dataclass; create the hidden view without mutating it.
    hidden = replace(_entry(), visible=False)
    snapshot = SpellListSnapshot(enabled=True, entries=(hidden,), addon_manifests=())
    rendered = section.render(snapshot).text
    command = next(part for part in rendered.split("`") if part.startswith("/spell"))
    parsed = runtime_llm._parse_spell_lines(command)
    assert parsed[0].name == "addon_spell_help"
    assert parsed[0].args == {"addon": "fixture-addon"}


@pytest.mark.parametrize("origin", ["head", "notification"])
def test_supplied_qualified_example_reaches_the_tool(origin):
    text = _render_examples()[origin == "notification"]
    result, runtime, client, calls, messages, state = _loop(text, ["完了"])
    assert result.loop_count == 1
    assert calls == [(NAME, {"intent": "friendly_wave"})]
    assert len(client.calls) == 1
    assert not any("[Spell Error:" in m.get("content", "") for m in messages)


def _loop(text, responses):
    runtime = SpellLoopRuntime()
    client = ScriptedClient(responses)
    calls = []
    messages = []
    state = {"_pulse_id": "synthetic-pulse", "_pulse_context": None,
             "_cancellation_token": None, "_activity_trace": []}

    async def fake_tool(name, args, *positional, **kwargs):
        calls.append((name, args))
        return "合成ツール成功", None, True

    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {NAME}), \
         patch.dict(runtime_llm.TOOL_REGISTRY, {}, clear=True), \
         patch.object(runtime_llm, "_list_router_callable_playbook_names", return_value=[]), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=fake_tool):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text=text, spell_enabled=True, llm_client=client, runtime=runtime,
            persona=SimpleNamespace(persona_id="synthetic_only"), building_id="synthetic_building",
            state=state, messages=messages, playbook=SimpleNamespace(name="synthetic_playbook"),
            event_callback=None, node_def=SimpleNamespace(memorize=None, speak=False),
        ))
    return result, runtime, client, calls, messages, state


@pytest.mark.parametrize("bad", [
    "/spell fixture-addon__missing args={}",
    f"/spell name='{NAME}'",
    f"/spell {NAME} args={{bad",
    f"/quick_spell name='{NAME}'",
    "/spell",
])
def test_failure_reaches_ui_and_persona_then_correction_runs_once(bad, caplog):
    result, runtime, client, calls, messages, state = _loop(bad, [GOOD, "訂正後の応答"])
    assert result.loop_count == 2
    assert len(client.calls) == 2
    assert calls == [(NAME, {"intent": "friendly_wave"})]
    assert "[Spell Error:" in client.calls[0][-1]["content"]
    visible = "\n".join(segment.text for segment in result.segments)
    assert "spellResultError" in visible
    assert any("[Spell Error:" in text for text in runtime.stored)
    assert state["_activity_trace"][0]["success"] is False
    assert state["_activity_trace"][1]["success"] is True
    assert "returning error" in caplog.text
    if "name='" in bad and "args=" not in bad:
        assert "スペル行の書式" in client.calls[0][-1]["content"]
    if bad.startswith("/quick_spell"):
        assert "/quick_spell で唱えたスペルに失敗" in client.calls[0][-1]["content"]


def test_unreadable_line_does_not_leak_into_early_speech():
    text = f"先に話す。\n/spell name='{NAME}'\n続き。"
    assert runtime_llm._extract_first_text_before(text) == "先に話す。"


def test_multiline_args_containing_unreadable_spell_text_are_not_executed():
    text = f'/spell name=\'{NAME}\' args={{"intent": "例\n/spell\n終わり"}}'
    malformed = []
    parsed = runtime_llm._parse_spell_lines(text, malformed_out=malformed)
    assert len(parsed) == 1
    assert parsed[0].args["intent"] == "例\n/spell\n終わり"
    assert malformed == []
