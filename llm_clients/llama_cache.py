"""llama.cpp KV cache slot manager for persistent per-persona caching."""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx

from tools.context import get_active_persona_id
from .base import LLMClient

logger = logging.getLogger(__name__)


class LlamaCacheManager:
    """Manages llama.cpp slot save/restore for per-persona KV cache persistence.

    Requires the llama.cpp server to be started with --slot-save-path <dir>.
    Slot IDs are allocated dynamically from a pool matching --parallel N.
    Cache files are named {persona_id}__{model_id}.bin inside slot_save_path.
    """

    def __init__(self, base_url: str, slot_save_path: str, parallel: int = 1) -> None:
        # Strip /v1 suffix — slot API lives at /slots/*, not /v1/slots/*
        base = base_url.rstrip("/")
        self._base_url = base[:-3] if base.endswith("/v1") else base
        self._slot_save_path = Path(slot_save_path).expanduser()
        self._parallel = parallel
        self._slot_queue: queue.Queue[int] = queue.Queue()
        for i in range(parallel):
            self._slot_queue.put(i)
        self._model_id: Optional[str] = None
        self._model_lock = threading.Lock()

    def _fetch_model_id(self) -> str:
        try:
            resp = httpx.get(f"{self._base_url}/v1/models", timeout=10.0)
            resp.raise_for_status()
            models = resp.json().get("data", [])
            if models:
                return models[0].get("id", "unknown")
        except Exception as exc:
            logger.warning("[llama_cache] Failed to fetch model ID from server: %s", exc)
        return "unknown"

    def get_model_id(self) -> str:
        with self._model_lock:
            if self._model_id is None:
                self._model_id = self._fetch_model_id()
                logger.info("[llama_cache] Server model: %s", self._model_id)
            return self._model_id

    def invalidate_model_id(self) -> None:
        """Clear cached model ID so it will be re-fetched on next use."""
        with self._model_lock:
            self._model_id = None

    def _safe_model_id(self) -> str:
        return self.get_model_id().replace("/", "_").replace("\\", "_").replace(":", "_")

    def cache_filename(self, persona_id: str) -> str:
        return f"{persona_id}__{self._safe_model_id()}.bin"

    def cache_exists(self, persona_id: str) -> bool:
        return (self._slot_save_path / self.cache_filename(persona_id)).exists()

    def acquire_slot(self, timeout: float = 300.0) -> int:
        try:
            slot = self._slot_queue.get(timeout=timeout)
            logger.debug("[llama_cache] Acquired slot %d", slot)
            return slot
        except queue.Empty:
            raise RuntimeError(
                f"No llama.cpp slot available within {timeout:.0f}s (parallel={self._parallel})"
            )

    def release_slot(self, slot: int) -> None:
        self._slot_queue.put(slot)
        logger.debug("[llama_cache] Released slot %d", slot)

    def restore(self, slot: int, persona_id: str) -> None:
        if not self.cache_exists(persona_id):
            logger.debug("[llama_cache] No cache for %s — starting fresh on slot %d", persona_id, slot)
            return
        url = f"{self._base_url}/slots/{slot}?action=restore"
        filename = self.cache_filename(persona_id)
        try:
            resp = httpx.post(url, json={"filename": filename}, timeout=60.0)
            if resp.status_code == 200:
                logger.debug("[llama_cache] Restored slot %d for %s (%s)", slot, persona_id, filename)
            else:
                logger.warning(
                    "[llama_cache] Restore slot %d returned HTTP %d: %s",
                    slot, resp.status_code, resp.text[:200],
                )
        except Exception as exc:
            logger.warning("[llama_cache] Restore failed for slot %d (%s): %s", slot, persona_id, exc)

    def save(self, slot: int, persona_id: str) -> None:
        self._slot_save_path.mkdir(parents=True, exist_ok=True)
        url = f"{self._base_url}/slots/{slot}?action=save"
        filename = self.cache_filename(persona_id)
        try:
            resp = httpx.post(url, json={"filename": filename}, timeout=60.0)
            if resp.status_code == 200:
                logger.debug("[llama_cache] Saved slot %d for %s (%s)", slot, persona_id, filename)
            else:
                logger.warning(
                    "[llama_cache] Save slot %d returned HTTP %d: %s",
                    slot, resp.status_code, resp.text[:200],
                )
        except Exception as exc:
            logger.warning("[llama_cache] Save failed for slot %d (%s): %s", slot, persona_id, exc)


class LlamaCachedClient(LLMClient):
    """Wraps any LLMClient with llama.cpp slot restore-before / save-after each inference."""

    def __init__(self, inner: LLMClient, cache: LlamaCacheManager) -> None:
        super().__init__(supports_images=getattr(inner, "supports_images", False))
        self._inner = inner
        self._cache = cache

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        self._inner.configure_parameters(parameters)

    def consume_usage(self):
        return self._inner.consume_usage()

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> str | Dict[str, Any]:
        persona_id = get_active_persona_id() or "unknown"
        slot = self._cache.acquire_slot()
        try:
            self._cache.restore(slot, persona_id)
            result = self._inner.generate(
                messages, tools=tools, response_schema=response_schema,
                temperature=temperature, **kwargs,
            )
            self._cache.save(slot, persona_id)
            return result
        finally:
            self._cache.release_slot(slot)

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        persona_id = get_active_persona_id() or "unknown"
        slot = self._cache.acquire_slot()
        stream_completed = False
        try:
            self._cache.restore(slot, persona_id)
            for chunk in self._inner.generate_stream(
                messages, tools=tools, response_schema=response_schema,
                temperature=temperature, **kwargs,
            ):
                yield chunk
            stream_completed = True
            self._cache.save(slot, persona_id)
        finally:
            if not stream_completed:
                logger.warning(
                    "[llama_cache] Stream interrupted for %s — slot %d cache not saved",
                    persona_id, slot,
                )
            self._cache.release_slot(slot)


__all__ = ["LlamaCacheManager", "LlamaCachedClient"]
