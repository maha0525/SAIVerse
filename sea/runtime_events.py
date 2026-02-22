from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

LOGGER = logging.getLogger(__name__)


class RuntimeEventRetryableError(RuntimeError):
    """Retryable runtime side-effect error (transport/temporary)."""


class RuntimeEventNonRetryableError(RuntimeError):
    """Non-retryable runtime side-effect error (invalid input/state)."""


class RuntimeEvents:
    def __init__(self, manager_ref: Any, runtime: Any):
        self.manager = manager_ref
        self.runtime = runtime

    def _classify(self, exc: Exception) -> Exception:
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return RuntimeEventNonRetryableError(str(exc))
        if isinstance(exc, (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
            return RuntimeEventRetryableError(str(exc))
        return RuntimeEventRetryableError(str(exc))

    def append_router_function_call(self, state: Dict[str, Any], selection: Optional[Dict[str, Any]], raw_text: str) -> None:
        payload = selection if isinstance(selection, dict) else {"raw": raw_text}
        try:
            args_text = json.dumps(payload, ensure_ascii=False)
        except Exception:
            args_text = json.dumps({"raw": str(raw_text)}, ensure_ascii=False)
        conv = state.get("messages")
        if not isinstance(conv, list):
            conv = []
        call_id = f"router_call_{uuid.uuid4().hex}"
        call_msg = {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "route_playbook", "arguments": args_text}}]}
        if conv and isinstance(conv[-1], dict) and conv[-1].get("role") == "assistant":
            conv[-1] = call_msg
        else:
            conv.append(call_msg)
        state["messages"] = conv
        state["_last_tool_call_id"] = call_id
        state["_last_tool_name"] = payload.get("playbook") or "sub_playbook"

    def append_tool_result_message(self, state: Dict[str, Any], source: str, payload: str) -> None:
        call_id = state.get("_last_tool_call_id")
        if not call_id:
            return
        conv = state.get("messages")
        if not isinstance(conv, list):
            conv = []
        conv.append({"role": "tool", "tool_call_id": call_id, "name": source or state.get("_last_tool_name") or "sub_playbook", "content": payload})
        state["messages"] = conv
        state["_last_tool_call_id"] = None

    def store_memory(self, persona: Any, text: str, *, role: str = "assistant", tags: Optional[List[str]] = None, pulse_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not text:
            return True
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning("[_store_memory] SAIMemory adapter unavailable for persona=%s — message will NOT be stored. Check embedding model setup.", getattr(persona, "persona_id", None))
            return False
        try:
            current_thread = adapter.get_current_thread()
            if current_thread is None:
                pid = getattr(persona, "persona_id", None) or "unknown"
                default_thread = f"{pid}:{adapter._PERSONA_THREAD_SUFFIX}"
                adapter.set_active_thread(default_thread)
                current_thread = default_thread
            message: Dict[str, Any] = {"role": role or "assistant", "content": text}
            clean_tags = [str(tag) for tag in (tags or []) if tag]
            if pulse_id:
                clean_tags.append(f"pulse:{pulse_id}")
            msg_metadata: Dict[str, Any] = {"tags": clean_tags} if clean_tags else {}
            if isinstance(metadata, dict):
                for key, value in metadata.items():
                    if key == "tags":
                        extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                        msg_metadata.setdefault("tags", []).extend(extra_tags)
                    else:
                        msg_metadata[key] = value
            if msg_metadata:
                message["metadata"] = msg_metadata
            thread_suffix = current_thread.split(":", 1)[1] if ":" in current_thread else current_thread
            adapter.append_persona_message(message, thread_suffix=thread_suffix)
            return True
        except Exception:
            LOGGER.warning("memorize node not stored", exc_info=True)
            return False

    def emit_speak(self, persona: Any, building_id: str, text: str, pulse_id: Optional[str] = None, record_history: bool = True) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        metadata: Dict[str, Any] = {"tags": ["conversation"]}
        if pulse_id:
            metadata["tags"].append(f"pulse:{pulse_id}")
        partners = [oid for oid in self.manager.occupants.get(building_id, []) if oid != persona.persona_id]
        if getattr(self.manager, "user_presence_status", "offline") in ("online", "away"):
            partners.append("user")
        if partners:
            metadata["with"] = partners
        msg["metadata"] = metadata
        if record_history:
            try:
                persona.history_manager.add_message(msg, building_id, heard_by=None)
                self.manager.gateway_handle_ai_replies(building_id, persona, [text])
            except Exception:
                LOGGER.exception("Failed to emit speak message")
        try:
            self.notify_unity_speak(persona, text)
        except Exception as exc:
            LOGGER.debug("Failed to notify Unity Gateway: %s", self._classify(exc), exc_info=True)

    def emit_say(self, persona: Any, building_id: str, text: str, pulse_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        msg_metadata: Dict[str, Any] = {"tags": [f"pulse:{pulse_id}"]} if pulse_id else {}
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if key == "tags":
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    msg_metadata.setdefault("tags", []).extend(extra_tags)
                else:
                    msg_metadata[key] = value
        partners = [oid for oid in self.manager.occupants.get(building_id, []) if oid != persona.persona_id]
        if getattr(self.manager, "user_presence_status", "offline") in ("online", "away"):
            partners.append("user")
        if partners:
            msg_metadata["with"] = partners
        if msg_metadata:
            msg["metadata"] = msg_metadata
        try:
            persona.history_manager.add_to_building_only(building_id, msg)
            self.manager.gateway_handle_ai_replies(building_id, persona, [text])
        except Exception:
            LOGGER.exception("Failed to emit say message")
        try:
            self.notify_unity_speak(persona, text)
        except Exception as exc:
            LOGGER.debug("Failed to notify Unity Gateway: %s", self._classify(exc), exc_info=True)

    def emit_think(self, persona: Any, pulse_id: str, text: str, record_history: bool = True) -> None:
        if not record_history:
            return
        adapter = getattr(persona, "sai_memory", None)
        try:
            if adapter and adapter.is_ready():
                adapter.append_persona_message({"role": "assistant", "content": text, "metadata": {"tags": ["internal", f"pulse:{pulse_id}"]}, "persona_id": persona.persona_id})
        except Exception:
            LOGGER.warning("think message not stored", exc_info=True)

    def notify_unity_speak(self, persona: Any, text: str) -> None:
        if not text:
            return
        unity_gateway = getattr(self.manager, "unity_gateway", None)
        if not unity_gateway:
            return
        persona_id = getattr(persona, "persona_id", "unknown")
        try:
            asyncio.get_running_loop()
            asyncio.create_task(unity_gateway.send_speak(persona_id, text))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(unity_gateway.send_speak(persona_id, text))
            loop.close()

    def load_anchors(self, persona: Any) -> Dict[str, Any]:
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return {}
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return {}
        db = self.manager.SessionLocal()
        try:
            from database.models import AI
            ai_row = db.query(AI).filter_by(AIID=persona_id).first()
            if ai_row and ai_row.METABOLISM_ANCHORS:
                return json.loads(ai_row.METABOLISM_ANCHORS)
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to load anchors for %s: %s", persona_id, self._classify(exc))
        finally:
            db.close()
        return {}

    def save_anchors(self, persona: Any, anchors: Dict[str, Any]) -> None:
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return
        db = self.manager.SessionLocal()
        try:
            from database.models import AI
            ai_row = db.query(AI).filter_by(AIID=persona_id).first()
            if ai_row:
                ai_row.METABOLISM_ANCHORS = json.dumps(anchors, ensure_ascii=False)
                db.commit()
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to save anchors for %s: %s", persona_id, self._classify(exc))
        finally:
            db.close()

    def resolve_metabolism_anchor(self, persona: Any) -> tuple[Optional[str], str]:
        persona_model = getattr(persona, "model", None)
        if not persona_model:
            return (None, "minimal")
        anchors = self.load_anchors(persona)
        now = datetime.now()
        self_entry = anchors.get(persona_model)
        if self_entry:
            try:
                updated_at = datetime.fromisoformat(self_entry["updated_at"])
                age = (now - updated_at).total_seconds()
                validity = self.runtime._get_anchor_validity_seconds(persona_model)
                if age <= validity:
                    return (self_entry["anchor_id"], "self")
            except (KeyError, ValueError, TypeError):
                pass
        best_entry: Optional[Dict[str, Any]] = None
        best_updated: Optional[datetime] = None
        for model_key, entry in anchors.items():
            if model_key == persona_model:
                continue
            try:
                updated_at = datetime.fromisoformat(entry["updated_at"])
                age = (now - updated_at).total_seconds()
                validity = self.runtime._get_anchor_validity_seconds(model_key)
                if age <= validity and (best_updated is None or updated_at > best_updated):
                    best_entry, best_updated = entry, updated_at
            except (KeyError, ValueError, TypeError):
                continue
        if best_entry:
            return (best_entry["anchor_id"], "other")
        return (None, "minimal")

    def maybe_run_metabolism(self, persona: Any, building_id: str, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        if not getattr(self.manager, "metabolism_enabled", False):
            return
        history_mgr = getattr(persona, "history_manager", None)
        anchor = getattr(history_mgr, "metabolism_anchor_message_id", None)
        if not history_mgr or not anchor:
            return
        high_wm = self.runtime._get_high_watermark(persona)
        if high_wm is None:
            return
        current_messages = history_mgr.get_history_from_anchor(anchor, required_tags=["conversation"])
        if len(current_messages) <= high_wm:
            return
        low_wm = self.runtime._get_low_watermark(persona)
        if low_wm is None or high_wm - low_wm < 20:
            return
        self.run_metabolism(persona, building_id, current_messages, low_wm, event_callback)

    def run_metabolism(self, persona: Any, building_id: str, current_messages: List[Dict[str, Any]], keep_count: int, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        evict_count = len(current_messages) - keep_count
        if event_callback:
            event_callback({"type": "metabolism", "status": "started", "content": f"記憶を整理しています（{len(current_messages)}件 → {keep_count}件）..."})
        memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
        if memory_weave_enabled and self.runtime._is_chronicle_enabled_for_persona(persona):
            try:
                self.runtime._generate_chronicle(persona, event_callback)
            except Exception as exc:
                LOGGER.warning("[metabolism] Chronicle generation failed: %s", self._classify(exc))
        new_anchor_id = current_messages[evict_count].get("id")
        if new_anchor_id:
            persona.history_manager.metabolism_anchor_message_id = new_anchor_id
            persona_model = getattr(persona, "model", None)
            if persona_model:
                self.runtime._update_anchor_for_model(persona, persona_model, new_anchor_id)
        if event_callback:
            event_callback({"type": "metabolism", "status": "completed", "content": f"記憶の整理が完了しました（{evict_count}件の会話をChronicleに圧縮）", "evicted": evict_count, "kept": keep_count})
