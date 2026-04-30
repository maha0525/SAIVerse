"""Pulse-root context construction (Intent A v0.14, Intent B v0.11 — Phase 1.1).

Replaces ``context_profile`` as the primary mechanism for assembling the
LLM messages that start a Pulse. The new model is "Track + entry-line +
Handler" driven:

- The persona's currently-running Track determines *which* Handler is in
  charge of the Pulse and what guidance (固定情報) is appropriate.
- The entry line role (``main_line`` / ``sub_line``) determines *which*
  cache layer the Pulse lives in: layer [2] main cache for main_line, layer
  [3] Track-internal sub-cache for sub_line.
- The first Pulse after Track activation (or after the cache TTL expires)
  injects "fixed information" — Track identity, available playbooks,
  pulse_completion_notice — at the cache head. Subsequent Pulses skip the
  fixed block to keep the prompt cache hot.

This module exposes ``prepare_pulse_root_context`` as a parallel path to
the legacy ``runtime_context.prepare_context``. Phase 1.2's
``meta_judgment.json`` consumes this directly; Phase 1.4 migrates the
remaining Playbooks. While both paths coexist, the legacy path stays the
default for unmodified Playbooks.

The "first Pulse" decision is recorded in ``action_tracks.metadata`` under
``cache_built_at`` (Unix epoch seconds). ``cache_ttl_seconds`` (also from
metadata, falling back to a Handler default) tells us when to treat the
cache as stale and re-emit the fixed block.

See:
- docs/intent/persona_cognitive_model.md §"メインラインの Pulse 開始プロンプト構成"
- docs/intent/persona_action_tracks.md §"Pulse プロンプトのキャッシュ構造実装方針"
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# cache_built_at bookkeeping (action_tracks.metadata.cache_built_at)
# ----------------------------------------------------------------------------

# Default TTL when neither Track metadata nor Handler specifies one. Matches
# the conservative end of Anthropic's 5-minute base TTL plus a margin.
_DEFAULT_CACHE_TTL_SECONDS = 240


def _read_metadata(track: Any) -> Dict[str, Any]:
    raw = getattr(track, "track_metadata", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _write_metadata(track_manager: Any, track_id: str, metadata: Dict[str, Any]) -> None:
    """Persist updated track_metadata. Uses the manager's session so the
    write lands in the same DB the rest of the runtime reads from.

    No-ops gracefully if the manager doesn't expose a session factory —
    cache_built_at is best-effort information.
    """
    session_factory = getattr(track_manager, "SessionLocal", None)
    if session_factory is None:
        LOGGER.debug(
            "[pulse-root-context] No SessionLocal on track_manager; "
            "skipping cache_built_at persistence for track=%s",
            track_id,
        )
        return
    try:
        from database.models import ActionTrack

        db = session_factory()
        try:
            row = db.query(ActionTrack).filter_by(track_id=track_id).first()
            if row is None:
                return
            row.track_metadata = json.dumps(metadata, ensure_ascii=False)
            db.commit()
        finally:
            db.close()
    except Exception:
        LOGGER.exception(
            "[pulse-root-context] Failed to persist cache_built_at for track=%s",
            track_id,
        )


def is_first_pulse(track: Any, ttl_seconds: Optional[int] = None) -> bool:
    """Return True when the entry-line cache should be treated as fresh.

    "First pulse" means either of:
    1. ``cache_built_at`` is not yet set (Track just transitioned to running)
    2. ``cache_built_at + ttl_seconds < now`` (cache TTL has elapsed)

    The Pulse driver is expected to call ``mark_cache_built`` immediately
    after building the prompt so subsequent Pulses get the cached layout.
    """
    metadata = _read_metadata(track)
    built_at = metadata.get("cache_built_at")
    if not isinstance(built_at, (int, float)):
        return True
    ttl = ttl_seconds or metadata.get("cache_ttl_seconds") or _DEFAULT_CACHE_TTL_SECONDS
    try:
        ttl_int = int(ttl)
    except (TypeError, ValueError):
        ttl_int = _DEFAULT_CACHE_TTL_SECONDS
    return (int(built_at) + ttl_int) < int(time.time())


def mark_cache_built(track_manager: Any, track: Any) -> None:
    """Stamp the Track's metadata with the current Unix time as cache_built_at.

    Idempotent — called once per Pulse-root construction for a Track. The
    write skips when ``track_manager`` is unavailable, so callers don't have
    to special-case test environments.
    """
    track_id = getattr(track, "track_id", None)
    if not track_id:
        return
    metadata = _read_metadata(track)
    metadata["cache_built_at"] = int(time.time())
    _write_metadata(track_manager, track_id, metadata)


def reset_cache_built(track_manager: Any, track_id: str) -> None:
    """Force the next Pulse to re-emit the fixed block.

    Called when the Track transitions from pending/waiting → running through
    a path other than initial activation (e.g. resume_from_wait), or when an
    operator wants to force a fresh prompt. Removes only the
    ``cache_built_at`` key so other metadata stays intact.
    """
    metadata = _read_metadata(_get_track_or_none(track_manager, track_id))
    if "cache_built_at" not in metadata:
        return
    metadata.pop("cache_built_at", None)
    _write_metadata(track_manager, track_id, metadata)


def _get_track_or_none(track_manager: Any, track_id: str) -> Any:
    try:
        return track_manager.get(track_id)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Handler probing
# ----------------------------------------------------------------------------

# Map track_type → attribute on SAIVerseManager. Kept here (instead of inside
# each Handler module) so the runtime can resolve a Handler from a Track
# without importing every Handler subclass.
_HANDLER_ATTR_BY_TYPE = {
    "user_conversation": "user_conversation_handler",
    "social": "social_track_handler",
    "autonomous": "autonomous_track_handler",
}


def get_handler_for_track(manager: Any, track: Any) -> Optional[Any]:
    """Resolve the Handler instance responsible for the given Track.

    Returns None when no Handler matches the track_type — the caller should
    fall back to the legacy context path. This keeps custom / experimental
    Track types working without forcing them to register a Handler.
    """
    track_type = getattr(track, "track_type", None)
    if not track_type:
        return None
    attr = _HANDLER_ATTR_BY_TYPE.get(track_type)
    if not attr:
        return None
    return getattr(manager, attr, None)


# ----------------------------------------------------------------------------
# Section assembly
# ----------------------------------------------------------------------------

def _format_track_identity(track: Any) -> str:
    title = getattr(track, "title", None) or "(無題)"
    track_type = getattr(track, "track_type", None) or "?"
    track_id = getattr(track, "track_id", "")
    track_id_short = track_id[:8] + "…" if track_id else "?"
    intent = getattr(track, "intent", None) or ""
    lines = [
        "## アクティブ Track",
        f"- title: {title}",
        f"- type: {track_type}",
        f"- id: {track_id_short}",
    ]
    if intent:
        lines.append(f"- intent: {intent.strip()}")
    return "\n".join(lines)


def _format_handler_guidance(handler: Any) -> str:
    """Compose the handler-supplied fixed block.

    Pulls Handler attributes added in v0.8/v0.10/v0.11. Each piece is
    optional so Handlers stay lightweight; missing attributes are skipped.
    """
    sections: List[str] = []
    notice = getattr(handler, "pulse_completion_notice", None)
    if notice:
        sections.append(notice.strip())
    spells_doc = getattr(handler, "available_spells_doc", None)
    if spells_doc:
        sections.append(spells_doc.strip())
    track_specific = getattr(handler, "track_specific_guidance", None)
    if track_specific:
        sections.append(track_specific.strip())
    return "\n\n".join(sections)


def build_fixed_section(track: Any, handler: Any, line_role: str) -> str:
    """Assemble the cache-head block (固定情報) for a Pulse-root.

    Used only on first Pulse after Track activation or cache TTL elapse.
    The block is intentionally idempotent: passing the same Track + Handler
    yields byte-identical output, which is what makes Anthropic prompt
    caching usable for it.
    """
    parts = [
        _format_track_identity(track),
        _format_handler_guidance(handler),
    ]
    parts.append(
        f"## ライン情報\n- 起点ライン: {line_role}"
    )
    return "\n\n".join(p for p in parts if p)


def build_dynamic_section(
    track: Any,
    handler: Any,
    new_events: Optional[List[str]] = None,
) -> str:
    """Assemble the per-Pulse末尾 block (動的情報).

    Always re-emitted, never cached. Holds the time-sensitive bits: pause
    summary digest, new alert / external events, the latest received
    utterance.

    Handlers may override by providing a ``format_dynamic_section`` callable
    that receives ``(track, new_events)`` and returns a string. When absent
    the default formatter below is used.
    """
    formatter = getattr(handler, "format_dynamic_section", None)
    if callable(formatter):
        try:
            text = formatter(track, new_events or [])
            if isinstance(text, str):
                return text
        except Exception:
            LOGGER.exception(
                "[pulse-root-context] Handler.format_dynamic_section raised; falling back to default"
            )

    lines: List[str] = ["## 直近の状況"]
    pause_summary = getattr(track, "pause_summary", None)
    if pause_summary:
        lines.append(f"### 前回までのサマリ\n{pause_summary.strip()}")
    if new_events:
        lines.append("### 新着イベント")
        for ev in new_events:
            lines.append(f"- {ev}")
    if len(lines) == 1:
        # No dynamic info to add — return empty so callers can skip the block.
        return ""
    return "\n\n".join(lines)


# ----------------------------------------------------------------------------
# Top-level entry point
# ----------------------------------------------------------------------------

def prepare_pulse_root_context(
    runtime: Any,
    persona: Any,
    building_id: str,
    track: Any,
    line_role: str,
    handler: Any,
    *,
    new_events: Optional[List[str]] = None,
    legacy_context_requirements: Optional[Any] = None,
    pulse_id: Optional[str] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Build the messages list for a Pulse-root LLM invocation.

    The output is structured as:

        [legacy system prompt + history] (always)  ← layer [2] / [3] cache
        + (first Pulse only) "## アクティブ Track" + handler guidance
        + dynamic section (pause summary, new events) when non-empty

    Returns:
        (messages, was_first_pulse)
        - was_first_pulse: True when the fixed section was inserted; the
          caller is responsible for ``mark_cache_built`` when it accepts the
          result. Decoupling allows the caller to abort (e.g. cancellation)
          before stamping cache_built_at.

    The legacy ``_prepare_context`` is still invoked for the system prompt /
    history bedrock — Phase 1.1 layers Pulse-root semantics *on top of* the
    existing prompt rather than replacing it. Phase 1.4 will retire the
    legacy parts of ``_prepare_context`` once all Playbooks have migrated.
    """
    from sea.playbook_models import ContextRequirements

    requirements = legacy_context_requirements or ContextRequirements()

    # Always reuse the existing context preparation for the bedrock — it
    # handles persona system prompt, history retrieval, Memory Weave, and
    # token budgeting. Sub-cache filtering for line_role='sub_line' is
    # already applied at the SAIMemory layer (scope!='discardable').
    messages = runtime._prepare_context(
        persona,
        building_id,
        user_input=None,
        requirements=requirements,
        pulse_id=pulse_id,
        warnings=warnings,
    )

    first_pulse = is_first_pulse(track)
    fixed_text = build_fixed_section(track, handler, line_role) if first_pulse else ""
    dynamic_text = build_dynamic_section(track, handler, new_events=new_events)

    # user role + <system> タグ統一形式 (詳細: sea/runtime_llm.py の同様の
    # 自動ラップ箇所のコメント参照)。Gemini 等が messages 中途の system role
    # を受け付けないため、role='user' + <system>...</system> で送る。
    # system role 化への変更は Gemini 互換が壊れるため不可。
    appended = []
    if fixed_text:
        appended.append({"role": "user", "content": f"<system>{fixed_text}</system>"})
    if dynamic_text:
        appended.append({"role": "user", "content": f"<system>{dynamic_text}</system>"})

    if appended:
        messages = list(messages) + appended

    LOGGER.debug(
        "[pulse-root-context] Prepared track=%s line_role=%s first_pulse=%s "
        "fixed=%dchars dynamic=%dchars total_messages=%d",
        getattr(track, "track_id", "?"), line_role, first_pulse,
        len(fixed_text), len(dynamic_text), len(messages),
    )
    return messages, first_pulse
