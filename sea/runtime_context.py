from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from saiverse.model_configs import (
    calculate_cost,
    get_context_length,
    get_model_display_name,
    get_model_pricing,
    get_model_provider,
)
from tools import SPELL_TOOL_SCHEMAS, TOOL_REGISTRY

LOGGER = logging.getLogger(__name__)

def prepare_context(runtime, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None, warnings: Optional[List[Dict[str, Any]]] = None, preview_only: bool = False, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, cancellation_token: Optional[Any] = None) -> List[Dict[str, Any]]:
    from sea.playbook_models import ContextRequirements

    # Use provided requirements or default to full context
    reqs = requirements if requirements else ContextRequirements()

    messages: List[Dict[str, Any]] = []

    # ---- head: system prompt + Memory Weave + Visual Context ----
    # Cached Head Architecture (Phase 2-h) で section pipeline 経由に統一済み。
    # 旧 live state 直読み経路 (= section 群を毎回ここで組み立てる) は廃止。
    # snapshot 不在時は ensure_snapshot 経由で初回 capture が自動で走る。
    # 詳細: docs/intent/cached_head_architecture.md
    enabled_sections: set[str] = set()
    if reqs.system_prompt:
        enabled_sections.update({
            "common_prompt", "persona_self", "building", "spell_list",
        })
        if reqs.available_playbooks:
            enabled_sections.add("available_playbooks")
    if reqs.memory_weave:
        enabled_sections.add("memory_weave")
    if reqs.visual_context:
        enabled_sections.add("visual_context")

    if enabled_sections:
        try:
            from sea.head_pipeline import render_head_messages
            head_messages = render_head_messages(
                persona, runtime.manager, building_id,
                enabled_sections=enabled_sections,
            )
            if head_messages:
                messages.extend(head_messages)
                LOGGER.debug(
                    "[sea][prepare-context] Added %d head messages via pipeline",
                    len(head_messages),
                )
        except Exception:
            LOGGER.exception(
                "[sea][prepare-context] Failed to render head via cached_head_architecture",
            )

    # ---- history ----
    history_depth = reqs.history_depth
    if history_depth not in [0, "none"]:
        history_mgr = getattr(persona, "history_manager", None)
        if history_mgr:
            try:
                # Phase 3 段階 4-A (line vs tag responsibility separation,
                # 2026-05-01): context construction filters on line_role / scope
                # instead of metadata.tags. Tags are now reserved for semantic
                # classification (search / recall / Chronicle / Memopedia).
                #
                # Default policy:
                #   - line_role IN ('main_line') AND scope IN ('committed')
                #     → ペルソナのメインラインの会話履歴のみを context に含める
                #   - meta_judgment Pulse の discardable メッセージは別経路
                #     (MetaLayer._build_recent_judgments_block) で動的注入される
                #     ので、ここでは載せない
                #
                # 段階 4-D (2026-05-09): 旧 ``include_internal`` フォールバックを
                # 削除。サブラインメッセージへのアクセスは sub_line が起動する
                # 専用 Playbook 内で完結する設計に統合済み。
                required_line_roles = ["main_line"]
                required_scopes = ["committed"]

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
                                anchor_id,
                                required_line_roles=required_line_roles,
                                required_scopes=required_scopes,
                                pulse_id=pulse_id,
                            )
                            if recent_from_anchor:
                                recent = recent_from_anchor
                                used_anchor = True
                                history_mgr.metabolism_anchor_message_id = anchor_id
                                LOGGER.debug(
                                    "[sea][prepare-context] Anchor-based retrieval (%s): %d messages from anchor %s",
                                    resolution, len(recent), anchor_id,
                                )
                                # Phase 4-e: anchor の updated_at touch は LLM 呼び出し成功後
                                # (`runtime_llm.py` の usage 確認位置) で行う。ここで先行 touch
                                # すると、LLM 失敗時に「実際は cache 切れてるのに TTL 内」と
                                # 誤判定して次回も長大コンテキストを送る不整合になるため。
                        else:
                            # Case 3: no valid anchor — minimal load + Chronicle generation
                            memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
                            if memory_weave_enabled and runtime._is_chronicle_enabled_for_persona(persona):
                                if event_callback:
                                    event_callback({
                                        "type": "metabolism",
                                        "status": "started",
                                        "content": "Chronicleを生成しています...",
                                    })
                                try:
                                    LOGGER.info("[metabolism] Triggering Chronicle generation on anchor expiry")
                                    runtime._generate_chronicle(
                                        persona,
                                        event_callback=event_callback,
                                        cancellation_token=cancellation_token,
                                    )
                                    # Track Chronicle (v0.32, 2026-05-09): General と並行で走らせる
                                    try:
                                        runtime._generate_track_chronicle(persona)
                                    except Exception as exc:
                                        LOGGER.warning(
                                            "[metabolism] Track Chronicle generation on anchor expiry failed: %s",
                                            exc,
                                        )
                                    # Pre-response metabolism で発生した Memopedia 変化を即座に
                                    # event_message として SAIMemory に挿入する。これがないと、
                                    # 続く履歴取得で event_message が拾えず、AI 応答コンテキストに
                                    # 反映されないため、ペルソナは「自分が直前に行った記憶整理」を
                                    # 同じターンの応答時に認識できない（次ターンで初めて検知）。
                                    try:
                                        from saiverse.dynamic_state import DynamicStateManager
                                        DynamicStateManager.maybe_inject_event_messages(persona, runtime.manager)
                                    except Exception:
                                        LOGGER.exception("[dynamic_state] Event injection after pre-response metabolism failed")
                                except Exception as exc:
                                    LOGGER.warning("[metabolism] Chronicle generation on anchor expiry failed: %s", exc)
                                if event_callback:
                                    event_callback({
                                        "type": "metabolism",
                                        "status": "completed",
                                        "content": "Chronicle生成が完了しました",
                                    })

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
                                anchor_id,
                                required_line_roles=required_line_roles,
                                required_scopes=required_scopes,
                                pulse_id=pulse_id,
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
                    LOGGER.debug(
                        "[sea][prepare-context] Fetching history: limit=%d, mode=%s, pulse_id=%s, "
                        "balanced=%s, line_roles=%s, scopes=%s",
                        limit_value, "messages" if use_message_count else "chars", pulse_id,
                        reqs.history_balanced, required_line_roles, required_scopes,
                    )

                    if use_message_count:
                        # Message count mode - balanced not supported yet
                        recent = history_mgr.get_recent_history_by_count(
                            limit_value,
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
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
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
                            pulse_id=pulse_id,
                        )
                    else:
                        recent = history_mgr.get_recent_history(
                            limit_value,
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
                            pulse_id=pulse_id,
                        )

                    # Set metabolism anchor on first count-based retrieval (skip in preview).
                    # Phase 4-e: DB への永続化 (updated_at 書き込み) は LLM 呼び出し成功後に行う。
                    # ここで anchor を立てたまま LLM 失敗 → DB 未書き込み → 次回も Case 3 fallback
                    # → minimal load、という挙動になる。新規 anchor が永続化されないこと自体は
                    # Case 3 を毎回繰り返すだけで致命的ではない (まはー確認済 2026-05-08)。
                    metabolism_enabled_for_anchor = getattr(runtime.manager, "metabolism_enabled", False) if runtime.manager else False
                    if metabolism_enabled_for_anchor and recent and not preview_only:
                        oldest_id = recent[0].get("id")
                        if oldest_id:
                            history_mgr.metabolism_anchor_message_id = oldest_id
                            LOGGER.debug("[sea][prepare-context] Set metabolism anchor (in-memory) to %s; DB persist deferred to post-LLM-success", oldest_id)

                LOGGER.debug("[sea][prepare-context] Got %d history messages", len(recent))
                # Enrich messages with attachment context
                enriched_recent = runtime._enrich_history_with_attachments(recent)

                # ユーザー会話 Track 親保持機構 (v0.32, 2026-05-09)
                # オーナーユーザーとの会話メッセージを history 内既存数で不足する分だけ
                # 上部に補完する。Metabolism やコンテキスト圧縮で生メッセージが消えても
                # 親スレッドとして必ず一定数を確保する。詳細:
                # docs/intent/persona_cognition/track_chronicle.md (Stelis 親子モデル流用)
                supplementary_msgs: List[Dict[str, Any]] = []
                supplementary_oldest_ts: Optional[int] = None
                try:
                    from saiverse.user_conversation_preserver import (
                        get_owner_user_conversation_track_id,
                        get_supplementary_user_conversation_messages,
                    )
                    persona_id_for_owner = getattr(persona, "persona_id", None)
                    if persona_id_for_owner:
                        owner_track_id = get_owner_user_conversation_track_id(
                            persona_id_for_owner, runtime.manager
                        )
                        if owner_track_id:
                            sai_mem = getattr(persona, "sai_memory", None)
                            supplementary_msgs, supplementary_oldest_ts = (
                                get_supplementary_user_conversation_messages(
                                    sai_mem, owner_track_id, enriched_recent,
                                )
                            )
                except Exception:
                    LOGGER.debug(
                        "[sea][prepare-context] User-conversation supplement failed",
                        exc_info=True,
                    )

                # 時刻アンカー① (v0.32): 上部補完メッセージの直前。
                # 「以下、YYYY-MM-DD HH:MM:SS 以降のユーザーとの会話です」
                if supplementary_msgs:
                    if supplementary_oldest_ts:
                        try:
                            from datetime import datetime as _dt
                            ts_str = _dt.fromtimestamp(int(supplementary_oldest_ts)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            messages.append({
                                "role": "user",
                                "content": f"<system>以下、{ts_str} 以降のユーザーとの会話です</system>",
                            })
                            LOGGER.debug(
                                "[sea][prepare-context] Inserted timestamp anchor①: %s",
                                ts_str,
                            )
                        except (TypeError, ValueError, OSError):
                            pass
                    messages.extend(supplementary_msgs)

                # 時刻アンカー② (v0.32, 2026-05-09): history 内の最古残存メッセージの直前に
                # 「以下、YYYY-MM-DD HH:MM:SS 以降のやり取りです」を揮発挿入する。
                # メッセージそのものに時刻メタを付ける副作用 (ペルソナが時刻を真似する) を
                # 避けるため、Metabolism 起点に 1 か所だけ。詳細は
                # docs/intent/persona_cognition/track_chronicle.md §7
                if enriched_recent:
                    oldest_msg = enriched_recent[0]
                    oldest_ts = oldest_msg.get("created_at") or oldest_msg.get("timestamp")
                    if oldest_ts:
                        try:
                            from datetime import datetime as _dt
                            ts_int = int(oldest_ts)
                            ts_str = _dt.fromtimestamp(ts_int).strftime("%Y-%m-%d %H:%M:%S")
                            anchor_msg = {
                                "role": "user",
                                "content": f"<system>以下、{ts_str} 以降のやり取りです</system>",
                            }
                            messages.append(anchor_msg)
                            LOGGER.debug(
                                "[sea][prepare-context] Inserted timestamp anchor②: %s",
                                ts_str,
                            )
                        except (TypeError, ValueError, OSError):
                            LOGGER.debug(
                                "[sea][prepare-context] Skipping timestamp anchor② (invalid ts=%r)",
                                oldest_ts,
                            )

                messages.extend(enriched_recent)
            except Exception as exc:
                LOGGER.exception("[sea][prepare-context] Failed to get history: %s", exc)

    # ---- Recalled Memory Context — 廃止済み ----
    # recalled_ids はシステムプロンプトへの注入から廃止。
    # recall_entry / recall_navigate ツールが想起時に直接会話履歴へ内容を追記する。

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
                        and not meta.get("__recalled_memory__")
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


def _expand_recalled_ids(
    runtime,
    persona: Any,
    recalled_ids: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
) -> None:
    """Expand recalled_ids into actual content and append as a system message.

    Resolves each recalled ID via URI resolver to get the actual content,
    then appends a single system message at the end of the messages list.
    """
    from saiverse.uri_resolver import UriResolver

    resolver = UriResolver(manager=runtime.manager)
    persona_id = getattr(persona, "persona_id", None)
    if not persona_id:
        return

    sections = []
    for item in recalled_ids:
        uri = item.get("uri", "")
        title = item.get("title", "")
        source_type = item.get("type", "")

        if not uri:
            continue

        try:
            resolved = resolver.resolve(uri, persona_id=persona_id)
            if resolved and resolved.content:
                content = resolved.content
                # Truncate per item to keep context manageable
                if len(content) > 1500:
                    content = content[:1500] + "..."
                label = "Chronicle" if source_type == "chronicle" else "Memopedia"
                sections.append(f"### [{label}] {title}\n{content}")
            else:
                LOGGER.debug(
                    "[sea][prepare-context] Could not resolve recalled URI: %s", uri,
                )
        except Exception as exc:
            LOGGER.debug(
                "[sea][prepare-context] Error resolving recalled URI %s: %s", uri, exc,
            )

    if sections:
        recalled_text = "## 想起した記憶\n以下はワーキングメモリに保持されている記憶です。\n\n" + "\n\n".join(sections)
        messages.append({
            "role": "system",
            "content": recalled_text,
            "metadata": {"__recalled_memory__": True},
        })
        LOGGER.info(
            "[sea][prepare-context] Expanded %d/%d recalled_ids into context (%d chars)",
            len(sections), len(recalled_ids), len(recalled_text),
        )
        LOGGER.debug(
            "[sea][prepare-context] Recalled memory content:\n%s", recalled_text,
        )


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

    # Select playbook (same logic as run_meta_user)
    if meta_playbook:
        playbook = runtime._load_playbook_for(meta_playbook, persona, building_id)
        if playbook is None:
            playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)
    else:
        playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)

    # Build context messages (without recording user message to history)
    # Use full context preset to match what sub_speak actually sees, not the
    # meta-playbook's own context_requirements (which may lack memory_weave etc.)
    # Phase 3 段階 4-D (2026-05-09): 旧 CONTEXT_PROFILES 削除に伴いインライン化。
    from sea.playbook_models import ContextRequirements
    preview_requirements = ContextRequirements(
        history_depth="full",
        history_balanced=False,
        system_prompt=True,
        memory_weave=True,
        working_memory=True,
        inventory=True,
        building_items=True,
        available_playbooks=True,
        visual_context=True,
        realtime_context=True,
    )
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
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    persona_model = getattr(persona, "model", None) or BUILTIN_DEFAULT_LITE_MODEL
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
