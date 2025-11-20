"""History bookkeeping helpers for PersonaCore."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional

from database.models import AI as AIModel


class PersonaHistoryMixin:
    """Provide timeline utilities and persistence helpers."""

    SessionLocal: Any
    auto_count: int
    conscious_log: List[Dict[str, Any]]
    conscious_log_path: Any
    current_building_id: str
    entry_markers: Dict[str, int]
    emotion: Dict[str, Dict[str, float]]
    history_manager: Any
    id_to_name_map: Dict[str, str]
    is_visitor: bool
    last_auto_prompt_times: Dict[str, float]
    occupants: Dict[str, List[str]]
    persona_id: str
    persona_name: str
    pulse_cursors: Dict[str, int]
    timezone: Any
    timezone_name: str

    def _parse_timestamp_to_utc(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(float(value), tz=dt_timezone.utc)
            if isinstance(value, str):
                raw = value.strip()
                if not raw:
                    return None
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=dt_timezone.utc)
                return parsed.astimezone(dt_timezone.utc)
        except Exception:
            logging.debug(
                "Failed to parse timestamp '%s' for persona %s",
                value,
                self.persona_id,
            )
            return None
        return None

    def _format_timezone_offset(self, dt_obj: datetime) -> str:
        offset = dt_obj.utcoffset()
        if offset is None:
            return "UTC+00:00"
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        hours, minutes = divmod(total_minutes, 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    def _format_local_timestamp(self, dt_obj: datetime) -> str:
        offset_label = self._format_timezone_offset(dt_obj)
        tz_label = dt_obj.tzname() or self.timezone_name
        return f"{dt_obj.strftime('%Y-%m-%d %H:%M:%S')} {tz_label} ({offset_label})"

    def _format_elapsed(self, delta: timedelta) -> str:
        if delta.total_seconds() < 0:
            delta = timedelta(0)
        total_minutes = int(delta.total_seconds() // 60)
        days, rem_minutes = divmod(total_minutes, 1440)
        hours, minutes = divmod(rem_minutes, 60)
        parts: List[str] = []
        if days:
            parts.append(f"{days}日")
        if hours:
            parts.append(f"{hours}時間")
        if minutes:
            parts.append(f"{minutes}分")
        if not parts:
            parts.append("0分")
        return " ".join(parts)

    def _timestamp_to_epoch(
        self, primary: Any, secondary: Any = None
    ) -> Optional[int]:
        result: Optional[int] = None
        for candidate in (primary, secondary):
            if candidate is None:
                continue
            if isinstance(candidate, (int, float)):
                result = int(candidate)
                break
            if isinstance(candidate, str):
                raw = candidate.strip()
                if not raw:
                    continue
                try:
                    result = int(float(raw))
                    break
                except ValueError:
                    dt_obj = self._parse_timestamp_to_utc(raw)
                    if dt_obj is not None:
                        result = int(dt_obj.timestamp())
                        break
        logging.debug(
            "[recall] normalised timestamp primary=%s secondary=%s -> %s",
            primary,
            secondary,
            result,
        )
        return result

    def _occupants_snapshot(self, building_id: str) -> List[str]:
        occupants = self.occupants.get(building_id, []) or []
        snapshot: List[str] = []
        for pid in occupants:
            if not pid:
                continue
            pid_str = str(pid)
            if pid_str not in snapshot:
                snapshot.append(pid_str)
        if (
            building_id == self.current_building_id
            and self.persona_id not in snapshot
        ):
            snapshot.append(self.persona_id)
        return snapshot

    def _save_session_metadata(self) -> None:
        if self.is_visitor:
            self.history_manager.save_all()
            self._save_conscious_log()
            return

        db = self.SessionLocal()
        try:
            update_data = {
                "EMOTION": json.dumps(self.emotion, ensure_ascii=False),
                "AUTO_COUNT": self.auto_count,
                "LAST_AUTO_PROMPT_TIMES": json.dumps(
                    self.last_auto_prompt_times, ensure_ascii=False
                ),
            }
            db.query(AIModel).filter(AIModel.AIID == self.persona_id).update(
                update_data
            )
            db.commit()
            logging.info("Saved dynamic state to DB for %s.", self.persona_name)
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to save session data to DB for %s: %s",
                self.persona_name,
                exc,
                exc_info=True,
            )
        finally:
            db.close()

        if getattr(self, "messages", None):
            self.history_manager.save_all()
        self._save_conscious_log()

    def get_building_history(
        self, building_id: str, raw: bool = False
    ) -> List[Dict[str, str]]:
        return self.history_manager.building_histories.get(building_id, [])

    def _save_conscious_log(self) -> None:
        self.conscious_log_path.parent.mkdir(parents=True, exist_ok=True)
        data_to_save = {
            "log": self.conscious_log,
            "pulse_cursors": self.pulse_cursors,
            "pulse_cursor_format": "seq",
            "pulse_indices": self.pulse_cursors,
        }
        self.conscious_log_path.write_text(
            json.dumps(data_to_save, ensure_ascii=False), encoding="utf-8"
        )

    def _mark_entry(self, building_id: str) -> None:
        try:
            hist = self.history_manager.building_histories.get(building_id, [])
            last_seq = 0
            if hist:
                try:
                    last_seq = int(hist[-1].get("seq", len(hist)))
                except (TypeError, ValueError):
                    last_seq = len(hist)
            self.entry_markers[building_id] = last_seq
            prior_cursor = self.pulse_cursors.get(building_id, 0)
            self.pulse_cursors[building_id] = max(prior_cursor, last_seq)
            logging.debug(
                "[entry] entry marker set: %s -> %d (prev_cursor=%d)",
                building_id,
                last_seq,
                prior_cursor,
            )
        except Exception:
            pass

    def register_entry(self, building_id: str) -> None:
        self._mark_entry(building_id)


__all__ = ["PersonaHistoryMixin"]
