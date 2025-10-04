from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import List, Optional
import threading

from sai_memory.config import Settings, load_settings
from sai_memory.memory.recall import (
    Embedder,
    semantic_recall_groups,
)
from sai_memory.memory.storage import (
    add_message,
    get_messages_last,
    get_messages_paginated,
    get_or_create_thread,
    init_db,
    upsert_embedding,
)

LOGGER = logging.getLogger(__name__)


class SAIMemoryAdapter:
    """Thin integration layer that lets SAIVerse talk to SAIMemory storage."""

    _PERSONA_THREAD_SUFFIX = "__persona__"

    def __init__(
        self,
        persona_id: str,
        *,
        persona_dir: Optional[Path] = None,
        resource_id: Optional[str] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        base_settings = settings or load_settings()
        self.persona_id = persona_id
        self.persona_dir = persona_dir or (Path.home() / ".saiverse" / "personas" / persona_id)
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.persona_dir / "memory.db"

        resolved_resource = resource_id or (base_settings.resource_id or persona_id)
        self.settings = replace(base_settings, db_path=str(db_path), resource_id=resolved_resource)
        self._db_lock = threading.RLock()

        if not self.settings.memory_enabled:
            LOGGER.warning("SAIMemory disabled via settings; adapter will no-op")
            self.conn = None
            self.embedder = None
            return

        try:
            self.conn = init_db(self.settings.db_path, check_same_thread=False)
        except Exception as exc:
            LOGGER.exception("Failed to initialise SAIMemory DB at %s", self.settings.db_path)
            self.conn = None
            self.embedder = None
            raise exc

        try:
            self.embedder = Embedder(model=self.settings.embed_model)
        except Exception as exc:
            LOGGER.exception("Failed to load embedding model '%s'", self.settings.embed_model)
            self.embedder = None
            raise exc

        LOGGER.info(
            "SAIMemory adapter initialised for persona=%s db=%s (resource=%s)",
            self.persona_id,
            self.settings.db_path,
            self.settings.resource_id,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def append_building_message(self, building_id: str, message: dict) -> None:
        self._append_message(building_id=building_id, message=message)

    def append_persona_message(self, message: dict) -> None:
        self._append_message(building_id=None, message=message)

    def recent_messages(self, building_id: str, max_chars: int) -> List[dict]:
        if not self._ready:
            return []
        thread_id = self._thread_id(building_id)
        try:
            with self._db_lock:
                rows = get_messages_last(self.conn, thread_id, self.settings.last_messages)  # type: ignore[arg-type]
        except Exception as exc:
            LOGGER.warning("Failed to fetch recent messages for %s: %s", thread_id, exc)
            return []

        selected: List[dict] = []
        consumed = 0
        for msg in reversed(rows):
            text = msg.content or ""
            consumed += len(text)
            if consumed > max_chars:
                break
            selected.insert(0, {"role": msg.role, "content": text, "created_at": msg.created_at})
        return selected

    def recent_persona_messages(self, max_chars: int) -> List[dict]:
        if not self._ready:
            return []
        thread_id = self._thread_id(None)
        try:
            with self._db_lock:
                all_rows = _fetch_all_messages(self.conn, thread_id)
        except Exception as exc:
            LOGGER.warning("Failed to fetch persona messages for %s: %s", thread_id, exc)
            return []

        selected: List[dict] = []
        consumed = 0
        for msg in reversed(all_rows):
            text = msg.content or ""
            consumed += len(text)
            if consumed > max_chars:
                break
            selected.insert(0, {
                "role": "assistant" if msg.role == "model" else msg.role,
                "content": text,
                "created_at": msg.created_at,
            })
        return selected

    def recall_snippet(
        self,
        building_id: str,
        query_text: str,
        *,
        max_chars: int = 800,
    ) -> str:
        if not self._ready:
            return ""
        if not query_text or not query_text.strip():
            return ""

        thread_id = self._thread_id(building_id)
        resource_id = self.settings.resource_id if self.settings.scope == "resource" else None

        try:
            with self._db_lock:
                groups = semantic_recall_groups(
                    self.conn,
                    self.embedder,
                    query_text,
                    thread_id=thread_id,
                    resource_id=resource_id,
                    topk=self.settings.topk,
                    range_before=self.settings.range_before,
                    range_after=self.settings.range_after,
                    scope=self.settings.scope,
                )
        except Exception as exc:
            LOGGER.warning("SAIMemory recall failed for %s: %s", thread_id, exc)
            return ""

        lines: List[str] = ["[Memory Recall]"]
        seen: set[str] = set()
        for seed, bundle, score in groups:
            for msg in bundle:
                if msg.id in seen:
                    continue
                seen.add(msg.id)
                content = (msg.content or "").strip().replace("\n", " ")
                if not content:
                    continue
                if len(content) > 240:
                    content = content[:240] + "…"
                dt = datetime.fromtimestamp(msg.created_at)
                ts = dt.strftime("%Y-%m-%d %H:%M")
                role = msg.role
                if score is not None and msg.id == seed.id:
                    lines.append(f"- {role} @ {ts} (score={score:.3f}): {content}")
                else:
                    lines.append(f"- {role} @ {ts}: {content}")
                if len("\n".join(lines)) > max_chars:
                    snippet = "\n".join(lines)
                    return snippet[: max_chars - 1] + "…"

        if len(lines) == 1:
            return ""
        snippet = "\n".join(lines)
        if len(snippet) > max_chars:
            snippet = snippet[: max_chars - 1] + "…"
        return snippet

    def update_overview(self, building_id: str, provider) -> Optional[str]:
        if not self._ready or provider is None:
            return None
        from sai_memory.summary import update_overview_with_llm

        thread_id = self._thread_id(building_id)
        try:
            with self._db_lock:
                return update_overview_with_llm(
                    self.conn,
                    provider,
                    thread_id=thread_id,
                    max_chars=self.settings.summary_max_chars,
                )
        except Exception as exc:
            LOGGER.warning("Failed to update overview for %s: %s", thread_id, exc)
            return None

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                LOGGER.exception("Failed to close SAIMemory connection")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        return self.conn is not None and self.embedder is not None

    @property
    def _ready(self) -> bool:
        return self.is_ready()

    def _thread_id(self, building_id: Optional[str]) -> str:
        suffix = building_id if building_id is not None else self._PERSONA_THREAD_SUFFIX
        return f"{self.persona_id}:{suffix}"

    def _append_message(self, *, building_id: Optional[str], message: dict) -> None:
        if not self._ready:
            return
        try:
            role = message.get("role", "system")
            content = message.get("content", "")
            timestamp = message.get("timestamp")
            created_at = self._timestamp_to_epoch(timestamp)
            thread_id = self._thread_id(building_id)
            resource_id = building_id or self.settings.resource_id

            with self._db_lock:
                get_or_create_thread(self.conn, thread_id, resource_id)  # type: ignore[arg-type]
                mid = add_message(
                    self.conn,
                    thread_id=thread_id,
                    role=role,
                    content=content,
                    resource_id=resource_id,
                    created_at=created_at,
                )
                if content and content.strip() and self.embedder is not None:
                    vector = self.embedder.embed([content])[0]
                    upsert_embedding(self.conn, mid, vector)
            LOGGER.debug(
                "SAIMemory upserted message=%s thread=%s role=%s", mid, thread_id, role
            )
        except Exception as exc:
            LOGGER.warning("Failed to append message to SAIMemory (building=%s): %s", building_id, exc)

    @staticmethod
    def _timestamp_to_epoch(value: Optional[str]) -> int:
        if not value:
            return int(time.time())
        try:
            dt = datetime.fromisoformat(value)
            return int(dt.timestamp())
        except Exception:
            return int(time.time())


def _fetch_all_messages(conn, thread_id: str, page_size: int = 200):
    page = 0
    rows = []
    while True:
        batch = get_messages_paginated(conn, thread_id, page=page, page_size=page_size)  # type: ignore[arg-type]
        if not batch:
            break
        rows.extend(batch)
        page += 1
    return rows
