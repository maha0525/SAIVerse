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
    def _key(model: str, system_instruction: str) -> Tuple[str, str]:
        digest = hashlib.sha256(system_instruction.encode("utf-8")).hexdigest()
        return (model, digest)

    def ensure(
        self,
        client: Any,
        model: str,
        system_instruction: str,
        ttl_seconds: int,
        *,
        min_tokens: int = DEFAULT_MIN_TOKENS,
    ) -> Optional[str]:
        """この head に対応する生きた cache の name を返す。無ければ作成。

        作成不能 (トークン不足 / API エラー) なら None を返し、呼び出し側は
        explicit cache 無しの通常コール (system_instruction inline) にフォールバックする。
        """
        if not client or not model or not system_instruction:
            return None
        key = self._key(model, system_instruction)
        now = time.time()

        with self._lock:
            ent = self._entries.get(key)
            if ent and ent.expire_at > now + _EXPIRY_SAFETY_MARGIN:
                return ent.name

        # 最小トークンガード: 下限未満は create が失敗するので事前に弾く。
        try:
            tc = client.models.count_tokens(model=model, contents=system_instruction)
            total = getattr(tc, "total_tokens", 0) or 0
            if total < min_tokens:
                LOGGER.debug(
                    "[gemini_cache] skip create (head %d < min %d tokens) model=%s",
                    total, min_tokens, model,
                )
                return None
        except Exception:
            LOGGER.warning("[gemini_cache] count_tokens failed model=%s", model, exc_info=True)
            return None

        try:
            from google.genai import types
            digest = key[1]
            cache = client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    display_name=f"{DISPLAY_NAME_PREFIX}{digest[:24]}",
                    system_instruction=system_instruction,
                    ttl=f"{int(ttl_seconds)}s",
                ),
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
            "[gemini_cache] created cache name=%s model=%s ttl=%ds",
            name, model, int(ttl_seconds),
        )
        return name

    def get_live_entry(self, model: str, system_instruction: str) -> Optional[_CacheEntry]:
        """生きている cache entry を返す (timer/状態参照用、M3)。無ければ None。"""
        key = self._key(model, system_instruction)
        now = time.time()
        with self._lock:
            ent = self._entries.get(key)
        if ent and ent.expire_at > now:
            return ent
        return None


_DEFAULT_CONTROLLER: Optional[GeminiCacheController] = None


def get_gemini_cache_controller() -> GeminiCacheController:
    """プロセス共有の単一コントローラを返す。"""
    global _DEFAULT_CONTROLLER
    if _DEFAULT_CONTROLLER is None:
        _DEFAULT_CONTROLLER = GeminiCacheController()
    return _DEFAULT_CONTROLLER
