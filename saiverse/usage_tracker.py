"""Usage tracker for LLM API calls.

Records token usage and cost to the database.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Optional

from .model_configs import calculate_cost

LOGGER = logging.getLogger(__name__)


class UsageTracker:
    """Singleton tracker for recording LLM usage to database.

    Thread-safe implementation with batch writing capability.
    """

    _instance: Optional["UsageTracker"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "UsageTracker":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._pending_records: list[dict[str, Any]] = []
        self._pending_lock = threading.Lock()
        self._batch_size = 1  # Flush immediately for debugging
        self._session_factory = None

    def configure(self, session_factory) -> None:
        """Configure the tracker with a session factory from the manager.

        This ensures usage records are written to the same database the
        manager is using (respects --db-file, test env, etc.).
        """
        self._session_factory = session_factory
        LOGGER.debug("UsageTracker configured with session factory")

    def record_usage(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_ttl: str = "",
        persona_id: Optional[str] = None,
        building_id: Optional[str] = None,
        node_type: Optional[str] = None,
        playbook_name: Optional[str] = None,
        category: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Record a single LLM usage event.

        Args:
            model_id: The model identifier
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cached_tokens: Number of tokens served FROM cache (cache read)
            cache_write_tokens: Number of tokens written TO cache
            cache_ttl: Cache TTL used ("5m" or "1h") - affects write cost calculation
            persona_id: Optional persona ID
            building_id: Optional building ID
            node_type: Type of node (llm, router, etc.)
            playbook_name: Name of the playbook if applicable
            category: Usage category (persona_speak, memory_weave_generate, etc.)
            timestamp: Optional timestamp (defaults to now)
        """
        # Calculate cost (with cache discount and write premium if applicable)
        cost_usd = calculate_cost(
            model_id, input_tokens, output_tokens, cached_tokens, cache_write_tokens,
            cache_ttl=cache_ttl,
        )

        record = {
            "timestamp": timestamp or datetime.now(),
            "persona_id": persona_id,
            "building_id": building_id,
            "model_id": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cost_usd": cost_usd if cost_usd > 0 else None,
            "node_type": node_type,
            "playbook_name": playbook_name,
            "category": category,
        }

        with self._pending_lock:
            self._pending_records.append(record)
            if len(self._pending_records) >= self._batch_size:
                self._flush_to_db()

        LOGGER.debug(
            "Usage recorded: model=%s input=%d output=%d cached=%d cache_write=%d cost=$%.6f persona=%s",
            model_id,
            input_tokens,
            output_tokens,
            cached_tokens,
            cache_write_tokens,
            cost_usd,
            persona_id,
        )

    def _get_session_factory(self):
        """Get the session factory, preferring the configured one."""
        if self._session_factory is not None:
            return self._session_factory
        # Fallback to the module-level SessionLocal
        from database.session import SessionLocal
        return SessionLocal

    def _flush_to_db(self) -> None:
        """Flush pending records to database. Must be called with _pending_lock held."""
        if not self._pending_records:
            return

        records_to_write = self._pending_records[:]
        self._pending_records.clear()

        try:
            from database.models import LLMUsageLog

            session = self._get_session_factory()()

            try:
                for record in records_to_write:
                    log_entry = LLMUsageLog(
                        TIMESTAMP=record["timestamp"],
                        PERSONA_ID=record["persona_id"],
                        BUILDING_ID=record["building_id"],
                        MODEL_ID=record["model_id"],
                        INPUT_TOKENS=record["input_tokens"],
                        OUTPUT_TOKENS=record["output_tokens"],
                        CACHED_TOKENS=record.get("cached_tokens", 0) or 0,
                        COST_USD=record["cost_usd"],
                        NODE_TYPE=record["node_type"],
                        PLAYBOOK_NAME=record["playbook_name"],
                        CATEGORY=record.get("category"),
                    )
                    session.add(log_entry)
                session.commit()
                LOGGER.debug("Flushed %d usage records to database", len(records_to_write))
            except Exception as e:
                LOGGER.error("Failed to write usage records: %s", e)
                session.rollback()
            finally:
                session.close()
        except Exception as e:
            LOGGER.error("Failed to connect to database for usage tracking: %s", e)

    def flush(self) -> None:
        """Force flush all pending records to database."""
        with self._pending_lock:
            self._flush_to_db()


# Global instance getter
def get_usage_tracker() -> UsageTracker:
    """Get the global UsageTracker instance."""
    return UsageTracker()
