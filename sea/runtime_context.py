from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from saiverse.model_configs import (
    calculate_cost,
    get_context_length,
    get_model_display_name,
    get_model_pricing,
    get_model_provider,
)

LOGGER = logging.getLogger(__name__)

def prepare_context(runtime, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None, warnings: Optional[List[Dict[str, Any]]] = None, preview_only: bool = False) -> List[Dict[str, Any]]:
    from sea.playbook_models import ContextRequirements

    # Use provided requirements or default to full context
    reqs = requirements if requirements else ContextRequirements()

    messages: List[Dict[str, Any]] = []

    # ---- system prompt ----
    if reqs.system_prompt:
        system_sections: List[str] = []

        # 1. Common prompt (world setting, framework explanation)
        common_prompt_template = getattr(persona, "common_prompt", None)
        LOGGER.debug("common_prompt_template is %s (type=%s)", common_prompt_template, type(common_prompt_template))
        if common_prompt_template:
            try:
                # Get building info for variable expansion
                building_obj = getattr(persona, "buildings", {}).get(building_id)
                building_name = building_obj.name if building_obj else building_id
                city_name = getattr(persona, "current_city_id", "unknown_city")

                # Expand variables in common prompt using safe replace (avoid conflict with JSON examples)
                common_text = common_prompt_template
                replacements = {
                    "{current_persona_name}": getattr(persona, "persona_name", "Unknown"),
                    "{current_persona_id}": getattr(persona, "persona_id", "unknown_id"),
                    "{current_building_name}": building_name,
                    "{current_city_name}": city_name,
                    "{current_persona_system_instruction}": getattr(persona, "persona_system_instruction", ""),
                    "{current_building_system_instruction}": getattr(building_obj, "system_instruction", "") if building_obj else "",
                    "{linked_user_name}": getattr(persona, "linked_user_name", "the user"),
                }
                for placeholder, value in replacements.items():
                    common_text = common_text.replace(placeholder, value)
                system_sections.append(common_text.strip())
            except Exception as exc:
                LOGGER.error("Failed to format common prompt: %s", exc, exc_info=True)

        # 2. "## あなたについて" section
        persona_section_parts: List[str] = []
        persona_sys = getattr(persona, "persona_system_instruction", "") or ""
        if persona_sys:
            persona_section_parts.append(persona_sys.strip())

        # persona inventory
        if reqs.inventory:
            try:
                inv_builder = getattr(persona, "_inventory_summary_lines", None)
                inv_lines: List[str] = inv_builder() if callable(inv_builder) else []
            except Exception:
                inv_lines = []
            if inv_lines:
                persona_section_parts.append("### インベントリ\n" + "\n".join(inv_lines))

        if persona_section_parts:
            system_sections.append("## あなたについて\n" + "\n\n".join(persona_section_parts))

        # 3. "## {building_name}" section (current location)
        try:
            building_obj = getattr(persona, "buildings", {}).get(building_id)
            if building_obj:
                building_section_parts: List[str] = []

                # Building system instruction
                # NOTE: Datetime variables ({current_time}, etc.) are no longer expanded here.
                # Time information is now provided via Realtime Context at the end of messages
                # to improve LLM context caching efficiency.
                # Use base_system_instruction (without items) to avoid duplication
                # with the building_items block below.
                building_sys = getattr(building_obj, "base_system_instruction", None) or getattr(building_obj, "system_instruction", None)
                if building_sys:
                    building_section_parts.append(str(building_sys).strip())

                # Building items
                if reqs.building_items:
                    try:
                        items_by_building = getattr(runtime.manager, "items_by_building", {}) or {}
                        item_registry = getattr(runtime.manager, "item_registry", {}) or {}
                        b_items = items_by_building.get(building_id, [])
                        lines = []
                        for iid in b_items:
                            data = item_registry.get(iid, {})
                            raw_name = data.get("name", "") or ""
                            name = raw_name.strip() if raw_name.strip() else "(名前なし)"
                            desc = (data.get("description") or "").strip() or "(説明なし)"
                            lines.append(f"- [{iid}] {name}: {desc}")
                        if lines:
                            building_section_parts.append("### 建物内のアイテム\n" + "\n".join(lines))
                    except Exception:
                        LOGGER.warning("Failed to collect building items for %s", building_id, exc_info=True)

                if building_section_parts:
                    building_name = getattr(building_obj, "name", building_id)
                    system_sections.append(f"## {building_name} (ID: {building_id})\n" + "\n\n".join(building_section_parts))
        except Exception:
            LOGGER.warning("Failed to build building section for system prompt", exc_info=True)

        # 4. "## 利用可能な能力" section (available playbooks)
        if reqs.available_playbooks:
            try:
                from tools import TOOL_REGISTRY
                list_playbooks_func = TOOL_REGISTRY.get("list_available_playbooks")
                if list_playbooks_func:
                    # Get available playbooks JSON (tool returns string; accept old tuple form)
                    playbooks_raw = list_playbooks_func(
                        persona_id=getattr(persona, "persona_id", None),
                        building_id=building_id
                    )
                    playbooks_json = playbooks_raw[0] if isinstance(playbooks_raw, tuple) else playbooks_raw
                    if playbooks_json:
                        import json
                        playbooks_list = json.loads(playbooks_json)
                        if playbooks_list:
                            playbooks_formatted = json.dumps(playbooks_list, ensure_ascii=False, indent=2)
                            system_sections.append(f"## 利用可能な能力\n以下のPlaybookを実行できます：\n```json\n{playbooks_formatted}\n```")
            except Exception as exc:
                LOGGER.debug("Failed to add available playbooks section: %s", exc)

        # 5. "## 現在の状況" section (working memory)
        if reqs.working_memory:
            try:
                sai_mem = getattr(persona, "sai_memory", None)
                if sai_mem and sai_mem.is_ready():
                    wm_data = sai_mem.load_working_memory()
                    if wm_data:
                        import json as json_mod
                        wm_text = json_mod.dumps(wm_data, ensure_ascii=False, indent=2)
                        system_sections.append(f"## 現在の状況\n```json\n{wm_text}\n```")
                        LOGGER.debug("[sea][prepare-context] Added working_memory section")
            except Exception as exc:
                LOGGER.debug("Failed to add working_memory section: %s", exc)

        # NOTE: Spatial context (Unity) has been moved to Realtime Context
        # to improve LLM context caching efficiency.

        system_text = "\n\n---\n\n".join([s for s in system_sections if s])
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # ---- Memory Weave context (Chronicle + Memopedia) ----
    # Inserted between system prompt and visual context
    _mw_persona_enabled = runtime._is_memory_weave_context_enabled(persona) if reqs.memory_weave else False
    LOGGER.info("[sea][prepare-context] memory_weave=%s, persona_enabled=%s", reqs.memory_weave, _mw_persona_enabled)
    if reqs.memory_weave and _mw_persona_enabled:
        try:
            from builtin_data.tools.get_memory_weave_context import get_memory_weave_context
            from tools.context import persona_context
            persona_id = getattr(persona, "persona_id", None)

            # Get persona_dir from sai_memory adapter (same pattern as working_memory)
            sai_mem = getattr(persona, "sai_memory", None)
            persona_dir_path = getattr(sai_mem, "persona_dir", None) if sai_mem else None
            persona_dir = str(persona_dir_path) if persona_dir_path else None

            LOGGER.info("[sea][prepare-context] Calling get_memory_weave_context for persona=%s dir=%s", persona_id, persona_dir)
            with persona_context(persona_id, persona_dir, runtime.manager):
                mw_messages = get_memory_weave_context(persona_id=persona_id, persona_dir=persona_dir)
            LOGGER.info("[sea][prepare-context] get_memory_weave_context returned %d messages", len(mw_messages))
            if mw_messages:
                messages.extend(mw_messages)
                LOGGER.debug("[sea][prepare-context] Added %d Memory Weave context messages", len(mw_messages))
        except Exception as exc:
            LOGGER.exception("[sea][prepare-context] Failed to get Memory Weave context: %s", exc)

    # ---- visual context (Building / Persona images) ----
    # Inserted right after system prompt but before conversation history
    if reqs.visual_context:
        try:
            from builtin_data.tools.get_visual_context import get_visual_context
            from tools.context import persona_context, get_active_manager
            persona_id = getattr(persona, "persona_id", None)
            persona_dir = getattr(persona, "persona_dir", None)
            with persona_context(persona_id, persona_dir, runtime.manager):
                visual_messages = get_visual_context(building_id=building_id)
            if visual_messages:
                messages.extend(visual_messages)
                LOGGER.debug("[sea][prepare-context] Added %d visual context messages", len(visual_messages))
        except Exception as exc:
            LOGGER.debug("[sea][prepare-context] Failed to get visual context: %s", exc)

    # ---- history ----
    history_depth = reqs.history_depth
    if history_depth not in [0, "none"]:
        history_mgr = getattr(persona, "history_manager", None)
        if history_mgr:
            try:
                # Determine which tags to include
                required_tags = ["conversation"]
                if reqs.include_internal:
                    required_tags.append("internal")

                # Parse history_depth format
                # - "full": use max_history_messages (message count) or context_length (character limit)
                # - "Nmessages" (e.g., "10messages"): message count limit
                # - integer or numeric string: character limit
                use_message_count = False
                limit_value = 2000  # fallback
                used_anchor = False
                recent = []

                if history_depth == "full":
                    metabolism_enabled = getattr(runtime.manager, "metabolism_enabled", False) if runtime.manager else False

                    if metabolism_enabled and not preview_only:
                        # Persistent anchor resolution with 3-level fallback
                        anchor_id, resolution = runtime._resolve_metabolism_anchor(persona)

                        if anchor_id:
                            # Case 1 or 2: valid anchor found
                            recent_from_anchor = history_mgr.get_history_from_anchor(
                                anchor_id, required_tags=required_tags, pulse_id=pulse_id,
                            )
                            if recent_from_anchor:
                                recent = recent_from_anchor
                                used_anchor = True
                                history_mgr.metabolism_anchor_message_id = anchor_id
                                LOGGER.debug(
                                    "[sea][prepare-context] Anchor-based retrieval (%s): %d messages from anchor %s",
                                    resolution, len(recent), anchor_id,
                                )
                                # Persist anchor for current model (touch updated_at)
                                persona_model = getattr(persona, "model", None)
                                if persona_model:
                                    runtime._update_anchor_for_model(persona, persona_model, anchor_id)
                        else:
                            # Case 3: no valid anchor — minimal load + Chronicle generation
                            memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
                            if memory_weave_enabled and runtime._is_chronicle_enabled_for_persona(persona):
                                try:
                                    LOGGER.info("[metabolism] Triggering Chronicle generation on anchor expiry")
                                    runtime._generate_chronicle(persona)
                                except Exception as exc:
                                    LOGGER.warning("[metabolism] Chronicle generation on anchor expiry failed: %s", exc)

                            # Load minimal history (low watermark)
                            low_wm = runtime._get_low_watermark(persona)
                            limit_value = low_wm if low_wm and low_wm > 0 else 20
                            use_message_count = True
                            LOGGER.debug(
                                "[sea][prepare-context] Minimal load (no valid anchor): %d messages",
                                limit_value,
                            )

                    elif metabolism_enabled and preview_only:
                        # Preview mode: use anchor for retrieval but don't persist or generate Chronicle
                        anchor_id, resolution = runtime._resolve_metabolism_anchor(persona)
                        if anchor_id:
                            recent_from_anchor = history_mgr.get_history_from_anchor(
                                anchor_id, required_tags=required_tags, pulse_id=pulse_id,
                            )
                            if recent_from_anchor:
                                recent = recent_from_anchor
                                used_anchor = True
                        if not used_anchor:
                            low_wm = runtime._get_low_watermark(persona)
                            limit_value = low_wm if low_wm and low_wm > 0 else 20
                            use_message_count = True

                    if not used_anchor and not metabolism_enabled:
                        # Metabolism disabled — traditional count/char retrieval
                        max_hist_msgs = getattr(runtime.manager, "max_history_messages_override", None) if runtime.manager else None
                        if max_hist_msgs is None:
                            from saiverse.model_configs import get_default_max_history_messages
                            persona_model = getattr(persona, "model", None)
                            if persona_model:
                                max_hist_msgs = get_default_max_history_messages(persona_model)
                        if max_hist_msgs is not None:
                            limit_value = max_hist_msgs
                            use_message_count = True
                            LOGGER.debug("[sea][prepare-context] Using max_history_messages=%d", max_hist_msgs)
                        else:
                            limit_value = getattr(persona, "context_length", 2000)

                elif isinstance(history_depth, str) and history_depth.endswith("messages"):
                    # Message count mode: "10messages", "20messages", etc.
                    try:
                        limit_value = int(history_depth[:-8])  # Remove "messages" suffix
                        use_message_count = True
                    except ValueError:
                        limit_value = 10  # fallback for message count
                        use_message_count = True
                else:
                    try:
                        limit_value = int(history_depth)
                    except (ValueError, TypeError):
                        limit_value = 2000  # fallback

                # Fetch history if not already retrieved via anchor
                if not used_anchor:
                    LOGGER.debug("[sea][prepare-context] Fetching history: limit=%d, mode=%s, pulse_id=%s, balanced=%s, tags=%s",
                                limit_value, "messages" if use_message_count else "chars", pulse_id, reqs.history_balanced, required_tags)

                    if use_message_count:
                        # Message count mode - balanced not supported yet
                        recent = history_mgr.get_recent_history_by_count(
                            limit_value,
                            required_tags=required_tags,
                            pulse_id=pulse_id,
                        )
                    elif reqs.history_balanced:
                        # Get conversation partners for balanced retrieval
                        participant_ids = ["user"]
                        occupants = runtime.manager.occupants.get(building_id, [])
                        persona_id = getattr(persona, "persona_id", None)
                        for oid in occupants:
                            if oid != persona_id:
                                participant_ids.append(oid)
                        LOGGER.debug("[sea][prepare-context] Balancing across: %s", participant_ids)
                        recent = history_mgr.get_recent_history_balanced(
                            limit_value,
                            participant_ids,
                            required_tags=required_tags,
                            pulse_id=pulse_id,
                        )
                    else:
                        # Filter by required tags or current pulse_id
                        recent = history_mgr.get_recent_history(
                            limit_value,
                            required_tags=required_tags,
                            pulse_id=pulse_id,
                        )

                    # Set metabolism anchor on first count-based retrieval and persist (skip in preview)
                    metabolism_enabled_for_anchor = getattr(runtime.manager, "metabolism_enabled", False) if runtime.manager else False
                    if metabolism_enabled_for_anchor and recent and not preview_only:
                        oldest_id = recent[0].get("id")
                        if oldest_id:
                            history_mgr.metabolism_anchor_message_id = oldest_id
                            persona_model = getattr(persona, "model", None)
                            if persona_model:
                                runtime._update_anchor_for_model(persona, persona_model, oldest_id)
                            LOGGER.debug("[sea][prepare-context] Set metabolism anchor to %s (persisted)", oldest_id)

                LOGGER.debug("[sea][prepare-context] Got %d history messages", len(recent))
                # Enrich messages with attachment context
                enriched_recent = runtime._enrich_history_with_attachments(recent)
                messages.extend(enriched_recent)
            except Exception as exc:
                LOGGER.exception("[sea][prepare-context] Failed to get history: %s", exc)

    # ---- Realtime Context ----
    # Time-sensitive info placed just BEFORE the last user message to improve LLM caching.
    # This ensures LLM responds to user input, not the realtime context.
    if reqs.realtime_context:
        try:
            realtime_msg = runtime._build_realtime_context(persona, building_id, messages)
            if realtime_msg:
                # Find the last user message and insert realtime context before it
                last_user_idx = None
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user" and not messages[i].get("metadata", {}).get("__realtime_context__"):
                        last_user_idx = i
                        break

                if last_user_idx is not None:
                    # Insert before last user message
                    messages.insert(last_user_idx, realtime_msg)
                    LOGGER.debug("[sea][prepare-context] Added realtime context before last user message (idx=%d)", last_user_idx)
                else:
                    # No user message found, append at end
                    messages.append(realtime_msg)
                    LOGGER.debug("[sea][prepare-context] Added realtime context at end (no user message found)")
        except Exception as exc:
            LOGGER.debug("[sea][prepare-context] Failed to build realtime context: %s", exc)

    # ---- Token budget check ----
    try:
        from saiverse.token_estimator import estimate_messages_tokens
        from saiverse.model_configs import get_context_length, get_model_provider

        persona_model = getattr(persona, "model", None)
        if persona_model:
            provider = get_model_provider(persona_model)
            context_length = get_context_length(persona_model)
            estimated_tokens = estimate_messages_tokens(messages, provider)
            LOGGER.debug(
                "[sea][prepare-context] Token budget: estimated=%d, limit=%d (model=%s)",
                estimated_tokens, context_length, persona_model,
            )

            if estimated_tokens > context_length:
                # Over budget: trim history messages from oldest until within budget
                # Find indices of history messages (not system, not visual context, not realtime)
                history_indices = []
                for i, msg in enumerate(messages):
                    meta = msg.get("metadata") or {}
                    if (
                        msg.get("role") != "system"
                        and not meta.get("__visual_context__")
                        and not meta.get("__realtime_context__")
                        and not meta.get("__memory_weave_context__")
                    ):
                        history_indices.append(i)

                original_count = len(history_indices)
                # Remove oldest history messages until under budget
                while history_indices and estimated_tokens > context_length:
                    remove_idx = history_indices.pop(0)
                    removed_msg = messages[remove_idx]
                    removed_tokens = estimate_messages_tokens([removed_msg], provider)
                    estimated_tokens -= removed_tokens
                    messages[remove_idx] = None  # mark for removal

                # Clean up None entries
                messages = [m for m in messages if m is not None]
                remaining_count = len(history_indices)

                warning_msg = {
                    "type": "warning",
                    "warning_code": "context_auto_trimmed",
                    "content": (
                        f"コンテキスト超過のため、履歴を直近{original_count}件→{remaining_count}件に"
                        f"自動削減しました（推定: {estimated_tokens:,} / {context_length:,}トークン）。"
                        f"ChatOptionsでメッセージ数上限を下げてください。"
                    ),
                }
                LOGGER.warning(
                    "[sea][prepare-context] Context auto-trimmed: %d -> %d messages (est=%d, limit=%d)",
                    original_count, remaining_count, estimated_tokens, context_length,
                )
                if warnings is not None:
                    warnings.append(warning_msg)

            elif estimated_tokens > context_length * 0.85:
                # Approaching limit: warn but continue
                warning_msg = {
                    "type": "warning",
                    "warning_code": "context_approaching_limit",
                    "content": (
                        f"コンテキスト使用量がモデルの上限に近づいています"
                        f"（推定: {estimated_tokens:,} / {context_length:,}トークン）。"
                        f"ChatOptionsでメッセージ数上限を下げることを検討してください。"
                    ),
                }
                LOGGER.warning(
                    "[sea][prepare-context] Context approaching limit: est=%d, limit=%d (%.0f%%)",
                    estimated_tokens, context_length, estimated_tokens / context_length * 100,
                )
                if warnings is not None:
                    warnings.append(warning_msg)
    except Exception as exc:
        LOGGER.debug("[sea][prepare-context] Token budget check failed: %s", exc)

    return messages

def preview_context(
    runtime,
    persona: Any,
    building_id: str,
    user_input: str,
    meta_playbook: Optional[str] = None,
    image_count: int = 0,
    document_count: int = 0,
) -> Dict[str, Any]:
    """Build the context that would be sent to the LLM, without executing anything.

    Returns a dict with messages, token estimates, cost estimates, and model info.
    Does NOT record the user message to history or call any LLM.
    """
    from saiverse.token_estimator import estimate_messages_tokens, estimate_image_tokens
    from saiverse.model_configs import (
        get_model_provider, get_context_length,
        get_model_display_name, get_model_pricing, calculate_cost,
    )

    # Select playbook (same logic as run_meta_user)
    if meta_playbook:
        playbook = runtime._load_playbook_for(meta_playbook, persona, building_id)
        if playbook is None:
            playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)
    else:
        playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)

    # Build context messages (without recording user message to history)
    # Use "conversation" profile requirements to match what sub_speak actually sees,
    # not the meta-playbook's own context_requirements (which may lack memory_weave etc.)
    from sea.playbook_models import CONTEXT_PROFILES
    preview_requirements = CONTEXT_PROFILES["conversation"]["requirements"]
    context_warnings: List[Dict[str, Any]] = []
    messages = runtime._prepare_context(
        persona, building_id, user_input=None,
        requirements=preview_requirements,
        warnings=context_warnings,
        preview_only=True,
    )

    # Append the user message manually (in real flow it comes from history)
    if user_input:
        messages.append({"role": "user", "content": user_input})

    # Classify each message into a section
    persona_model = getattr(persona, "model", None) or "gemini-2.5-flash-lite-preview-09-2025"
    provider = get_model_provider(persona_model)

    section_order = [
        "system_prompt", "memory_weave_chronicle", "memory_weave_memopedia",
        "memory_weave", "visual_context",
        "history", "realtime_context", "user_message",
    ]
    section_labels = {
        "system_prompt": "System Prompt",
        "memory_weave_chronicle": "Memory Weave — Chronicle",
        "memory_weave_memopedia": "Memory Weave — Memopedia",
        "memory_weave": "Memory Weave",
        "visual_context": "Visual Context",
        "history": "Conversation History",
        "realtime_context": "Realtime Context",
        "user_message": "Your Message",
        "attachments": "Attachments",
    }
    section_tokens: Dict[str, int] = {s: 0 for s in section_order}
    section_tokens["attachments"] = 0
    section_msg_counts: Dict[str, int] = {s: 0 for s in section_order}
    section_msg_counts["attachments"] = 0

    annotated_messages: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        meta = msg.get("metadata") or {}
        # Determine section
        if msg.get("role") == "system":
            section = "system_prompt"
        elif meta.get("__memory_weave_context__"):
            mw_type = meta.get("__memory_weave_type__", "")
            if mw_type == "chronicle":
                section = "memory_weave_chronicle"
            elif mw_type == "memopedia":
                section = "memory_weave_memopedia"
            else:
                section = "memory_weave"
        elif meta.get("__visual_context__"):
            section = "visual_context"
        elif meta.get("__realtime_context__"):
            section = "realtime_context"
        elif i == len(messages) - 1 and msg.get("role") == "user" and user_input and msg.get("content") == user_input:
            section = "user_message"
        else:
            section = "history"

        msg_tokens = estimate_messages_tokens([msg], provider)
        section_tokens[section] += msg_tokens
        section_msg_counts[section] += 1

        annotated_messages.append({
            "role": msg.get("role", "unknown"),
            "content": msg.get("content", ""),
            "section": section,
            "tokens": msg_tokens,
        })

    # Add estimated attachment tokens
    attachment_tokens = 0
    if image_count > 0:
        attachment_tokens += image_count * estimate_image_tokens(provider)
    if document_count > 0:
        # Rough estimate: ~500 tokens per document (varies widely)
        attachment_tokens += document_count * 500
    section_tokens["attachments"] = attachment_tokens

    total_input_tokens = sum(section_tokens.values())
    context_length = get_context_length(persona_model)
    pricing = get_model_pricing(persona_model)

    # Cost range: best case (all cached) to worst case (all cache-write)
    cache_kwargs = runtime._get_cache_kwargs()
    cache_enabled = cache_kwargs.get("enable_cache", False)
    cache_ttl = cache_kwargs.get("cache_ttl", "5m")

    # Determine cache type (explicit for Anthropic, implicit for Gemini, etc.)
    from saiverse.model_configs import get_cache_config
    cache_config = get_cache_config(persona_model)
    cache_type = cache_config.get("type", "implicit")

    if cache_enabled and pricing and pricing.get("cached_input_per_1m_tokens") is not None:
        # Best case: everything is a cache hit
        cost_best = calculate_cost(
            persona_model, total_input_tokens, 0,
            cached_tokens=total_input_tokens, cache_write_tokens=0,
        )
        # Worst case: everything is a cache write
        cost_worst = calculate_cost(
            persona_model, total_input_tokens, 0,
            cached_tokens=0, cache_write_tokens=total_input_tokens,
            cache_ttl=cache_ttl,
        )
    else:
        # No cache: single estimate
        cost_best = calculate_cost(persona_model, total_input_tokens, 0)
        cost_worst = cost_best

    # Build sections summary
    all_sections = section_order + ["attachments"]
    sections_summary = []
    for s in all_sections:
        if section_tokens.get(s, 0) > 0 or section_msg_counts.get(s, 0) > 0:
            sections_summary.append({
                "name": s,
                "label": section_labels.get(s, s),
                "tokens": section_tokens.get(s, 0),
                "message_count": section_msg_counts.get(s, 0),
            })

    return {
        "persona_id": getattr(persona, "persona_id", "unknown"),
        "persona_name": getattr(persona, "persona_name", "Unknown"),
        "model": persona_model,
        "model_display_name": get_model_display_name(persona_model),
        "provider": provider,
        "context_length": context_length,
        "sections": sections_summary,
        "total_input_tokens": total_input_tokens,
        "estimated_cost_best_usd": round(cost_best, 6),
        "estimated_cost_worst_usd": round(cost_worst, 6),
        "cache_enabled": cache_enabled,
        "cache_ttl": cache_ttl if cache_enabled else None,
        "cache_type": cache_type if cache_enabled else None,
        "pricing": pricing or {},
        "messages": annotated_messages,
    }
