from __future__ import annotations

import logging
import os
import uuid
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import re

from sea.playbook_models import NodeType, PlaybookSchema, PlaybookValidationError, validate_playbook_graph
from sea.langgraph_runner import compile_playbook
from database.models import Playbook as PlaybookModel
from model_configs import get_model_parameter_defaults

LOGGER = logging.getLogger(__name__)


def _format(template: str, variables: Dict[str, Any]) -> str:
    try:
        return template.format(**variables)
    except Exception:
        # 安全側でそのまま返す
        return template


class SEARuntime:
    """Lightweight executor for meta playbooks until full LangGraph port."""

    def __init__(self, manager_ref: Any):
        self.manager = manager_ref
        self.playbooks_dir = Path(__file__).parent / "playbooks"
        self._playbook_cache: Dict[str, PlaybookSchema] = {}
        self._dump_path = os.getenv("SAIVERSE_SEA_DUMP")  # set to a filepath to capture LLM I/O
        self._trace = bool(os.getenv("SAIVERSE_SEA_TRACE"))

    # ---------------- meta entrypoints -----------------
    def run_meta_user(self, persona, user_input: str, building_id: str) -> List[str]:
        """Router -> subgraph -> speak. Returns spoken strings for gateway/UI."""
        playbook = self._choose_playbook(kind="user", persona=persona, building_id=building_id)
        result = self._run_playbook(playbook, persona, building_id, user_input, auto_mode=False, record_history=True)
        return result

    def run_meta_auto(self, persona, building_id: str, occupants: List[str]) -> None:
        """Router -> subgraph -> think. For autonomous loop, no direct user output."""
        playbook = self._choose_playbook(kind="auto", persona=persona, building_id=building_id)
        self._run_playbook(playbook, persona, building_id, user_input=None, auto_mode=True, record_history=True)

    # ---------------- core runner -----------------
    def _run_playbook(
        self,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        user_input: Optional[str],
        auto_mode: bool,
        record_history: bool = True,
    ) -> List[str]:
        # Prepare shared context (system prompt, history, inventories)
        base_messages = self._prepare_context(persona, building_id, user_input)

        # Try LangGraph path first
        compiled_ok = self._compile_with_langgraph(playbook, persona, building_id, user_input, auto_mode, base_messages)
        if compiled_ok is not None:
            return compiled_ok

        # fallback lightweight executor
        node_map = playbook.node_map()
        current = node_map.get(playbook.start_node)
        outputs: List[str] = []
        last_text = user_input or ""
        variables = {
            "input": user_input or "",
            "persona_id": persona.persona_id,
            "persona_name": persona.persona_name,
            "messages": list(base_messages),
            "context_bundle": [],
            "context_bundle_text": "",
        }
        pulse_id = uuid.uuid4().hex

        while current:
            # meta exec: run sub-playbook directly (skip LLM node for exec)
            if playbook.name.startswith("meta_") and current.id == "exec":
                sub_name = variables.get("selected_playbook") or variables.get("last") or "basic_chat"
                sub_pb = self._load_playbook_for(str(sub_name).strip(), persona, building_id) or self._basic_chat_playbook()
                sub_input = None
                args = variables.get("selected_args") or {}
                if isinstance(args, dict):
                    sub_input = args.get("input") or args.get("query")
                if not sub_input:
                    sub_input = variables.get("input")

                sub_outputs = self._run_playbook(sub_pb, persona, building_id, sub_input, auto_mode, record_history=True)

                ingested = self._ingest_context_from_subplaybook(variables, sub_name, sub_outputs)
                if ingested:
                    last_text = variables.get("context_bundle_text") or last_text
                elif sub_outputs:
                    last_text = sub_outputs[-1]
                variables["last"] = last_text
                current = node_map.get(current.next) if current.next else None
                continue

            if current.type == NodeType.LLM:
                prompt = _format(current.action, variables)
                schema_consumed = False
                try:
                    msg_base = variables.get("messages", [])
                    messages = list(msg_base) + [{"role": "user", "content": prompt}]
                    text = persona.llm_client.generate(
                        messages,
                        tools=[],
                        temperature=self._default_temperature(persona),
                        response_schema=getattr(current, "response_schema", None),
                    )
                    self._dump_llm_io(playbook.name, current.id, persona, messages, text)
                    schema_consumed = self._process_structured_output(current, text, variables)
                    # update conversation history buffer for subsequent nodes
                    variables["messages"] = messages + [{"role": "assistant", "content": text}]
                except Exception as exc:
                    LOGGER.error("SEA LLM node failed: %s", exc)
                    text = "(error in llm node)"
                last_text = text
                variables["last"] = text

                # meta router: interpret selection hint (best-effort)
                if playbook.name.startswith("meta_") and current.id == "router" and not schema_consumed:
                    self._update_router_selection(variables, text)

            elif current.type == NodeType.TOOL:
                from tools import TOOL_REGISTRY  # lazy import

                tool_name = current.action
                tool_func = TOOL_REGISTRY.get(tool_name)
                if tool_func is None:
                    LOGGER.warning("SEA tool %s not found", tool_name)
                    last_text = f"Tool {tool_name} not found"
                else:
                    try:
                        tool_input = variables.get("last") or variables.get("input") or ""
                        result = tool_func(tool_input) if callable(tool_func) else None
                        last_text = str(result)
                        variables["last"] = last_text
                    except Exception as exc:
                        last_text = f"Tool error: {exc}"
                        LOGGER.exception("SEA tool %s failed", tool_name)

            elif current.type == NodeType.SPEAK:
                speak_text = _format(current.action, {**variables, "last": last_text}) if current.action else last_text
                self._emit_speak(persona, building_id, speak_text, record_history=record_history)
                outputs.append(speak_text)
                last_text = speak_text

            elif current.type == NodeType.THINK:
                note = _format(current.action, {**variables, "last": last_text}) if current.action else last_text
                self._emit_think(persona, pulse_id, note, record_history=record_history)
                last_text = note
                variables["last"] = note
                outputs.append(note)

            next_id = getattr(current, "next", None)
            current = node_map.get(next_id) if next_id else None

        return outputs

    # LangGraph compile wrapper -----------------------------------------
    def _compile_with_langgraph(
        self,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        user_input: Optional[str],
        auto_mode: bool,
        base_messages: List[Dict[str, Any]],
    ) -> Optional[List[str]]:
        _lg_outputs: List[str] = []
        temperature = self._default_temperature(persona)

        compiled = compile_playbook(
            playbook,
            llm_node_factory=lambda node_def: self._lg_llm_node(node_def, persona, playbook),
            tool_node_factory=lambda action: self._lg_tool_node(action),
            speak_node=lambda state: self._lg_speak_node(state, persona, building_id, _lg_outputs),
            think_node=lambda state: self._lg_think_node(state, persona, _lg_outputs),
            exec_node_factory=(lambda node_def: self._lg_exec_node(node_def, playbook, persona, building_id, auto_mode, _lg_outputs))
            if playbook.name.startswith("meta_")
            else None,
        )
        if not compiled:
            return None

        initial_state = {
            "messages": list(base_messages),
            "inputs": {"input": user_input or ""},
            "context": {},
            "last": user_input or "",
            "outputs": _lg_outputs,
            "persona_obj": persona,
            "context_bundle": [],
            "context_bundle_text": "",
        }

        # If already inside a running loop (e.g., async route), fall back to lightweight executor
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop and running_loop.is_running():
            return None

        try:
            asyncio.run(compiled(initial_state))
        except Exception:
            LOGGER.exception("SEA LangGraph execution failed; falling back to lightweight executor")
            return None
        # speak/think nodes already emitted; return collected texts for UI consistency
        return list(_lg_outputs)

    def _lg_llm_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema):
        async def node(state: dict):
            variables = {
                "input": state.get("inputs", {}).get("input", ""),
                "last": state.get("last", ""),
                "persona_id": getattr(persona, "persona_id", None),
                "persona_name": getattr(persona, "persona_name", None),
                "context_bundle_text": state.get("context_bundle_text", ""),
            }
            prompt = _format(node_def.action, variables)
            text = ""
            schema_consumed = False
            try:
                base_msgs = state.get("messages", [])
                messages = list(base_msgs) + [{"role": "user", "content": prompt}]
                text = persona.llm_client.generate(
                    messages,
                    tools=[],
                    temperature=self._default_temperature(persona),
                    response_schema=getattr(node_def, "response_schema", None),
                )
                self._dump_llm_io(playbook.name, getattr(node_def, "id", ""), persona, messages, text)
                schema_consumed = self._process_structured_output(node_def, text, state)
            except Exception as exc:
                LOGGER.error("SEA LangGraph LLM failed: %s", exc)
                text = "(error in llm node)"
            state["last"] = text
            state["messages"] = messages + [{"role": "assistant", "content": text}]

            # meta router handling
            if playbook.name.startswith("meta_") and getattr(node_def, "id", "") == "router" and not schema_consumed:
                self._update_router_selection(state, text)
            return state

        return node

    def _default_temperature(self, persona: Any) -> Optional[float]:
        try:
            model_name = getattr(persona, "model", None)
            if not model_name:
                return None
            defaults = get_model_parameter_defaults(model_name)
            temp = defaults.get("temperature")
            if temp is None:
                return None
            try:
                return float(temp)
            except Exception:
                return None
        except Exception:
            return None

    def _dump_llm_io(
        self,
        playbook_name: str,
        node_id: str,
        persona: Any,
        messages: List[Dict[str, Any]],
        output_text: str,
    ) -> None:
        if not self._dump_path:
            return
        try:
            entry = {
                "playbook": playbook_name,
                "node": node_id,
                "persona_id": getattr(persona, "persona_id", None),
                "persona_name": getattr(persona, "persona_name", None),
                "messages": messages,
                "output": output_text,
            }
            Path(self._dump_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._dump_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False))
                f.write("\n")
        except Exception:
            LOGGER.debug("failed to dump LLM io", exc_info=True)

    def _debug_playbook(self, pb: PlaybookSchema, source: str) -> None:
        if not self._trace:
            return
        try:
            summary = {
                "source": source,
                "name": pb.name,
                "start": pb.start_node,
                "nodes": [
                    {
                        "id": n.id,
                        "type": getattr(n, "type", None),
                        "next": getattr(n, "next", None),
                        "action": getattr(n, "action", None),
                    }
                    for n in pb.nodes
                ],
            }
            LOGGER.debug("[sea] playbook loaded: %s", json.dumps(summary, ensure_ascii=False))
        except Exception:
            LOGGER.debug("[sea] playbook debug failed", exc_info=True)

    def _process_structured_output(self, node_def: Any, text: str, state: Dict[str, Any]) -> bool:
        schema = getattr(node_def, "response_schema", None)
        if not schema:
            return False
        parsed = self._extract_structured_json(text)
        if parsed is None:
            LOGGER.warning("[sea] structured output parse failed for node %s", getattr(node_def, "id", "?"))
            return False
        key = getattr(node_def, "output_key", None) or getattr(node_def, "id", "") or "node"
        self._store_structured_result(state, key, parsed)
        if getattr(node_def, "id", "") == "router":
            self._update_router_selection(state, text, parsed)
            self._append_router_function_call(state, parsed, text)
        return True

    def _store_structured_result(self, state: Dict[str, Any], key: str, data: Any) -> None:
        state[key] = data
        flat = self._flatten_dict(data)
        for path, value in flat.items():
            state[f"{key}.{path}"] = value

    def _flatten_dict(self, value: Any, prefix: str = "") -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if isinstance(value, dict):
            for k, v in value.items():
                new_prefix = f"{prefix}.{k}" if prefix else str(k)
                result.update(self._flatten_dict(v, new_prefix))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                new_prefix = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
                result.update(self._flatten_dict(item, new_prefix))
        else:
            result[prefix or "value"] = value
        return result

    def _extract_structured_json(self, text: str) -> Optional[Dict[str, Any]]:
        candidate = text.strip()
        if not candidate:
            return None
        if candidate.startswith("```"):
            parts = candidate.split("```")
            for seg in parts:
                seg = seg.strip()
                if seg.startswith("{") and seg.endswith("}"):
                    candidate = seg
                    break
        if not candidate.startswith("{"):
            match = re.search(r"\{.*\}", candidate, re.DOTALL)
            if match:
                candidate = match.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            return None

    def _update_router_selection(self, state: Dict[str, Any], text: str, parsed: Optional[Dict[str, Any]] = None) -> None:
        selection = parsed or {}
        playbook_value = selection.get("playbook") if isinstance(selection, dict) else None
        if not playbook_value:
            playbook_value = selection.get("playbook_name") if isinstance(selection, dict) else None
        if not playbook_value:
            stripped = str(text).strip()
            playbook_value = stripped.split()[0] if stripped else "basic_chat"
        state["selected_playbook"] = playbook_value or "basic_chat"
        args_obj = selection.get("args") if isinstance(selection, dict) else None
        if isinstance(args_obj, dict):
            state["selected_args"] = args_obj
        else:
            state["selected_args"] = {"input": state.get("input")}

    def _lg_tool_node(self, tool_name: str):
        from tools import TOOL_REGISTRY
        from tools.context import persona_context

        async def node(state: dict):
            tool_func = TOOL_REGISTRY.get(tool_name)
            last = state.get("last") or state.get("inputs", {}).get("input") or ""
            persona_obj = state.get("persona_obj")
            try:
                persona_dir = getattr(persona_obj, "persona_log_path", None)
                persona_dir = persona_dir.parent if persona_dir else Path.cwd()
                persona_id = getattr(persona_obj, "persona_id", None)
                manager_ref = getattr(persona_obj, "manager_ref", None)
                if persona_id and persona_dir:
                    with persona_context(persona_id, persona_dir, manager_ref):
                        result = tool_func(last) if callable(tool_func) else None
                else:
                    result = tool_func(last) if callable(tool_func) else None
                state["last"] = str(result)
            except Exception as exc:
                state["last"] = f"Tool error: {exc}"
                LOGGER.exception("SEA LangGraph tool %s failed", tool_name)
            return state

        return node

    def _lg_exec_node(
        self,
        node_def: Any,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        auto_mode: bool,
        outputs: Optional[List[str]] = None,
    ):
        async def node(state: dict):
            sub_name = state.get("selected_playbook") or state.get("last") or "basic_chat"
            sub_pb = self._load_playbook_for(str(sub_name).strip(), persona, building_id) or self._basic_chat_playbook()
            sub_input = None
            args = state.get("selected_args") or {}
            if isinstance(args, dict):
                sub_input = args.get("input") or args.get("query")
            if not sub_input:
                sub_input = state.get("inputs", {}).get("input")

            try:
                sub_outputs = await asyncio.to_thread(
                    self._run_playbook, sub_pb, persona, building_id, sub_input, auto_mode, True
                )
            except Exception as exc:
                LOGGER.exception("SEA LangGraph exec sub-playbook failed")
                state["last"] = f"Sub-playbook error: {exc}"
                if outputs is not None:
                    outputs.append(state["last"])
                return state

            ingested = self._ingest_context_from_subplaybook(state, sub_name, sub_outputs)
            if ingested:
                state["last"] = state.get("context_bundle_text") or state.get("last")
            elif sub_outputs:
                state["last"] = sub_outputs[-1]
            return state

        return node

    def _lg_speak_node(self, state: dict, persona: Any, building_id: str, outputs: Optional[List[str]] = None):
        text = state.get("last") or ""
        self._emit_speak(persona, building_id, text)
        if outputs is not None:
            outputs.append(text)
        return state

    def _lg_think_node(self, state: dict, persona: Any, outputs: Optional[List[str]] = None):
        text = state.get("last") or ""
        pulse = uuid.uuid4().hex
        self._emit_think(persona, pulse, text)
        if outputs is not None:
            outputs.append(text)
        return state

    # ---------------- context helpers -----------------
    def _append_router_function_call(
        self,
        state: Dict[str, Any],
        selection: Optional[Dict[str, Any]],
        raw_text: str,
    ) -> None:
        payload = selection if isinstance(selection, dict) else None
        if payload is None:
            payload = {"raw": raw_text}
        try:
            args_text = json.dumps(payload, ensure_ascii=False)
        except Exception:
            args_text = json.dumps({"raw": str(raw_text)}, ensure_ascii=False)
        conv = state.get("messages")
        if not isinstance(conv, list):
            conv = []
        call_id = f"router_call_{uuid.uuid4().hex}"
        call_msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "route_playbook",
                        "arguments": args_text,
                    },
                }
            ],
        }
        if conv and isinstance(conv[-1], dict) and conv[-1].get("role") == "assistant":
            conv[-1] = call_msg
        else:
            conv.append(call_msg)
        state["messages"] = conv
        state["_last_tool_call_id"] = call_id
        state["_last_tool_name"] = payload.get("playbook") or "sub_playbook"

    def _ingest_context_from_subplaybook(
        self,
        state: Dict[str, Any],
        source: str,
        sub_outputs: Optional[List[str]],
    ) -> bool:
        bundle = state.setdefault("context_bundle", [])
        state.setdefault("context_bundle_text", "")
        if not sub_outputs:
            return False
        joined = "\n".join(str(item).strip() for item in sub_outputs if str(item).strip())
        if not joined:
            return False
        entry: Dict[str, Any] = {"source": source, "raw": joined}
        parsed = self._extract_structured_json(joined)
        if parsed is not None:
            entry["data"] = parsed
        bundle.append(entry)
        state["context_bundle_text"] = self._render_context_bundle(bundle)
        self._append_tool_result_message(state, source, joined)
        return True

    def _render_context_bundle(self, bundle: List[Dict[str, Any]]) -> str:
        blocks: List[str] = []
        for idx, entry in enumerate(bundle, 1):
            label = entry.get("source") or f"context_{idx}"
            payload = entry.get("raw") or ""
            data = entry.get("data")
            if isinstance(data, (dict, list)):
                try:
                    payload = json.dumps(data, ensure_ascii=False)
                except Exception:
                    payload = str(data)
            payload = str(payload).strip()
            blocks.append(f"[{label}]\n{payload}" if payload else f"[{label}]")
        return "\n\n".join(blocks)

    def _append_tool_result_message(
        self,
        state: Dict[str, Any],
        source: str,
        payload: str,
    ) -> None:
        call_id = state.get("_last_tool_call_id")
        if not call_id:
            return
        conv = state.get("messages")
        if not isinstance(conv, list):
            conv = []
        message = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": source or state.get("_last_tool_name") or "sub_playbook",
            "content": payload,
        }
        conv.append(message)
        state["messages"] = conv
        state["_last_tool_call_id"] = None

    # ---------------- helpers -----------------
    def _emit_speak(self, persona: Any, building_id: str, text: str, record_history: bool = True) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        if record_history:
            try:
                persona.history_manager.add_message(msg, building_id, heard_by=None)
                self.manager.gateway_handle_ai_replies(building_id, persona, [text])
            except Exception:
                LOGGER.exception("Failed to emit speak message")

    def _emit_think(self, persona: Any, pulse_id: str, text: str, record_history: bool = True) -> None:
        if not record_history:
            return
        adapter = getattr(persona, "sai_memory", None)
        try:
            if adapter and adapter.is_ready():
                adapter.append_persona_message(
                    {
                        "role": "assistant",
                        "content": text,
                        "metadata": {"tags": ["internal", f"pulse:{pulse_id}"]},
                        "persona_id": persona.persona_id,
                    }
                )
        except Exception:
            LOGGER.debug("think message not stored", exc_info=True)

    def _prepare_context(self, persona: Any, building_id: str, user_input: Optional[str]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []

        # ---- system prompt ----
        system_parts: List[str] = []
        persona_sys = getattr(persona, "persona_system_instruction", "") or ""
        if persona_sys:
            system_parts.append(persona_sys.strip())

        # building system instruction if available
        try:
            building_obj = getattr(persona, "buildings", {}).get(building_id)
            if building_obj and getattr(building_obj, "system_instruction", None):
                system_parts.append(str(building_obj.system_instruction).strip())
        except Exception:
            pass

        # persona inventory
        try:
            inv_builder = getattr(persona, "_inventory_summary_lines", None)
            inv_lines: List[str] = inv_builder() if callable(inv_builder) else []
        except Exception:
            inv_lines = []
        if inv_lines:
            system_parts.append("### インベントリ\n" + "\n".join(inv_lines))

        # building inventory (items located in building)
        try:
            items_by_building = getattr(self.manager, "items_by_building", {}) or {}
            item_registry = getattr(self.manager, "item_registry", {}) or {}
            b_items = items_by_building.get(building_id, [])
            lines = []
            for iid in b_items:
                data = item_registry.get(iid, {})
                name = data.get("name", iid)
                desc = (data.get("description") or "").strip() or "(説明なし)"
                lines.append(f"- [{iid}] {name}: {desc}")
            if lines:
                system_parts.append("### 建物内のアイテム\n" + "\n".join(lines))
        except Exception:
            pass

        system_text = "\n\n".join([s for s in system_parts if s])
        if system_text:
            messages.append({"role": "system", "content": system_text})

        # ---- history ----
        history_mgr = getattr(persona, "history_manager", None)
        if history_mgr:
            try:
                recent = history_mgr.get_recent_history(getattr(persona, "context_length", 2000))
                messages.extend(recent)
            except Exception:
                pass

        return messages

    def _choose_playbook(self, kind: str, persona: Any, building_id: str) -> PlaybookSchema:
        """Resolve playbook by kind with DB→disk→fallback."""
        candidates = ["meta_user" if kind == "user" else "meta_auto", "basic_chat"]
        for name in candidates:
            pb = self._load_playbook_for(name, persona, building_id)
            if pb:
                return pb
        return self._basic_chat_playbook()

    def _basic_chat_playbook(self) -> PlaybookSchema:
        return PlaybookSchema(
            name="basic_chat",
            description="Reply based on input",
            input_schema=[{"name": "input", "description": "User or system input"}],
            nodes=[
                {
                    "id": "llm",
                    "type": "llm",
                    "action": "You are a helpful persona. Respond briefly to: {input}",
                    "next": "speak",
                },
                {
                    "id": "speak",
                    "type": "speak",
                    "action": None,
                    "next": None,
                },
            ],
            start_node="llm",
        )

    # playbook loading helpers -----------------------------------------
    def _load_playbook_for(self, name: str, persona: Any, building_id: str) -> Optional[PlaybookSchema]:
        pb = self._load_playbook_from_db(name, persona, building_id)
        if not pb:
            LOGGER.warning("[sea] playbook '%s' not found in DB (persona=%s building=%s)", name, getattr(persona, "persona_id", None), building_id)
        return pb

    def _visible(self, model: PlaybookModel, persona: Any, building_id: str) -> bool:
        scope = (model.scope or "public").lower()
        if scope == "public":
            return True
        if scope == "personal":
            return model.created_by_persona_id == getattr(persona, "persona_id", None)
        if scope == "building":
            return model.building_id == building_id
        return False

    def _load_playbook_from_db(self, name: str, persona: Any, building_id: str) -> Optional[PlaybookSchema]:
        session_maker = getattr(self.manager, "SessionLocal", None)
        if session_maker is None:
            return None
        try:
            session = session_maker()
        except Exception:
            return None
        try:
            try:
                rec = (
                    session.query(PlaybookModel)
                    .filter(PlaybookModel.name == name)
                    .first()
                )
            except Exception:
                LOGGER.debug("Playbook table not ready; skipping DB load")
                return None
            if not rec or not self._visible(rec, persona, building_id):
                return None
            try:
                data = json.loads(rec.nodes_json)
                pb = PlaybookSchema(**data)
                validate_playbook_graph(pb)
                self._debug_playbook(pb, source="db")
                return pb
            except PlaybookValidationError as exc:
                LOGGER.error("[sea] playbook %s failed validation: %s", name, exc)
                return None
            except Exception:
                LOGGER.exception("Failed to parse playbook %s from DB", name)
                return None
        finally:
            session.close()

    # Disk fallbackを無効化（バグ隠し防止のため）
    def _load_playbook_from_disk(self, name: str) -> Optional[PlaybookSchema]:
        return None
