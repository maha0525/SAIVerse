"""Gemini explicit cache controller (Phase 3 / M1)。

Anthropic の cache_control (ステートレス) と違い、Gemini の explicit cache は
``client.caches.create()`` でリソース (cache.name) を作り、``generate_content`` 時に
``cached_content=name`` で参照、不要になったら ``caches.delete()`` する **ステートフル**
な機構。本コントローラはそのライフサイクル (作成 / 再利用 / 失効判定) を担う。

M1 スコープ: 「Gemini で実際に cache hit する最小配線」。
- キャッシュ対象は **system_instruction (= head)**。head_pipeline が安定を保証するので
  内容ハッシュで同一性を判定し、生きていれば再利用する。
- key = ``(model, sha256(system_instruction))``。同一 head は同一 cache に集約。
- 最小トークン (Flash: 1024) 未満は create が失敗するので silent fallback (None 返し)。

未実装 (後続 M):
- M2: orphan cleanup (起動時 list → delete)、head 変更 (metabolism) での明示 invalidate
- M3: persona/line 紐付け (timer 表示用)、expire_time の正確な反映
- M4: 標準モードの pulse 終了 delete

設計: docs/intent/cache_lifecycle_control.md §4.5 / §7 Phase 3
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

LOGGER = logging.getLogger(__name__)

# SAIVerse 由来の cache を識別する displayName prefix (orphan cleanup 用)。
DISPLAY_NAME_PREFIX = "saiverse:"
# create 失敗を避けるための最小トークン (Gemini Flash 系の下限)。
DEFAULT_MIN_TOKENS = 1024
# expire 直前の再利用を避けるための安全マージン (秒)。
_EXPIRY_SAFETY_MARGIN = 10.0

_TTL_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)


def parse_ttl_seconds(ttl: Any, default: int = 300) -> int:
    """"5m" / "15m" / "30m" / "1h" / "300s" 等を秒に変換する。

    数値ならそのまま秒として扱う。解釈不能なら ``default``。
    """
    if isinstance(ttl, (int, float)):
        return int(ttl)
    if not isinstance(ttl, str):
        return default
    m = _TTL_RE.match(ttl)
    if not m:
        return default
    value = int(m.group(1))
    unit = m.group(2).lower()
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class _CacheEntry:
    name: str           # Gemini cache resource name (cached_content に渡す値)
    model: str
    expire_at: float    # epoch seconds (再利用可否判定用)
    ttl_seconds: int


class GeminiCacheController:
    """(model, head ハッシュ) 単位で Gemini explicit cache を管理する。

    in-memory のみ (非永続)。プロセス再起動で消えるため、実体の掃除は起動時の
    orphan cleanup (M2) が担う。
    """

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], _CacheEntry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _content_signature(contents: Optional[list]) -> str:
        """contents (types.Content の列) を決定的な文字列に直列化する (ハッシュ用)。

        各メッセージの role とテキスト part を連結。非テキスト part (画像等) は
        mime/サイズの marker で表現して内容変化を検出する。
        """
        if not contents:
            return ""
        lines = []
        for c in contents:
            role = getattr(c, "role", "") or ""
            parts = getattr(c, "parts", None) or []
            chunks = []
            for p in parts:
                text = getattr(p, "text", None)
                if text:
                    chunks.append(text)
                    continue
                inline = getattr(p, "inline_data", None)
                if inline is not None:
                    data = getattr(inline, "data", b"") or b""
                    mime = getattr(inline, "mime_type", "") or ""
                    chunks.append(f"<{mime}:{len(data)}>")
            lines.append(role + "\x1f" + "\x1e".join(chunks))
        return "\x1d".join(lines)

    def _key(self, model: str, system_instruction: str, contents: Optional[list]) -> Tuple[str, str]:
        raw = system_instruction + "\x1c" + self._content_signature(contents)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return (model, digest)

    @staticmethod
    def _approx_chars(system_instruction: str, contents: Optional[list]) -> int:
        """sys + contents のおおよその文字数 (最小トークンガード用)。

        1 token >= 1 char なので「文字数 < min_tokens」なら確実にトークン不足
        (= create 失敗確定)。これで API を叩かずに弾ける (日本語でも誤って弾かない)。
        """
        total = len(system_instruction or "")
        for c in (contents or []):
            for p in (getattr(c, "parts", None) or []):
                total += len(getattr(p, "text", "") or "")
        return total

    def ensure(
        self,
        client: Any,
        model: str,
        system_instruction: str,
        ttl_seconds: int,
        *,
        contents: Optional[list] = None,
        min_tokens: int = DEFAULT_MIN_TOKENS,
    ) -> Optional[str]:
        """``system_instruction`` + ``contents`` (キャッシュ対象の prefix) に対応する
        生きた cache の name を返す。無ければ作成。

        ``contents`` が None/空なら system_instruction のみをキャッシュする。
        作成不能 (トークン不足 / API エラー) なら None を返し、呼び出し側は
        explicit cache 無しの通常コールにフォールバックする。
        """
        if not client or not model or (not system_instruction and not contents):
            return None
        key = self._key(model, system_instruction, contents)
        now = time.time()

        with self._lock:
            ent = self._entries.get(key)
            if ent and ent.expire_at > now + _EXPIRY_SAFETY_MARGIN:
                return ent.name

        # 最小トークンガード (文字数で安全側に判定、API コールなし)。
        if self._approx_chars(system_instruction, contents) < min_tokens:
            LOGGER.debug("[gemini_cache] skip create (prefix below min tokens) model=%s", model)
            return None

        try:
            from google.genai import types
            cfg_kwargs: Dict[str, Any] = {
                "display_name": f"{DISPLAY_NAME_PREFIX}{key[1][:24]}",
                "ttl": f"{int(ttl_seconds)}s",
            }
            if system_instruction:
                cfg_kwargs["system_instruction"] = system_instruction
            if contents:
                cfg_kwargs["contents"] = contents
            cache = client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(**cfg_kwargs),
            )
        except Exception:
            LOGGER.warning("[gemini_cache] caches.create failed model=%s", model, exc_info=True)
            return None

        name = getattr(cache, "name", None)
        if not name:
            return None
        entry = _CacheEntry(
            name=name,
            model=model,
            expire_at=now + int(ttl_seconds),
            ttl_seconds=int(ttl_seconds),
        )
        with self._lock:
            self._entries[key] = entry
        LOGGER.info(
            "[gemini_cache] created cache name=%s model=%s ttl=%ds cached_msgs=%d",
            name, model, int(ttl_seconds), len(contents or []),
        )
        return name


_DEFAULT_CONTROLLER: Optional[GeminiCacheController] = None


def get_gemini_cache_controller() -> GeminiCacheController:
    """プロセス共有の単一コントローラを返す。"""
    global _DEFAULT_CONTROLLER
    if _DEFAULT_CONTROLLER is None:
        _DEFAULT_CONTROLLER = GeminiCacheController()
    return _DEFAULT_CONTROLLER
