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
from tools.context import persona_context

LOGGER = logging.getLogger(__name__)

# ── Spell system (text-based tool invocation) ──

_MAX_SPELL_LOOPS = int(os.getenv("SAIVERSE_SPELL_MAX_ROUNDS", "3"))

# Canonical form: /spell name='tool' args={...}
_SPELL_PATTERN = re.compile(
    r"^/spell\s+name='([^']+)'\s+args=(.+)$",
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


def _parse_spell_args(args_raw: str) -> Optional[dict]:
    """Parse spell args string (Python dict or JSON). Returns dict or None."""
    try:
        result = ast.literal_eval(args_raw)
    except (ValueError, SyntaxError):
        try:
            result = json.loads(args_raw)
        except json.JSONDecodeError:
            LOGGER.warning("[sea][spell] Failed to parse args: %s", args_raw)
            return None
    if not isinstance(result, dict):
        LOGGER.warning("[sea][spell] Args is not a dict: %s", type(result))
        return None
    return result


def _parse_fuzzy_spell_args(args_raw: str) -> Optional[dict]:
    """Parse informal key=value... spell args into a dict.

    Handles single/double-quoted values, dict literals, and bare words.
    Falls back to _parse_spell_args for standard dict/JSON forms.
    """
    result = _parse_spell_args(args_raw)
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


def _build_spell_details_html(tool_name: str, tool_args: dict, display_name: str, result_str: str = "") -> str:
    """Build a styled ``<details>`` HTML block for spell UI display."""
    args_str = str(tool_args)
    # Escape HTML in result to prevent injection
    result_escaped = (
        result_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if result_str else ""
    )
    result_section = (
        f'<div class="spellResultLabel">Result:</div>'
        f'<div class="spellResult">{result_escaped}</div>'
        if result_escaped else ""
    )
    return (
        f'<details class="spellBlock">'
        f'<summary class="spellSummary">'
        f'<span class="spellIcon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        f'<path d="M12 2L15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2z"/>'
        f'</svg></span>'
        f'<span>{display_name}</span>'
        f'</summary>'
        f'<div class="spellContent">'
        f'<div class="spellParams"><code>{args_str}</code></div>'
        f'{result_section}'
        f'</div>'
        f'</details>'
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
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback):
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
) -> str:
    """Execute a single spell tool in a thread executor. Returns result string."""
    from pathlib import Path

    tool_func = TOOL_REGISTRY.get(tool_name)
    if not tool_func:
        result_str = f"Spell '{tool_name}' not found in registry"
        LOGGER.error("[sea][spell] %s", result_str)
        return result_str

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

        def _run():
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback):
                return tool_func(**tool_args)

        if inspect.iscoroutinefunction(tool_func):
            with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook_name, auto_mode=False, event_callback=event_callback):
                raw_result = await tool_func(**tool_args)
        else:
            raw_result = await asyncio.get_event_loop().run_in_executor(None, _run)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
        result_str = str(raw_result)
        LOGGER.info("[sea][spell] Executed %s → %s", tool_name, result_str[:200])
    except Exception as exc:
        result_str = f"Spell error ({tool_name}): {type(exc).__name__}: {exc}"
        LOGGER.exception("[sea][spell] %s failed", tool_name)

    return result_str


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
) -> Tuple[str, List[Tuple[str, str]], int]:
    """Execute the spell loop with parallel spell execution per LLM round.

    Each round: find ALL /spell lines → execute in parallel → re-invoke LLM once.
    Sequential rounds handle dependency chains (result of round N used in round N+1).

    Returns ``(final_text, details_blocks, loop_count)``.
    ``details_blocks`` is a list of ``(text_before, html_details)`` pairs where
    only the first entry of each round carries the text_before prefix.
    """
    from sea.pulse_context import PulseLogEntry

    if not spell_enabled or not text:
        return text, [], 0

    loop_count = 0
    details_blocks: List[Tuple[str, str]] = []

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

            # Execute all spells in parallel
            results: List[str] = list(await asyncio.gather(*[
                _run_spell_tool_async(name, args, persona, state, playbook.name, event_callback)
                for name, args, _, _ in valid_spells
            ]))

            # All spell results in one user message (reduces per-result message overhead)
            combined_results = "\n".join(
                f"[Spell Result: {name}]\n{result}"
                for (name, _, _, _), result in zip(valid_spells, results)
            )
            messages.append({"role": "user", "content": f"<system>{combined_results}</system>"})

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

            # Build UI details blocks (first spell carries text_before; others get "")
            for i, ((name, args, _, _), result) in enumerate(zip(valid_spells, results)):
                schema = SPELL_TOOL_SCHEMAS.get(name)
                display = (schema.spell_display_name if schema else "") or name
                details_blocks.append((
                    text_before if i == 0 else "",
                    _build_spell_details_html(name, args, display, result),
                ))

            # Re-invoke LLM once for the entire round
            LOGGER.info("[sea][spell] Re-invoking LLM after round %d (%d spell(s))", loop_count, len(valid_spells))
            retry_result = llm_client.generate(
                messages,
                tools=None,
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

            if isinstance(retry_result, dict):
                text = retry_result.get("content", "")
            elif isinstance(retry_result, str):
                text = retry_result
            else:
                text = ""

            LOGGER.info("[sea][spell] After round %d: has_more_spells=%s",
                        loop_count, bool(_SPELL_PATTERN.search(text)))

        LOGGER.info("[sea][spell] Completed %d round(s), %d total spell(s)",
                    loop_count, len(details_blocks))
        return text, details_blocks, loop_count
    except Exception as exc:
        # Any unhandled error in the spell pipeline: log with traceback,
        # inject a system-visible error note for the next LLM turn, and
        # return the original text so the caller can still save it.
        LOGGER.exception(
            "[sea][spell] spell loop fatal error after %d round(s); "
            "preserving original message, skipping remaining spells",
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
        return text, details_blocks, loop_count


def lg_llm_node(runtime, node_def: Any, persona: Any, building_id: str, playbook: PlaybookSchema, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
    async def node(state: dict):
        # Check for cancellation at start of node
        cancellation_token = state.get("_cancellation_token")
        if cancellation_token:
            cancellation_token.raise_if_cancelled()

        # Send status event for node execution
        node_id = getattr(node_def, "id", "llm")
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
        # Merge state into variables for template formatting
        if playbook.name == 'sub_router_user':
            action_dbg = getattr(node_def, 'action', None)
            LOGGER.debug('[sea][router-debug] action=%s model_type=%s avail_len=%s',
                         (action_dbg[:120] + '...') if isinstance(action_dbg, str) and len(action_dbg) > 120 else action_dbg,
                         getattr(node_def, 'model_type', None),
                         len(str(state.get('available_playbooks'))) if state.get('available_playbooks') is not None else None)

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
            # Determine base messages: use context_profile if set, otherwise state["_messages"]
            _profile_name = getattr(node_def, "context_profile", None)
            if _profile_name:
                from sea.playbook_models import CONTEXT_PROFILES
                _profile = CONTEXT_PROFILES.get(_profile_name)
                if _profile:
                    _cache_key = f"_ctx_profile_{_profile_name}"
                    if _cache_key not in state:
                        # Exclude current pulse messages from SAIMemory — PulseContext
                        # provides them instead, avoiding duplication of memorized messages.
                        state[_cache_key] = runtime._prepare_context(
                            persona, building_id,
                            state.get("input") or None,
                            _profile["requirements"],
                            pulse_id=state.get("_pulse_id"),
                            exclude_pulse_id=state.get("_pulse_id"),
                            event_callback=event_callback,
                        )
                        LOGGER.info("[sea] Prepared context for profile '%s' (node=%s, %d messages, exclude_pulse=%s)",
                                    _profile_name, node_id, len(state[_cache_key]), state.get("_pulse_id"))
                    _profile_base = state[_cache_key]
                    _pulse_ctx = state.get("_pulse_context")
                    _intermediate = _pulse_ctx.get_protocol_messages() if _pulse_ctx else []
                    base_msgs = list(_profile_base) + list(_intermediate)
                else:
                    LOGGER.warning("[sea] Unknown context_profile '%s' on node '%s', falling back to state messages", _profile_name, node_id)
                    base_msgs = state.get("_messages", [])
            else:
                base_msgs = state.get("_messages", [])
            action_template = getattr(node_def, "action", None)
            if action_template:
                prompt = _format(action_template, variables)
                # Auto-wrap in <system> tags to distinguish from user messages
                if not prompt.lstrip().startswith("<system>"):
                    prompt = f"<system>{prompt}</system>"
                messages = list(base_msgs) + [{"role": "user", "content": prompt}]
            else:
                messages = list(base_msgs)

            # Dynamically add enum to response_schema if available_playbooks exists
            response_schema = getattr(node_def, "response_schema", None)
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
                                    })
                                    continue
                                text_chunks.append(chunk)
                                event_callback({
                                    "type": "streaming_chunk",
                                    "content": chunk,
                                    "persona_id": getattr(persona, "persona_id", None),
                                    "node_id": getattr(node_def, "id", "llm"),
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
                _spell_text, _spell_details_blocks, _spell_loop_count = await _run_spell_loop(
                    text=result.get("content", "") if result.get("type") == "text" else "",
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
                    # _store_memory in P0-4. Skip bubble1/bubble2 emission here.
                    _node_speak_flag = getattr(node_def, "speak", True)
                    if _node_speak_flag is False:
                        LOGGER.info(
                            "[sea][spell] speak=false node — skipping bubble1/bubble2 _emit_say "
                            "(handoff route B); records remain in line storage layer only"
                        )
                    else:
                        pulse_id = state.get("_pulse_id")
                        eff_bid = runtime._effective_building_id(persona, building_id)

                        # Bubble 1: text before the first spell (no metadata)
                        _first_text_before = _spell_details_blocks[0][0] if _spell_details_blocks else ""
                        if _first_text_before.strip():
                            if event_callback:
                                event_callback({
                                    "type": "say",
                                    "content": _first_text_before,
                                    "persona_id": getattr(persona, "persona_id", None),
                                })
                            runtime._emit_say(persona, eff_bid, _first_text_before, pulse_id=pulse_id)

                        # Bubble 2: all details blocks + continuation text (with metadata)
                        _bubble2_parts: list[str] = []
                        for _i, (_tb, _db) in enumerate(_spell_details_blocks):
                            if _i > 0 and _tb:
                                _bubble2_parts.append(_tb)
                            _bubble2_parts.append(_db)
                        if result.get("content"):
                            _bubble2_parts.append(result["content"])
                        _spell_bubble2 = "\n".join(_bubble2_parts)

                        _spell_msg_meta: Dict[str, Any] = {}
                        _spell_at = state.get("_activity_trace")
                        if _spell_at:
                            _spell_msg_meta["activity_trace"] = list(_spell_at)

                        if event_callback:
                            _say_event: Dict[str, Any] = {
                                "type": "say",
                                "content": _spell_bubble2,
                                "persona_id": getattr(persona, "persona_id", None),
                            }
                            if _spell_at:
                                _say_event["activity_trace"] = list(_spell_at)
                            event_callback(_say_event)

                        runtime._emit_say(persona, eff_bid, _spell_bubble2, pulse_id=pulse_id,
                                          metadata=_spell_msg_meta if _spell_msg_meta else None)
                        LOGGER.info("[sea][spell] Tool-mode: emitted bubble1 + bubble2 to UI and Building history")

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

                runtime._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
            else:
                LOGGER.info("[DEBUG] Entering normal mode (no tools)")
                # Normal mode (no tools)
                state["tool_called"] = False

                # Check speak flag for streaming output
                speak_flag = getattr(node_def, "speak", None)
                streaming_enabled = _is_llm_streaming_enabled()
                LOGGER.info("[DEBUG] Streaming check: speak_flag=%s, response_schema=%s, streaming_enabled=%s, event_callback=%s",
                           speak_flag, response_schema is None, streaming_enabled, event_callback is not None)
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
                    for stream_attempt in range(max_stream_retries):
                        text_chunks = []
                        stream_iter = llm_client.generate_stream(
                            messages,
                            tools=[],
                            temperature=runtime._default_temperature(persona),
                            **runtime._get_cache_kwargs(),
                        )
                        try:
                            for chunk in stream_iter:
                                # Check cancellation between chunks
                                if cancellation_token and cancellation_token.is_cancelled():
                                    LOGGER.info("[sea] Streaming cancelled by user during chunk loop")
                                    cancelled_during_stream = True
                                    break

                                # Thinking chunks are dicts, text chunks are strings
                                if isinstance(chunk, dict) and chunk.get("type") == "thinking":
                                    event_callback({
                                        "type": "streaming_thinking",
                                        "content": chunk["content"],
                                        "persona_id": getattr(persona, "persona_id", None),
                                        "node_id": getattr(node_def, "id", "llm"),
                                    })
                                    continue
                                text_chunks.append(chunk)
                                # Send each text chunk to UI
                                event_callback({
                                    "type": "streaming_chunk",
                                    "content": chunk,
                                    "persona_id": getattr(persona, "persona_id", None),
                                    "node_id": getattr(node_def, "id", "llm"),
                                })
                        finally:
                            # Explicitly close to disconnect HTTP streaming from LLM API
                            # This stops API-side token generation and billing
                            if hasattr(stream_iter, 'close'):
                                stream_iter.close()
                        text = "".join(text_chunks)

                        if cancelled_during_stream:
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
                    else:
                        LOGGER.warning("[DEBUG] No usage data from LLM client")

                    # Consume reasoning (thinking) from LLM — store as metadata, not in content
                    reasoning_entries = llm_client.consume_reasoning()
                    reasoning_text = "\n\n".join(
                        e.get("text", "") for e in reasoning_entries if e.get("text")
                    ) if reasoning_entries else ""
                    reasoning_details = llm_client.consume_reasoning_details()

                    # ── Spell loop (parallel execution per round) ──
                    text, _spell_details_blocks_ns, _spell_loop_count_ns = await _run_spell_loop(
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

                    if _spell_loop_count_ns > 0:
                        pulse_id = state.get("_pulse_id")
                        eff_bid = runtime._effective_building_id(persona, building_id)

                        # Bubble 1: discard streamed content, re-emit just text_before clean
                        _first_text_before_ns = _spell_details_blocks_ns[0][0] if _spell_details_blocks_ns else ""
                        if event_callback:
                            event_callback({
                                "type": "streaming_discard",
                                "persona_id": getattr(persona, "persona_id", None),
                                "node_id": getattr(node_def, "id", "llm"),
                            })
                        if _first_text_before_ns.strip():
                            if event_callback:
                                event_callback({
                                    "type": "say",
                                    "content": _first_text_before_ns,
                                    "persona_id": getattr(persona, "persona_id", None),
                                })
                            runtime._emit_say(persona, eff_bid, _first_text_before_ns, pulse_id=pulse_id)

                        # Bubble 2: all details blocks + continuation (with metadata)
                        _bubble2_parts: list[str] = []
                        for _i_ns, (_tb_ns, _db_ns) in enumerate(_spell_details_blocks_ns):
                            if _i_ns > 0 and _tb_ns:
                                _bubble2_parts.append(_tb_ns)
                            _bubble2_parts.append(_db_ns)
                        if text:
                            _bubble2_parts.append(text)
                        _spell_bubble2_ns = "\n".join(_bubble2_parts)

                        _spell_msg_meta_ns: Dict[str, Any] = {}
                        if llm_usage_metadata:
                            _spell_msg_meta_ns["llm_usage"] = llm_usage_metadata
                        _spell_at_ns = state.get("_activity_trace")
                        if _spell_at_ns:
                            _spell_msg_meta_ns["activity_trace"] = list(_spell_at_ns)
                        accumulator = state.get("_pulse_usage_accumulator")
                        if accumulator:
                            _spell_msg_meta_ns["llm_usage_total"] = dict(accumulator)

                        if event_callback:
                            _say_event_ns: Dict[str, Any] = {
                                "type": "say",
                                "content": _spell_bubble2_ns,
                                "persona_id": getattr(persona, "persona_id", None),
                            }
                            if _spell_at_ns:
                                _say_event_ns["activity_trace"] = list(_spell_at_ns)
                            if _spell_msg_meta_ns:
                                _say_event_ns["metadata"] = _spell_msg_meta_ns
                            event_callback(_say_event_ns)

                        runtime._emit_say(persona, eff_bid, _spell_bubble2_ns, pulse_id=pulse_id,
                                          metadata=_spell_msg_meta_ns if _spell_msg_meta_ns else None)
                        LOGGER.info("[sea][spell] Normal-stream: emitted bubble1 + bubble2 (len=%d)", len(_spell_bubble2_ns))

                        # text = continuation only (for state["last"] / memorize — no duplication)
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
                        }
                        if reasoning_text:
                            completion_event["reasoning"] = reasoning_text
                        if _speak_base_metadata and isinstance(_speak_base_metadata, dict):
                            completion_event["metadata"] = _speak_base_metadata
                        event_callback(completion_event)

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
                        _last_bmsg = runtime._emit_say(persona, eff_bid, text, pulse_id=pulse_id, metadata=msg_metadata if msg_metadata else None)
                        # 後続ツールが新しい persona_context 配下でも
                        # 最新の message_id を参照できるよう state に残す。
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

                    # Consume reasoning (thinking) from LLM — store as metadata
                    reasoning_entries = llm_client.consume_reasoning()
                    reasoning_text = "\n\n".join(
                        e.get("text", "") for e in reasoning_entries if e.get("text")
                    ) if reasoning_entries else ""
                    reasoning_details = llm_client.consume_reasoning_details()

                    # ── Spell loop (parallel execution per round) ──
                    if isinstance(text, str):
                        # Normal text mode - run spell processing
                        text, _spell_details_blocks_sync, _spell_loop_count_sync = await _run_spell_loop(
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
                        _spell_details_blocks_sync = []
                        _spell_loop_count_sync = 0

                    if _spell_loop_count_sync > 0:
                        # Bubble 1: text_before of first spell (no metadata)
                        _first_text_before_sync = _spell_details_blocks_sync[0][0] if _spell_details_blocks_sync else ""
                        if _first_text_before_sync.strip() and speak_flag is True:
                            pulse_id = state.get("_pulse_id")
                            eff_bid = runtime._effective_building_id(persona, building_id)
                            runtime._emit_say(persona, eff_bid, _first_text_before_sync, pulse_id=pulse_id)
                            if event_callback is not None:
                                event_callback({
                                    "type": "say",
                                    "content": _first_text_before_sync,
                                    "persona_id": getattr(persona, "persona_id", None),
                                })

                        # Bubble 2: all details blocks + continuation
                        # text = bubble2 content for the speak_flag path below to emit with metadata
                        _bubble2_parts_sync: list[str] = []
                        for _i_sync, (_tb_sync, _db_sync) in enumerate(_spell_details_blocks_sync):
                            if _i_sync > 0 and _tb_sync:
                                _bubble2_parts_sync.append(_tb_sync)
                            _bubble2_parts_sync.append(_db_sync)
                        if text:
                            _bubble2_parts_sync.append(text)
                        text = "\n".join(_bubble2_parts_sync)

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
            state["_messages"] = messages + [{"role": "assistant", "content": _msg_content}]

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
            # Parse memorize config - can be True or {"tags": [...]}
            if isinstance(memorize_config, dict):
                memorize_tags = memorize_config.get("tags", [])
            else:
                memorize_tags = []

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

                if not runtime._store_memory(
                    persona,
                    content_to_save,
                    role="assistant",
                    tags=list(memorize_tags),
                    pulse_id=pulse_id,
                    metadata=_memorize_metadata if _memorize_metadata else None,
                    playbook_name=playbook.name,
                    pulse_context=pulse_context,
                    paired_action_text=prompt,
                ):
                    _memorize_ok = False
                else:
                    LOGGER.debug(
                        "[sea][llm] Memorized response (assistant) with paired_action_text len=%s",
                        len(prompt) if prompt else 0,
                    )

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
                    })

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
            ):
                LOGGER.warning("[sea][llm] Important dual-write failed for node %s", node_id)

        # Debug: log speak_content at end of LLM node
        speak_content = state.get("speak_content", "")
        LOGGER.info("[DEBUG] LLM node end: state['speak_content'] = '%s'", speak_content)

        # Note: output_mapping in node definition handles state variable assignment
        # No special handling needed here anymore
        return state

    return node
