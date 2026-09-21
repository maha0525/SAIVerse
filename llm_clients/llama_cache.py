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
        self._slot_save_path.mkdir(parents=True, exist_ok=True)
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


def _delegated_attribute(name: str, default: Any) -> property:
    """inner の属性をそのまま見せる property を作る。

    wrapper が自前の値を持つと、呼び出し側は「包まれていない client なら
    得られたはずの値」を取り損ねる。既定値は duck-typed な inner (テストの
    fake 等) が属性を持たない場合の保険。
    """

    def getter(self: "LlamaCachedClient") -> Any:
        return getattr(self._inner, name, default)

    def setter(self: "LlamaCachedClient", value: Any) -> None:
        if self._wiring:
            # 基底 __init__ が配る既定値 (model="" 等)。これを通すと、包んだ
            # 瞬間に inner の値が既定値へ潰れる。包むことは inner を変えない。
            return
        setattr(self._inner, name, value)

    return property(getter, setter)


class LlamaCachedClient(LLMClient):
    """Wraps any LLMClient with llama.cpp slot restore-before / save-after each inference.

    **この wrapper は完全な facade である**: 推論の前後に slot の restore / save を
    足すだけで、呼び出し側から見える状態は inner のものと一寸も違わない。

    理由は実測にある (docs/issues/llama_cached_client_state_delegation_missing.md)。
    一部だけ委譲していた頃、``consume_reasoning`` が wrapper 自身の空の state を
    返すため、``llama_slot_save_path`` を設定したモデルを使うペルソナの思考が
    発言の記録から丸ごと落ちていた。SEA runtime は factory が返したオブジェクト
    (= この wrapper) に対して ``consume_*`` を呼ぶので、**委譲していない
    ``consume_*`` / ``_store_*`` / 属性は、そのまま「存在しないこと」になる**。

    新しい state を ``LLMClient`` に足すときは、ここにも委譲を足すこと。
    """

    #: 配線中 (基底 __init__ の実行中) は、委譲属性への代入を inner へ通さない。
    _wiring = False

    def __init__(self, inner: LLMClient, cache: LlamaCacheManager) -> None:
        # 委譲属性は inner を書き換える property なので、基底の __init__ が
        # self.config_key = "" 等を実行する時点で _inner が要る。その既定値が
        # inner へ届くと、設定済みの client を包んだ瞬間に値が潰れる — 配線中は
        # setter を素通しにして、包むことが inner を変えないようにする。
        # (factory は未設定の inner を包むので現状の経路では差が出ないが、
        #  任意の LLMClient を包む契約なのでこの守りは消してはならない。)
        self._inner = inner
        self._wiring = True
        try:
            super().__init__()
        finally:
            self._wiring = False
        self._cache = cache

    # ── 属性の委譲 ──────────────────────────────────────────────────────
    #: 価格引き当ての設定キー。usage を記録するのは inner なので、factory が
    #: wrapper へ代入した設定キーを inner まで通す。通さないと inner の
    #: _store_usage が self.model (API 名) へフォールバックし、同名の従量課金版
    #: 設定の単価が引き当てられる
    #: (docs/intent/model_provider_management.md「使用量の帰属」)。
    config_key = _delegated_attribute("config_key", "")
    #: API のモデル名。wrapper が空文字を返すと、呼び出し側は「どのモデルが
    #: 答えたか」を wrapper 越しには言えなくなる。
    model = _delegated_attribute("model", "")
    supports_images = _delegated_attribute("supports_images", False)
    supports_audio = _delegated_attribute("supports_audio", False)
    supports_video = _delegated_attribute("supports_video", False)

    # ── バックエンドの起動・貸出札 ──────────────────────────────────────
    def ensure_backend(self) -> None:
        self._inner.ensure_backend()

    def backend_lease(self):
        return self._inner.backend_lease()

    # ── リクエストの組み立て ────────────────────────────────────────────
    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        self._inner.configure_parameters(parameters)

    def prefer_minimal_reasoning(self) -> None:
        """リクエストを組むのは inner なので、思考の指定も inner へ渡す。"""
        self._inner.prefer_minimal_reasoning()

    def response_token_limit(self) -> Optional[int]:
        """送る応答の上限は inner のもの (リクエストを組むのは inner)。"""
        return self._inner.response_token_limit()

    # ── 応答から取り出す state ─────────────────────────────────────────
    # 応答を解析して state を積むのは inner なので、取り出し口も積み口も
    # inner へ通す。積み口 (_store_*) も要る — SEA runtime は「覗いてから
    # 戻す」(_store_tool_detection で putback) を行うため、片方だけ委譲すると
    # 戻した値が wrapper に埋もれる。
    def consume_usage(self):
        return self._inner.consume_usage()

    def consume_tool_detection(self) -> Dict[str, Any] | None:
        return self._inner.consume_tool_detection()

    def _store_tool_detection(self, result: Dict[str, Any] | None) -> None:
        self._inner._store_tool_detection(result)

    def consume_reasoning(self) -> List[Dict[str, str]]:
        return self._inner.consume_reasoning()

    def _store_reasoning(self, entries: List[Dict[str, str]] | None) -> None:
        self._inner._store_reasoning(entries)

    def consume_reasoning_details(self) -> Any:
        return self._inner.consume_reasoning_details()

    def _store_reasoning_details(self, details: Any) -> None:
        self._inner._store_reasoning_details(details)

    def consume_thought_signature(self) -> Optional[str]:
        return self._inner.consume_thought_signature()

    def _store_thought_signature(self, value: Optional[str]) -> None:
        self._inner._store_thought_signature(value)

    def consume_attachments(self) -> List[Dict[str, Any]]:
        return self._inner.consume_attachments()

    def _store_attachment(self, metadata: Dict[str, Any]) -> None:
        self._inner._store_attachment(metadata)

    def _store_usage(self, *args: Any, **kwargs: Any) -> None:
        self._inner._store_usage(*args, **kwargs)

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        *,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return self._inner.generate_with_tool_detection(
            messages, tools, temperature=temperature, **kwargs,
        )

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
        # restore (slot キャッシュ読込) より先にサーバーの存在を保証する。
        # idle 自動停止後は inner の送信時点では遅い — restore が先に
        # 停止済みポートへ飛んでしまう
        self._inner.ensure_backend()
        # restore〜save の全体に貸出札を掛ける。/slots は save 中も「暇」を
        # 返すため、札なしだと保存中のサーバーを idle 停止が撃てる
        with self._inner.backend_lease():
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
        self._inner.ensure_backend()  # restore より先 (generate と同じ理由)
        with self._inner.backend_lease():  # restore〜save 全体 (generate と同じ理由)
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
