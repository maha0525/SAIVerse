"""Shared helpers for Track-mutating spells (track_create / activate / pause /
complete / abort).

All Track-status-changing spells defer their effect to Pulse completion via
``PulseContext.enqueue_track_op`` — the runtime applies the queued ops at the
end of the current Playbook, then triggers the next Pulse for any newly active
Track. This avoids the failure mode where the LLM, having committed to a
Track switch in the current main-cache, keeps emitting "next-Track work"
within the current Pulse (Intent A v0.14, Intent B v0.11).

Spells call into this module so the deferred-vs-immediate decision and the
notice text are consistent across all five Track tools.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from tools.context import get_active_pulse_context

LOGGER = logging.getLogger("saiverse.tools.track_common")

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


def enqueue_or_warn(
    pulse_ctx: Any,
    op_type: str,
    track_id: Optional[str] = None,
    **args: Any,
) -> bool:
    """Enqueue the op onto the PulseContext if available; log if not.

    Returns True when the op was queued for Pulse-completion application,
    False when the caller should fall back to immediate execution (no Pulse
    context — typically a CLI / test environment).
    """
    if pulse_ctx is None or not hasattr(pulse_ctx, "enqueue_track_op"):
        LOGGER.warning(
            "[track_common] No PulseContext available for op_type=%s track_id=%s — "
            "falling back to immediate execution",
            op_type, track_id,
        )
        return False
    pulse_ctx.enqueue_track_op(op_type, track_id=track_id, **args)
    return True
