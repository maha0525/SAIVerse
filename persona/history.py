"""
History and pulse tracking helpers for PersonaCore.
"""

import logging
from typing import Dict

_log = logging.getLogger(__name__)


def initialise_pulse_state(persona) -> None:
    hist_map = persona.history_manager.building_histories
    computed_cursors: Dict[str, int] = {}
    max_seq_map: Dict[str, int] = {}
    raw_cursor_data = persona._raw_pulse_cursor_data if hasattr(persona, "_raw_pulse_cursor_data") else {}
    _log.info(
        "[init_pulse] persona=%s hist_map_keys=%s raw_cursor_keys=%s format=%s current_building=%s",
        getattr(persona, "persona_id", "?"),
        sorted(hist_map.keys()),
        sorted(raw_cursor_data.keys()),
        getattr(persona, "_raw_pulse_cursor_format", "?"),
        getattr(persona, "current_building_id", "?"),
    )
    for b_id, hist in hist_map.items():
        max_seq = 0
        for msg in hist:
            try:
                seq_val = int(msg.get("seq", 0))
            except (TypeError, ValueError):
                _log.debug("Failed to parse seq value %r, defaulting to 0", msg.get("seq"))
                seq_val = 0
            max_seq = max(max_seq, seq_val)
        max_seq_map[b_id] = max_seq
        raw_value = raw_cursor_data.get(b_id)
        cursor = max_seq
        _log.info(
            "[init_pulse] persona=%s b_id=%s max_seq=%d raw_value=%s -> cursor_before_clamp=%s",
            getattr(persona, "persona_id", "?"),
            b_id, max_seq, raw_value,
            raw_value if raw_value is None else "will_compute",
        )
        if raw_value is not None:
            if persona._raw_pulse_cursor_format == "seq":
                try:
                    cursor = int(raw_value)
                except (TypeError, ValueError):
                    _log.debug("Failed to parse cursor raw_value %r, defaulting to max_seq %d", raw_value, max_seq)
                    cursor = max_seq
                cursor = max(0, min(cursor, max_seq))
            else:
                try:
                    count = int(raw_value)
                except (TypeError, ValueError):
                    _log.debug("Failed to parse count raw_value %r, defaulting to hist length %d", raw_value, len(hist))
                    count = len(hist)
                if count <= 0:
                    cursor = 0
                else:
                    idx = min(count, len(hist))
                    if idx == 0:
                        cursor = 0
                    else:
                        ref = hist[idx - 1]
                        try:
                            cursor = int(ref.get("seq", idx))
                        except (TypeError, ValueError):
                            cursor = idx
        computed_cursors[b_id] = max(0, cursor)
        _log.info("[init_pulse] persona=%s b_id=%s final_cursor=%d", getattr(persona, "persona_id", "?"), b_id, computed_cursors[b_id])

    for b_id, hist in hist_map.items():
        if b_id not in computed_cursors:
            computed_cursors[b_id] = max_seq_map.get(b_id, 0)

    persona.pulse_cursors = computed_cursors

    for b_id in hist_map:
        if b_id not in persona.entry_markers:
            # Use the restored cursor, not max_seq.
            # max_seq would block unseen pre-restart messages when the persona's
            # current_building_id at startup differs from the building they were
            # actually chatting in before the restart.
            persona.entry_markers[b_id] = computed_cursors.get(b_id, 0)
