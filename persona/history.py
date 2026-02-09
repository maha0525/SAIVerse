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
        raw_value = persona._raw_pulse_cursor_data.get(b_id) if hasattr(persona, "_raw_pulse_cursor_data") else None
        cursor = max_seq
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

    for b_id, hist in hist_map.items():
        if b_id not in computed_cursors:
            computed_cursors[b_id] = max_seq_map.get(b_id, 0)

    persona.pulse_cursors = computed_cursors

    for b_id, hist in hist_map.items():
        if b_id not in persona.entry_markers:
            persona.entry_markers[b_id] = max_seq_map.get(b_id, 0)

    if persona.current_building_id in hist_map:
        persona.entry_markers[persona.current_building_id] = persona.pulse_cursors.get(
            persona.current_building_id,
            persona.entry_markers.get(persona.current_building_id, 0),
        )
