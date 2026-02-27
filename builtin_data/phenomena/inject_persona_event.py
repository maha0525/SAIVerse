"""Phenomenon: inject_persona_event

Bridges PhenomenonManager to PulseController — records an event in
persona_event_log and immediately triggers a playbook execution for
the target persona.

This is the standard exit path for external events (X mentions,
SwitchBot sensors, webhooks, etc.) to reach personas.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from phenomena.core import PhenomenonSchema

LOGGER = logging.getLogger(__name__)


def inject_persona_event(
    persona_id: str,
    event_description: str,
    meta_playbook: Optional[str] = None,
    playbook_params_json: Optional[str] = None,
    event_type: Optional[str] = None,
    _manager: Any = None,
    **_kwargs: Any,
) -> str:
    """Inject an external event into a persona's action pipeline.

    1. Records the event in persona_event_log (with EVENT_TYPE / PAYLOAD).
    2. Submits a schedule-priority execution to PulseController so the
       persona processes the event via a playbook.

    Args:
        persona_id: Target persona ID.
        event_description: Human-readable description (becomes user_input).
        meta_playbook: Meta playbook to use (default: "meta_user").
        playbook_params_json: JSON string of playbook params (incl. selected_playbook).
        event_type: Event type tag for persona_event_log.
        _manager: SAIVerseManager reference (injected by PhenomenonManager).
    """
    if _manager is None:
        LOGGER.error(
            "[inject_persona_event] No _manager provided — cannot inject event for %s",
            persona_id,
        )
        return "error: no manager reference"

    # --- 1. Record to persona_event_log ---
    try:
        _manager.record_persona_event(
            persona_id=persona_id,
            content=event_description,
            event_type=event_type,
            payload=playbook_params_json,
        )
        LOGGER.info(
            "[inject_persona_event] Recorded event for %s (type=%s)",
            persona_id, event_type,
        )
    except Exception:
        LOGGER.error(
            "[inject_persona_event] Failed to record event for %s",
            persona_id, exc_info=True,
        )

    # --- 2. Submit to PulseController ---
    pulse_controller = getattr(_manager, "pulse_controller", None)
    if pulse_controller is None:
        LOGGER.error(
            "[inject_persona_event] No pulse_controller on manager — cannot trigger playbook"
        )
        return "error: no pulse_controller"

    # Resolve persona's current building
    persona = _manager.all_personas.get(persona_id)
    if persona is None:
        LOGGER.warning(
            "[inject_persona_event] Persona %s not found in all_personas", persona_id
        )
        return f"error: persona {persona_id} not found"

    building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        LOGGER.warning(
            "[inject_persona_event] Persona %s has no current_building_id", persona_id
        )
        return f"error: persona {persona_id} has no building"

    # Parse playbook params
    playbook_params = None
    if playbook_params_json:
        try:
            playbook_params = json.loads(playbook_params_json)
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning(
                "[inject_persona_event] Failed to parse playbook_params_json: %s",
                playbook_params_json,
            )

    # Build enriched user input with <system> tag
    # Extract author info from playbook_params for richer context
    extra_lines = []
    if playbook_params:
        author_username = playbook_params.get("trigger_author_username")
        author_name = playbook_params.get("trigger_author_name")
        mention_text = playbook_params.get("trigger_mention_text")
        if author_username or author_name:
            who = author_name or author_username
            if author_username:
                who = f"{who} (@{author_username})"
            extra_lines.append(f"差出人: {who}")
        if mention_text:
            extra_lines.append(f"内容: {mention_text}")

    extra_block = "\n".join(extra_lines)
    if extra_block:
        user_input = f"""<system>
[外部イベント通知]
{extra_block}
</system>"""
    else:
        user_input = f"""<system>
[外部イベント通知]
{event_description}
</system>"""

    # When playbook_params has selected_playbook, use meta_user_manual
    # which skips the LLM router and goes directly to exec(selected_playbook).
    # The trigger params (trigger_tweet_id, etc.) are forwarded to the sub-playbook
    # via the _initial_params mechanism in the runtime.
    if playbook_params and "selected_playbook" in playbook_params:
        effective_playbook = "meta_user_manual"
        LOGGER.info(
            "[inject_persona_event] Using meta_user_manual with selected_playbook='%s'",
            playbook_params["selected_playbook"],
        )
    else:
        effective_playbook = meta_playbook or "meta_user"

    try:
        pulse_controller.submit_schedule(
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata={"source": "external_event", "event_type": event_type},
            meta_playbook=effective_playbook,
            playbook_params=playbook_params,
        )
        LOGGER.info(
            "[inject_persona_event] Submitted to PulseController: persona=%s, playbook=%s, params=%s",
            persona_id, effective_playbook, playbook_params,
        )
        return "ok"
    except Exception:
        LOGGER.error(
            "[inject_persona_event] Failed to submit to PulseController",
            exc_info=True,
        )
        return "error: pulse submission failed"


def schema() -> PhenomenonSchema:
    return PhenomenonSchema(
        name="inject_persona_event",
        description="外部イベントをペルソナに注入し、Playbook実行をトリガーする。"
        "IntegrationManager → PhenomenonManager → PulseController のブリッジ。",
        parameters={
            "type": "object",
            "properties": {
                "persona_id": {
                    "type": "string",
                    "description": "対象のペルソナID",
                },
                "event_description": {
                    "type": "string",
                    "description": "イベントの説明（ペルソナへのプロンプトになる）",
                },
                "meta_playbook": {
                    "type": "string",
                    "description": "使用するメタPlaybook名（デフォルト: meta_user）",
                },
                "playbook_params_json": {
                    "type": "string",
                    "description": "Playbook実行パラメータ（JSON文字列）",
                },
                "event_type": {
                    "type": "string",
                    "description": "イベント種別タグ（persona_event_log用）",
                },
            },
            "required": ["persona_id", "event_description"],
        },
        is_async=True,
    )
