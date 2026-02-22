from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


class RuntimeEmitters:
    """Emit/output helpers delegated from SEARuntime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def emit_speak(
        self,
        persona: Any,
        building_id: str,
        text: str,
        pulse_id: Optional[str] = None,
        record_history: bool = True,
    ) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        metadata: Dict[str, Any] = {"tags": ["conversation"]}
        if pulse_id:
            metadata["tags"].append(f"pulse:{pulse_id}")

        partners = []
        occupants = self.runtime.manager.occupants.get(building_id, [])
        for oid in occupants:
            if oid != persona.persona_id:
                partners.append(oid)
        presence = getattr(self.runtime.manager, "user_presence_status", "offline")
        if presence in ("online", "away"):
            partners.append("user")
        if partners:
            metadata["with"] = partners

        msg["metadata"] = metadata
        if record_history:
            try:
                persona.history_manager.add_message(msg, building_id, heard_by=None)
                self.runtime.manager.gateway_handle_ai_replies(building_id, persona, [text])
            except Exception:
                LOGGER.exception("Failed to emit speak message")
        self.notify_unity_speak(persona, text)

    def emit_say(
        self,
        persona: Any,
        building_id: str,
        text: str,
        pulse_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        msg_metadata: Dict[str, Any] = {}
        if pulse_id:
            msg_metadata["tags"] = [f"pulse:{pulse_id}"]
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if key == "tags":
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    msg_metadata.setdefault("tags", []).extend(extra_tags)
                else:
                    msg_metadata[key] = value

        partners = []
        occupants = self.runtime.manager.occupants.get(building_id, [])
        for oid in occupants:
            if oid != persona.persona_id:
                partners.append(oid)
        presence = getattr(self.runtime.manager, "user_presence_status", "offline")
        if presence in ("online", "away"):
            partners.append("user")
        if partners:
            msg_metadata["with"] = partners

        if msg_metadata:
            msg["metadata"] = msg_metadata
        try:
            persona.history_manager.add_to_building_only(building_id, msg)
            self.runtime.manager.gateway_handle_ai_replies(building_id, persona, [text])
        except Exception:
            LOGGER.exception("Failed to emit say message")
        self.notify_unity_speak(persona, text)

    def emit_think(self, persona: Any, pulse_id: str, text: str, record_history: bool = True) -> None:
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
            LOGGER.warning("think message not stored", exc_info=True)

    def notify_unity_speak(self, persona: Any, text: str) -> None:
        """Send persona speak event to Unity Gateway if connected."""
        if not text:
            return
        unity_gateway = getattr(self.runtime.manager, "unity_gateway", None)
        if not unity_gateway:
            return
        try:
            persona_id = getattr(persona, "persona_id", "unknown")
            try:
                asyncio.get_running_loop()
                asyncio.create_task(unity_gateway.send_speak(persona_id, text))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(unity_gateway.send_speak(persona_id, text))
                loop.close()
        except Exception as exc:
            LOGGER.debug("Failed to notify Unity Gateway: %s", exc)
