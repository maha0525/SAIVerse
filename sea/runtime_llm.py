from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_clients.exceptions import LLMError
from sea.runtime_utils import _format, _is_llm_streaming_enabled
from saiverse.logging_config import log_sea_trace
from sea.playbook_models import PlaybookSchema
from saiverse.usage_tracker import get_usage_tracker
# Module-level imports for tools registry symbols.
#
# Rationale: we previously lazy-imported these inside functions, which worked
# for the first call (sys.modules cache hit) but broke when certain addons
# (notably saiverse-voice-tts via its GPT-SoVITS loader) temporarily remove
# the ``tools`` package and its submodules from sys.modules. A parallel LLM
# thread hitting a lazy ``from tools import ...`` / ``from tools.context
# import ...`` during that window resolved to the wrong ``tools`` package
# elsewhere on sys.path (GPT-SoVITS's own tools/) — or to ModuleNotFoundError
# for submodules like ``tools.context`` that don't exist there at all. By
# binding these names at module import time, we freeze the references to
# the real SAIVerse ``tools`` package regardless of later sys.modules
# manipulation. See memory/project_tts_import_pollution.md.
from tools import SPELL_TOOL_NAMES, SPELL_TOOL_SCHEMAS, TOOL_REGISTRY
from tools.core import parse_tool_result
from tools.context import persona_context

LOGGER = logging.getLogger(__name__)

# ── Spell system (text-based tool invocation) ──

_MAX_SPELL_LOOPS = int(os.getenv("SAIVERSE_SPELL_MAX_ROUNDS", "3"))

# Canonical form: /spell name='tool' args={...}
_SPELL_PATTERN = re.compile(
    r"^/spell\s+name='([^']+)'\s+args=(.+)$",
    re.MULTILINE,
)
# Args-omitted form (no explicit ``args=``): ``/spell name='X'``
# Indicates "execute this Spell, but the args are not yet known". Used by the
# pre_spells dynamic-args path: schedule_manager / inject_persona_event etc.
# enqueue this form when the user (or schedule definition) only specified the
# Spell name; _execute_pre_spells then routes through ``spell_args_decider``
# Playbook to fill in the args at runtime.
_SPELL_PATTERN_NO_ARGS = re.compile(
    r"^/spell\s+name='([^']+)'\s*$",
    re.MULTILINE,
)
# Fuzzy form: /spell tool_name key='value' key2='value2' ...
_SPELL_PATTERN_FUZZY = re.compile(
    r"^/spell\s+(\w+)\s+(.+)$",
    re.MULTILINE,
)
# key=value pair within fuzzy args (value may be single/double-quoted, dict literal, or bare word)
_KV_PATTERN = re.compile(
    r"(\w+)="
    r"(?:'([^']*)'|\"([^\"]*)\"|(\{[^}]*\})|([\w\-./]+))"
)

# Pipeline Streaming (Phase 2-C, voice_tts_pipeline_streaming intent doc):
# 句読点 + 改行を sub-text の文区切りとして扱う。 GPT-SoVITS の cut5 と同様の
# 基準。 「、」 「,」 で切ると短すぎる文があるが、 voice-tts 側で同 message_id
# の audio_stream に連結合成されるので OK (= 各 sub-text は独立に合成され、
# stream に push される)。
_SENTENCE_BOUNDARY_CHARS = set("。！？．!?\n")
# 弱い区切り (= 早く流したい時に追加で見る境界)。 強い区切りより優先度低い。
_SENTENCE_BOUNDARY_SOFT_CHARS = set("、，,;:")
# sub-text の中身が 「区切り文字 + 空白だけ」 のとき voice-tts (GPT-SoVITS) は
# 「有効なテキストを入力してください」 で合成失敗する。 sub-speak emit 時に
# 1 文字でも区切り・空白以外を含むかチェックして skip するために、 voice-able
# でない文字集合を定義する。
_SUB_TEXT_NON_VOICEABLE_CHARS = (
    _SENTENCE_BOUNDARY_CHARS | _SENTENCE_BOUNDARY_SOFT_CHARS | set(" 　\t\r")
)


def _has_voiceable_content(text: str) -> bool:
    """``text`` に句読点 + 空白以外の文字が 1 つでも含まれるかを返す。
    voice-tts に渡しても合成可能な実体を持つかの判定に使う。
    """
    for ch in text:
        if ch not in _SUB_TEXT_NON_VOICEABLE_CHARS:
            return True
    return False


def _find_next_sentence_boundary(
    buffer: str, start: int, use_soft: bool = True,
) -> int:
    """``buffer[start:]`` の中で最初に出てくる文区切り文字の **次** の index
    を返す。 見つからなければ -1。

    返り値は 「sub-text として切り出してよい範囲の終端 (exclusive)」 として
    使う。 つまり ``buffer[start:returned_idx]`` が境界文字を含む 1 sub-text。

    ``use_soft`` で 「、 ，」 等の弱い区切りも文区切りに含めるか制御する。
    Pipeline Streaming では低遅延を優先するので default True (= 短い句でも
    voice-tts に流す)。
    """
    if start >= len(buffer):
        return -1
    chars = (
        _SENTENCE_BOUNDARY_CHARS | _SENTENCE_BOUNDARY_SOFT_CHARS
        if use_soft else _SENTENCE_BOUNDARY_CHARS
    )
    for i in range(start, len(buffer)):
        if buffer[i] in chars:
            return i + 1
    return -1




def _resolve_response_schema_source(
    source: str, variables: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Resolve a response_schema_source string into a JSON schema dict.

    Supported forms:
    - ``spell:<spell_name>`` — loads ``SPELL_TOOL_SCHEMAS[<spell_name>].parameters``
      (the Spell's input JSON Schema). Used by spell_args_decider Playbook to
      drive structured output for dynamic Spell argument generation.
    - ``arg:<key>`` — reads ``variables[<key>]`` and uses it directly as the schema.
      The value should be a JSON Schema dict (or a JSON string that parses to one).
      Used by callers that need to inject *dynamic* enum lists or other
      situation-specific schema details that cannot be expressed statically in
      the Playbook JSON. Example: meta_judgment v2 builds the schema in
      Python with the current alert / pending track IDs as enum values, then
      passes it via Playbook input args.

    Returns None if the source cannot be resolved.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    if source.startswith("spell:"):
        spell_name = source[len("spell:"):].strip()
        if not spell_name:
            return None
        schema = SPELL_TOOL_SCHEMAS.get(spell_name)
        if schema is None:
            LOGGER.warning(
                "[sea][llm] response_schema_source 'spell:%s' references unknown spell",
                spell_name,
            )
            return None
        params = getattr(schema, "parameters", None)
        if not isinstance(params, dict):
            LOGGER.warning(
                "[sea][llm] Spell '%s' has no usable parameters schema (got %r)",
                spell_name, type(params).__name__,
            )
            return None
        return params
    if source.startswith("arg:"):
        key = source[len("arg:"):].strip()
        if not key:
            LOGGER.warning("[sea][llm] response_schema_source 'arg:' is missing key")
            return None
        if not isinstance(variables, dict):
            LOGGER.warning(
                "[sea][llm] response_schema_source 'arg:%s' but no variables provided",
                key,
            )
            return None
        value = variables.get(key)
        if value is None:
            LOGGER.warning(
                "[sea][llm] response_schema_source 'arg:%s' resolved to None",
                key,
            )
            return None
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                LOGGER.warning(
                    "[sea][llm] response_schema_source 'arg:%s' value is a string but not valid JSON",
                    key,
                )
                return None
            if isinstance(parsed, dict):
                return parsed
            LOGGER.warning(
                "[sea][llm] response_schema_source 'arg:%s' parsed JSON is not an object",
                key,
            )
            return None
        LOGGER.warning(
            "[sea][llm] response_schema_source 'arg:%s' has unexpected type %r",
            key, type(value).__name__,
        )
        return None
    LOGGER.warning("[sea][llm] Unrecognized response_schema_source form: %r", source)
    return None


def _parse_spell_args(args_raw: str, *, silent: bool = False) -> Optional[dict]:
    """Parse spell args string (Python dict or JSON). Returns dict or None.

    When ``silent=True``, parse failures are downgraded to DEBUG (used by the
    fuzzy parser which routinely tries this strict path first and recovers via
    the KV pattern). Without ``silent``, failures are logged at WARNING since
    they indicate the LLM produced a canonical-form spell with malformed args.
    """
    try:
        result = ast.literal_eval(args_raw)
    except (ValueError, SyntaxError):
        try:
            result = json.loads(args_raw)
        except json.JSONDecodeError:
            (LOGGER.debug if silent else LOGGER.warning)(
                "[sea][spell] Failed to parse args: %s", args_raw
            )
            return None
    if not isinstance(result, dict):
        (LOGGER.debug if silent else LOGGER.warning)(
            "[sea][spell] Args is not a dict: %s", type(result)
        )
        return None
    return result


def _parse_fuzzy_spell_args(args_raw: str) -> Optional[dict]:
    """Parse informal key=value... spell args into a dict.

    Handles single/double-quoted values, dict literals, and bare words.
    Falls back to _parse_spell_args for standard dict/JSON forms. The strict
    fallback is invoked with ``silent=True`` so its failure (the common case
    when the LLM uses fuzzy syntax like ``track_id='...'``) does not pollute
    the log — fuzzy KV parsing recovers without user-visible noise.
    """
    result = _parse_spell_args(args_raw, silent=True)
    if result is not None:
        return result
    pairs = {}
    for m in _KV_PATTERN.finditer(args_raw):
        key = m.group(1)
        # Groups 2-5 correspond to: single-quoted, double-quoted, dict-literal, bare-word
        value_raw = next(v for v in m.groups()[1:] if v is not None)
        # dict literals: try to parse as proper dict
        if value_raw.startswith("{"):
            parsed = _parse_spell_args(value_raw)
            pairs[key] = parsed if parsed is not None else value_raw
        else:
            pairs[key] = value_raw
    if pairs:
        return pairs
    return None


def _normalize_spell_line(tool_name: str, tool_args: dict) -> str:
    """Produce the canonical /spell line for a given tool name and args dict."""
    return f"/spell name='{tool_name}' args={json.dumps(tool_args, ensure_ascii=False)}"


def _parse_spell_line(text: str):
    """Parse the first /spell invocation in *text* (canonical form only).

    Returns ``(tool_name, tool_args, match)`` or ``None``.
    """
    m = _SPELL_PATTERN.search(text)
    if not m:
        return None
    tool_args = _parse_spell_args(m.group(2).strip())
    if tool_args is None:
        return None
    return m.group(1), tool_args, m


def _parse_spell_lines(text: str) -> List[Tuple[str, dict, Any, str]]:
    """Parse ALL /spell invocations in *text*, including fuzzy (informal) syntax.

    Returns list of ``(tool_name, tool_args, match, normalized_line)``.
    - ``match`` points to the original text position (for text_before calculation).
    - ``normalized_line`` is the canonical ``/spell name='...' args={...}`` form,
      which is used in SAIMemory storage so the persona learns correct syntax.
    Unparseable entries are silently skipped.
    """
    found: List[Tuple[str, dict, Any, str]] = []
    matched_spans: List[Tuple[int, int]] = []

    # Pass 1: canonical form
    for m in _SPELL_PATTERN.finditer(text):
        tool_args = _parse_spell_args(m.group(2).strip())
        if tool_args is not None:
            normalized = _normalize_spell_line(m.group(1), tool_args)
            found.append((m.group(1), tool_args, m, normalized))
            matched_spans.append(m.span())

    # Pass 2: fuzzy form — skip spans already matched by canonical pattern
    for m in _SPELL_PATTERN_FUZZY.finditer(text):
        span = m.span()
        if any(s <= span[0] < e for s, e in matched_spans):
            continue
        tool_name = m.group(1)
        tool_args = _parse_fuzzy_spell_args(m.group(2).strip())
        if tool_args is not None:
            normalized = _normalize_spell_line(tool_name, tool_args)
            LOGGER.info("[sea][spell] Fuzzy-parsed spell '%s' → %s", tool_name, normalized)
            found.append((tool_name, tool_args, m, normalized))
            matched_spans.append(span)

    # Sort by position in text so rounds process spells in order
    found.sort(key=lambda x: x[2].start())
    return found


def _build_spell_user_only_block(
    tool_name: str,
    tool_args: dict,
    display_name: str,
    result_str: str = "",
) -> str:
    """Build a ``<user_only>`` block carrying one spell invocation + result.

    Structure (Phase 2-B-step3 / 2-E, voice_tts_pipeline_streaming intent doc):

        <user_only alt="{display_name}">
        /spell name='{tool_name}' args={...}
        <details class="spellResult">
          <summary class="spellSummary">
            <span class="spellIcon"><svg ...>star</svg></span>
            <span>{display_name}</span>
          </summary>
          {escaped_result}
        </details>
        </user_only>

    - ``alt`` flows into other-persona ingestion (``strip_for_other_persona``)
      as ``[{display_name}]`` placeholder so the spell call is perceived but
      details are hidden.
    - The ``/spell ...`` line is the canonical normalized form (matches the
      assistant message persisted to context) — kept as raw text so the
      persona who emitted it can see exactly what they invoked.
    - Result is HTML-escaped and wrapped in ``<details class="spellResult">``
      for the foldable UI display. The summary reuses the existing
      ``.spellSummary`` / ``.spellIcon`` styling shared with legacy
      ``<details class="spellBlock">`` records (= consistent purple disclosure
      look between old and new records).
    - The whole block is stripped from voice/external paths by
      ``strip_user_only`` and replaced with placeholder by
      ``strip_for_other_persona``.
    """
    spell_line = _normalize_spell_line(tool_name, tool_args)
    result_escaped = (
        result_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if result_str else ""
    )
    alt_escaped = (
        (display_name or tool_name)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    display_name_escaped = (
        (display_name or tool_name)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    if result_escaped:
        result_section = (
            f'<details class="spellResult">'
            f'<summary class="spellSummary">'
            f'<span class="spellIcon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            f'<path d="M12 2L15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2z"/>'
            f'</svg></span>'
            f'<span>{display_name_escaped}</span>'
            f'</summary>'
            f'{result_escaped}'
            f'</details>'
        )
    else:
        result_section = ""
    return (
        f'<user_only alt="{alt_escaped}">\n'
        f'{spell_line}\n'
        f'{result_section}'
        f'</user_only>'
    )


# ── Handy Tool inline execution (legacy, kept for non-spell tool_call path) ──

_MAX_HANDY_TOOL_LOOPS = 3


def _execute_handy_tool_inline(
    tool_name: str,
    tool_args: dict,
    persona: Any,
    building_id: str,
    playbook_name: str,
    state: dict,
    messages: list,
    runtime: Any,
    event_callback: Optional[Callable] = None,
    thought_signature: Optional[str] = None,
) -> str:
    """Execute a handy tool inline within the LLM node and append protocol messages.

    Returns the tool result string. Modifies `messages` in place (appends
    assistant tool_call + tool result messages).
    """
    from pathlib import Path
    from sea.pulse_context import PulseLogEntry

    tc_id = f"tc_{uuid.uuid4().hex}"

    # Append assistant tool_call message to conversation
    tc_entry: Dict[str, Any] = {
        "id": tc_id,
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)},
    }
    # Gemini thinking models require thought_signature echoed back on function call parts
    if thought_signature:
        tc_entry["thought_signature"] = thought_signature
    tool_call_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [tc_entry],
    }
    messages.append(tool_call_msg)

    # Execute the tool
    tool_func = TOOL_REGISTRY.get(tool_name)
    if not tool_func:
        result_str = f"Tool '{tool_name}' not found in registry"
        LOGGER.error("[sea][handy] %s", result_str)
    else:
        persona_obj = state.get("_persona_obj") or persona
        persona_id = getattr(persona_obj, "persona_id", "unknown")
        persona_dir = getattr(persona_obj, "persona_log_path", None)
        persona_dir = persona_dir.parent if persona_dir else Path.cwd()
        manager_ref = getattr(persona_obj, "manager_ref", None)
        try:
            # ``llm_messages=messages`` snapshot lets spells like ``run_playbook``
            # fork their sub-line from the parent LLM node's actual messages.
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback, llm_messages=messages):
                raw_result = tool_func(**tool_args)
            result_str = str(raw_result)
            LOGGER.info("[sea][handy] Executed %s → %s", tool_name, result_str[:200])
        except Exception as exc:
            result_str = f"Handy tool error ({tool_name}): {exc}"
            LOGGER.exception("[sea][handy] %s failed", tool_name)

    # Append tool result message to conversation
    tool_result_msg = {
        "role": "tool",
        "tool_call_id": tc_id,
        "name": tool_name,
        "content": result_str,
    }
    messages.append(tool_result_msg)

    # Record to PulseContext
    _pulse_ctx = state.get("_pulse_context")
    if _pulse_ctx:
        # Assistant tool_call entry
        _pulse_ctx.append(PulseLogEntry(
            role="assistant", content="",
            node_id=f"handy_{tool_name}", playbook_name=playbook_name,
            tool_calls=[{
                "id": tc_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)},
            }],
        ))
        # Tool result entry
        _pulse_ctx.append(PulseLogEntry(
            role="tool", content=result_str,
            node_id=f"handy_{tool_name}", playbook_name=playbook_name,
            tool_call_id=tc_id, tool_name=tool_name,
        ))

    # Store to SAIMemory with handy_tool tag
    pulse_id = state.get("_pulse_id")
    runtime._store_memory(
        persona,
        f"[Handy Tool: {tool_name}]\n{result_str}",
        role="system",
        tags=["conversation", "handy_tool"],
        pulse_id=pulse_id,
        playbook_name=playbook_name,
    )

    # Record to activity trace (merged into final say event, not a separate bubble)
    _at = state.get("_activity_trace")
    if isinstance(_at, list):
        _at.append({"action": "handy_tool", "name": tool_name, "playbook": playbook_name})

    return result_str


async def _run_spell_tool_async(
    tool_name: str,
    tool_args: dict,
    persona: Any,
    state: dict,
    playbook_name: str,
    event_callback: Optional[Callable],
    messages: Optional[list] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Execute a single spell tool. Returns ``(result_string, metadata)``.

    Tool return values are normalized via ``tools.core.parse_tool_result``,
    which accepts:
    - ``str`` → ``(str, None, None, None)``
    - ``(str, dict)`` → 2-tuple form, dict carries ``{"media": [...]}`` etc.
    - ``(str, ToolResult|str|None, file_path)`` → 3-tuple form
    - ``(str, ToolResult|str|None, file_path, dict)`` → 4-tuple form (see.py 等)
    - ``ToolResult`` / ``dict`` → see ``parse_tool_result`` for details

    spell 経路では現状 ``content`` と ``metadata`` のみ下流に渡す。 metadata
    の ``media`` は spell-result message に attachment として乗り、 LLM が
    画像 / ファイル等を multimodal で受け取る。 snippet と file_path は
    受け取るだけで未使用 (別 Phase で SAIMemory 経路を設計予定)。

    Errors become ``(error_message, None)`` so the spell loop can continue
    and the persona's original utterance still reaches Building/SAIMemory.
    """
    from pathlib import Path

    tool_func = TOOL_REGISTRY.get(tool_name)
    if not tool_func:
        result_str = f"Spell '{tool_name}' not found in registry"
        LOGGER.error("[sea][spell] %s", result_str)
        return result_str, None

    # Wide try: covers persona_context setup, executor dispatch, tool
    # invocation. Any failure becomes a string result so the outer spell
    # loop can still proceed and, more importantly, the persona's utterance
    # survives to Building/SAIMemory even if the tool path is broken.
    try:
        persona_obj = state.get("_persona_obj") or persona
        persona_id = getattr(persona_obj, "persona_id", "unknown")
        persona_dir = getattr(persona_obj, "persona_log_path", None)
        persona_dir = persona_dir.parent if persona_dir else Path.cwd()
        manager_ref = getattr(persona_obj, "manager_ref", None)
        # Forward the active PulseContext so Track-mutating spells can enqueue
        # their effect onto deferred_track_ops (Intent A v0.14 / Intent B v0.11).
        pulse_ctx = state.get("_pulse_context")

        def _run():
            # ``llm_messages=messages`` snapshot lets spells like ``run_playbook``
            # fork their sub-line from the parent LLM node's actual messages.
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback, pulse_context=pulse_ctx, llm_messages=messages):
                return tool_func(**tool_args)

        if inspect.iscoroutinefunction(tool_func):
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback, pulse_context=pulse_ctx, llm_messages=messages):
                raw_result = await tool_func(**tool_args)
        else:
            raw_result = await asyncio.get_event_loop().run_in_executor(None, _run)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result

        # Normalize via tools.core.parse_tool_result so 4-tuple returns
        # ``(text, ToolResult, file_path, metadata)`` are unpacked correctly.
        # spell 経路では現状 ``(content, metadata)`` のみ消費する。 snippet と
        # file_path は受け取るだけで未使用 (chat 経路と同じ扱い)。
        # SAIMemory への snippet 記録経路は別 Phase で設計予定。
        # 詳細: docs/issues/native_tool_return_4tuple_bug.md
        content, _snippet, _file_path, result_metadata = parse_tool_result(raw_result)
        result_str = content
        LOGGER.info("[sea][spell] Executed %s → %s", tool_name, result_str[:200])
        return result_str, result_metadata
    except Exception as exc:
        result_str = f"Spell error ({tool_name}): {type(exc).__name__}: {exc}"
        LOGGER.exception("[sea][spell] %s failed", tool_name)
        return result_str, None


def _emit_bubble1_early(
    *,
    runtime: Any,
    persona: Any,
    building_id: str,
    text: str,
    speak_flag: Any,
    pulse_id: Optional[str],
    event_callback: Optional[Callable],
    node_id: str,
    send_streaming_discard: bool,
) -> str:
    """Spell loop 開始前に bubble1 を早期 emit して、 voice-tts の TTS 合成を
    Spell 実行と並行で走らせる (Phase 1)。

    Spell が無い、 unknown spell しか無い、 text_before が空、 ``speak`` が
    False の場合は no-op で ``""`` を返す。 早期 emit した時はその text_before
    を返し、 caller は後段の bubble1 emit を skip する判定に使う。

    ``send_streaming_discard``: streaming mode のみ True にする (= UI に途中の
    streaming chunk を破棄させて bubble1 を綺麗に再描画させるため)。 tool mode
    と non-streaming mode は streaming chunk を出さないので False。

    Why: ``<spell>`` を含む LLM 応答では、 bubble1 (= spell 前のテキスト)
    の内容は LLM 出力時点で確定している。 これを ``_run_spell_loop`` の完了
    後 (= spell 実行 数分後の可能性) まで待ってから emit していた旧設計だと、
    persona_speak hook → voice-tts enqueue → TTS 合成 が全て spell 完了後に
    なる。 早期 emit すれば spell 実行と TTS が並行し、 ユーザは spell 待ち
    中も発言1 の音声を聞ける。
    """
    if speak_flag is False:
        return ""
    text_before = _extract_first_text_before(text)
    if not text_before.strip():
        return ""
    if event_callback:
        if send_streaming_discard:
            event_callback({
                "type": "streaming_discard",
                "persona_id": getattr(persona, "persona_id", None),
                "node_id": node_id,
                "pulse_id": pulse_id,
            })
        event_callback({
            "type": "say",
            "content": text_before,
            "persona_id": getattr(persona, "persona_id", None),
            "pulse_id": pulse_id,
        })
    eff_bid = runtime._effective_building_id(persona, building_id)
    runtime._emit_say(persona, eff_bid, text_before, pulse_id=pulse_id)
    LOGGER.info(
        "[sea][spell] bubble1 emitted early (len=%d) — TTS will run in "
        "parallel with spell loop", len(text_before),
    )
    return text_before


def _extract_first_text_before(text: str) -> str:
    """``text`` の中から最初の有効 spell 行までの本文を返す。

    Spell が無い (= 通常応答)、 または unknown spell しか無い場合は ``""``
    を返す。 ``_run_spell_loop`` の最初のラウンドで計算される ``text_before``
    と同じ値になるよう、 ``_parse_spell_lines`` + ``SPELL_TOOL_NAMES`` で
    フィルタする手順を共有する。

    Phase 1 (bubble1 早期 emit、 voice-tts Spell 待ち遅延対策) で caller が
    spell loop 実行 **前** に bubble1 部分のテキストを取り出すために使う。
    spell 実行は数分かかる場合があるため (= 画像生成等)、 spell 開始前に
    bubble1 を emit すると persona_speak hook → voice-tts enqueue が即座に
    走り、 ユーザは spell 待ち中も発言1 の音声を聞ける。
    """
    if not text:
        return ""
    all_parsed = _parse_spell_lines(text)
    valid_spells = [
        (name, args, m, norm) for name, args, m, norm in all_parsed
        if name in SPELL_TOOL_NAMES
    ]
    if not valid_spells:
        return ""
    return text[:valid_spells[0][2].start()].rstrip()


async def _consume_pipeline_stream(
    stream_iter: Any,
    *,
    runtime: Any,
    persona: Any,
    building_id: str,
    node_def: Any,
    state: dict,
    pipeline_msg_id: Optional[str],
    sub_seq_start: int,
    cancellation_token: Any,
    event_callback: Optional[Callable],
) -> Tuple[str, int, bool, bool]:
    """1 つの LLM streaming call を消費し、 sub-speak 発火と UI への
    ``streaming_chunk`` イベント送出を担う。

    LLM 1 回目の応答にも、 spell 実行後の retry 応答にも、 同じロジックで
    使い回せるよう関数化した。 各呼び出しは独立した stream を 1 つ消費
    する。 spell_detected はローカル管理 (= round ごとにリセット相当)。

    voice-tts に渡す音声テキストは **すべて sub-speak 経由** で送る。 stream
    終端で文区切りに達してない residual も最後の sub-speak として flush する。
    finalize 側は ``final_voice_text=""`` 固定で voice-tts に 「stream close
    + wav 保存」 だけ依頼する設計 (= caller 側で全文 vs 既送 の差分比較を
    しない、 残テキストの送信を最終処理に残さない)。

    引数:
    - ``stream_iter``: ``llm_client.generate_stream(...)`` の戻り値
    - ``pipeline_msg_id``: ``_emit_speak_start`` で発番した placeholder ID。
      None の場合は sub-speak emit を行わない (= UI streaming_chunk のみ)
    - ``sub_seq_start``: 既に消費済の sub-speak 連番。 この値の次から発番

    返り値:
    ``(text, next_sub_seq, spell_detected, cancelled)``

    - ``text``: 受信した chunk を joined した完成 text
    - ``next_sub_seq``: helper 内で発火した sub-speak の最終連番。 caller は
      これを次の呼び出しの ``sub_seq_start`` にする
    - ``spell_detected``: この stream 中に ``/spell`` 行を検出したか
      (= 検出後は voice-tts emit を停止した)。 ログ用途
    - ``cancelled``: ``cancellation_token`` が発火したか
    """
    text_chunks: List[str] = []
    sub_seq = sub_seq_start
    spell_detected = False
    cancelled = False
    last_emit_pos = 0

    def _emit_fragment(fragment: str) -> None:
        """voice-able な fragment を 1 つ sub-speak として送出。"""
        nonlocal sub_seq
        if not _has_voiceable_content(fragment):
            return
        sub_seq += 1
        runtime._emit_sub_speak(
            persona,
            runtime._effective_building_id(persona, building_id),
            pipeline_msg_id,
            fragment,
            sub_seq,
            pulse_id=state.get("_pulse_id"),
        )

    try:
        for chunk in stream_iter:
            if cancellation_token and cancellation_token.is_cancelled():
                cancelled = True
                break

            if isinstance(chunk, dict) and chunk.get("type") == "thinking":
                if event_callback:
                    event_callback({
                        "type": "streaming_thinking",
                        "content": chunk["content"],
                        "persona_id": getattr(persona, "persona_id", None),
                        "node_id": getattr(node_def, "id", "llm"),
                        "pulse_id": state.get("_pulse_id"),
                    })
                continue

            text_chunks.append(chunk)
            if event_callback:
                event_callback({
                    "type": "streaming_chunk",
                    "content": chunk,
                    "persona_id": getattr(persona, "persona_id", None),
                    "node_id": getattr(node_def, "id", "llm"),
                    "pulse_id": state.get("_pulse_id"),
                })
                LOGGER.debug(
                    "[sea][llm][diag] streaming_chunk emitted: persona=%s pulse=%s pipeline_msg=%s len=%d",
                    getattr(persona, "persona_id", None),
                    state.get("_pulse_id"),
                    pipeline_msg_id,
                    len(chunk),
                )

            if pipeline_msg_id and not spell_detected:
                _buf = "".join(text_chunks)
                _tail = _buf[last_emit_pos:]
                _spell_match = _SPELL_PATTERN.search(_tail)
                if _spell_match:
                    _pre_spell = _tail[: _spell_match.start()].rstrip()
                    _emit_fragment(_pre_spell)
                    last_emit_pos += len(_tail[: _spell_match.start()])
                    spell_detected = True
                else:
                    while True:
                        _boundary = _find_next_sentence_boundary(_buf, last_emit_pos)
                        if _boundary < 0:
                            break
                        _emit_fragment(_buf[last_emit_pos:_boundary])
                        last_emit_pos = _boundary
    finally:
        if hasattr(stream_iter, "close"):
            stream_iter.close()

    text = "".join(text_chunks)

    # Stream 終端で last_emit_pos < len(text) の residual (= 文区切りに達して
    # ない最後の chunk) を最後の sub-speak として flush。 spell 行検出後の
    # 残り (= /spell 以降) は spell loop で <user_only> wrap される対象なので
    # voice-tts に渡してはいけない、 ここでは flush しない。
    if pipeline_msg_id and not spell_detected and last_emit_pos < len(text):
        _emit_fragment(text[last_emit_pos:].rstrip())

    return text, sub_seq, spell_detected, cancelled


async def _run_spell_loop(
    text: str,
    spell_enabled: bool,
    llm_client: Any,
    runtime: Any,
    persona: Any,
    building_id: str,
    state: dict,
    messages: list,
    playbook: Any,
    event_callback: Optional[Callable],
    node_def: Any = None,
    pipeline_streaming_state: Optional[dict] = None,
) -> Tuple[str, str, int]:
    """Execute the spell loop with parallel spell execution per LLM round.

    Each round: find ALL /spell lines → execute in parallel → re-invoke LLM once.
    Sequential rounds handle dependency chains (result of round N used in round N+1).

    Returns ``(full_merged_text, final_continuation, loop_count)``:

    - ``full_merged_text``: 各ラウンドの ``text_before`` + 各 spell の
      ``<user_only>`` ブロック を順次連結し、 最終ラウンドの continuation を
      末尾に append した 1 string。 caller は 「1 応答 = 1 record」 として
      ペルソナ履歴 / 建物履歴 / UI バブルに記録する用途で使う。
    - ``final_continuation``: 最終ラウンド (= spell が含まれない LLM 応答)
      の text のみ。 旧コード時代の 「spell loop 戻り値の text」 と等価で、
      caller は ``state["last"]`` (= 後段の memorize ノードが SAIMemory に
      保存する値) に入れる用途で使う。 これにより SAIMemory には
      「最終発言のみのレコード」 が単独で残り、 巨大な統合 record の重複
      を避ける。
    - ``loop_count``: 実行されたラウンド数。

    When ``loop_count == 0`` (no spells parsed), ``full_merged_text`` と
    ``final_continuation`` はどちらも入力の ``text`` をそのまま返す。

    ``pipeline_streaming_state`` (= ストリーミング応答経路の Pipeline
    Streaming 用): 非 None なら spell 実行後の LLM 再呼び出しを
    ``generate_stream()`` で行い、 ``_consume_pipeline_stream`` 経由で
    chunk を UI に流しつつ sub-speak を発火する。 dict の中身は
    ``{"msg_id": str, "sub_seq": int, "cancellation_token": ...}``。
    helper が dict を in-place mutate して sub_seq を更新する。 None の場合
    は従来通り ``generate()`` 単発呼び出しで全文一括受信。
    """
    from sea.pulse_context import PulseLogEntry

    if not spell_enabled or not text:
        return text, text, 0

    loop_count = 0
    merged_parts: List[str] = []

    # メタ判断 Pulse のとき、判断 LLM の独白 + 発動 spell + 結果を
    # PulseContext.meta_judgment_buffer に蓄積する (Phase 2 / handoff Part 2)。
    # Pulse 完了時に MetaLayer がここから meta_judgment_log を書く。
    _is_meta_judgment_pulse = state.get("_pulse_type") == "meta_judgment"
    if _is_meta_judgment_pulse:
        _meta_pulse_ctx = state.get("_pulse_context")
        if _meta_pulse_ctx is not None:
            _meta_pulse_ctx.init_meta_judgment_buffer()
            _meta_pulse_ctx.append_meta_judgment_thought(text)

    # Wrap the entire loop so any failure (unknown import state, LLM retry
    # failure, tool result serialization crash, etc.) is downgraded and the
    # persona's original utterance ``text`` is preserved. The caller saves
    # ``text`` to Building/SAIMemory — losing it just because the spell
    # system hit an internal error is too aggressive.
    try:
        while loop_count < _MAX_SPELL_LOOPS:
            # Parse all spells from current text (canonical + fuzzy), filter to registered ones
            all_parsed = _parse_spell_lines(text)
            valid_spells = [
                (name, args, m, norm) for name, args, m, norm in all_parsed
                if name in SPELL_TOOL_NAMES
            ]
            unknown = [name for name, _, _, _ in all_parsed if name not in SPELL_TOOL_NAMES]
            for name in unknown:
                LOGGER.warning("[sea][spell] Unknown spell '%s', skipping", name)

            if not valid_spells:
                break

            loop_count += 1
            spell_names = [s[0] for s in valid_spells]
            LOGGER.info("[sea][spell] Round %d: executing %d spell(s) in parallel: %s",
                        loop_count, len(valid_spells), spell_names)

            # text_before = text preceding the first spell
            text_before = text[:valid_spells[0][2].start()].rstrip()

            # Canonical assistant message: text_before + normalized spell lines
            all_spell_lines_normalized = "\n".join(norm for _, _, _, norm in valid_spells)
            assistant_content = (text_before + "\n" + all_spell_lines_normalized).strip()
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute all spells in parallel.
            # ``messages`` is snapshotted into a contextvar via persona_context so
            # spells like run_playbook can fork their sub-line from the parent
            # LLM node's actual conversation context (intent A v0.14).
            # Each entry is (text, optional metadata dict). Spells like
            # run_playbook use the metadata to forward sub-playbook media
            # (image generation results, etc.) up to the parent line so the
            # next LLM round can attach them as multimodal content.
            results: List[Tuple[str, Optional[Dict[str, Any]]]] = list(await asyncio.gather(*[
                _run_spell_tool_async(name, args, persona, state, playbook.name, event_callback, messages=messages)
                for name, args, _, _ in valid_spells
            ]))

            # メタ判断 Pulse の発動 spell + 結果をバッファに記録
            if _is_meta_judgment_pulse:
                _meta_pulse_ctx = state.get("_pulse_context")
                if _meta_pulse_ctx is not None:
                    for (name, args, _, _), (result_text, _) in zip(valid_spells, results):
                        _meta_pulse_ctx.append_meta_judgment_spell(name, args, result_text)

            # All spell results in one user message (reduces per-result message overhead)
            combined_results = "\n".join(
                f"[Spell Result: {name}]\n{result_text}"
                for (name, _, _, _), (result_text, _) in zip(valid_spells, results)
            )
            # Aggregate media from all spell results so the next LLM round can
            # see images / files etc. as attachments. iter_image_media() in each
            # LLM client picks this up via message["metadata"]["media"].
            aggregated_media: List[Dict[str, Any]] = []
            for _, result_meta in results:
                if isinstance(result_meta, dict):
                    media_list = result_meta.get("media")
                    if isinstance(media_list, list):
                        aggregated_media.extend(media_list)
            spell_result_msg: Dict[str, Any] = {
                "role": "user",
                "content": f"<system>{combined_results}</system>",
            }
            if aggregated_media:
                spell_result_msg["metadata"] = {"media": aggregated_media}
                LOGGER.info(
                    "[sea][spell] Round %d: attached %d media item(s) from spell results",
                    loop_count, len(aggregated_media),
                )
            messages.append(spell_result_msg)

            # Record to PulseContext
            pulse_ctx = state.get("_pulse_context")
            if pulse_ctx:
                pulse_ctx.append(PulseLogEntry(
                    role="assistant", content=assistant_content,
                    node_id=f"spell_round_{loop_count}", playbook_name=playbook.name,
                ))
                pulse_ctx.append(PulseLogEntry(
                    role="system", content=combined_results,
                    node_id=f"spell_round_{loop_count}", playbook_name=playbook.name,
                ))

            # Store to SAIMemory as single entries — spell lines (assistant) + all results
            # combined (system). This avoids N separate result entries per round.
            #
            # 7-layer storage routing (Intent A v0.14, Intent B v0.11):
            # - line_role / line_id / origin_track_id come from the active LineFrame
            #   on PulseContext. This makes the entry land in the layer that
            #   matches the caller's line (e.g. main_line → [2], sub_line root →
            #   [3], sub_line nested → [4] when scope='volatile').
            # - Tags now respect the LLM node's `memorize.tags` config when set;
            #   falling back to the legacy ["conversation"] default preserves
            #   prior behavior for nodes that don't declare memorize.
            pulse_id = state.get("_pulse_id")
            pulse_context = state.get("_pulse_context")
            memorize_cfg = getattr(node_def, "memorize", None) if node_def is not None else None
            if isinstance(memorize_cfg, dict):
                node_memorize_tags = list(memorize_cfg.get("tags") or [])
            else:
                node_memorize_tags = []
            assistant_tags = node_memorize_tags or ["conversation"]
            spell_tags = (node_memorize_tags + ["spell"]) if node_memorize_tags else ["conversation", "spell"]

            if assistant_content:
                runtime._store_memory(
                    persona, assistant_content, role="assistant",
                    tags=assistant_tags, pulse_id=pulse_id, playbook_name=playbook.name,
                    pulse_context=pulse_context,
                )
            if combined_results:
                runtime._store_memory(
                    persona, combined_results, role="system",
                    tags=spell_tags, pulse_id=pulse_id, playbook_name=playbook.name,
                    pulse_context=pulse_context,
                )

            # Record to activity trace
            _at = state.get("_activity_trace")
            if isinstance(_at, list):
                for name, _, _, _ in valid_spells:
                    _at.append({"action": "spell", "name": name, "playbook": playbook.name})

            # Accumulate this round's content into merged_parts: round-leading
            # text_before (= raw text before the first /spell line of this round)
            # followed by one ``<user_only>`` block per executed spell.
            # The final LLM continuation (= ``text`` after the retry below) is
            # appended once the loop exits — see the return path.
            if text_before:
                merged_parts.append(text_before)
            for (name, args, _, _), (result_text, _) in zip(valid_spells, results):
                schema = SPELL_TOOL_SCHEMAS.get(name)
                display = (schema.spell_display_name if schema else "") or name
                merged_parts.append(
                    _build_spell_user_only_block(name, args, display, result_text)
                )

            # Re-invoke LLM once for the entire round.
            # Pipeline Streaming で呼ばれた時 (= pipeline_streaming_state が
            # 非 None) は generate_stream + helper 経由で chunk を流しながら
            # sub-speak を発火する。 そうでない (= (2) Tool mode streaming や
            # (4) 全文一括経路から呼ばれた時) は従来通り generate() 単発で
            # 全文受信する。
            #
            # ``tools=[]`` を明示で渡す: Gemini の ``generate_stream`` は
            # ``tools=None`` を 「デフォルト spell スキーマ集 (GEMINI_TOOLS_SPEC)
            # を使う」 と解釈してしまい、 その中の 1 つに含まれる Gemini 未対応
            # の type (例 "TUPLE") で 400 INVALID_ARGUMENT を起こす。 spell loop
            # の retry は 「spell 結果を踏まえた継続発話」 なので tools 不要、
            # 明示的に空リストで送る。
            #
            # retry 前に ``text = ""`` でリセット: helper / generate() で例外が
            # 出ると ``retry_result`` の代入が走らず、 ``text`` は round 開始時
            # の値 (= 初回応答全文、 spell 行込み) のまま except 節に流れる。
            # その状態で except 節の ``if text: merged_parts.append(text)``
            # が動くと、 spell 結果の後ろに初回応答全文がそのまま二重表示・二重
            # 保存される。 ここで text を空にしておけば、 失敗時に空の append
            # は skip される (= partial で停止)。
            LOGGER.info("[sea][spell] Re-invoking LLM after round %d (%d spell(s))", loop_count, len(valid_spells))
            text = ""
            if pipeline_streaming_state is not None:
                _retry_stream = llm_client.generate_stream(
                    messages,
                    tools=[],
                    temperature=runtime._default_temperature(persona),
                    **runtime._get_cache_kwargs(),
                )
                _retry_text, _retry_sub_seq, _retry_spell_detected, _retry_cancelled = await _consume_pipeline_stream(
                    _retry_stream,
                    runtime=runtime,
                    persona=persona,
                    building_id=building_id,
                    node_def=node_def,
                    state=state,
                    pipeline_msg_id=pipeline_streaming_state.get("msg_id"),
                    sub_seq_start=int(pipeline_streaming_state.get("sub_seq", 0) or 0),
                    cancellation_token=pipeline_streaming_state.get("cancellation_token"),
                    event_callback=event_callback,
                )
                pipeline_streaming_state["sub_seq"] = _retry_sub_seq
                retry_result = _retry_text
                if _retry_cancelled:
                    LOGGER.info(
                        "[sea][spell] Round %d streaming retry cancelled mid-flight; "
                        "breaking out of spell loop",
                        loop_count,
                    )
            else:
                retry_result = llm_client.generate(
                    messages,
                    tools=[],
                    temperature=runtime._default_temperature(persona),
                    **runtime._get_cache_kwargs(),
                )

            retry_usage = llm_client.consume_usage()
            if retry_usage:
                get_usage_tracker().record_usage(
                    model_id=retry_usage.model,
                    input_tokens=retry_usage.input_tokens,
                    output_tokens=retry_usage.output_tokens,
                    cached_tokens=retry_usage.cached_tokens,
                    cache_write_tokens=retry_usage.cache_write_tokens,
                    cache_ttl=retry_usage.cache_ttl,
                    persona_id=getattr(persona, "persona_id", None),
                    building_id=building_id,
                    node_type="llm_spell_retry",
                    playbook_name=playbook.name,
                    category="persona_speak",
                )
                from saiverse.model_configs import calculate_cost
                retry_cost = calculate_cost(
                    retry_usage.model, retry_usage.input_tokens, retry_usage.output_tokens,
                    retry_usage.cached_tokens, retry_usage.cache_write_tokens, cache_ttl=retry_usage.cache_ttl,
                )
                runtime._accumulate_usage(
                    state, retry_usage.model, retry_usage.input_tokens,
                    retry_usage.output_tokens, retry_cost,
                    retry_usage.cached_tokens, retry_usage.cache_write_tokens,
                )
                # Phase 4-e: anchor touch を LLM 成功後に移動 (旧: prepare_context 内の先行 touch)
                runtime._touch_anchor_after_llm_call(persona, retry_usage)

            if isinstance(retry_result, dict):
                text = retry_result.get("content", "")
            elif isinstance(retry_result, str):
                text = retry_result
            else:
                text = ""

            # メタ判断 Pulse の retry text もバッファに追記
            if _is_meta_judgment_pulse:
                _meta_pulse_ctx = state.get("_pulse_context")
                if _meta_pulse_ctx is not None:
                    _meta_pulse_ctx.append_meta_judgment_thought(text)

            # Spell-loop retry の I/O も llm_io.log に記録する。これがないと
            # メタ判断や spell 入りの応答ラウンドの全数を観測できない (送信時の
            # messages と応答 text のペアが、メインの _dump_llm_io 1 件分しか
            # 残らないため、デバッグでログを追っても "retry が起きたかどうか"
            # を確定できない)。round ごとに source は LLM ノードと同じものを
            # 使い、node_id だけを spell_round_<N> に変える。
            try:
                runtime._dump_llm_io(
                    playbook.name,
                    f"spell_round_{loop_count}",
                    persona,
                    messages,
                    text,
                )
            except Exception:
                LOGGER.warning("[sea][spell] failed to dump spell-loop retry I/O", exc_info=True)

            LOGGER.info("[sea][spell] After round %d: has_more_spells=%s",
                        loop_count, bool(_SPELL_PATTERN.search(text)))

        LOGGER.info("[sea][spell] Completed %d round(s)", loop_count)
        final_continuation = text or ""
        if loop_count == 0:
            # No spells parsed — return the original text unchanged so the
            # caller's normal (non-spell) emit path stays correct.
            return text, text, 0
        # Append the final LLM continuation (= text after the last retry).
        if final_continuation:
            merged_parts.append(final_continuation)
        return "\n".join(merged_parts), final_continuation, loop_count
    except Exception as exc:
        # Any unhandled error in the spell pipeline: log with traceback,
        # inject a system-visible error note for the next LLM turn, and
        # return what was assembled so far so the caller can still save it.
        LOGGER.exception(
            "[sea][spell] spell loop fatal error after %d round(s); "
            "preserving partial message, skipping remaining spells",
            loop_count,
        )
        error_note = (
            f"[Spell System Error] スペル実行系で内部エラーが発生しました "
            f"({type(exc).__name__}: {exc})。"
            f"発言はそのまま保存され、以降のスペル呼び出しはスキップされました。"
        )
        try:
            messages.append({"role": "user", "content": f"<system>{error_note}</system>"})
        except Exception:
            LOGGER.debug("[sea][spell] failed to append error note to messages", exc_info=True)
        final_continuation = text or ""
        if loop_count == 0:
            return text, text, 0
        if final_continuation:
            merged_parts.append(final_continuation)
        return "\n".join(merged_parts), final_continuation, loop_count


async def _decide_spell_args_via_playbook(
    spell_name: str,
    runtime: Any,
    persona: Any,
    building_id: str,
    outer_state: dict,
    event_callback: Optional[Callable],
) -> Optional[Dict[str, Any]]:
    """Run ``spell_args_decider`` Playbook (sub_line) to obtain args for a Spell.

    The Playbook reads parent line messages (via the snapshot pipeline added in
    v0.25) so the persona's cognition includes the ongoing context. The Playbook
    must include ``args`` in its ``output_schema`` and the inner LLM node should
    use ``response_schema_source: "spell:{spell_name}"`` + ``output_key: "args"``
    to surface the structured output through the sub-line → parent_state path.

    Returns the args dict, or None if the Playbook is missing / produced no args.
    """
    pb = runtime._load_playbook_for("spell_args_decider", persona, building_id)
    if pb is None:
        LOGGER.warning(
            "[sea][pre_spells] spell_args_decider Playbook not found; cannot decide "
            "args for '%s'. Install builtin_data/playbooks/public/spell_args_decider.json.",
            spell_name,
        )
        return None

    # Build a fresh parent_state for the decider, snapshotting what the sub-line
    # needs from the caller (mirrors run_playbook Spell pattern). The decider's
    # output_schema entries (`args`) are written back into this dict on completion.
    decider_parent_state: Dict[str, Any] = {
        "_messages": list(outer_state.get("_messages") or []),
        "_pulse_context": outer_state.get("_pulse_context"),
        "_pulse_id": outer_state.get("_pulse_id"),
    }

    try:
        await asyncio.to_thread(
            runtime._run_playbook,
            pb, persona, building_id,
            None,  # user_input — decider reads spell_name via initial_params
            False,  # auto_mode
            record_history=True,
            parent_state=decider_parent_state,
            event_callback=event_callback,
            initial_params={"spell_name": spell_name},
            line="sub",
            isolate_pulse_context=False,
        )
    except Exception:
        LOGGER.exception(
            "[sea][pre_spells] spell_args_decider raised for '%s'", spell_name,
        )
        return None

    args = decider_parent_state.get("args")
    if not isinstance(args, dict):
        LOGGER.warning(
            "[sea][pre_spells] spell_args_decider produced no 'args' dict for '%s' "
            "(got %r). Ensure the Playbook's output_schema includes 'args' and the "
            "inner LLM node uses output_key='args'.",
            spell_name, type(args).__name__,
        )
        return None
    return dict(args)


async def _execute_pre_spells(
    pre_spells: List[str],
    runtime: Any,
    persona: Any,
    building_id: str,
    state: dict,
    playbook: Any,
    event_callback: Optional[Callable],
) -> None:
    """Execute UI-requested spells before the first LLM call of a Pulse.

    Triggered by the chat API when the user manually selects a Playbook in
    the UI ("ツール指定" mode), and by schedule_manager when a schedule
    specifies a Spell to run. Each entry in ``pre_spells`` is a Spell
    invocation string in one of two forms:

    - ``/spell name='X' args={...}`` — fully specified args (executed as-is)
    - ``/spell name='X'`` — args omitted; resolved at runtime by invoking
      the ``spell_args_decider`` Playbook so the persona's own cognition
      decides the args (mirrors the Spell loop pattern where the persona
      writes the args in their utterance).

    Behavior:
    - Parse each entry via ``_parse_spell_lines`` first; if that fails, try
      the no-args form via ``_SPELL_PATTERN_NO_ARGS``. Unknown spells / un-
      parseable entries log a warning and are skipped.
    - For no-args entries, run ``spell_args_decider`` Playbook (sub_line)
      to obtain the args. The Playbook reads parent line messages via the
      snapshot pipeline (v0.25) so the persona can decide args from their
      ongoing context.
    - Execute valid spells in parallel via ``_run_spell_tool_async``, the
      same path used by the regular spell loop.
    - Append a single ``<system>``-tagged user message to
      ``state["_messages"]`` containing the combined results, so the
      first LLM round sees them as if the user had requested them.
    - Forward any media (images, etc.) returned by spells via
      ``message["metadata"]["media"]`` so the LLM gets attachments.

    Idempotency is enforced by the caller via ``state["_pre_spells_executed"]``.
    See: docs/intent/persona_cognition/nested_subline_spell.md §13 (v0.2)
    """
    from sea.pulse_context import PulseLogEntry

    if not pre_spells:
        return

    messages = state.get("_messages")
    if not isinstance(messages, list):
        LOGGER.warning("[sea][pre_spells] state['_messages'] is missing; skipping")
        return

    # Phase 1: parse entries, splitting into "args known" and "args needed"
    # buckets. The latter triggers spell_args_decider before execution.
    fully_specified: List[Tuple[str, dict, str]] = []
    needs_decision: List[str] = []  # spell names whose args must be decided
    for entry in pre_spells:
        if not isinstance(entry, str) or not entry.strip():
            continue
        parsed = _parse_spell_lines(entry)
        if parsed:
            for name, args, _, normalized in parsed:
                if name not in SPELL_TOOL_NAMES:
                    LOGGER.warning("[sea][pre_spells] Unknown spell '%s', skipping", name)
                    continue
                fully_specified.append((name, args, normalized))
            continue
        # Try args-omitted form
        m = _SPELL_PATTERN_NO_ARGS.search(entry)
        if m:
            spell_name = m.group(1)
            if spell_name not in SPELL_TOOL_NAMES:
                LOGGER.warning("[sea][pre_spells] Unknown spell '%s' (no-args form), skipping", spell_name)
                continue
            needs_decision.append(spell_name)
            continue
        LOGGER.warning("[sea][pre_spells] Could not parse spell entry: %r", entry)

    # Phase 2: resolve args for entries that need decision via spell_args_decider
    decided: List[Tuple[str, dict, str]] = []
    for spell_name in needs_decision:
        try:
            args = await _decide_spell_args_via_playbook(
                spell_name, runtime, persona, building_id, state, event_callback,
            )
        except Exception:
            LOGGER.exception(
                "[sea][pre_spells] spell_args_decider failed for '%s'; skipping",
                spell_name,
            )
            continue
        if args is None:
            LOGGER.warning(
                "[sea][pre_spells] spell_args_decider returned no args for '%s'; skipping",
                spell_name,
            )
            continue
        normalized = _normalize_spell_line(spell_name, args)
        decided.append((spell_name, args, normalized))

    valid_specs: List[Tuple[str, dict, str]] = fully_specified + decided
    if not valid_specs:
        return

    LOGGER.info(
        "[sea][pre_spells] Executing %d UI-requested spell(s) before first LLM call: %s",
        len(valid_specs), [s[0] for s in valid_specs],
    )

    results: List[Tuple[str, Optional[Dict[str, Any]]]] = list(await asyncio.gather(*[
        _run_spell_tool_async(name, args, persona, state, playbook.name, event_callback, messages=messages)
        for name, args, _ in valid_specs
    ]))

    triggered_lines = [norm for _, _, norm in valid_specs]
    result_lines = [
        f"[Spell Result: {name}]\n{result_text}"
        for (name, _, _), (result_text, _) in zip(valid_specs, results)
    ]
    system_body = (
        "ユーザーの操作により以下のスペルを事前に実行しました。"
        "結果を踏まえて応答してください。\n\n"
        "[Triggered by user]\n"
        + "\n".join(triggered_lines)
        + "\n\n"
        + "\n\n".join(result_lines)
    )
    spell_result_msg: Dict[str, Any] = {
        "role": "user",
        "content": f"<system>{system_body}</system>",
    }

    aggregated_media: List[Dict[str, Any]] = []
    for _, result_meta in results:
        if isinstance(result_meta, dict):
            media_list = result_meta.get("media")
            if isinstance(media_list, list):
                aggregated_media.extend(media_list)
    if aggregated_media:
        spell_result_msg["metadata"] = {"media": aggregated_media}
        LOGGER.info(
            "[sea][pre_spells] Attached %d media item(s) from pre-spell results",
            len(aggregated_media),
        )

    messages.append(spell_result_msg)

    pulse_ctx = state.get("_pulse_context")
    if pulse_ctx:
        pulse_ctx.append(PulseLogEntry(
            role="system", content=system_body,
            node_id="pre_spells", playbook_name=playbook.name,
        ))

    pulse_id = state.get("_pulse_id")
    try:
        runtime._store_memory(
            persona, system_body, role="system",
            tags=["conversation", "spell", "pre_spell"],
            pulse_id=pulse_id, playbook_name=playbook.name,
            pulse_context=pulse_ctx,
        )
    except Exception:
        LOGGER.exception("[sea][pre_spells] Failed to persist pre-spell results to SAIMemory")

    _at = state.get("_activity_trace")
    if isinstance(_at, list):
        for name, _, _ in valid_specs:
            _at.append({"action": "pre_spell", "name": name, "playbook": playbook.name})


def lg_llm_node(runtime, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
    async def node(state: dict):
        # Check for cancellation at start of node
        cancellation_token = state.get("_cancellation_token")
        if cancellation_token:
            cancellation_token.raise_if_cancelled()

        # ── Pre-spells: execute UI-requested spells before the first LLM call ──
        # Set by the chat API for "ツール指定" mode. Runs at most once per Pulse,
        # gated by state["_pre_spells_executed"]. Result messages flow into
        # state["_messages"] via the normal spell-loop machinery, so the first
        # LLM round sees them.
        _pre_spells = state.get("_pre_spells")
        if _pre_spells and not state.get("_pre_spells_executed"):
            state["_pre_spells_executed"] = True
            try:
                await _execute_pre_spells(
                    _pre_spells, runtime, persona, building_id, state, playbook, event_callback,
                )
            except Exception:
                LOGGER.exception("[sea][pre_spells] Pre-spell execution failed; continuing without pre-spell results")

        # Send status event for node execution
        node_id = getattr(node_def, "id", "llm")
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})

        # Build variables for template formatting
        # System variables (_ prefix) are excluded — only playbook variables are exposed to templates
        variables = {
            "input": state.get("input", ""),
            "last": state.get("last", ""),
            "persona_id": getattr(persona, "persona_id", None),
            "persona_name": getattr(persona, "persona_name", None),
            **{k: v for k, v in state.items() if not k.startswith("_")},
        }

        # Debug: log template variables for novel_writing playbook
        if playbook.name == "novel_writing":
            node_id = getattr(node_def, "id", "")
            if node_id.startswith("chapter_"):
                # Log specific variables used in chapter templates
                relevant_keys = ["novel_title", "chapter_1_title", "chapter_2_title", "chapter_3_title", "chapter_4_title"]
                relevant_vars = {k: variables.get(k) for k in relevant_keys}
                LOGGER.debug("[sea][novel_writing] Node %s: relevant variables = %s", node_id, relevant_vars)
        text = ""
        schema_consumed = False
        prompt = None  # Will store the expanded prompt for memorize
        try:
            # Phase 3 段階 4-D (2026-05-09): context_profile / CONTEXT_PROFILES 経路を削除。
            # 最新仕様 (Intent A v0.14, Intent B v0.11) では line: 'main'/'sub' に集約されており、
            # base messages は run 起動時に組み立てられた state["_messages"] が source of truth。
            base_msgs = state.get("_messages", [])
            action_template = getattr(node_def, "action", None)
            if action_template:
                prompt = _format(action_template, variables)
                # ============================================================
                # 設計上の重要判断 — user role + <system> タグの理由
                # (変更を検討する前に必ず読むこと。「system っぽい指示なのに
                #  role='system' じゃないのはおかしい」という直感だけで直すと
                #  プロバイダ互換性が壊れる)
                #
                # Playbook の action テキストは LLM への指示で、本来なら
                # role='system' で送りたい。が、各プロバイダの差異により
                # 共通形式で system role を「途中に挿入」することができない:
                #
                #   - Gemini: system role は context 先頭でしか受け付けない。
                #     messages の中途で role='system' を出すと無視されるか
                #     エラーになる。
                #   - Anthropic: system は messages の外側に別フィールドで
                #     渡す仕様 (messages 配列の途中に role='system' を含め
                #     ても効果が限定的)。
                #   - OpenAI / NIM 等: 受け付けはするが、複数 system が並ぶ
                #     と挙動が安定しない / 後段で吸い込まれることがある。
                #
                # 全プロバイダで共通の挙動を保つため、本プロジェクトでは
                # 「指示系メッセージは user role + content を <system>...</system>
                # で囲む」形式に統一している。llm_clients/* も <system>
                # タグを「LLM が指示として認識すべき高優先度ブロック」として
                # 扱うよう調整済み。
                #
                # 「直すべき」ではなく「対策済み」。 system role に変えると
                # Gemini 互換が壊れる。同様の <system>…</system> 投入箇所が
                # sea/runtime.py / sea/pulse_context.py / sea/runtime_nodes.py 等
                # にもあるが、全て同じ理由でこの形になっている。
                # ============================================================
                if not prompt.lstrip().startswith("<system>"):
                    prompt = f"<system>{prompt}</system>"
                messages = list(base_msgs) + [{"role": "user", "content": prompt}]
            else:
                messages = list(base_msgs)

            # Dynamically add enum to response_schema if available_playbooks exists
            response_schema = getattr(node_def, "response_schema", None)

            # Resolve response_schema_source if response_schema is not explicitly set.
            # Supports 'spell:<name>' to load a registered Spell's input schema from
            # SPELL_TOOL_SCHEMAS. Template variables ({state_var}) are expanded first.
            if response_schema is None:
                schema_source = getattr(node_def, "response_schema_source", None)
                if schema_source:
                    try:
                        resolved_source = _format(schema_source, variables)
                    except Exception:
                        LOGGER.warning(
                            "[sea][llm] Failed to expand response_schema_source template %r",
                            schema_source, exc_info=True,
                        )
                        resolved_source = schema_source
                    response_schema = _resolve_response_schema_source(
                        resolved_source, variables=variables
                    )
                    if response_schema is None:
                        LOGGER.warning(
                            "[sea][llm] response_schema_source %r resolved to None; "
                            "node %s will run without structured output",
                            resolved_source, getattr(node_def, "id", "?"),
                        )

            if response_schema and "available_playbooks" in state:
                response_schema = runtime._add_playbook_enum(response_schema, state.get("available_playbooks"))

            # Select LLM client based on model_type and structured output needs
            needs_structured_output = response_schema is not None
            llm_client = runtime._select_llm_client(node_def, persona, needs_structured_output=needs_structured_output, state=state)

            # Inject model-specific system prompt if configured
            _model_config_key = getattr(llm_client, "config_key", None)
            if _model_config_key:
                from saiverse.model_configs import get_model_system_prompt
                _model_sys_prompt = get_model_system_prompt(_model_config_key)
                if _model_sys_prompt:
                    _injected = False
                    for _mi, _msg in enumerate(messages):
                        if _msg.get("role") == "system":
                            # Create new dict to avoid mutating shared base_msgs
                            messages[_mi] = {**_msg, "content": _msg["content"] + "\n\n---\n\n" + _model_sys_prompt}
                            _injected = True
                            break
                    if not _injected:
                        messages.insert(0, {"role": "system", "content": _model_sys_prompt})
                    LOGGER.debug("[sea] Injected model-specific system prompt for %s", _model_config_key)

            # Check if tools are available for this node
            available_tools = getattr(node_def, "available_tools", None)
            LOGGER.info("[DEBUG] available_tools = %s", available_tools)

            # Check if spells are enabled for this persona (spells replace handy tool injection)
            _spell_enabled = state.get("_spell_enabled", False)

            effective_tools: list[str] = list(available_tools or [])

            if effective_tools:
                LOGGER.info("[DEBUG] Entering tools mode (generate with tools)")
                # Tool calling mode - use unified generate() with tools
                tools_spec = runtime._build_tools_spec(effective_tools, llm_client)

                # Check if we should use streaming in tool mode
                speak_flag = getattr(node_def, "speak", None)
                streaming_enabled = _is_llm_streaming_enabled()
                use_tool_streaming = (
                    speak_flag is True
                    and response_schema is None
                    and streaming_enabled
                    and event_callback is not None
                )
                LOGGER.info("[DEBUG] Tool mode streaming check: speak=%s, streaming=%s, event_cb=%s → use_tool_streaming=%s",
                           speak_flag, streaming_enabled, event_callback is not None, use_tool_streaming)

                if use_tool_streaming:
                    # ── Streaming tool mode ──
                    # Stream text chunks to UI while tools are buffered internally.
                    # After stream ends, consume_tool_detection() tells us whether
                    # LLM chose a tool or just produced text.
                    LOGGER.info("[DEBUG] Using streaming generation with tools")
                    max_stream_retries = 3
                    text = ""
                    cancelled_during_stream = False
                    for stream_attempt in range(max_stream_retries):
                        text_chunks: list[str] = []
                        stream_iter = llm_client.generate_stream(
                            messages,
                            tools=tools_spec,
                            temperature=runtime._default_temperature(persona),
                            **runtime._get_cache_kwargs(),
                        )
                        try:
                            for chunk in stream_iter:
                                if cancellation_token and cancellation_token.is_cancelled():
                                    LOGGER.info("[sea] Tool streaming cancelled by user")
                                    cancelled_during_stream = True
                                    break
                                if isinstance(chunk, dict) and chunk.get("type") == "thinking":
                                    event_callback({
                                        "type": "streaming_thinking",
                                        "content": chunk["content"],
                                        "persona_id": getattr(persona, "persona_id", None),
                                        "node_id": getattr(node_def, "id", "llm"),
                                        "pulse_id": state.get("_pulse_id"),
                                    })
                                    continue
                                text_chunks.append(chunk)
                                event_callback({
                                    "type": "streaming_chunk",
                                    "content": chunk,
                                    "persona_id": getattr(persona, "persona_id", None),
                                    "node_id": getattr(node_def, "id", "llm"),
                                    "pulse_id": state.get("_pulse_id"),
                                })
                        finally:
                            if hasattr(stream_iter, 'close'):
                                stream_iter.close()
                        text = "".join(text_chunks)

                        if cancelled_during_stream:
                            break
                        if text.strip():
                            break
                        # Tool call with no text is valid — check before retrying
                        _peek_tool = llm_client.consume_tool_detection()
                        if _peek_tool and _peek_tool.get("type") in ("tool_call", "both"):
                            # Put it back for later consumption
                            llm_client._store_tool_detection(_peek_tool)
                            break
                        # Truly empty (no text, no tool call) — discard and retry
                        discarded_usage = llm_client.consume_usage()
                        LOGGER.warning(
                            "[sea][llm] Empty tool-streaming response (attempt %d/%d). "
                            "Discarding usage (in=%d, out=%d) and retrying...",
                            stream_attempt + 1, max_stream_retries,
                            discarded_usage.input_tokens if discarded_usage else 0,
                            discarded_usage.output_tokens if discarded_usage else 0,
                        )
                    else:
                        LOGGER.error(
                            "[sea][llm] Empty tool-streaming response after %d attempts.",
                            max_stream_retries,
                        )

                    # Consume reasoning
                    _tool_reasoning = llm_client.consume_reasoning()
                    _tool_reasoning_text = "\n\n".join(
                        e.get("text", "") for e in _tool_reasoning if e.get("text")
                    ) if _tool_reasoning else ""
                    if _tool_reasoning_text:
                        state["_reasoning_text"] = _tool_reasoning_text
                    _tool_reasoning_details = llm_client.consume_reasoning_details()
                    if _tool_reasoning_details is not None:
                        state["_reasoning_details"] = _tool_reasoning_details

                    # Record usage
                    usage = llm_client.consume_usage()
                    llm_usage_metadata: Dict[str, Any] | None = None
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm_tool_stream",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        from saiverse.model_configs import calculate_cost, get_model_display_name
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        llm_usage_metadata = {
                            "model": usage.model,
                            "model_display_name": get_model_display_name(usage.model),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cached_tokens": usage.cached_tokens,
                            "cache_write_tokens": usage.cache_write_tokens,
                            "cost_usd": cost,
                        }
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)
                        # Phase 4-e: anchor touch を LLM 成功後に移動 (旧: prepare_context 内の先行 touch)
                        runtime._touch_anchor_after_llm_call(persona, usage)

                    # Check tool detection — did LLM call a tool?
                    tool_detection = llm_client.consume_tool_detection()
                    LOGGER.info("[DEBUG] Tool detection after streaming: %s",
                               tool_detection.get("type") if tool_detection else None)

                    # Use tool_detection as the result for the common tool branching below
                    if tool_detection and tool_detection.get("type") in ("tool_call", "both"):
                        result = tool_detection

                        if tool_detection.get("type") == "both" and text.strip():
                            # "both": text + tool call — keep the streamed text in UI and Building history
                            _speak_metadata_key = getattr(node_def, "metadata_key", None)
                            _speak_base_metadata = state.get(_speak_metadata_key) if _speak_metadata_key else None

                            completion_event: Dict[str, Any] = {
                                "type": "streaming_complete",
                                "persona_id": getattr(persona, "persona_id", None),
                                "node_id": getattr(node_def, "id", "llm"),
                                "pulse_id": state.get("_pulse_id"),
                            }
                            if _tool_reasoning_text:
                                completion_event["reasoning"] = _tool_reasoning_text
                            if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                                completion_event["metadata"] = _speak_base_metadata
                            event_callback(completion_event)

                            # Record to Building history
                            pulse_id = state.get("_pulse_id")
                            msg_metadata: Dict[str, Any] = {}
                            if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                                msg_metadata.update(_speak_base_metadata)
                            if llm_usage_metadata:
                                msg_metadata["llm_usage"] = llm_usage_metadata
                            if _tool_reasoning_text:
                                msg_metadata["reasoning"] = _tool_reasoning_text
                            if _tool_reasoning_details is not None:
                                msg_metadata["reasoning_details"] = _tool_reasoning_details
                            _at_both = state.get("_activity_trace")
                            if _at_both:
                                msg_metadata["activity_trace"] = list(_at_both)
                            eff_bid = runtime._effective_building_id(persona, building_id)
                            _last_bmsg = runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
                            if isinstance(_last_bmsg, dict):
                                _last_mid = _last_bmsg.get("message_id")
                                if _last_mid:
                                    state["_last_message_id"] = str(_last_mid)
                            LOGGER.info("[sea] 'both' response: text kept in UI and Building history (len=%d), tool call continues", len(text))
                        elif text_chunks:
                            # "tool_call" only — discard streamed text
                            event_callback({
                                "type": "streaming_discard",
                                "persona_id": getattr(persona, "persona_id", None),
                                "node_id": getattr(node_def, "id", "llm"),
                                "pulse_id": state.get("_pulse_id"),
                            })
                            LOGGER.info("[sea] Streaming text discarded — tool_call only (no speak content)")
                    else:
                        # No tool call — this is a normal text response
                        result = {"type": "text", "content": text}

                        # Send streaming_complete + emit say (same as normal streaming mode)
                        _speak_metadata_key = getattr(node_def, "metadata_key", None)
                        _speak_base_metadata = state.get(_speak_metadata_key) if _speak_metadata_key else None

                        completion_event: Dict[str, Any] = {
                            "type": "streaming_complete",
                            "persona_id": getattr(persona, "persona_id", None),
                            "node_id": getattr(node_def, "id", "llm"),
                            "pulse_id": state.get("_pulse_id"),
                        }
                        if _tool_reasoning_text:
                            completion_event["reasoning"] = _tool_reasoning_text
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            completion_event["metadata"] = _speak_base_metadata
                        event_callback(completion_event)

                        # Record to Building history
                        pulse_id = state.get("_pulse_id")
                        msg_metadata: Dict[str, Any] = {}
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            msg_metadata.update(_speak_base_metadata)
                        if llm_usage_metadata:
                            msg_metadata["llm_usage"] = llm_usage_metadata
                        if _tool_reasoning_text:
                            msg_metadata["reasoning"] = _tool_reasoning_text
                        if _tool_reasoning_details is not None:
                            msg_metadata["reasoning_details"] = _tool_reasoning_details
                        _at_stream = state.get("_activity_trace")
                        if _at_stream:
                            msg_metadata["activity_trace"] = list(_at_stream)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            msg_metadata["llm_usage_total"] = dict(accumulator)
                        eff_bid = runtime._effective_building_id(persona, building_id)
                        _last_bmsg = runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
                        # 後続ツールが新しい persona_context 配下でも
                        # 最新の message_id を参照できるよう state に残す。
                        if isinstance(_last_bmsg, dict):
                            _last_mid = _last_bmsg.get("message_id")
                            if _last_mid:
                                state["_last_message_id"] = str(_last_mid)

                else:
                    # ── Synchronous tool mode (original) ──
                    result = llm_client.generate(
                        messages,
                        tools=tools_spec,
                        temperature=runtime._default_temperature(persona),
                        **runtime._get_cache_kwargs(),
                    )

                    # Consume reasoning (thinking) from tool-mode LLM call
                    _tool_reasoning = llm_client.consume_reasoning()
                    _tool_reasoning_text = "\n\n".join(
                        e.get("text", "") for e in _tool_reasoning if e.get("text")
                    ) if _tool_reasoning else ""
                    if _tool_reasoning_text:
                        state["_reasoning_text"] = _tool_reasoning_text
                    _tool_reasoning_details = llm_client.consume_reasoning_details()
                    if _tool_reasoning_details is not None:
                        state["_reasoning_details"] = _tool_reasoning_details

                    # Record usage
                    usage = llm_client.consume_usage()
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm_tool",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        # Accumulate into pulse total
                        from saiverse.model_configs import calculate_cost
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)
                        # Phase 4-e: anchor touch を LLM 成功後に移動 (旧: prepare_context 内の先行 touch)
                        runtime._touch_anchor_after_llm_call(persona, usage)

                # ── Common tool result handling (shared by streaming & sync) ──
                # Parse output_keys to determine where to store results
                output_keys_spec = getattr(node_def, "output_keys", None)
                text_key = None
                function_call_key = None
                if output_keys_spec:
                    for mapping in output_keys_spec:
                        if "text" in mapping:
                            text_key = mapping["text"]
                        if "function_call" in mapping:
                            function_call_key = mapping["function_call"]

                # Debug: log result type and keys
                LOGGER.info("[DEBUG] LLM result type='%s', has content=%s, has tool_name=%s",
                           result.get("type"), "content" in result, "tool_name" in result)

                # ── Spell loop (parallel execution per round) ──
                _pre_spell_text = result.get("content", "") if result.get("type") == "text" else ""
                _bubble1_emitted_early = _emit_bubble1_early(
                    runtime=runtime,
                    persona=persona,
                    building_id=building_id,
                    text=_pre_spell_text,
                    speak_flag=getattr(node_def, "speak", True),
                    pulse_id=state.get("_pulse_id"),
                    event_callback=event_callback,
                    node_id=getattr(node_def, "id", "llm"),
                    send_streaming_discard=False,
                )
                _spell_text, _spell_continuation, _spell_loop_count = await _run_spell_loop(
                    text=_pre_spell_text,
                    spell_enabled=_spell_enabled,
                    llm_client=llm_client,
                    runtime=runtime,
                    persona=persona,
                    building_id=building_id,
                    state=state,
                    messages=messages,
                    playbook=playbook,
                    event_callback=event_callback,
                    node_def=node_def,
                )
                if _spell_loop_count > 0:
                    result = {"type": "text", "content": _spell_text}

                    # Intent A v0.14 / Intent B v0.11 (handoff route B):
                    # speak: false nodes are internal-processing nodes. They must
                    # not flush spell-driven content to the UI or Building history
                    # — the Spell loop already routed records to the active line's
                    # storage layer ([2]/[3]/[4]) via PulseContext-aware
                    # _store_memory in P0-4. Skip emission here.
                    _node_speak_flag = getattr(node_def, "speak", True)
                    if _node_speak_flag is False:
                        LOGGER.info(
                            "[sea][spell] speak=false node — skipping _emit_say "
                            "(handoff route B); records remain in line storage layer only"
                        )
                    else:
                        # Phase 2-B-step3 (voice_tts_pipeline_streaming): single
                        # _emit_say with the full merged text (= round 1 text_before
                        # + <user_only> spell blocks + further rounds + final
                        # continuation). The old bubble1/bubble2 dual-emit is gone.
                        #
                        # If Phase 1 already emitted text_before early (= TTS warm-
                        # up while spell loop ran), strip the duplicate leading
                        # text_before from _spell_text so building history sees
                        # the same record only once. Phase 2-C will remove the
                        # Phase 1 path entirely (sub-speak handles the warm-up).
                        pulse_id = state.get("_pulse_id")
                        eff_bid = runtime._effective_building_id(persona, building_id)

                        _spell_emit_text = _spell_text
                        if _bubble1_emitted_early and _spell_emit_text.startswith(_bubble1_emitted_early):
                            _spell_emit_text = _spell_emit_text[len(_bubble1_emitted_early):].lstrip("\n")

                        _spell_msg_meta: Dict[str, Any] = {}
                        _spell_at = state.get("_activity_trace")
                        if _spell_at:
                            _spell_msg_meta["activity_trace"] = list(_spell_at)

                        if event_callback:
                            _say_event: Dict[str, Any] = {
                                "type": "say",
                                "content": _spell_emit_text,
                                "persona_id": getattr(persona, "persona_id", None),
                                "pulse_id": pulse_id,
                            }
                            if _spell_at:
                                _say_event["activity_trace"] = list(_spell_at)
                            event_callback(_say_event)

                        runtime._emit_say(persona, eff_bid, _spell_emit_text, pulse_id=pulse_id,
                                          metadata=_spell_msg_meta if _spell_msg_meta else None)
                        LOGGER.info(
                            "[sea][spell] Tool-mode: emitted merged spell text (len=%d, phase1_early=%s)",
                            len(_spell_emit_text), bool(_bubble1_emitted_early),
                        )

                if result["type"] == "tool_call":
                    LOGGER.info("[DEBUG] Entering tool_call branch")
                    # Only tool call, no text
                    if output_keys_spec:
                        # New behavior: use explicit output_keys
                        if function_call_key:
                            state[f"{function_call_key}.name"] = result["tool_name"]
                            # Store full args dict (for tool_call node dynamic execution)
                            state[f"{function_call_key}.args"] = result["tool_args"] if isinstance(result["tool_args"], dict) else {}
                            if isinstance(result["tool_args"], dict):
                                for arg_name, arg_value in result["tool_args"].items():
                                    state[f"{function_call_key}.args.{arg_name}"] = arg_value
                                    LOGGER.debug("[sea] Stored %s.args.%s = %s", function_call_key, arg_name, arg_value)
                        # Set conditional_next flags
                        state["tool_called"] = True
                        state["has_speak_content"] = False
                    else:
                        # Legacy behavior: use predefined keys
                        state["tool_called"] = True
                        state["tool_name"] = result["tool_name"]
                        state["tool_args"] = result["tool_args"]
                        state["has_speak_content"] = False
                        # Expand tool_args for legacy args_input (tool_arg_*)
                        if isinstance(result["tool_args"], dict):
                            for key, value in result["tool_args"].items():
                                state[f"tool_arg_{key}"] = value
                                LOGGER.debug("[sea] Expanded tool_arg_%s = %s", key, value)

                    # Record tool call info for message protocol (function calling)
                    _tc_id = f"tc_{uuid.uuid4().hex}"
                    state["_last_tool_call_id"] = _tc_id
                    state["_last_tool_name"] = result["tool_name"]
                    state["_last_tool_args_json"] = json.dumps(
                        result["tool_args"], ensure_ascii=False
                    ) if isinstance(result["tool_args"], dict) else "{}"
                    # Gemini thinking models require thought_signature on function call parts
                    state["_last_thought_signature"] = result.get("thought_signature")

                    # Format as JSON for logging
                    text = json.dumps({
                        "tool": result["tool_name"],
                        "args": result["tool_args"]
                    }, ensure_ascii=False)
                    LOGGER.info("[sea] Tool call detected: %s", text)

                elif result["type"] == "both":
                    LOGGER.info("[DEBUG] Entering 'both' branch (text + tool call)")
                    # Both text and tool call
                    # In streaming mode, text from text_chunks is authoritative
                    # (tool_detection content may be truncated if LLM client accumulation has issues).
                    # In sync mode, result["content"] is the only source.
                    _both_text = text if (use_tool_streaming and text) else result.get("content", "")
                    if output_keys_spec:
                        # New behavior: use explicit output_keys
                        if text_key:
                            state[text_key] = _both_text
                            LOGGER.debug("[sea] Stored %s = (text, length=%d)", text_key, len(_both_text))
                        if function_call_key:
                            state[f"{function_call_key}.name"] = result["tool_name"]
                            # Store full args dict (for tool_call node dynamic execution)
                            state[f"{function_call_key}.args"] = result["tool_args"] if isinstance(result["tool_args"], dict) else {}
                            if isinstance(result["tool_args"], dict):
                                for arg_name, arg_value in result["tool_args"].items():
                                    state[f"{function_call_key}.args.{arg_name}"] = arg_value
                                    LOGGER.debug("[sea] Stored %s.args.%s = %s", function_call_key, arg_name, arg_value)
                        # Set conditional_next flags
                        state["tool_called"] = True
                        state["has_speak_content"] = bool(text_key)
                    else:
                        # Legacy behavior: use predefined keys
                        state["tool_called"] = True
                        state["tool_name"] = result["tool_name"]
                        state["tool_args"] = result["tool_args"]
                        state["has_speak_content"] = True
                        state["speak_content"] = _both_text
                        # Expand tool_args for legacy args_input (tool_arg_*)
                        if isinstance(result["tool_args"], dict):
                            for key, value in result["tool_args"].items():
                                state[f"tool_arg_{key}"] = value
                                LOGGER.debug("[sea] Expanded tool_arg_%s = %s", key, value)

                    # Record tool call info for message protocol (function calling)
                    _tc_id = f"tc_{uuid.uuid4().hex}"
                    state["_last_tool_call_id"] = _tc_id
                    state["_last_tool_name"] = result["tool_name"]
                    state["_last_tool_args_json"] = json.dumps(
                        result["tool_args"], ensure_ascii=False
                    ) if isinstance(result["tool_args"], dict) else "{}"
                    # Gemini thinking models require thought_signature on function call parts
                    state["_last_thought_signature"] = result.get("thought_signature")

                    text = _both_text
                    LOGGER.info("[sea] Both text and tool call detected: tool=%s, text_length=%d",
                                result["tool_name"], len(text))

                else:
                    LOGGER.info("[DEBUG] Entering 'else' branch (normal text response)")
                    # Normal text response (no tool call)
                    state["tool_called"] = False

                    if output_keys_spec and text_key:
                        # New behavior: store in explicit text_key
                        state[text_key] = result["content"]
                        LOGGER.info("[sea][llm] Stored state['%s'] = %s", text_key, result["content"])
                        state["has_speak_content"] = True
                    else:
                        # Legacy behavior: no specific text storage (just in "last")
                        state["has_speak_content"] = True

                    text = result["content"]
                    # 2026-05-20: Gemini 3.x の thoughtSignature を state に保存。
                    # _assistant_msg 構築時に message トップレベルにセットされ、
                    # SAIMemory permanence layer 経由で次ターンへ echo される。
                    # 詳細は docs/intent/thought_signature_persistence.md
                    state["_last_thought_signature"] = result.get("thought_signature")

                runtime._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
            else:
                LOGGER.info("[DEBUG] Entering normal mode (no tools)")
                # Normal mode (no tools)
                state["tool_called"] = False

                # Check speak flag for streaming output
                speak_flag = getattr(node_def, "speak", None)
                streaming_enabled = _is_llm_streaming_enabled()
                LOGGER.info("[DEBUG] Streaming check: speak_flag=%s, response_schema=%s, streaming_enabled=%s, event_callback=%s",
                           speak_flag, response_schema is not None, streaming_enabled, event_callback is not None)
                use_streaming = (
                    speak_flag is True
                    and response_schema is None
                    and streaming_enabled
                    and event_callback is not None
                )

                if use_streaming:
                    LOGGER.info("[DEBUG] Using streaming generation (speak=true)")
                    # Streaming mode: yield chunks to UI (with retry for empty response)
                    max_stream_retries = 3
                    text = ""
                    cancelled_during_stream = False

                    # Pipeline Streaming: LLM streaming と並行で voice-tts に
                    # 文区切りごとに sub-speak を投げる経路。 ストリーミング応答
                    # を返す経路では常にこの方式を使う (旧 Phase 1 bubble1 早期
                    # emit は撤去済)。
                    #
                    # 仕組み:
                    # - 開始時に _emit_speak_start で placeholder + msg_id 発番
                    # - chunk 受信ごとに文区切り検出 → _emit_sub_speak (sub_seq=N)
                    # - 最初の /spell 行を検出したら sub-speak emit を停止 (spell
                    #   行は spell loop が <user_only> で wrap してから finalize
                    #   経由で送るので、 単独で voice-tts に渡してはいけない)
                    # - spell loop 完了後 (or 通常完了後) に _emit_speak_finalize
                    #   で placeholder を確定 + final hook 発火。 final_voice_text
                    #   は 「last sub-speak 以降の残テキスト」 を strip_user_only
                    #   済の形で渡す (= 重複合成を回避)
                    pipeline_msg_id: Optional[str] = runtime._emit_speak_start(
                        persona,
                        runtime._effective_building_id(persona, building_id),
                        pulse_id=state.get("_pulse_id"),
                    )
                    if not pipeline_msg_id:
                        LOGGER.error(
                            "[sea][pipeline] _emit_speak_start failed; "
                            "downstream finalize will be skipped — placeholder leak risk",
                        )
                    pipeline_sub_seq = 0

                    for stream_attempt in range(max_stream_retries):
                        stream_iter = llm_client.generate_stream(
                            messages,
                            tools=[],
                            temperature=runtime._default_temperature(persona),
                            **runtime._get_cache_kwargs(),
                        )
                        _initial_text, pipeline_sub_seq, _initial_spell_detected, _initial_cancelled = await _consume_pipeline_stream(
                            stream_iter,
                            runtime=runtime,
                            persona=persona,
                            building_id=building_id,
                            node_def=node_def,
                            state=state,
                            pipeline_msg_id=pipeline_msg_id,
                            sub_seq_start=pipeline_sub_seq,
                            cancellation_token=cancellation_token,
                            event_callback=event_callback,
                        )
                        text = _initial_text
                        if _initial_cancelled:
                            cancelled_during_stream = True
                            break  # Don't retry on cancellation

                        # Check for server-side stream interruption (e.g. 504 DEADLINE_EXCEEDED)
                        _stream_error = (
                            llm_client.consume_stream_error()
                            if hasattr(llm_client, "consume_stream_error") else None
                        )
                        if _stream_error:
                            LOGGER.warning(
                                "[sea][llm] Stream interrupted by server: code=%s status=%s — "
                                "will re-speak after storing partial response",
                                _stream_error.get("code"), _stream_error.get("status", ""),
                            )
                            state["_stream_error"] = _stream_error
                            break  # Don't retry; handle at speak level below

                        # Check for empty response
                        if text.strip():
                            break  # Got valid response

                        # Empty response - discard usage and retry
                        discarded_usage = llm_client.consume_usage()
                        LOGGER.warning(
                            "[sea][llm] Empty streaming response (attempt %d/%d). "
                            "Discarding usage (in=%d, out=%d) and retrying...",
                            stream_attempt + 1, max_stream_retries,
                            discarded_usage.input_tokens if discarded_usage else 0,
                            discarded_usage.output_tokens if discarded_usage else 0,
                        )
                    else:
                        # All retries exhausted
                        LOGGER.error(
                            "[sea][llm] Empty streaming response after %d attempts. "
                            "Proceeding with empty response.",
                            max_stream_retries
                        )

                    # Cancellation cleanup: placeholder を発番済みのまま
                    # cancellation で抜けた場合、 voice-tts 側 audio_stream が
                    # close されず、 building history の _streaming_placeholder
                    # も残り続ける。 ここで finalize して 「partial で確定 +
                    # voice-tts に is_final=True を送って stream close + wav
                    # 保存」 を強制する。 下流の spell loop / emit 経路は二重
                    # finalize しないよう pipeline_msg_id を倒しておく。
                    if cancelled_during_stream and pipeline_msg_id:
                        _cancel_eff_bid = runtime._effective_building_id(persona, building_id)
                        pipeline_sub_seq += 1
                        try:
                            runtime._emit_speak_finalize(
                                persona, _cancel_eff_bid, pipeline_msg_id, text or "",
                                pulse_id=state.get("_pulse_id"),
                                extra_metadata=None,
                                final_sub_seq=pipeline_sub_seq,
                                final_voice_text="",
                            )
                            state["_last_message_id"] = pipeline_msg_id
                        except Exception:
                            LOGGER.warning(
                                "[sea][pipeline] cancellation finalize raised; "
                                "placeholder may remain unconfirmed",
                                exc_info=True,
                            )
                        LOGGER.info(
                            "[sea][pipeline] Cancelled mid-stream: finalized placeholder "
                            "msg=%s seq=%d partial_len=%d",
                            pipeline_msg_id, pipeline_sub_seq, len(text or ""),
                        )
                        pipeline_msg_id = None

                    # Record usage (even if cancelled — tokens were consumed)
                    usage = llm_client.consume_usage()
                    LOGGER.info("[DEBUG] consume_usage returned: %s", usage)
                    llm_usage_metadata: Dict[str, Any] | None = None
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm_stream",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        LOGGER.info("[DEBUG] Usage recorded: model=%s in=%d out=%d cached=%d cache_write=%d", usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens)
                        # Build llm_usage metadata for message
                        from saiverse.model_configs import calculate_cost, get_model_display_name
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        llm_usage_metadata = {
                            "model": usage.model,
                            "model_display_name": get_model_display_name(usage.model),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cached_tokens": usage.cached_tokens,
                            "cache_write_tokens": usage.cache_write_tokens,
                            "cost_usd": cost,
                        }
                        # Accumulate into pulse total
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)
                        # Phase 4-e: anchor touch を LLM 成功後に移動 (旧: prepare_context 内の先行 touch)
                        runtime._touch_anchor_after_llm_call(persona, usage)
                    else:
                        LOGGER.warning("[DEBUG] No usage data from LLM client")

                    # Consume reasoning (thinking) from LLM — store as metadata, not in content
                    reasoning_entries = llm_client.consume_reasoning()
                    reasoning_text = "\n\n".join(
                        e.get("text", "") for e in reasoning_entries if e.get("text")
                    ) if reasoning_entries else ""
                    reasoning_details = llm_client.consume_reasoning_details()

                    # 2026-05-20: Gemini 3.x の thoughtSignature を stream 完了後に保持。
                    # ストリーム経路は最後の chunk まで読まないと signature が確定しない
                    # ため、_consume_pipeline_stream の後で consume する。
                    # 詳細は docs/intent/thought_signature_persistence.md
                    state["_last_thought_signature"] = llm_client.consume_thought_signature()
                    LOGGER.debug(
                        "[sea][llm][sig-trace] stream path: state['_last_thought_signature'] set = %s (%d bytes)",
                        "present" if state["_last_thought_signature"] else "None",
                        len(state["_last_thought_signature"]) if isinstance(state["_last_thought_signature"], (bytes, str)) else 0,
                    )

                    # ── Spell loop (parallel execution per round) ──
                    # Pipeline Streaming で sub-speak が音声合成のウォームアップを
                    # 担うので、 旧 Phase 1 (= spell loop 開始前の bubble1 早期
                    # emit) は不要。 完全撤去済。
                    #
                    # pipeline_streaming_state を渡すことで spell loop 内の 2 回目
                    # 以降の LLM 呼び出しも streaming 化される。 retry の chunk が
                    # UI に流れつつ、 文区切りで sub-speak も発火する。 helper は
                    # state dict を in-place mutate して sub_seq を更新する。
                    _pipeline_spell_state: Optional[dict] = None
                    if pipeline_msg_id:
                        _pipeline_spell_state = {
                            "msg_id": pipeline_msg_id,
                            "sub_seq": pipeline_sub_seq,
                            "cancellation_token": cancellation_token,
                        }

                    text, _continuation_ns, _spell_loop_count_ns = await _run_spell_loop(
                        text=text,
                        spell_enabled=_spell_enabled,
                        llm_client=llm_client,
                        runtime=runtime,
                        persona=persona,
                        building_id=building_id,
                        state=state,
                        messages=messages,
                        playbook=playbook,
                        event_callback=event_callback,
                        node_def=node_def,
                        pipeline_streaming_state=_pipeline_spell_state,
                    )

                    if _pipeline_spell_state is not None:
                        pipeline_sub_seq = int(_pipeline_spell_state.get("sub_seq", pipeline_sub_seq) or pipeline_sub_seq)

                    if _spell_loop_count_ns > 0:
                        # Intent A v0.14 / Intent B v0.11 (handoff route B):
                        # speak: false nodes skip the _emit_say path entirely.
                        # The Spell loop already routed records to the active
                        # line's storage layer; emitting a "say" event would
                        # surface internal-processing content as a persona
                        # utterance.
                        _node_speak_flag_ns = getattr(node_def, "speak", True)
                        if _node_speak_flag_ns is False:
                            LOGGER.info(
                                "[sea][spell] speak=false node — skipping Normal-stream _emit_say "
                                "(handoff route B); records remain in line storage layer only"
                            )
                            # speak=false の場合でも placeholder を放置すると
                            # _streaming_placeholder=True で残り続けるので、
                            # voice-tts に空文字で finalize して close する。
                            if pipeline_msg_id:
                                pipeline_sub_seq += 1
                                runtime._emit_speak_finalize(
                                    persona,
                                    runtime._effective_building_id(persona, building_id),
                                    pipeline_msg_id, text,
                                    pulse_id=state.get("_pulse_id"),
                                    extra_metadata=None,
                                    final_sub_seq=pipeline_sub_seq,
                                    final_voice_text="",
                                )
                        else:
                            pulse_id = state.get("_pulse_id")
                            eff_bid = runtime._effective_building_id(persona, building_id)

                            _spell_msg_meta_ns: Dict[str, Any] = {}
                            if llm_usage_metadata:
                                _spell_msg_meta_ns["llm_usage"] = llm_usage_metadata
                            _spell_at_ns = state.get("_activity_trace")
                            if _spell_at_ns:
                                _spell_msg_meta_ns["activity_trace"] = list(_spell_at_ns)
                            accumulator = state.get("_pulse_usage_accumulator")
                            if accumulator:
                                _spell_msg_meta_ns["llm_usage_total"] = dict(accumulator)

                            # Pipeline Streaming finalize: placeholder を全文 (= text、
                            # merged form) で確定。 voice-tts は sub-speak 経由で
                            # 全テキストを既に受け取っており、 finalize hook では
                            # ``final_voice_text=""`` で 「stream close + wav 保存」
                            # のみ依頼する (= 残テキストを最終処理で渡さない設計)。

                            if event_callback:
                                # spell 入り応答ではストリーミング中の表示
                                # (= raw text + 生の /spell 行が積み上がった
                                # bubble) と完了後の整形済み bubble (= merged
                                # text、 <user_only> wrap 済) で content が
                                # 完全一致しないため、 frontend が新規 bubble
                                # を追加してしまい 2 つ並ぶ。 ``streaming_discard``
                                # を先に送って streaming bubble を捨ててから
                                # ``say`` で整形済み 1 件だけを残す。
                                event_callback({
                                    "type": "streaming_discard",
                                    "persona_id": getattr(persona, "persona_id", None),
                                    "node_id": getattr(node_def, "id", "llm"),
                                    "pulse_id": pulse_id,
                                })
                                _say_event_ns: Dict[str, Any] = {
                                    "type": "say",
                                    "content": text,
                                    "persona_id": getattr(persona, "persona_id", None),
                                    "pulse_id": pulse_id,
                                }
                                if _spell_at_ns:
                                    _say_event_ns["activity_trace"] = list(_spell_at_ns)
                                if _spell_msg_meta_ns:
                                    _say_event_ns["metadata"] = _spell_msg_meta_ns
                                event_callback(_say_event_ns)

                            if pipeline_msg_id:
                                pipeline_sub_seq += 1
                                runtime._emit_speak_finalize(
                                    persona, eff_bid, pipeline_msg_id, text,
                                    pulse_id=pulse_id,
                                    extra_metadata=_spell_msg_meta_ns if _spell_msg_meta_ns else None,
                                    final_sub_seq=pipeline_sub_seq,
                                    final_voice_text="",
                                )
                                state["_last_message_id"] = pipeline_msg_id
                                LOGGER.info(
                                    "[sea][pipeline] Normal-stream spell+finalize: msg=%s final_seq=%d",
                                    pipeline_msg_id, pipeline_sub_seq,
                                )

                        # state["last"] が後段の memorize ノードで SAIMemory に
                        # 保存される。 spell が走った時、 ここで text を merged
                        # 全文のままにすると、 ペルソナ履歴経由で記録される 1
                        # 件目と内容が完全一致する重複レコードが SAIMemory に
                        # 残ってしまう。 最終発言部分だけ (= continuation) に
                        # 置き換えて、 SAIMemory には 「最終発言のみのレコード」
                        # が単独で残るようにする (= 旧コード相当)。
                        text = _continuation_ns
                    else:
                        # No spells — normal completion path
                        # Resolve metadata_key for speak (e.g., media attachments from tool execution)
                        _speak_metadata_key = getattr(node_def, "metadata_key", None)
                        _speak_base_metadata = state.get(_speak_metadata_key) if _speak_metadata_key else None

                        # Send completion event with reasoning and metadata
                        completion_event: Dict[str, Any] = {
                            "type": "streaming_complete",
                            "persona_id": getattr(persona, "persona_id", None),
                            "node_id": getattr(node_def, "id", "llm"),
                            "pulse_id": state.get("_pulse_id"),
                        }
                        if reasoning_text:
                            completion_event["reasoning"] = reasoning_text
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            completion_event["metadata"] = _speak_base_metadata
                        event_callback(completion_event)
                        LOGGER.debug(
                            "[sea][llm][diag] streaming_complete emitted (no-spell path): persona=%s pulse=%s",
                            getattr(persona, "persona_id", None),
                            state.get("_pulse_id"),
                        )

                        # Record to Building history with usage metadata (include pulse total)
                        pulse_id = state.get("_pulse_id")
                        msg_metadata: Dict[str, Any] = {}
                        # Merge base metadata first (e.g., media from tool execution)
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            msg_metadata.update(_speak_base_metadata)
                        if llm_usage_metadata:
                            msg_metadata["llm_usage"] = llm_usage_metadata
                        if reasoning_text:
                            msg_metadata["reasoning"] = reasoning_text
                        if reasoning_details is not None:
                            msg_metadata["reasoning_details"] = reasoning_details
                        _at_stream = state.get("_activity_trace")
                        if _at_stream:
                            msg_metadata["activity_trace"] = list(_at_stream)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            msg_metadata["llm_usage_total"] = dict(accumulator)
                        eff_bid = runtime._effective_building_id(persona, building_id)

                        if pipeline_msg_id:
                            # Pipeline Streaming: placeholder を text 全文で finalize。
                            # voice-tts は sub-speak 経由で全テキストを既に受け取って
                            # おり、 finalize hook では ``final_voice_text=""`` で
                            # 「stream close + wav 保存」 のみ依頼する。
                            pipeline_sub_seq += 1
                            runtime._emit_speak_finalize(
                                persona, eff_bid, pipeline_msg_id, text,
                                pulse_id=pulse_id,
                                extra_metadata=msg_metadata if msg_metadata else None,
                                final_sub_seq=pipeline_sub_seq,
                                final_voice_text="",
                            )
                            state["_last_message_id"] = pipeline_msg_id
                            LOGGER.info(
                                "[sea][pipeline] Normal-stream finalize: msg=%s final_seq=%d",
                                pipeline_msg_id, pipeline_sub_seq,
                            )
                        else:
                            # Defensive fallback: _emit_speak_start に失敗して
                            # placeholder を作れなかったケース。 sub-speak は出てない
                            # ので _emit_say で 1 回 emit し直す (= 履歴を失わない)。
                            LOGGER.warning(
                                "[sea][pipeline] no placeholder msg_id — falling back to _emit_say",
                            )
                            _last_bmsg = runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
                            if isinstance(_last_bmsg, dict):
                                _last_mid = _last_bmsg.get("message_id")
                                if _last_mid:
                                    state["_last_message_id"] = str(_last_mid)

                        # ── 504 DEADLINE_EXCEEDED: re-speak after partial response ──
                        _stream_err = state.pop("_stream_error", None)
                        if _stream_err and text.strip():
                            _err_code = _stream_err.get("code", 504)
                            _err_msg = _stream_err.get("message", "Deadline expired before operation could complete.")
                            LOGGER.warning(
                                "[sea][llm] Triggering re-speak after 504 stream interruption for persona=%s",
                                getattr(persona, "persona_id", None),
                            )

                            # 1. Emit info event to frontend
                            if event_callback:
                                event_callback({
                                    "type": "info",
                                    "content": (
                                        f"ℹ️ メッセージの生成が予期せず終了しました。"
                                        f"({_err_code} {_err_msg})\n"
                                        "ペルソナが再発言を行います。"
                                    ),
                                    "persona_id": getattr(persona, "persona_id", None),
                                })

                            # 2. Store partial to SAIMemory now (before continuation, to preserve order)
                            runtime._store_memory(
                                persona, text,
                                role="assistant",
                                tags=["conversation"],
                                pulse_id=state.get("_pulse_id"),
                                # 2026-05-20: thought_signature 永続化 (stream 中断時の partial 経路)
                                thought_signature=state.get("_last_thought_signature"),
                            )

                            # 3. Build continuation messages:
                            #    existing context + assistant(partial) + user(<system>prompt</system>)
                            _cont_messages = list(messages) + [
                                {"role": "assistant", "content": text},
                                {"role": "user", "content": (
                                    "<system>あなたの応答がサーバータイムアウトにより途中で終了しました。"
                                    "続きがあれば引き続き発言してください。</system>"
                                )},
                            ]

                            # 4. Stream continuation
                            _cont_chunks: list[str] = []
                            try:
                                _cont_iter = llm_client.generate_stream(
                                    _cont_messages,
                                    tools=[],
                                    temperature=runtime._default_temperature(persona),
                                    **runtime._get_cache_kwargs(),
                                )
                                for _cont_chunk in _cont_iter:
                                    if isinstance(_cont_chunk, dict):
                                        continue
                                    _cont_chunks.append(_cont_chunk)
                                    if event_callback:
                                        event_callback({
                                            "type": "streaming_chunk",
                                            "content": _cont_chunk,
                                            "persona_id": getattr(persona, "persona_id", None),
                                            "node_id": getattr(node_def, "id", "llm"),
                                            "pulse_id": state.get("_pulse_id"),
                                        })
                            finally:
                                if hasattr(_cont_iter, "close"):
                                    _cont_iter.close()

                            _cont_text = "".join(_cont_chunks)

                            if _cont_text.strip():
                                # Send streaming_complete for continuation
                                if event_callback:
                                    event_callback({
                                        "type": "streaming_complete",
                                        "persona_id": getattr(persona, "persona_id", None),
                                        "node_id": getattr(node_def, "id", "llm"),
                                        "pulse_id": state.get("_pulse_id"),
                                    })
                                # Store continuation to building history
                                runtime._emit_say(persona, eff_bid, _cont_text, pulse_id=pulse_id)
                                # state["speak_content"] = continuation so compose/memorize node
                                # stores it to SAIMemory (partial was stored directly above)
                                text = _cont_text
                            else:
                                LOGGER.warning("[sea][llm] Re-speak after 504 returned empty response")

                    # Store reasoning in state for downstream speak/say nodes
                    if reasoning_text:
                        state["_reasoning_text"] = reasoning_text
                    if reasoning_details is not None:
                        state["_reasoning_details"] = reasoning_details
                else:
                    # Non-streaming mode
                    LOGGER.debug("[sea][llm] Calling llm_client.generate() with response_schema=%s", response_schema is not None)
                    text = llm_client.generate(
                        messages,
                        tools=[],
                        temperature=runtime._default_temperature(persona),
                        response_schema=response_schema,
                        **runtime._get_cache_kwargs(),
                    )
                    LOGGER.debug("[sea][llm] llm_client.generate() returned: type=%s, len=%s, repr=%s", type(text).__name__, len(text) if isinstance(text, str) else "(not str)", repr(text)[:200] if isinstance(text, str) else text)

                    # Record usage
                    usage = llm_client.consume_usage()
                    llm_usage_metadata: Dict[str, Any] | None = None
                    if usage:
                        get_usage_tracker().record_usage(
                            model_id=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=usage.cached_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            cache_ttl=usage.cache_ttl,
                            persona_id=getattr(persona, "persona_id", None),
                            building_id=building_id,
                            node_type="llm",
                            playbook_name=playbook.name,
                            category="persona_speak",
                        )
                        # Build llm_usage metadata for message
                        from saiverse.model_configs import calculate_cost, get_model_display_name
                        cost = calculate_cost(usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cache_write_tokens, cache_ttl=usage.cache_ttl)
                        llm_usage_metadata = {
                            "model": usage.model,
                            "model_display_name": get_model_display_name(usage.model),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cached_tokens": usage.cached_tokens,
                            "cache_write_tokens": usage.cache_write_tokens,
                            "cost_usd": cost,
                        }
                        # Accumulate into pulse total
                        runtime._accumulate_usage(state, usage.model, usage.input_tokens, usage.output_tokens, cost, usage.cached_tokens, usage.cache_write_tokens)
                        # Phase 4-e: anchor touch を LLM 成功後に移動 (旧: prepare_context 内の先行 touch)
                        runtime._touch_anchor_after_llm_call(persona, usage)

                    # Consume reasoning (thinking) from LLM — store as metadata
                    reasoning_entries = llm_client.consume_reasoning()
                    reasoning_text = "\n\n".join(
                        e.get("text", "") for e in reasoning_entries if e.get("text")
                    ) if reasoning_entries else ""
                    reasoning_details = llm_client.consume_reasoning_details()

                    # ── Spell loop (parallel execution per round) ──
                    _bubble1_emitted_early_sync = ""
                    if isinstance(text, str):
                        # Normal text mode - run spell processing
                        _bubble1_emitted_early_sync = _emit_bubble1_early(
                            runtime=runtime,
                            persona=persona,
                            building_id=building_id,
                            text=text,
                            speak_flag=speak_flag,
                            pulse_id=state.get("_pulse_id"),
                            event_callback=event_callback,
                            node_id=getattr(node_def, "id", "llm"),
                            send_streaming_discard=False,
                        )
                        text, _continuation_sync, _spell_loop_count_sync = await _run_spell_loop(
                            text=text,
                            spell_enabled=_spell_enabled,
                            llm_client=llm_client,
                            runtime=runtime,
                            persona=persona,
                            building_id=building_id,
                            state=state,
                            messages=messages,
                            playbook=playbook,
                            event_callback=event_callback,
                            node_def=node_def,
                        )
                    else:
                        # text is dict (from structured output) - skip spell processing
                        LOGGER.debug("[sea][llm] text is dict (structured output), skipping spell processing")
                        _continuation_sync = text if isinstance(text, str) else ""
                        _spell_loop_count_sync = 0

                    if _spell_loop_count_sync > 0 and isinstance(text, str):
                        # Phase 2-B-step3 (voice_tts_pipeline_streaming): text now
                        # holds the full merged spell output (round 1 text_before
                        # + <user_only> blocks + further rounds + final
                        # continuation). The downstream ``if speak_flag is True``
                        # block emits it via a single _emit_say.
                        #
                        # If Phase 1 already emitted text_before early, slice it
                        # off so the downstream emit doesn't write the same prefix
                        # twice (Phase 2-C will remove this transitional split).
                        if _bubble1_emitted_early_sync and text.startswith(_bubble1_emitted_early_sync):
                            text = text[len(_bubble1_emitted_early_sync):].lstrip("\n")

                    # If speak=true but streaming disabled, send complete text and record to Building history
                    LOGGER.info("[DEBUG] speak_flag=%s, event_callback=%s, text_len=%d",
                               speak_flag, event_callback is not None, len(text) if text else 0)
                    if speak_flag is True:
                        pulse_id = state.get("_pulse_id")
                        # Resolve metadata_key for speak (e.g., media attachments from tool execution)
                        _speak_metadata_key2 = getattr(node_def, "metadata_key", None)
                        _speak_base_metadata2 = state.get(_speak_metadata_key2) if _speak_metadata_key2 else None
                        msg_metadata: Dict[str, Any] = {}
                        # Merge base metadata first (e.g., media from tool execution)
                        if _speak_base_metadata2 and isinstance(_speak_base_metadata2, dict):
                            msg_metadata.update(_speak_base_metadata2)
                        if llm_usage_metadata:
                            msg_metadata["llm_usage"] = llm_usage_metadata
                        if reasoning_text:
                            msg_metadata["reasoning"] = reasoning_text
                        if reasoning_details is not None:
                            msg_metadata["reasoning_details"] = reasoning_details
                        _at_speak = state.get("_activity_trace")
                        if _at_speak:
                            msg_metadata["activity_trace"] = list(_at_speak)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            msg_metadata["llm_usage_total"] = dict(accumulator)
                        eff_bid = runtime._effective_building_id(persona, building_id)
                        _last_bmsg = runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
                        # 後続ツールが新しい persona_context 配下でも
                        # 最新の message_id を参照できるよう state に残す。
                        if isinstance(_last_bmsg, dict):
                            _last_mid = _last_bmsg.get("message_id")
                            if _last_mid:
                                state["_last_message_id"] = str(_last_mid)
                        if event_callback is not None:
                            LOGGER.info("[DEBUG] Sending 'say' event with content: %s", text[:100] if text else "(empty)")
                            say_event: Dict[str, Any] = {
                                "type": "say",
                                "content": text,
                                "persona_id": getattr(persona, "persona_id", None),
                                "pulse_id": pulse_id,
                            }
                            if reasoning_text:
                                say_event["reasoning"] = reasoning_text
                            if _at_speak:
                                say_event["activity_trace"] = list(_at_speak)
                            if msg_metadata:
                                say_event["metadata"] = msg_metadata
                            event_callback(say_event)

                    # Store remaining reasoning for say/speak node (non-speak path)
                    if reasoning_text:
                        state["_reasoning_text"] = reasoning_text
                    if reasoning_details is not None:
                        state["_reasoning_details"] = reasoning_details

                runtime._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
                schema_consumed = runtime._process_structured_output(node_def, text, state)

                # Set has_speak_content based on schema_consumed
                # If structured output was consumed, we need to set this flag
                # Otherwise, it's already set in the tool handling code above
                if schema_consumed:
                    # Structured output means we have usable data, set flag to True
                    # This allows conditional_next to proceed correctly
                    state["has_speak_content"] = True

                # If output_key is specified but no response_schema, store the raw text
                if not schema_consumed:
                    output_key = getattr(node_def, "output_key", None)
                    if output_key:
                        state[output_key] = text
                        LOGGER.info("[sea][llm] Stored plain text to state['%s'] = %s", output_key, text)

                # Process output_keys even in normal mode (no tools)
                output_keys_spec = getattr(node_def, "output_keys", None)
                if output_keys_spec:
                    for mapping in output_keys_spec:
                        if "text" in mapping:
                            text_key = mapping["text"]
                            state[text_key] = text
                            LOGGER.info("[sea][llm] (normal mode) Stored state['%s'] = %s", text_key, text)
                            state["has_speak_content"] = True
                            break
        except LLMError:
            # Propagate LLM errors to the caller for proper handling
            raise
        except Exception as exc:
            LOGGER.error("SEA LangGraph LLM failed: %s: %s", type(exc).__name__, exc)
            # Convert to LLMError so it propagates to the frontend
            raise LLMError(
                f"LLM node failed: {type(exc).__name__}: {exc}",
                original_error=exc,
            ) from exc
        state["last"] = text
        # Structured output may return a dict; serialise to JSON string
        # so that subsequent LLM calls receive valid message content.
        _msg_content = json.dumps(text, ensure_ascii=False) if isinstance(text, dict) else text

        # When tool call detected, create proper function-calling assistant message
        if state.get("tool_called") and state.get("_last_tool_call_id"):
            _tc_speak = _msg_content if state.get("has_speak_content") else ""
            _tc_entry: Dict[str, Any] = {
                "id": state["_last_tool_call_id"],
                "type": "function",
                "function": {
                    "name": state.get("_last_tool_name", ""),
                    "arguments": state.get("_last_tool_args_json", "{}"),
                },
            }
            # Gemini thinking models require thought_signature echoed back
            _thought_sig = state.get("_last_thought_signature")
            if _thought_sig:
                _tc_entry["thought_signature"] = _thought_sig
            _assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": _tc_speak,
                "tool_calls": [_tc_entry],
            }
            state["_messages"] = messages + [_assistant_msg]
            LOGGER.info("[sea][llm] Appended assistant message with tool_calls (id=%s, tool=%s)",
                       state["_last_tool_call_id"], state.get("_last_tool_name"))
        else:
            # 2026-05-20: text-only assistant message に thought_signature を乗せる。
            # SAIMemory adapter._append_message が message.get("thought_signature")
            # を読み取り、専用カラムへ永続化する。
            _text_assistant_msg: Dict[str, Any] = {"role": "assistant", "content": _msg_content}
            _text_thought_sig = state.get("_last_thought_signature")
            if _text_thought_sig:
                _text_assistant_msg["thought_signature"] = _text_thought_sig
            state["_messages"] = messages + [_text_assistant_msg]

        # Append LLM interaction to PulseContext (replaces _intermediate_msgs)
        _pulse_ctx = state.get("_pulse_context")
        if _pulse_ctx:
            from sea.pulse_context import PulseLogEntry
            # Record the prompt (user message)
            if prompt:
                _pulse_ctx.append(PulseLogEntry(
                    role="user", content=prompt,
                    node_id=node_id, playbook_name=playbook.name))
            # Record the assistant response (with optional tool_calls)
            _tc_list = None
            if state.get("tool_called") and state.get("_last_tool_call_id"):
                _tc_entry_pc: Dict[str, Any] = {
                    "id": state["_last_tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": state.get("_last_tool_name", ""),
                        "arguments": state.get("_last_tool_args_json", "{}"),
                    },
                }
                _ts_pc = state.get("_last_thought_signature")
                if _ts_pc:
                    _tc_entry_pc["thought_signature"] = _ts_pc
                _tc_list = [_tc_entry_pc]
            # speak: false ノード (要約ノード等) でも実際の応答テキストはあるので
            # 空文字列ではなく実テキストを記録する。空にする旧挙動はおそらく過去の手癖で、
            # 後段で "空 assistant" として messages に流入する原因になっていた
            # (まはー指摘 2026-04-28)。
            _pulse_ctx.append(PulseLogEntry(
                role="assistant",
                content=_msg_content,
                node_id=node_id, playbook_name=playbook.name,
                tool_calls=_tc_list,
                important=getattr(node_def, "important", False) or False))

        # Trace: log prompt→response (truncation handled by log_sea_trace)
        _prompt_str = prompt or "(no prompt)"
        if schema_consumed:
            _output_key = getattr(node_def, "output_key", None) or node_id
            _out_val = state.get(_output_key, text)
            if isinstance(_out_val, dict):
                import json as _json
                _resp_str = _json.dumps(_out_val, ensure_ascii=False, default=str)
            else:
                _resp_str = str(_out_val)
            log_sea_trace(playbook.name, node_id, "LLM", f"prompt=\"{_prompt_str}\" → {_resp_str}")
        else:
            _resp_str = str(text) if text else "(empty)"
            log_sea_trace(playbook.name, node_id, "LLM", f"prompt=\"{_prompt_str}\" → \"{_resp_str}\"")

        # Handle memorize option - save prompt and response to SAIMemory
        memorize_config = getattr(node_def, "memorize", None)
        LOGGER.debug("[_lg_llm_node] node=%s memorize_config=%s type=%s schema_consumed=%s",
                   getattr(node_def, "id", "?"), memorize_config, type(memorize_config), schema_consumed)
        if memorize_config:
            pulse_id = state.get("_pulse_id")
            pulse_context = state.get("_pulse_context")
            # Parse memorize config - can be True or {"tags": [...], "scope": ..., "line_role": ...}
            if isinstance(memorize_config, dict):
                memorize_tags = memorize_config.get("tags", [])
                # Phase 1.3: meta-judgment ノードが分岐ターンを scope='discardable' で
                # 保存するための明示指定経路。memorize.scope を渡すと _store_memory に
                # そのまま転送される (None なら DB の DEFAULT 'committed' に従う)。
                memorize_scope = memorize_config.get("scope")
                # 同じく line_role を上書きできるようにする (既定は LineFrame 由来)。
                memorize_line_role = memorize_config.get("line_role")
            else:
                memorize_tags = []
                memorize_scope = None
                memorize_line_role = None

            # Intent A v0.14 / Intent B v0.11 (handoff route C):
            # Skip the legacy "save prompt as user role" path. The action template
            # (`prompt`) used to be persisted as a standalone user message, which
            # mixed it with real user utterances on the persona's timeline.
            # Instead, attach it to the assistant response via the
            # `paired_action_text` column so post-hoc inspection ("why did this
            # assistant turn happen?") still works without polluting the
            # conversation log.
            _memorize_ok = True

            # Save response (assistant role) — paired with the prompt that
            # produced it, so the action template lives alongside the response
            # rather than as a separate fake-user turn.
            if text and text != "(error in llm node)":
                # If structured output was consumed, format as JSON string for memory
                content_to_save = text
                if schema_consumed and isinstance(text, dict):
                    content_to_save = json.dumps(text, ensure_ascii=False, indent=2)
                    LOGGER.debug("[sea][llm] Structured output formatted as JSON for memory")

                # Build metadata for memorize (reasoning text + reasoning_details for multi-turn)
                _memorize_metadata: Dict[str, Any] = {}
                _mem_reasoning = state.get("_reasoning_text", "")
                if _mem_reasoning:
                    _memorize_metadata["reasoning"] = _mem_reasoning
                _mem_rd = state.get("_reasoning_details")
                if _mem_rd is not None:
                    _memorize_metadata["reasoning_details"] = _mem_rd

                _memorize_sig = state.get("_last_thought_signature")
                LOGGER.debug(
                    "[sea][llm][sig-trace] memorize call: thought_signature = %s (%d bytes)",
                    "present" if _memorize_sig else "None",
                    len(_memorize_sig) if isinstance(_memorize_sig, (bytes, str)) else 0,
                )
                stored_message_id = runtime._store_memory(
                    persona,
                    content_to_save,
                    role="assistant",
                    tags=list(memorize_tags),
                    pulse_id=pulse_id,
                    metadata=_memorize_metadata if _memorize_metadata else None,
                    playbook_name=playbook.name,
                    pulse_context=pulse_context,
                    paired_action_text=prompt,
                    scope=memorize_scope,
                    line_role=memorize_line_role,
                    # 2026-05-20: Gemini 3.x の thoughtSignature を永続化。state には
                    # gemini.py の text/stream 経路から伝搬済み (LLM ノード処理内)。
                    thought_signature=_memorize_sig,
                    return_message_id=True,
                )
                if not stored_message_id:
                    _memorize_ok = False
                else:
                    LOGGER.debug(
                        "[sea][llm] Memorized response (assistant) with paired_action_text len=%s scope=%s",
                        len(prompt) if prompt else 0, memorize_scope,
                    )
                    # メタ判断ターンの scope='discardable' → 'committed' 昇格は
                    # TrackManager の状態遷移 hook 経由で行う (saiverse_manager.py
                    # 内の hook が pulse_id ベースで pulse 内の line_role='meta_judgment'
                    # AND scope='discardable' を検索して UPDATE する)。
                    # ここでは何もしない: 保存は scope='discardable' のままで完了し、
                    # その Pulse 内で Track 状態遷移が起きれば後で hook が拾う。

            if not _memorize_ok and event_callback:
                event_callback({"type": "warning", "content": "記憶の保存に失敗しました。会話内容が記録されていない可能性があります。", "warning_code": "memorize_failed", "display": "toast"})

            # Activity trace: record LLM memorize
            if not playbook.name.startswith(("meta_", "sub_")):
                pb_display = playbook.display_name or playbook.name
                node_label = getattr(node_def, "label", None) or node_id
                _at = state.get("_activity_trace")
                if isinstance(_at, list):
                    _at.append({"action": "memorize", "name": node_label, "playbook": pb_display})
                if event_callback:
                    event_callback({
                        "type": "activity", "action": "memorize", "name": node_label,
                        "playbook": pb_display, "status": "completed",
                        "persona_id": getattr(persona, "persona_id", None),
                        "persona_name": getattr(persona, "persona_name", None),
                        "pulse_id": state.get("_pulse_id"),
                    })
                    LOGGER.debug(
                        "[sea][diag] activity emitted (llm-memorize, meta/sub guarded): name=%s playbook=%s persona=%s pulse=%s",
                        node_label, pb_display,
                        getattr(persona, "persona_id", None),
                        state.get("_pulse_id"),
                    )

        # Important flag: dual-write to messages (long-term memory) if not already memorized
        _is_important = getattr(node_def, "important", False)
        if _is_important and not memorize_config and text and text != "(error in llm node)":
            pulse_id = state.get("_pulse_id")
            content_to_save = text
            if schema_consumed and isinstance(text, dict):
                content_to_save = json.dumps(text, ensure_ascii=False, indent=2)
            if not runtime._store_memory(
                persona, content_to_save,
                role="assistant",
                tags=["conversation"],
                pulse_id=pulse_id,
                playbook_name=playbook.name,
                # 2026-05-20: thought_signature 永続化 (important dual-write 経路)
                thought_signature=state.get("_last_thought_signature"),
            ):
                LOGGER.warning("[sea][llm] Important dual-write failed for node %s", node_id)

        # Debug: log speak_content at end of LLM node
        speak_content = state.get("speak_content", "")
        LOGGER.info("[DEBUG] LLM node end: state['speak_content'] = '%s'", speak_content)

        # Note: output_mapping in node definition handles state variable assignment
        # No special handling needed here anymore
        return state

    # Wrap node in persona_context so LLM clients can read the active
    # persona via tools.context.get_active_persona_id().
    # Without this wrap, only spell/tool execution paths set the context
    # (see _execute_handy_tool_inline / _run_spell_tool_async), and the
    # LLM call itself runs with persona_id == None — which causes e.g.
    # LlamaCachedClient to collapse every persona's slot cache into a
    # single "unknown__<model>.bin" file.
    async def node_with_persona_context(state: dict):
        from pathlib import Path
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return await node(state)
        persona_log_path = getattr(persona, "persona_log_path", None)
        persona_dir = persona_log_path.parent if persona_log_path else Path.cwd()
        manager_ref = getattr(persona, "manager_ref", None)
        with persona_context(
            persona_id, persona_dir, manager_ref,
            playbook_name=playbook.name,
        ):
            return await node(state)

    return node_with_persona_context
