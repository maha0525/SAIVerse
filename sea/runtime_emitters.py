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
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        # Build metadata with tags and conversation partners
        metadata: Dict[str, Any] = {"tags": ["conversation"]}
        if pulse_id:
            metadata["tags"].append(f"pulse:{pulse_id}")
        # Merge extra metadata (reasoning, reasoning_details, etc.)
        if isinstance(extra_metadata, dict):
            for key, value in extra_metadata.items():
                if key == "tags":
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    metadata["tags"].extend(extra_tags)
                else:
                    metadata[key] = value

        # Add conversation partners to "with" field
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
        building_msg: Optional[Dict[str, Any]] = None
        if record_history:
            try:
                from saiverse.content_tags import strip_in_heart
                heard_by_list = list(occupants)
                if persona.persona_id not in heard_by_list:
                    heard_by_list.append(persona.persona_id)
                # SAIMemory: 生のテキスト（<in_heart>タグ含む）を保存
                persona.history_manager.add_to_persona_only(msg)
                # building_histories: <in_heart>を除去したテキストを保存
                building_content = strip_in_heart(text)
                building_msg_dict = {**msg, "content": building_content}
                building_msg = persona.history_manager.add_to_building_only(
                    building_id, building_msg_dict, heard_by=heard_by_list
                )
                # BuildingHistory保存完了直後にmessage_idを確定させる。
                # これにより後続のアドオンツール（TTSなど）が get_active_message_id() で
                # 正しいIDを取得してメタデータを紐付けられる。
                msg_id = building_msg.get("message_id") if building_msg else None
                if msg_id:
                    from tools.context import set_active_message_id
                    set_active_message_id(str(msg_id))
                self.runtime.manager.gateway_handle_ai_replies(building_id, persona, [text])
            except Exception:
                LOGGER.exception("Failed to emit speak message")
        self.notify_unity_speak(persona, text)
        return building_msg

    def emit_say(
        self,
        persona: Any,
        building_id: str,
        text: str,
        pulse_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
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
        building_msg: Optional[Dict[str, Any]] = None
        try:
            from saiverse.content_tags import strip_in_heart, wrap_spell_blocks
            heard_by_list = list(occupants)
            if persona.persona_id not in heard_by_list:
                heard_by_list.append(persona.persona_id)
            # スペルブロックを <user_only alt="Name"> でラッピング、<in_heart> を除去
            building_content = wrap_spell_blocks(strip_in_heart(text))
            building_msg_for_hist = {**msg, "content": building_content}
            building_msg = persona.history_manager.add_to_building_only(
                building_id, building_msg_for_hist, heard_by=heard_by_list
            )
            # BuildingHistory 保存完了直後に message_id を ContextVar に確定させる。
            # 後続のアドオンツール (TTS 等) が get_active_message_id() で正しい ID を
            # 取得できるよう、emit_speak と同様の配線を行う。
            msg_id = building_msg.get("message_id") if building_msg else None
            if msg_id:
                from tools.context import set_active_message_id
                set_active_message_id(str(msg_id))
            self.runtime.manager.gateway_handle_ai_replies(building_id, persona, [text])
        except Exception:
            LOGGER.exception("Failed to emit say message")
        self.notify_unity_speak(persona, text)
        return building_msg

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
