"""Shared helpers for Track-mutating spells (track_create / activate / pause /
complete / abort).

All Track-status-changing spells defer their effect to Pulse completion via
``PulseContext.enqueue_track_op`` — the runtime applies the queued ops at the
end of the current Playbook, then triggers the next Pulse for any newly active
Track. This avoids the failure mode where the LLM, having committed to a
Track switch in the current main-cache, keeps emitting "next-Track work"
within the current Pulse (Intent A v0.14, Intent B v0.11).

When no PulseContext is available (CLI / test / MetaLayer-spawned Playbook
without a parent Pulse), the operation is executed immediately against the
TrackManager — ``apply_track_op`` guarantees the operation is attempted on
either path, removing the burden from each caller.

メタ判断ターンの scope='discardable' → 'committed' 昇格 (Intent A v0.14
「[B] 移動: 分岐ターンをそのまま残す」) は本モジュールでは行わない。
TrackManager の状態遷移 hook 経由で、SAIVerseManager 起動時に登録された
昇格ハンドラが実施する (saiverse/saiverse_manager.py 参照)。本ファイルの
責務は Track 操作 op を deferred / 即時で apply する一点に絞る。
"""
from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional

from tools.context import get_active_pulse_context

LOGGER = logging.getLogger("saiverse.tools.track_common")


class TrackOpResult(NamedTuple):
    """Outcome of ``apply_track_op``.

    - ``deferred=True``: queued onto PulseContext, will run at Pulse completion.
      ``track`` is None because the operation has not run yet.
    - ``deferred=False``: executed immediately. ``track`` holds the resulting
      ActionTrack row (may be None if the underlying op_method returned None).
    """
    deferred: bool
    track: Optional[Any]

# track_type → entry_line_role default lookup (Intent A v0.14, Intent B v0.11).
# Resolution goes through the corresponding Handler's `default_entry_line_role`
# class attribute so the source of truth stays in saiverse/track_handlers/.
# Keep this map narrow — unknown types fall back to 'main_line' which is the
# safer default (= other-talk per Intent A invariant 9).
_HANDLER_MODULE_BY_TRACK_TYPE = {
    "autonomous": ("saiverse.track_handlers.autonomous_track_handler", "AutonomousTrackHandler"),
    "user_conversation": ("saiverse.track_handlers.user_conversation_handler", "UserConversationTrackHandler"),
    "social": ("saiverse.track_handlers.social_track_handler", "SocialTrackHandler"),
}


def resolve_default_entry_line_role(track_type: str) -> str:
    """Return the default entry-line role for a Track type.

    Looks up the Handler class registered for the type and reads its
    ``default_entry_line_role`` attribute. Falls back to ``"main_line"`` for
    unknown types (other-talk default — safer than silently picking sub).
    """
    mapping = _HANDLER_MODULE_BY_TRACK_TYPE.get(track_type)
    if not mapping:
        LOGGER.debug(
            "[track_common] No handler registered for track_type=%s; "
            "defaulting entry_line_role='main_line'",
            track_type,
        )
        return "main_line"
    module_path, class_name = mapping
    try:
        import importlib
        module = importlib.import_module(module_path)
        handler_cls = getattr(module, class_name)
        return getattr(handler_cls, "default_entry_line_role", "main_line")
    except Exception as exc:
        LOGGER.warning(
            "[track_common] Failed to resolve handler for track_type=%s "
            "(%s.%s): %s — falling back to main_line",
            track_type, module_path, class_name, exc,
        )
        return "main_line"

# Persona-facing notice attached to every deferred Track op response. Keep it
# direct: the LLM treats the spell result as ground truth and we want it to
# (a) understand the op WILL happen, (b) stop emitting more spells in the
# current utterance.
DEFERRED_NOTICE = (
    "この操作は今のPulse完了後に自動で適用されます。"
    "切替後のTrackで作業を進めるため、これ以上スペルは使わず、"
    "今の発言だけを締めくくってください。"
)


def get_pulse_context() -> Optional[Any]:
    """Return the active PulseContext, or None when running outside a Pulse.

    Standalone CLIs and direct tests will see None and fall back to immediate
    execution paths inside each spell.
    """
    return get_active_pulse_context()


def apply_track_op(
    pulse_ctx: Any,
    op_type: str,
    track_id: Optional[str] = None,
    *,
    track_manager: Optional[Any] = None,
    **args: Any,
) -> TrackOpResult:
    """Apply a Track operation, choosing deferred or immediate execution.

    - With a PulseContext (normal Pulse-driven flow): the op is enqueued and
      will run at Pulse completion. Returns ``TrackOpResult(deferred=True,
      track=None)``.
    - Without a PulseContext (CLI / test / MetaLayer-spawned Playbook): the
      op is executed immediately against the TrackManager. Returns
      ``TrackOpResult(deferred=False, track=<resulting ActionTrack>)``.

    Either path guarantees the operation is attempted before return — callers
    must not assume a no-op outcome on a missing PulseContext.

    Args:
        pulse_ctx: The current PulseContext, or None when outside a Pulse.
        op_type: TrackManager method name (e.g. "activate", "pause",
            "complete", "abort"). Must match an existing method.
        track_id: Target Track id passed to both deferred and immediate paths.
        track_manager: Override for immediate execution. When None, a fresh
            ``TrackManager(session_factory=SessionLocal)`` is constructed —
            callers running in production should pass their module-level
            instance to avoid extra session-factory wiring.
        **args: Extra kwargs forwarded to both paths.

    Raises:
        ValueError: ``op_type`` does not name a TrackManager method.
        Any exception raised by the TrackManager method (e.g.
        ``InvalidTrackStateError``, ``TrackNotFoundError``) is propagated to
        the caller so it can produce the appropriate user-facing error.
    """
    if pulse_ctx is not None and hasattr(pulse_ctx, "enqueue_track_op"):
        pulse_ctx.enqueue_track_op(op_type, track_id=track_id, **args)
        return TrackOpResult(deferred=True, track=None)

    if track_manager is None:
        from database.session import SessionLocal
        from saiverse.track_manager import TrackManager

        track_manager = TrackManager(session_factory=SessionLocal)

    op_method = getattr(track_manager, op_type, None)
    if op_method is None or not callable(op_method):
        raise ValueError(
            f"apply_track_op: unknown op_type '{op_type}' "
            f"(no method on {type(track_manager).__name__})"
        )

    LOGGER.info(
        "[track_common] No PulseContext — executing op_type=%s track_id=%s immediately",
        op_type, track_id,
    )
    track = op_method(track_id, **args)
    return TrackOpResult(deferred=False, track=track)
