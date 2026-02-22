from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sea.runtime_context import prepare_context as prepare_context_impl

LOGGER = logging.getLogger(__name__)


def maybe_run_metabolism(
    runtime: Any,
    persona: Any,
    building_id: str,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    if not getattr(runtime.manager, "metabolism_enabled", False):
        return

    history_mgr = getattr(persona, "history_manager", None)
    anchor = getattr(history_mgr, "metabolism_anchor_message_id", None)
    if not history_mgr or not anchor:
        return

    high_wm = runtime._get_high_watermark(persona)
    if high_wm is None:
        return

    current_messages = history_mgr.get_history_from_anchor(anchor, required_tags=["conversation"])
    if len(current_messages) <= high_wm:
        return

    low_wm = runtime._get_low_watermark(persona)
    if low_wm is None or high_wm - low_wm < 20:
        return

    LOGGER.info(
        "[metabolism] Triggering metabolism for %s: %d messages > high_wm=%d, will keep %d",
        getattr(persona, "persona_id", "?"),
        len(current_messages),
        high_wm,
        low_wm,
    )
    runtime._run_metabolism(persona, building_id, current_messages, low_wm, event_callback)


def run_metabolism(
    runtime: Any,
    persona: Any,
    building_id: str,
    current_messages: List[Dict[str, Any]],
    keep_count: int,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    evict_count = len(current_messages) - keep_count

    if event_callback:
        event_callback(
            {
                "type": "metabolism",
                "status": "started",
                "content": f"記憶を整理しています（{len(current_messages)}件 → {keep_count}件）...",
            }
        )

    memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
    if memory_weave_enabled and runtime._is_chronicle_enabled_for_persona(persona):
        try:
            runtime._generate_chronicle(persona, event_callback)
        except Exception as exc:
            LOGGER.warning("[metabolism] Chronicle generation failed: %s", exc)

    new_anchor_id = current_messages[evict_count].get("id")
    if new_anchor_id:
        persona.history_manager.metabolism_anchor_message_id = new_anchor_id
        persona_model = getattr(persona, "model", None)
        if persona_model:
            runtime._update_anchor_for_model(persona, persona_model, new_anchor_id)
        LOGGER.info("[metabolism] Updated anchor to %s (evicted %d, kept %d)", new_anchor_id, evict_count, keep_count)

    if event_callback:
        event_callback(
            {
                "type": "metabolism",
                "status": "completed",
                "content": f"記憶の整理が完了しました（{evict_count}件の会話をChronicleに圧縮）",
                "evicted": evict_count,
                "kept": keep_count,
            }
        )


def prepare_context(
    runtime: Any,
    persona: Any,
    building_id: str,
    user_input: Optional[str],
    requirements: Optional[Any] = None,
    pulse_id: Optional[str] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
    preview_only: bool = False,
) -> List[Dict[str, Any]]:
    return prepare_context_impl(
        runtime,
        persona,
        building_id,
        user_input,
        requirements=requirements,
        pulse_id=pulse_id,
        warnings=warnings,
        preview_only=preview_only,
    )


def build_realtime_context(
    runtime: Any,
    persona: Any,
    building_id: str,
    history_messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    sections: List[str] = []

    now = datetime.now(persona.timezone)
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    current_time_str = now.strftime(f"%Y年%m月%d日({weekday_names[now.weekday()]}) %H:%M")
    sections.append(f"現在時刻: {current_time_str}")

    prev_ai_timestamp = None
    persona_id = getattr(persona, "persona_id", None)
    persona_name = getattr(persona, "persona_name", None)
    for msg in reversed(history_messages):
        role = msg.get("role", "")
        if role == "assistant" or (persona_name and msg.get("sender") == persona_name):
            ts_str = msg.get("created_at") or msg.get("timestamp")
            if ts_str:
                try:
                    if isinstance(ts_str, datetime):
                        prev_ai_timestamp = ts_str
                    else:
                        prev_ai_timestamp = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    break
                except (ValueError, TypeError):
                    pass

    if prev_ai_timestamp:
        if prev_ai_timestamp.tzinfo is not None:
            prev_ai_timestamp = prev_ai_timestamp.astimezone(persona.timezone)
        prev_time_str = prev_ai_timestamp.strftime(f"%Y年%m月%d日({weekday_names[prev_ai_timestamp.weekday()]}) %H:%M")
        sections.append(f"あなたの前回発言: {prev_time_str}")

    try:
        unity_gateway = getattr(runtime.manager, "unity_gateway", None)
        if unity_gateway and getattr(unity_gateway, "is_running", False):
            spatial_state = unity_gateway.spatial_state.get(persona_id) if persona_id else None
            if spatial_state:
                distance = getattr(spatial_state, "distance_to_player", None)
                is_visible = getattr(spatial_state, "is_visible", None)

                spatial_lines = []
                if distance is not None:
                    spatial_lines.append(f"プレイヤーとの距離: {distance:.1f}m")
                if is_visible is not None:
                    visibility_text = "見える" if is_visible else "見えない"
                    spatial_lines.append(f"プレイヤーの視認: {visibility_text}")

                if spatial_lines:
                    sections.append("空間情報: " + " / ".join(spatial_lines))
                    LOGGER.debug(
                        "[sea][realtime-context] Added spatial info: distance=%.1f, visible=%s",
                        distance,
                        is_visible,
                    )
    except Exception as exc:
        LOGGER.debug("[sea][realtime-context] Failed to get spatial context: %s", exc)

    if not sections:
        return None

    content = "<system>\n## リアルタイム情報\n" + "\n".join(f"- {s}" for s in sections) + "\n</system>"
    return {"role": "user", "content": content, "metadata": {"__realtime_context__": True}}
