from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sai_memory.config import Settings, load_settings
from sai_memory.memory.chunking import chunk_text
from sai_memory.memory.recall import (
    Embedder,
    semantic_recall_groups,
)
from sai_memory.memory.storage import (
    add_message,
    Message,
    get_messages_last,
    get_messages_paginated,
    get_or_create_thread,
    init_db,
    compose_message_content,
    replace_message_embeddings,
)
from sai_memory.backup import BackupError, run_backup

LOGGER = logging.getLogger(__name__)


def _auto_backup_enabled() -> bool:
    value = os.getenv("SAIMEMORY_BACKUP_ON_START", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


class SAIMemoryAdapter:
    """Thin integration layer that lets SAIVerse talk to SAIMemory storage."""

    _PERSONA_THREAD_SUFFIX = "__persona__"
    _ACTIVE_STATE_FILENAME = "active_state.json"

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
            # Create working_memory table if not exists
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS working_memory (
                    persona_id TEXT PRIMARY KEY,
                    data TEXT,
                    updated_at REAL
                )
            """)
            self.conn.commit()
        except Exception as exc:
            LOGGER.exception("Failed to initialise SAIMemory DB at %s", self.settings.db_path)
            self.conn = None
            self.embedder = None
            raise exc

        try:
            self.embedder = Embedder(
                model=self.settings.embed_model,
                local_model_path=self.settings.embed_model_path,
                model_dim=self.settings.embed_model_dim,
            )
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

        if _auto_backup_enabled():
            threading.Thread(target=self._run_startup_backup, daemon=True).start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_working_memory(self) -> Dict[str, Any]:
        """Load working memory from DB.

        Returns:
            Dict containing working memory data, or empty dict if not found.
        """
        if not self._ready:
            return {}
        try:
            with self._db_lock:
                cur = self.conn.execute(
                    "SELECT data FROM working_memory WHERE persona_id = ?",
                    (self.persona_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
                return {}
        except Exception as exc:
            LOGGER.warning("Failed to load working_memory for %s: %s", self.persona_id, exc)
            return {}

    def save_working_memory(self, data: Dict[str, Any]) -> None:
        """Save working memory to DB.

        Args:
            data: Dict to persist as working memory.
        """
        if not self._ready:
            return
        try:
            with self._db_lock:
                json_data = json.dumps(data, ensure_ascii=False)
                updated_at = time.time()
                self.conn.execute(
                    """
                    INSERT INTO working_memory (persona_id, data, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(persona_id) DO UPDATE SET data = ?, updated_at = ?
                    """,
                    (self.persona_id, json_data, updated_at, json_data, updated_at)
                )
                self.conn.commit()
                LOGGER.debug("Saved working_memory for %s", self.persona_id)
        except Exception as exc:
            LOGGER.warning("Failed to save working_memory for %s: %s", self.persona_id, exc)

    def append_building_message(
        self,
        building_id: str,
        message: dict,
        *,
        thread_suffix: Optional[str] = None,
    ) -> None:
        self._append_message(building_id=building_id, message=message, thread_suffix=thread_suffix)

    def append_persona_message(
        self,
        message: dict,
        *,
        thread_suffix: Optional[str] = None,
    ) -> None:
        self._append_message(building_id=None, message=message, thread_suffix=thread_suffix)

    def recent_messages(self, building_id: str, max_chars: int) -> List[dict]:
        if not self._ready:
            return []
        thread_id = self._thread_id(building_id)
        try:
            with self._db_lock:
                rows = get_messages_last(self.conn, thread_id, self.settings.last_messages)  # type: ignore[arg-type]
                payloads = [self._payload_from_message_locked(msg) for msg in rows]
        except Exception as exc:
            LOGGER.warning("Failed to fetch recent messages for %s: %s", thread_id, exc)
            return []

        selected: List[dict] = []
        consumed = 0
        for payload in reversed(payloads):
            text = payload.get("content", "") or ""
            consumed += len(text)
            if consumed > max_chars:
                break
            selected.insert(0, payload)
        return selected

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_startup_backup(self) -> None:
        db_path = Path(self.settings.db_path)
        rdiff_path = os.getenv("SAIMEMORY_RDIFF_PATH")
        try:
            run_backup(persona_id=self.persona_id, db_path=db_path, rdiff_path=rdiff_path)
            LOGGER.info("Auto SAIMemory backup completed for persona=%s", self.persona_id)
        except BackupError as exc:
            LOGGER.warning("Auto SAIMemory backup skipped for persona=%s: %s", self.persona_id, exc)
        except Exception:
            LOGGER.exception("Unexpected error during auto SAIMemory backup for %s", self.persona_id)

    def recent_persona_messages(
        self,
        max_chars: int,
        *,
        required_tags: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[dict]:
        if not self._ready:
            return []
        thread_id = self._thread_id(None)
        try:
            with self._db_lock:
                all_rows = _fetch_all_messages(self.conn, thread_id)
                payloads = [self._payload_from_message_locked(msg) for msg in all_rows]
        except Exception as exc:
            LOGGER.warning("Failed to fetch persona messages for %s: %s", thread_id, exc)
            return []

        selected: List[dict] = []
        consumed = 0
        required_tags = required_tags or []
        pulse_tag = f"pulse:{pulse_id}" if pulse_id else None

        for payload in reversed(payloads):
            tags = []
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                raw_tags = metadata.get("tags")
                if isinstance(raw_tags, list):
                    tags = [str(tag) for tag in raw_tags if tag]

            include = True
            if required_tags:
                include = any(tag in tags for tag in required_tags)
            if pulse_tag and pulse_tag in tags:
                include = True
            if not tags and required_tags:
                # fallback: include legacy entries without tags only if we expect conversation logs
                include = not required_tags or "conversation" not in required_tags
            if not include:
                continue
            text = payload.get("content", "") or ""
            consumed += len(text)
            if consumed > max_chars:
                break
            selected.insert(0, payload)
        return selected

    def recent_persona_messages_balanced(
        self,
        max_chars: int,
        participant_ids: List[str],
        *,
        required_tags: Optional[List[str]] = None,
        pulse_id: Optional[str] = None,
    ) -> List[dict]:
        """Get recent messages balanced across conversation partners.

        Allocates max_chars equally among participants, retrieving recent
        messages from each partner's conversations.

        Args:
            max_chars: Total character budget
            participant_ids: List of partner IDs to balance (e.g., ["user", "persona_b"])
            required_tags: Only include messages with these tags
            pulse_id: Always include messages with this pulse ID

        Returns:
            List of messages, sorted by timestamp, balanced across participants
        """
        if not self._ready or not participant_ids:
            return []

        thread_id = self._thread_id(None)
        try:
            with self._db_lock:
                all_rows = _fetch_all_messages(self.conn, thread_id)
                payloads = [self._payload_from_message_locked(msg) for msg in all_rows]
        except Exception as exc:
            LOGGER.warning("Failed to fetch persona messages for balancing: %s", exc)
            return []

        required_tags = required_tags or []
        pulse_tag = f"pulse:{pulse_id}" if pulse_id else None

        # Group messages by participant
        # Key: participant_id, Value: list of (index, payload) tuples
        participant_groups: Dict[str, List[tuple]] = {pid: [] for pid in participant_ids}
        other_messages: List[tuple] = []  # Messages without "with" or with unknown participants

        for idx, payload in enumerate(payloads):
            metadata = payload.get("metadata") or {}
            tags = metadata.get("tags", [])
            with_list = metadata.get("with", [])

            # Check if message matches required tags
            include = True
            if required_tags:
                include = any(tag in tags for tag in required_tags)
            if pulse_tag and pulse_tag in tags:
                include = True
            if not include:
                continue

            # Assign to participant groups
            if with_list:
                for partner in with_list:
                    if partner in participant_groups:
                        participant_groups[partner].append((idx, payload))
            else:
                # Messages without "with" go to other
                other_messages.append((idx, payload))

        # Calculate per-participant budget
        num_participants = len(participant_ids)
        per_participant_chars = max_chars // num_participants if num_participants > 0 else max_chars

        # Select messages from each participant (most recent first)
        selected_with_idx: List[tuple] = []

        for pid in participant_ids:
            group = participant_groups.get(pid, [])
            consumed = 0
            for idx, payload in reversed(group):
                text = payload.get("content", "") or ""
                if consumed + len(text) > per_participant_chars:
                    break
                consumed += len(text)
                selected_with_idx.append((idx, payload))

        # Add some "other" messages if there's remaining budget
        total_consumed = sum(len(p.get("content", "") or "") for _, p in selected_with_idx)
        remaining = max_chars - total_consumed
        if remaining > 0 and other_messages:
            for idx, payload in reversed(other_messages):
                text = payload.get("content", "") or ""
                if len(text) > remaining:
                    break
                remaining -= len(text)
                selected_with_idx.append((idx, payload))

        # Sort by original index to maintain chronological order
        selected_with_idx.sort(key=lambda x: x[0])
        return [payload for _, payload in selected_with_idx]

    def list_thread_summaries(self, max_preview_chars: int = 120) -> List[Dict[str, Any]]:
        if not self._ready:
            return []
        try:
            with self._db_lock:
                cur = self.conn.execute("SELECT id FROM threads ORDER BY id ASC")
                rows = cur.fetchall()
                active_suffix = self._active_persona_suffix()
                summaries: List[Dict[str, Any]] = []
                for (thread_id,) in rows:
                    first_messages = get_messages_paginated(self.conn, thread_id, page=0, page_size=1)
                    preview = ""
                    first_id: Optional[str] = None
                    if first_messages:
                        first_msg = first_messages[0]
                        first_id = first_msg.id
                        preview = compose_message_content(self.conn, first_msg)
                        if max_preview_chars > 0 and len(preview) > max_preview_chars:
                            preview = preview[: max_preview_chars - 1] + "…"
                    suffix = thread_id.split(":", 1)[1] if ":" in thread_id else thread_id
                    summaries.append(
                        {
                            "thread_id": thread_id,
                            "suffix": suffix,
                            "preview": preview.strip(),
                            "first_message_id": first_id,
                            "active": bool(active_suffix and suffix == active_suffix),
                        }
                    )
                return summaries
        except Exception as exc:
            LOGGER.warning("Failed to list threads for persona %s: %s", self.persona_id, exc)
            return []

    def get_thread_messages(self, thread_id: str, page: int = 0, page_size: int = 100) -> List[dict]:
        if not self._ready:
            return []
        try:
            with self._db_lock:
                msgs = get_messages_paginated(self.conn, thread_id, page=page, page_size=page_size)  # type: ignore[arg-type]
                return [self._payload_from_message_locked(msg) for msg in msgs]
        except Exception as exc:
            LOGGER.warning("Failed to get messages for thread %s: %s", thread_id, exc)
            return []

    def count_thread_messages(self, thread_id: str) -> int:
        if not self._ready:
            return 0
        try:
            with self._db_lock:
                cur = self.conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?", (thread_id,))  # type: ignore[attr-defined]
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            LOGGER.warning("Failed to count messages for thread %s: %s", thread_id, exc)
            return 0

    def update_message_content(self, message_id: str, new_content: str) -> bool:
        """Legacy wrapper for update_message with only content."""
        return self.update_message(message_id, new_content=new_content)

    def update_message(
        self, 
        message_id: str, 
        new_content: Optional[str] = None, 
        new_created_at: Optional[int] = None
    ) -> bool:
        """Update message content and/or timestamp.
        
        Args:
            message_id: ID of the message to update
            new_content: New content (optional, if None content is unchanged)
            new_created_at: New timestamp as Unix epoch (optional)
            
        Returns:
            True if successful, False otherwise
        """
        if not self._ready:
            return False
        if new_content is None and new_created_at is None:
            return True  # Nothing to update
        try:
            with self._db_lock:
                # Check message exists
                cur = self.conn.execute("SELECT content FROM messages WHERE id=?", (message_id,))  # type: ignore[attr-defined]
                row = cur.fetchone()
                if row is None:
                    return False
                
                current_content = row[0]
                    
                # Update fields as needed
                if new_content is not None and new_created_at is not None:
                    self.conn.execute(  # type: ignore[attr-defined]
                        "UPDATE messages SET content=?, created_at=? WHERE id=?",
                        (new_content, new_created_at, message_id),
                    )
                elif new_content is not None:
                    self.conn.execute(  # type: ignore[attr-defined]
                        "UPDATE messages SET content=? WHERE id=?",
                        (new_content, message_id),
                    )
                elif new_created_at is not None:
                    self.conn.execute(  # type: ignore[attr-defined]
                        "UPDATE messages SET created_at=? WHERE id=?",
                        (new_created_at, message_id),
                    )
                
                # Update embeddings only if content changed
                if new_content is not None:
                    self.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))  # type: ignore[attr-defined]
                    content_strip = new_content.strip()
                    if content_strip and self.embedder is not None:
                        chunks = chunk_text(
                            content_strip,
                            min_chars=self.settings.chunk_min_chars,
                            max_chars=self.settings.chunk_max_chars,
                        )
                        payload = [chunk.strip() for chunk in chunks if chunk and chunk.strip()]
                        if payload:
                            vectors = self.embedder.embed(payload, is_query=False)
                            replace_message_embeddings(self.conn, message_id, vectors)   # type: ignore[attr-defined]
                
                self.conn.commit()  # type: ignore[attr-defined]
                return True
        except Exception as exc:
            LOGGER.warning("Failed to update message %s: %s", message_id, exc)
            return False

    def delete_message(self, message_id: str) -> bool:
        if not self._ready:
            return False
        try:
            with self._db_lock:
                self.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))  # type: ignore[attr-defined]
                self.conn.execute("DELETE FROM messages WHERE id=?", (message_id,))  # type: ignore[attr-defined]
                self.conn.commit()  # type: ignore[attr-defined]
                return True
        except Exception as exc:
            LOGGER.warning("Failed to delete message %s: %s", message_id, exc)
            return False

    def delete_thread(self, thread_id: str) -> bool:
        if not self._ready:
            return False
        from sai_memory.memory.storage import delete_thread
        try:
            with self._db_lock:
                return delete_thread(self.conn, thread_id)
        except Exception as exc:
            LOGGER.warning("Failed to delete thread %s: %s", thread_id, exc)
            return False

    def recall_snippet(
        self,
        building_id: Optional[str] = None,
        query_text: str = "",
        *,
        max_chars: int = 800,
        exclude_created_at: Optional[int | List[int]] = None,
        topk: Optional[int] = None,
        range_before: Optional[int] = None,
        range_after: Optional[int] = None,
    ) -> str:
        if not self._ready:
            return ""
        if not query_text or not query_text.strip():
            return ""

        thread_id = self._thread_id(building_id)
        # Disable both thread_id and resource_id filters to search across all threads
        search_thread_id = None
        search_resource_id = None

        guard_ids: set[str] = set()
        try:
            with self._db_lock:
                recall_topk = self.settings.topk if topk is None else max(1, int(topk))
                before = self.settings.range_before if range_before is None else max(0, int(range_before))
                after = self.settings.range_after if range_after is None else max(0, int(range_after))
                guard_count = max(0, self.settings.last_messages)
                if guard_count > 0:
                    recent_msgs = get_messages_last(self.conn, thread_id, guard_count)
                    guard_ids = {m.id for m in recent_msgs}
                effective_topk = recall_topk + len(guard_ids)
                groups_raw = semantic_recall_groups(
                    self.conn,
                    self.embedder,
                    query_text,
                    thread_id=search_thread_id,
                    resource_id=search_resource_id,
                    topk=effective_topk,
                    range_before=before,
                    range_after=after,
                    scope=self.settings.scope,
                    exclude_message_ids=guard_ids,
                    required_tags=["conversation"],
                )
                groups = []
                for seed, bundle, score in groups_raw:
                    formatted = [
                        (msg, compose_message_content(self.conn, msg))
                        for msg in bundle
                    ]
                    groups.append((seed, formatted, score))
        except Exception as exc:
            LOGGER.warning("SAIMemory recall failed for %s: %s", thread_id, exc)
            return ""

        lines: List[str] = ["[Memory Recall]"]
        exclude_created_values: set[int] = set()
        if exclude_created_at is not None:
            if isinstance(exclude_created_at, (list, tuple, set)):
                candidates = exclude_created_at
            else:
                candidates = [exclude_created_at]
            for value in candidates:
                if value is None:
                    continue
                try:
                    exclude_created_values.add(int(value))
                except (TypeError, ValueError):
                    continue
        seen: set[str] = set()
        for seed, bundle, score in groups:
            for msg, rendered in bundle:
                if msg.id in seen or msg.id in guard_ids:
                    continue
                seen.add(msg.id)
                if exclude_created_values and msg.created_at in exclude_created_values:
                    continue
                if msg.role == "system":
                    continue
                content = (rendered or "").strip()
                if not content:
                    continue
                dt = datetime.fromtimestamp(msg.created_at)
                ts = dt.strftime("%Y-%m-%d %H:%M")
                role = msg.role
                entry = f"- {role} @ {ts}: {content}"
                if score is not None and msg.id == seed.id:
                    entry = f"- {role} @ {ts} (score={score:.3f}): {content}"
                candidate = lines + [entry]
                combined = "\n".join(candidate)
                if len(combined) > max_chars:
                    return "\n".join(lines)
                lines.append(entry)

        return "" if len(lines) == 1 else "\n".join(lines)

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

    def _thread_id(
        self,
        building_id: Optional[str] = None,
        *,
        thread_suffix: Optional[str] = None,
    ) -> str:
        if thread_suffix:
            suffix = thread_suffix
        else:
            if building_id is not None:
                suffix = building_id
            else:
                suffix = self._active_persona_suffix() or self._PERSONA_THREAD_SUFFIX
        return f"{self.persona_id}:{suffix}"

    def _payload_from_message_locked(self, msg) -> dict:
        if self.conn is None:
            content = msg.content or ""
        else:
            content = compose_message_content(self.conn, msg) or ""
        original_role = msg.role
        role = "assistant" if original_role == "model" else original_role
        if isinstance(role, str) and role.lower() == "system":
            role = "user"
            if content:
                content = f"<system>\n{content}\n</system>"
            else:
                content = "<system></system>"
        payload: Dict[str, Any] = {
            "id": msg.id,
            "thread_id": msg.thread_id,
            "role": role,
            "content": content,
            "created_at": msg.created_at,
        }
        if msg.metadata:
            payload["metadata"] = msg.metadata
        return payload

    def _active_persona_suffix(self) -> Optional[str]:
        path = self.persona_dir / self._ACTIVE_STATE_FILENAME
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOGGER.debug("Failed to read active state for %s: %s", self.persona_id, exc)
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOGGER.warning("Invalid JSON in %s: %s", path, exc)
            return None

        candidate: Optional[str] = None
        if isinstance(data, dict):
            for key in ("active_thread_id", "thread_id", "active_thread"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    break
        elif isinstance(data, str) and data.strip():
            candidate = data.strip()

        if candidate:
            return candidate
        return None

    def set_active_thread(self, thread_id: str) -> bool:
        """Set the active thread for this persona.
        
        Args:
            thread_id: Full thread ID (e.g., "persona_id:suffix")
            
        Returns:
            True if successful, False otherwise
        """
        if not thread_id:
            return False
            
        # Extract suffix from thread_id
        suffix = thread_id.split(":", 1)[1] if ":" in thread_id else thread_id
        
        path = self.persona_dir / self._ACTIVE_STATE_FILENAME
        try:
            state_data = {
                "active_thread_id": suffix,
                "updated_at": datetime.now().isoformat()
            }
            path.write_text(json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")
            LOGGER.info("Set active thread for %s to %s", self.persona_id, suffix)
            return True
        except Exception as exc:
            LOGGER.warning("Failed to set active thread for %s: %s", self.persona_id, exc)
            return False

    def get_current_thread(self) -> Optional[str]:
        """Get the current active thread suffix.
        
        Returns:
            The thread suffix (not the full thread_id), or None if not set
        """
        return self._active_persona_suffix()

    def _append_message(
        self,
        *,
        building_id: Optional[str],
        message: dict,
        thread_suffix: Optional[str] = None,
    ) -> None:
        if not self._ready:
            return
        try:
            role = message.get("role", "system")
            content = message.get("content", "")
            timestamp = message.get("timestamp")
            created_at = self._timestamp_to_epoch(timestamp)
            thread_id = self._thread_id(building_id, thread_suffix=thread_suffix)
            resource_id = building_id or self.settings.resource_id
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                metadata = None
            embedding_chunks = message.get("embedding_chunks")
            skip_embedding = False
            if embedding_chunks is not None:
                try:
                    skip_embedding = int(embedding_chunks) == 0
                except (TypeError, ValueError):
                    skip_embedding = False

            with self._db_lock:
                get_or_create_thread(self.conn, thread_id, resource_id)  # type: ignore[arg-type]
                mid = add_message(
                    self.conn,
                    thread_id=thread_id,
                    role=role,
                    content=content,
                    resource_id=resource_id,
                    created_at=created_at,
                    metadata=metadata,
                )
                if (not skip_embedding) and content and content.strip() and self.embedder is not None:
                    chunks = chunk_text(
                        content,
                        min_chars=self.settings.chunk_min_chars,
                        max_chars=self.settings.chunk_max_chars,
                    )
                    payload = [c.strip() for c in chunks if c and c.strip()]
                    if payload:
                        vectors = self.embedder.embed(payload, is_query=False)
                        replace_message_embeddings(self.conn, mid, vectors)
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
