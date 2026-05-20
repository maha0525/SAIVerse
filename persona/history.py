"""History and pulse tracking helpers for PersonaCore.

Phase 2+3 以降、 pulse_cursors / entry_markers は persona_pulse_cursor テーブルから
ロードする (conscious_log.json は廃止)。
"""

import logging
from typing import Dict

_log = logging.getLogger(__name__)


def initialise_pulse_state(persona) -> None:
    """ペルソナの pulse_cursors / entry_markers を persona_pulse_cursor から復元。

    DB に該当 (persona_id, building_id) 行がなければ 0 (= 全未読) で初期化する。
    """
    persona.pulse_cursors = {}
    persona.entry_markers = {}

    session_factory = getattr(persona, "SessionLocal", None)
    if session_factory is None:
        _log.info(
            "[init_pulse] persona=%s no SessionLocal — cursors empty",
            getattr(persona, "persona_id", "?"),
        )
        return

    from database.models import PersonaPulseCursor
    persona_id = getattr(persona, "persona_id", None)
    if not persona_id:
        return

    try:
        db = session_factory()
        try:
            rows = db.query(PersonaPulseCursor).filter_by(PERSONA_ID=persona_id).all()
            cursors: Dict[str, int] = {}
            markers: Dict[str, int] = {}
            for row in rows:
                cursors[row.BUILDING_ID] = int(row.CURSOR_SEQ or 0)
                markers[row.BUILDING_ID] = int(row.ENTRY_MARKER_SEQ or 0)
            persona.pulse_cursors = cursors
            persona.entry_markers = markers
            _log.info(
                "[init_pulse] persona=%s loaded %d pulse_cursor rows",
                persona_id, len(rows),
            )
        finally:
            db.close()
    except Exception:
        _log.warning(
            "[init_pulse] persona=%s failed to load pulse_cursors", persona_id,
            exc_info=True,
        )
