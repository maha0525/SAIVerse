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
    args_json: Optional[str] = None,
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
        meta_playbook: Meta playbook to use (default: "track_user_conversation").
        args_json: JSON string of playbook args. ``selected_playbook`` 指定は
            現在無効 (Phase 3 B 残件で pre_spells 経路に置き換え予定;
            handoff_2026-05-08 作業 2)。
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
            payload=args_json,
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

    # --- 2. Submit via PulseDispatcher (pulse_dispatch.md §7) ---
    dispatcher = getattr(_manager, "pulse_dispatcher", None)
    if dispatcher is None:
        LOGGER.error(
            "[inject_persona_event] No pulse_dispatcher on manager — cannot trigger playbook"
        )
        return "error: no pulse_dispatcher"

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

    # Parse playbook args
    playbook_args = None
    if args_json:
        try:
            playbook_args = json.loads(args_json)
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning(
                "[inject_persona_event] Failed to parse args_json: %s",
                args_json,
            )

    # Build enriched user input with <system> tag
    # Extract author info from playbook_args for richer context
    extra_lines = []
    if playbook_args:
        author_username = playbook_args.get("trigger_author_username")
        author_name = playbook_args.get("trigger_author_name")
        mention_text = playbook_args.get("trigger_mention_text")
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

    # selected_playbook 指定経路は Phase 3 移行に伴い一時無効化 (旧 meta_user_manual
    # 経路を削除したため)。Phase 3 B 残件 (handoff_2026-05-08 作業 2) で
    # pre_spells 経由に復活予定。それまではログ警告して通常経路で処理する。
    if playbook_args and "selected_playbook" in playbook_args:
        LOGGER.warning(
            "[inject_persona_event] selected_playbook in args is currently ignored "
            "(meta_user_manual route removed; pre_spells restoration pending in "
            "Phase 3 B). selected_playbook='%s'",
            playbook_args["selected_playbook"],
        )
    effective_playbook = meta_playbook or "track_user_conversation"

    def _dispatch_direct() -> None:
        """従来の応対経路: イベントを <system> プロンプト付き Pulse として submit。"""
        dispatcher.dispatch_phenomenon_event(
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata={"source": "external_event", "event_type": event_type},
            meta_playbook=effective_playbook,
            args=playbook_args,
        )
        LOGGER.info(
            "[inject_persona_event] Dispatched via PulseDispatcher: persona=%s, playbook=%s, args=%s",
            persona_id, effective_playbook, playbook_args,
        )

    try:
        if meta_playbook is None:
            # 自律行動 v2: 既定経路のイベントはイベント到着判断 (on_event) を通す。
            # Active かつユーザー会話中でないペルソナのみ判断が走り、engage_now を
            # 選んだときだけ _dispatch_direct で応対する。非 Active ペルソナ・
            # 会話中・判断が起動できない場合は従来どおり _dispatch_direct
            # (経路の判断基準は saiverse/autonomy_wiring.py handle_external_event)。
            # meta_playbook を明示指定した呼び出し (アドオンの専用経路) は
            # 呼び出し側の意図を尊重して従来どおり直接 dispatch する。
            from saiverse.autonomy_wiring import handle_external_event

            event_text = "\n".join([event_description] + extra_lines)
            route = handle_external_event(
                _manager, persona_id, event_text,
                dispatch_direct=_dispatch_direct,
            )
            LOGGER.info(
                "[inject_persona_event] on_event route=%s (persona=%s, type=%s)",
                route, persona_id, event_type,
            )
        else:
            _dispatch_direct()
        return "ok"
    except Exception:
        LOGGER.error(
            "[inject_persona_event] Failed to dispatch phenomenon event",
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
                    "description": "使用するメタPlaybook名（デフォルト: track_user_conversation）",
                },
                "args_json": {
                    "type": "string",
                    "description": "Playbook実行引数（JSON文字列）",
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
