"""Persona event management mixin extracted from saiverse_manager.py."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    pass

LOGGER = logging.getLogger(__name__)


class PersonaEventMixin:
    """Manages persona event logs (pending notifications for personas)."""

    def _load_persona_event_logs(self) -> None:
        """Load pending persona events from the database."""
        from database.models import PersonaEventLog, AI as AIModel
        
        db = self.SessionLocal()
        try:
            rows = (
                db.query(PersonaEventLog)
                .join(AIModel, PersonaEventLog.PERSONA_ID == AIModel.AIID)
                .filter(
                    AIModel.HOME_CITYID == self.city_id,
                    PersonaEventLog.STATUS == "pending",
                )
                .all()
            )
        except Exception as exc:
            LOGGER.error("Failed to load persona events: %s", exc, exc_info=True)
            rows = []
        finally:
            db.close()

        self.persona_pending_events = defaultdict(list)
        for row in rows:
            self.persona_pending_events[row.PERSONA_ID].append(
                {
                    "event_id": row.EVENT_ID,
                    "content": row.CONTENT,
                    "created_at": row.CREATED_AT,
                }
            )

    def record_persona_event(self, persona_id: str, content: str) -> None:
        """Add a new pending event for the specified persona."""
        from database.models import PersonaEventLog
        
        db = self.SessionLocal()
        try:
            entry = PersonaEventLog(PERSONA_ID=persona_id, CONTENT=content, STATUS="pending")
            db.add(entry)
            db.commit()
            db.refresh(entry)
            created_at = entry.CREATED_AT
            event_id = entry.EVENT_ID
        except Exception as exc:
            LOGGER.error("Failed to record persona event for %s: %s", persona_id, exc, exc_info=True)
            db.rollback()
            return
        finally:
            db.close()
        self.persona_pending_events[persona_id].append(
            {
                "event_id": event_id,
                "content": content,
                "created_at": created_at,
            }
        )

    def get_persona_pending_events(self, persona_id: str) -> List[Dict[str, Any]]:
        """Get pending events for a persona, sorted by creation time."""
        events = list(self.persona_pending_events.get(persona_id, []))
        events.sort(key=lambda e: e.get("created_at") or datetime.utcnow())
        return events

    def archive_persona_events(self, persona_id: str, event_ids: List[int]) -> None:
        """Archive (mark as processed) the given event IDs."""
        if not event_ids:
            return
        from database.models import PersonaEventLog
        
        db = self.SessionLocal()
        try:
            (
                db.query(PersonaEventLog)
                .filter(PersonaEventLog.EVENT_ID.in_(event_ids))
                .update({PersonaEventLog.STATUS: "archived"}, synchronize_session=False)
            )
            db.commit()
        except Exception as exc:
            LOGGER.error("Failed to archive persona events %s: %s", event_ids, exc, exc_info=True)
            db.rollback()
            return
        finally:
            db.close()

        pending = self.persona_pending_events.get(persona_id, [])
        if pending:
            remaining = [ev for ev in pending if ev.get("event_id") not in event_ids]
            if remaining:
                self.persona_pending_events[persona_id] = remaining
            else:
                self.persona_pending_events.pop(persona_id, None)


__all__ = ["PersonaEventMixin"]
