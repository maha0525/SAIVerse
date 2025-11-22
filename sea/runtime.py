from __future__ import annotations

import logging
import uuid
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
import json

from sea.playbook_models import NodeType, PlaybookSchema
from sea.langgraph_runner import compile_playbook
from database.models import Playbook as PlaybookModel

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

    # ---------------- meta entrypoints -----------------
    def run_meta_user(self, persona, user_input: str, building_id: str) -> List[str]:
        """Router -> subgraph -> speak. Returns spoken strings for gateway/UI."""
        playbook = self._choose_playbook(kind="user", persona=persona, building_id=building_id)
        result = self._run_playbook(playbook, persona, building_id, user_input, auto_mode=False)
        return result

    def run_meta_auto(self, persona, building_id: str, occupants: List[str]) -> None:
        """Router -> subgraph -> think. For autonomous loop, no direct user output."""
        playbook = self._choose_playbook(kind="auto", persona=persona, building_id=building_id)
        self._run_playbook(playbook, persona, building_id, user_input=None, auto_mode=True)

    # ---------------- core runner -----------------
    def _run_playbook(
        self,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        user_input: Optional[str],
        auto_mode: bool,
    ) -> List[str]:
        # Try LangGraph path first
        compiled_ok = self._compile_with_langgraph(playbook, persona, building_id, user_input)
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
        }
        pulse_id = uuid.uuid4().hex

        while current:
            if current.type == NodeType.LLM:
                prompt = _format(current.action, variables)
                try:
                    messages = [{"role": "user", "content": prompt}]
                    text = persona.llm_client.generate(messages, tools=[])
                except Exception as exc:
                    LOGGER.error("SEA LLM node failed: %s", exc)
                    text = "(error in llm node)"
                last_text = text
                variables["last"] = text

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
                self._emit_speak(persona, building_id, speak_text)
                outputs.append(speak_text)
                last_text = speak_text

            elif current.type == NodeType.THINK:
                note = _format(current.action, {**variables, "last": last_text}) if current.action else last_text
                self._emit_think(persona, pulse_id, note)
                last_text = note

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
    ) -> Optional[List[str]]:
        compiled = compile_playbook(
            playbook,
            llm_node_factory=lambda prompt: self._lg_llm_node(prompt, persona),
            tool_node_factory=lambda name: self._lg_tool_node(name),
            speak_node=lambda state: self._lg_speak_node(state, persona, building_id),
            think_node=lambda state: self._lg_think_node(state, persona),
        )
        if not compiled:
            return None

        initial_state = {
            "messages": [],
            "inputs": {"input": user_input or ""},
            "context": {},
            "last": user_input or "",
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
        # speak_node already pushed output; nothing to return
        return []

    def _lg_llm_node(self, prompt: str, persona: Any):
        async def node(state: dict):
            text = ""
            try:
                text = persona.llm_client.generate([{"role": "user", "content": prompt}], tools=[])
            except Exception as exc:
                LOGGER.error("SEA LangGraph LLM failed: %s", exc)
                text = "(error in llm node)"
            state["last"] = text
            state.setdefault("messages", []).append({"role": "assistant", "content": text})
            return state

        return node

    def _lg_tool_node(self, tool_name: str):
        from tools import TOOL_REGISTRY

        async def node(state: dict):
            tool_func = TOOL_REGISTRY.get(tool_name)
            last = state.get("last") or state.get("inputs", {}).get("input") or ""
            try:
                result = tool_func(last) if callable(tool_func) else None
                state["last"] = str(result)
            except Exception as exc:
                state["last"] = f"Tool error: {exc}"
                LOGGER.exception("SEA LangGraph tool %s failed", tool_name)
            return state

        return node

    def _lg_speak_node(self, state: dict, persona: Any, building_id: str):
        text = state.get("last") or ""
        self._emit_speak(persona, building_id, text)
        return state

    def _lg_think_node(self, state: dict, persona: Any):
        text = state.get("last") or ""
        pulse = uuid.uuid4().hex
        self._emit_think(persona, pulse, text)
        return state

    # ---------------- helpers -----------------
    def _emit_speak(self, persona: Any, building_id: str, text: str) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        try:
            persona.history_manager.add_message(msg, building_id, heard_by=None)
            self.manager.gateway_handle_ai_replies(building_id, persona, [text])
        except Exception:
            LOGGER.exception("Failed to emit speak message")

    def _emit_think(self, persona: Any, pulse_id: str, text: str) -> None:
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

    def _choose_playbook(self, kind: str, persona: Any, building_id: str) -> PlaybookSchema:
        """Resolve playbook by kind with DB→disk→fallback."""
        candidates = ["meta_user" if kind == "user" else "meta_auto", "basic_chat"]
        for name in candidates:
            pb = self._load_playbook_for(name, persona, building_id)
            if pb:
                return pb
        return self._basic_chat_playbook()

    def _load_playbook_for(self, name: str, persona: Any, building_id: str) -> Optional[PlaybookSchema]:
        pb = self._load_playbook_from_db(name, persona, building_id)
        if pb:
            return pb
        return self._load_playbook_from_disk(name)

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
                return PlaybookSchema(**data)
            except Exception:
                LOGGER.exception("Failed to parse playbook %s from DB", name)
                return None
        finally:
            session.close()

    def _load_playbook_from_disk(self, name: str) -> Optional[PlaybookSchema]:
        path = self.playbooks_dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PlaybookSchema(**data)
        except Exception:
            LOGGER.exception("Failed to load playbook %s from disk", name)
            return None

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
