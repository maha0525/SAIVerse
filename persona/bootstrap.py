"""
Bootstrapping helpers for PersonaCore initialisation.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from saiverse_memory import SAIMemoryAdapter
from database.models import AI as AIModel
from sqlalchemy.orm import Session


def load_action_priority(path: Path) -> Dict[str, int]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()}
        except Exception:
            logging.warning("Failed to load action priority from %s", path)
    return {"think": 1, "emotion_shift": 2, "move": 3}


def load_session_data(persona) -> None:
    """Populate persona fields from persisted session data."""
    if persona.is_visitor:
        persona.messages = []
        persona.conscious_log = []
        persona.pulse_cursors = {}
        persona.entry_markers = {}
        persona._raw_pulse_cursor_data = {}
        persona._raw_pulse_cursor_format = "count"
        return

    session: Session = persona.SessionLocal()
    try:
        db_ai: Optional[AIModel] = session.query(AIModel).filter(AIModel.AIID == persona.persona_id).first()
        if db_ai:
            persona.auto_count = db_ai.AUTO_COUNT or 0

            if db_ai.LAST_AUTO_PROMPT_TIMES:
                try:
                    persona.last_auto_prompt_times.update(json.loads(db_ai.LAST_AUTO_PROMPT_TIMES))
                except json.JSONDecodeError:
                    logging.warning("Could not parse LAST_AUTO_PROMPT_TIMES from DB for %s.", persona.persona_name)

            if db_ai.EMOTION:
                try:
                    persona.emotion = json.loads(db_ai.EMOTION)
                except json.JSONDecodeError:
                    logging.warning("Could not parse EMOTION from DB for %s.", persona.persona_name)
        else:
            logging.warning("No AI record found in DB for %s. Using default state.", persona.persona_id)
    except Exception as exc:
        logging.error("Failed to load session data from DB for %s: %s", persona.persona_name, exc, exc_info=True)
    finally:
        session.close()

    if persona.persona_log_path.exists():
        try:
            persona.messages = json.loads(persona.persona_log_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("Failed to load persona log, starting empty")
            persona.messages = []
    else:
        persona.messages = []

    if persona.conscious_log_path.exists():
        try:
            data = json.loads(persona.conscious_log_path.read_text(encoding="utf-8"))
            persona.conscious_log = data.get("log", [])
            raw_cursors = data.get("pulse_cursors")
            if raw_cursors is None:
                raw_cursors = data.get("pulse_indices", {})
            persona._raw_pulse_cursor_data = raw_cursors if isinstance(raw_cursors, dict) else {}
            fmt = data.get("pulse_cursor_format")
            persona._raw_pulse_cursor_format = fmt if isinstance(fmt, str) else "count"
        except json.JSONDecodeError:
            logging.warning("Failed to load conscious log, starting empty")
            persona.conscious_log = []
            persona._raw_pulse_cursor_data = {}
            persona._raw_pulse_cursor_format = "count"
    else:
        persona.conscious_log = []
        persona._raw_pulse_cursor_data = {}
        persona._raw_pulse_cursor_format = "count"


def initialise_memory_adapter(persona) -> Optional[SAIMemoryAdapter]:
    try:
        adapter = SAIMemoryAdapter(
            persona_id=persona.persona_id,
            persona_dir=persona.persona_log_path.parent,
            resource_id=persona.persona_id,
        )
        if adapter.is_ready():
            logging.info("SAIMemory ready for persona %s", persona.persona_id)
        else:
            logging.warning("SAIMemory adapter initialised but not ready for persona %s", persona.persona_id)
        return adapter
    except Exception as exc:
        logging.warning("Failed to initialise SAIMemory for %s: %s", persona.persona_id, exc)
        return None
