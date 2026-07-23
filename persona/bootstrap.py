"""
Bootstrapping helpers for PersonaCore initialisation.
"""

import json
import logging
from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from database.models import AI as AIModel
from sqlalchemy.orm import Session


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

    # Phase 2+3: conscious_log.json は廃止 (= log フィールドは事実上死んでいたため
    # 移管せず、 pulse_cursors / entry_markers は persona_pulse_cursor テーブルから
    # initialise_pulse_state が直接ロードする)。 旧 JSON が残っていても触らない。
    persona.conscious_log = []
    persona._raw_pulse_cursor_data = {}
    persona._raw_pulse_cursor_format = "seq"


def initialise_memory_adapter(persona) -> Optional[SAIMemoryAdapter]:
    try:
        adapter = SAIMemoryAdapter(
            persona_id=persona.persona_id,
            persona_dir=persona.persona_log_path.parent,
            resource_id=persona.persona_id,
            # ペルソナ登録経路 = 起動時自動バックアップを起こす正規の1点。
            # ツール・API 経路の使い捨て adapter はデフォルト False のまま。
            startup_backup=True,
            # プロセス死で孤児化した Stelis/subagent thread の復旧も登録経路
            # だけで行う (S4)。使い捨て adapter に許すと走行中の Stelis を
            # 誤って巻き戻すため。
            recover_orphaned_thread=True,
        )
        if adapter.is_ready():
            logging.info("SAIMemory ready for persona %s", persona.persona_id)
        else:
            logging.warning("SAIMemory adapter initialised but not ready for persona %s", persona.persona_id)
        return adapter
    except Exception as exc:
        logging.warning("Failed to initialise SAIMemory for %s: %s", persona.persona_id, exc)
        return None
