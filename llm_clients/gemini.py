"""Google Gemini client implementation."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import httpx
from google import genai
from google.genai import types

from .exceptions import (
    AuthenticationError,
    EmptyResponseError as LLMEmptyResponseError,
    InvalidRequestError,
    LLMError,
    LLMTimeoutError,
    ModelNotFoundError,
    PaymentError,
    RateLimitError,
    SafetyFilterError,
    ServerError,
)

try:  # pragma: no cover - optional defensive patching
    from google.genai import _api_client as _genai_api_client
except Exception:  # pragma: no cover - absence is fine
    _genai_api_client = None


# Thread-local storage for SSE error payload detected mid-stream (e.g. 504 DEADLINE_EXCEEDED).
# The SSE patch and generate_stream/call run in the same thread, so thread-local works here.
_sse_error_local: threading.local = threading.local()


# 安全性フィルターでブロックされた回の見出し (チャットのエラー札の一行目に出る)。
# 何が起きたかだけを書き、次の手立て (別モデルへ切り替えて発言の「再送」) は
# 画面側の説明文 (i18n app.page.text050) に任せる。「入力内容を変更して」とは
# 書かない — 発言は既に保存されていて、Gemini が拒んだ内容も別モデルなら
# 通ることがあるから (2026-09-24 修正)。
_RESPONSE_BLOCK_USER_MESSAGE = "Geminiの安全性フィルターにより、応答がブロックされました。"


def _prompt_block_user_message(block_reason: str) -> str:
    """プロンプト段階のブロック (prompt_feedback.block_reason) の見出し。"""
    return f"Geminiの安全性フィルターにより、応答がブロックされました（{block_reason}）。"


def _sdk_structured_parse_failure(exc: BaseException) -> Tuple[bool, Optional[str]]:
    """Detect a structured-output parse failure raised *inside* the google-genai SDK.

    With ``config.response_schema`` set to a dict/Schema, the SDK parses the
    model's text itself in ``types.GenerateContentResponse._from_response``
    (``result.parsed = json.loads(result_text)``) and only catches
    ``json.decoder.JSONDecodeError`` (verified in google-genai 1.63.0). A body
    that is valid JSON but exceeds CPython's integer-string conversion limit —
    the digit loop of docs/issues/sluice_structured_output_digit_loop.md —
    raises a bare ``ValueError`` that escapes, taking the whole response object
    with it: the caller never sees the text, so the failure cannot be diagnosed.

    This walks the traceback for that SDK frame and lifts its ``result_text``
    local (the raw model output) so it can be logged. It reads SDK internals by
    name, so it degrades instead of breaking: the first element says whether the
    failure came from that frame, the second is the raw body when it could be
    recovered and ``None`` when it could not.
    """
    tb = exc.__traceback__
    matched = False
    raw_text: Optional[str] = None
    while tb is not None:
        frame = tb.tb_frame
        module = frame.f_globals.get("__name__", "")
        if frame.f_code.co_name == "_from_response" and module.startswith("google.genai"):
            matched = True
            value = frame.f_locals.get("result_text")
            if isinstance(value, str):
                raw_text = value
        tb = tb.tb_next
    return matched, raw_text


class _AttemptCounter:
    """``for attempt in _AttemptCounter(n)`` — ``range(n)`` に「今の番号をもう一度」を足したもの。

    キャッシュを外した送り直しのように、再試行の回数として数えたくない
    やり直しのために使う (:meth:`repeat_current` を呼んでから ``continue``)。
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self._repeat = False

    def __iter__(self) -> Iterator[int]:
        attempt = 0
        while attempt < self.total:
            yield attempt
            if self._repeat:
                self._repeat = False
                continue
            attempt += 1

    def repeat_current(self) -> None:
        self._repeat = True


def _install_gemini_stream_patch() -> None:
    disable_flag = os.getenv("SAIVERSE_DISABLE_GEMINI_SSE_PATCH")
    if disable_flag and disable_flag.lower() not in {"0", "false", "off"}:
        logging.warning(
            "Skipping Gemini SSE patch because SAIVERSE_DISABLE_GEMINI_SSE_PATCH=%s",
            disable_flag,
        )
        return
    """Patch google.genai HttpResponse streaming to respect SSE framing."""

    if _genai_api_client is None:
        return

    HttpResponse = getattr(_genai_api_client, "HttpResponse", None)
    if HttpResponse is None:
        return
    if getattr(HttpResponse, "_saiverse_sse_patch", False):
        return

    def _clean_line(raw: Any) -> str:
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raw = raw.decode("utf-8", errors="ignore")
        return raw.rstrip("\r\n")

    class _SSEAccumulator:
        __slots__ = ("_buffer", "_error_buffer")

        def __init__(self) -> None:
            self._buffer: List[str] = []
            self._error_buffer: List[str] = []

        def feed(self, line: str) -> Optional[str]:
            if not line:
                if self._buffer:
                    data = "".join(self._buffer)
                    self._buffer.clear()
                    return data
                # Empty line may also delimit a bare JSON error block
                self._flush_error_buffer()
                return None
            if line.startswith("data:"):
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                self._buffer.append(value)
            elif line.startswith(":"):
                return None
            else:
                # Bare lines (not SSE data:) — accumulate as potential JSON error payload
                self._error_buffer.append(line)
            return None

        def flush(self) -> Optional[str]:
            if self._buffer:
                data = "".join(self._buffer)
                self._buffer.clear()
                return data
            return None

        def flush_remaining(self) -> None:
            """Call after stream iteration ends to detect any buffered SSE error."""
            self._flush_error_buffer()

        def _flush_error_buffer(self) -> None:
            if not self._error_buffer:
                return
            raw = "\n".join(self._error_buffer)
            self._error_buffer.clear()
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "error" in obj:
                    err = obj["error"]
                    _sse_error_local.error = {
                        "code": err.get("code"),
                        "message": err.get("message", ""),
                        "status": err.get("status", ""),
                    }
                    logging.warning(
                        "[gemini_sse] Server error detected in SSE stream: code=%s status=%s message=%s",
                        err.get("code"), err.get("status", ""), err.get("message", ""),
                    )
            except (json.JSONDecodeError, TypeError):
                pass

    original_segments = HttpResponse.segments
    original_async_segments = HttpResponse.async_segments

    def _yield_json_chunks(iterator: Iterator[str]) -> Iterator[Any]:
        _sse_logger = logging.getLogger("saiverse.llm")
        acc = _SSEAccumulator()
        for raw_line in iterator:
            cleaned = _clean_line(raw_line)
            _sse_logger.debug("Gemini SSE raw line: %s", cleaned)
            combined = acc.feed(cleaned)
            if combined:
                _sse_logger.debug("Gemini SSE payload: %s", combined)
                yield json.loads(combined)
        final = acc.flush()
        if final:
            _sse_logger.debug("Gemini SSE final payload: %s", final)
            yield json.loads(final)
        # After all lines consumed, check for any buffered error payload (e.g. 504 DEADLINE_EXCEEDED)
        acc.flush_remaining()

    async def _yield_json_chunks_async(iterator: Any) -> Any:
        _sse_logger = logging.getLogger("saiverse.llm")
        acc = _SSEAccumulator()
        async for raw_line in iterator:
            cleaned = _clean_line(raw_line)
            _sse_logger.debug("Gemini SSE raw line (async): %s", cleaned)
            combined = acc.feed(cleaned)
            if combined:
                _sse_logger.debug("Gemini SSE payload (async): %s", combined)
                yield json.loads(combined)
        final = acc.flush()
        if final:
            _sse_logger.debug("Gemini SSE final payload (async): %s", final)

    def _patched_segments(self) -> Iterator[Any]:
        if isinstance(self.response_stream, list) or self.response_stream is None:
            yield from original_segments(self)  # type: ignore[misc]
            return
        if hasattr(self.response_stream, "iter_lines"):
            iterator = self.response_stream.iter_lines()  # type: ignore[union-attr]
            yield from _yield_json_chunks(iterator)
        else:
            yield from original_segments(self)  # type: ignore[misc]

    async def _patched_async_segments(self) -> Any:
        if isinstance(self.response_stream, list) or self.response_stream is None:
            async for chunk in original_async_segments(self):  # type: ignore[misc]
                yield chunk
            return
        if hasattr(self.response_stream, "aiter_lines"):
            async for item in _yield_json_chunks_async(self.response_stream.aiter_lines()):
                yield item
            return
        if hasattr(self.response_stream, "content"):
            async def _content_line_iter():
                while True:
                    chunk = await self.response_stream.content.readline()
                    if not chunk:
                        break
                    yield chunk

            try:
                async for item in _yield_json_chunks_async(_content_line_iter()):
                    yield item
            finally:
                if hasattr(self, "_session") and self._session:
                    await self._session.close()
            return
        async for chunk in original_async_segments(self):  # type: ignore[misc]
            yield chunk

    HttpResponse.segments = _patched_segments  # type: ignore[assignment]
    HttpResponse.async_segments = _patched_async_segments  # type: ignore[assignment]
    HttpResponse._saiverse_sse_patch = True  # type: ignore[attr-defined]


_install_gemini_stream_patch()

from .gemini_utils import build_gemini_clients

from saiverse.media_utils import (
    iter_image_media,
    iter_audio_media,
    iter_video_media,
    load_image_bytes_for_llm,
    load_audio_bytes_for_llm,
    load_video_bytes_for_llm,
)
from saiverse.media_summary import (
    ensure_image_summary,
    ensure_audio_summary,
    ensure_video_summary,
)
from tools import GEMINI_TOOLS_SPEC
from saiverse.llm_router import route

from .base import EmptyResponseError, IncompleteStreamError, LLMClient, ToolSpecFormat, get_llm_logger
from saiverse.logging_config import log_timeout_event
from .utils import (
    CACHE_BOUNDARY_METADATA_KEY,
    content_to_text,
    is_truthy_flag,
    merge_reasoning_strings,
)


# Default safety settings (can be overridden via configure_parameters)
GEMINI_DEFAULT_SAFETY_SETTINGS = {
    "HARM_CATEGORY_HATE_SPEECH": "BLOCK_ONLY_HIGH",
    "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
    "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_ONLY_HIGH",
}

# Legacy constant for backward compatibility
GEMINI_SAFETY_CONFIG = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
]

# Allowed request parameters for Gemini API
GEMINI_ALLOWED_REQUEST_PARAMS = {
    "temperature", "top_p", "top_k", "max_output_tokens", "stop_sequences",
}

GEMINI_SAMPLING_REQUEST_PARAMS = {"temperature", "top_p", "top_k"}

_GEMINI_CONTENT_PAYLOAD_FIELDS = (
    "code_execution_result",
    "executable_code",
    "file_data",
    "function_call",
    "function_response",
    "inline_data",
    "text",
)

# Special parameters that need custom handling (not passed directly to API)
GEMINI_SPECIAL_PARAMS = {
    "thinking_level", "thinking_budget", "multi_turn_thinking",
    "safety_harassment", "safety_hate_speech",
    "safety_sexually_explicit", "safety_dangerous_content",
}

# Mapping from parameter names to Gemini HarmCategory names
SAFETY_PARAM_TO_CATEGORY = {
    "safety_harassment": "HARM_CATEGORY_HARASSMENT",
    "safety_hate_speech": "HARM_CATEGORY_HATE_SPEECH",
    "safety_sexually_explicit": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "safety_dangerous_content": "HARM_CATEGORY_DANGEROUS_CONTENT",
}

GROUNDING_TOOL = types.Tool(google_search=types.GoogleSearch())

# 思考の深さ (thinking_level) の語彙のうち、Gemini 3 系のどの世代でも受け付ける
# 最も浅い値。"minimal" は 3.5 Flash-Lite までの語彙で、3.7 以降では廃止されて
# おり、送ると API エラーになる (builtin_data/models/gemini-3.*.json の
# thinking_level.options と tests/test_gemini_latest_contract.py
# test_thinking_levels_match_each_model_contract)。3.1 Pro のように
# ["low", "high"] しか持たない世代もあるので、世代を跨いで安全なのは "low" だけ。
MINIMAL_THINKING_LEVEL = "low"


def _gemini_version(model: str) -> Optional[Tuple[int, int]]:
    """Return ``(major, minor)`` parsed from a ``gemini-<major>[.<minor>]-...`` name.

    ``gemini-4-argon`` -> ``(4, 0)``, ``gemini-3.8-flash`` -> ``(3, 8)``.
    Names that do not start with a version number (aliases, other vendors)
    return ``None``.
    """
    match = re.match(r"^gemini-(\d+)(?:\.(\d+))?(?:-|$)", (model or "").lower())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2) or 0))


def _is_gemini_3_or_later(model: str) -> bool:
    """Gemini 3 以降の世代か (thinking_level の世代、関数呼び出しに署名が要る世代)。

    版番号で判定するので Gemini 4 以降も拾う。旧来の判定 (``"-3-"`` を含む /
    ``gemini-3`` で始まる) も残し、これまで 3 系と判定していた名前が外れないようにする。
    """
    model_lower = (model or "").lower()
    if "-3-" in model_lower or model_lower.startswith("gemini-3"):
        return True
    version = _gemini_version(model_lower)
    return version is not None and version >= (3, 0)


def _uses_latest_generate_content_contract(model: str) -> bool:
    """Return whether *model* uses the July 2026 GenerateContent restrictions.

    Gemini 3.6 Flash and Gemini 3.5 Flash-Lite introduced the contract, and
    Google documents it as applying to future Gemini releases as well.  Model
    JSON capability flags can override this fallback for aliases or exceptions.
    """
    model_lower = (model or "").lower()
    if model_lower.startswith("gemini-3.5-flash-lite"):
        return True
    version = _gemini_version(model_lower)
    if version is None:
        return False
    return version >= (3, 6)


def _content_has_payload(content: types.Content) -> bool:
    """Whether a converted Gemini Content contains a non-empty API payload."""
    for part in content.parts or []:
        for field in _GEMINI_CONTENT_PAYLOAD_FIELDS:
            value = getattr(part, field, None)
            if isinstance(value, str):
                if value.strip():
                    return True
            elif value is not None:
                return True
    return False


def merge_tools_for_gemini(request_tools: Optional[List[types.Tool]]) -> List[types.Tool]:
    """Ensure grounding tool is included when no custom functions are declared."""
    request_tools = request_tools or GEMINI_TOOLS_SPEC
    has_functions = any(tool.function_declarations for tool in request_tools)
    if has_functions:
        return request_tools
    return [GROUNDING_TOOL] + request_tools


# 自動キャッシュの「応答後の保持秒数」の上限 (秒)。Gemini API 自体は
# cachedContents の ttl に下限も上限も定めていない (Firebase AI Logic の
# context caching doc: "There are no minimum or maximum bounds on the TTL")。
# 上限は「消し忘れたキャッシュの保存課金が際限なく伸びない」ための SAIVerse 側の歯止め。
AUTO_CACHE_KEEP_SECONDS_MAX = 3600


def clamp_auto_cache_keep_seconds(value: Any) -> int:
    """自動キャッシュの保持秒数を実際に使える範囲へ丸める。

    0 は「応答直後に手動で削除する」モードを意味する。負値や数値でない値は
    0 として扱い、上限は ``AUTO_CACHE_KEEP_SECONDS_MAX`` で切る。
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return min(seconds, AUTO_CACHE_KEEP_SECONDS_MAX)


# 継続キャッシュ (高速応答モード) の TTL (秒)。docs/intent/cache_lifecycle_control.md §3.5。
# 上限 20 分は、最後の LLM 呼び出しから anchor の TTL (Gemini 既定 1200 秒) が過ぎると
# 次の呼び出しで head が作り直され、キャッシュがもう一致しなくなるため。
# 下限 120 秒は、失効の 30 秒前 (gemini_cache._CONTINUOUS_EXPIRY_SAFETY_MARGIN) から
# 使わなくなるので、60 秒だと使える時間が 30 秒しか残らないため。
CONTINUOUS_CACHE_TTL_DEFAULT = 300
CONTINUOUS_CACHE_TTL_MIN = 120
CONTINUOUS_CACHE_TTL_MAX = 1200


def clamp_continuous_cache_ttl_seconds(value: Any) -> int:
    """継続キャッシュの TTL を 120〜1200 秒へ丸める。数値でなければ既定 300 秒。"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return CONTINUOUS_CACHE_TTL_DEFAULT
    return max(CONTINUOUS_CACHE_TTL_MIN, min(seconds, CONTINUOUS_CACHE_TTL_MAX))


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes", "on")


class GeminiClient(LLMClient):
    """Client for Google Gemini API."""

    def tool_spec_format(self) -> ToolSpecFormat:
        return "gemini"

    def __init__(
        self,
        model: str,
        config: Optional[Dict[str, Any]] | None = None,
        supports_images: bool = True,
        supports_audio: Optional[bool] = None,
        supports_video: Optional[bool] = None,
    ) -> None:
        cfg = config or {}
        # Resolve audio/video support from config (if not explicitly passed)
        if supports_audio is None:
            supports_audio = bool(cfg.get("supports_audio", False))
        if supports_video is None:
            supports_video = bool(cfg.get("supports_video", False))
        super().__init__(
            supports_images=supports_images,
            supports_audio=supports_audio,
            supports_video=supports_video,
        )
        prefer_paid = cfg.get("prefer_paid", False)
        # モデル設定の timeout (秒、正の数) があれば env GEMINI_TIMEOUT_SECONDS より優先する。
        # 長考するモデル (Gemini 4 Argon など) は公式が 15 分以上を推奨している。
        client_kwargs: Dict[str, Any] = {"prefer_paid": prefer_paid}
        timeout = cfg.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            client_kwargs["timeout_seconds"] = float(timeout)
        self.free_client, self.paid_client, self.client = build_gemini_clients(**client_kwargs)
        self.model = model

        # Phase 3 / M1: explicit cache 設定 (cache.type == "gemini_explicit" のときだけ有効)。
        # cfg はモデル設定 dict なので self.model の API 名 lookup を経ず直接持てる。
        self._cache_config: Dict[str, Any] = cfg.get("cache") or {}
        # cache.explicit_supported: false のモデルは caches.create (明示キャッシュ) を
        # 受け付けない (Gemini 4 Argon など)。省略時は True。
        self._explicit_cache_supported: bool = bool(
            self._cache_config.get("explicit_supported", True)
        )

        # Image size limit (raw bytes, before base64 encoding)
        self.max_image_bytes: Optional[int] = cfg.get("max_image_bytes")

        # Request parameters (temperature, top_p, etc.)
        self._request_params: Dict[str, Any] = {}

        latest_contract = _uses_latest_generate_content_contract(model)
        self._supports_sampling_parameters: bool = bool(
            cfg.get("supports_sampling_parameters", not latest_contract)
        )
        self._supports_model_prefill: bool = bool(
            cfg.get("supports_model_prefill", not latest_contract)
        )

        # Thinking configuration
        self._thinking_level: Optional[str] = None
        self._thinking_budget: Optional[int] = None
        self._include_thoughts: bool = cfg.get("include_thoughts", False)
        # Gemini 3 以降 (版番号で判定するので 4 以降も含む)。thinking_level の世代で、
        # 関数呼び出しパートの thought_signature を省くと API エラーになりうる。
        self._is_gemini_3_or_later: bool = _is_gemini_3_or_later(model)
        # Auto-enable for 2.5 and 3-or-later models
        if not self._include_thoughts:
            model_lower = (model or "").lower()
            self._include_thoughts = "2.5" in model_lower or self._is_gemini_3_or_later
        self._multi_turn_thinking: bool = cfg.get("multi_turn_thinking", True)

        # Safety settings overrides
        self._safety_overrides: Dict[str, str] = {}

        # Set by generate_stream when SSE stream was interrupted by a server error (e.g. 504)
        self._last_stream_error: Optional[Dict[str, Any]] = None

        # Pending cache storage info for usage tracking (set by _resolve_explicit_cache on create)
        self._pending_cache_storage: Optional[Tuple[str, int, int, Optional[float]]] = None  # (model, tokens, ttl_s, created_at)
        # Auto cache mode: generate 完了後に delete する cache name
        self._auto_cache_pending_cleanup: Optional[str] = None

    def _store_usage(self, input_tokens, output_tokens, model=None, cached_tokens=0,
                     cache_write_tokens=0, cache_ttl=""):
        super()._store_usage(input_tokens, output_tokens, model=model,
                             cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens,
                             cache_ttl=cache_ttl)
        pending = self._pending_cache_storage
        if pending and self._latest_usage:
            self._latest_usage.cache_storage_tokens = pending[1]
            self._latest_usage.cache_storage_ttl_seconds = pending[2]
            self._latest_usage.cache_storage_timestamp = pending[3]
            self._pending_cache_storage = None
        # Auto cache mode: usage 記録完了後に cache を即削除
        cleanup_name = getattr(self, "_auto_cache_pending_cleanup", None)
        if cleanup_name:
            self._auto_cache_pending_cleanup = None
            self._auto_cache_cleanup(cleanup_name)

    # ── Auto cache mode ──
    # 環境変数 SAIVERSE_GEMINI_AUTO_CACHE=1 か、グローバル設定 UI (環境タブ) で有効。
    # 全 Gemini 呼び出しで create→use→delete を自動適用し、入力を 1/10 価格にする。
    # どちらもクラス属性なので、API から書き換えると全インスタンスに即座に効く。
    _AUTO_CACHE_ENABLED: bool = os.getenv(
        "SAIVERSE_GEMINI_AUTO_CACHE", ""
    ).lower() in ("1", "true", "yes", "on")
    _AUTO_CACHE_MIN_TOKENS: int = 1024
    _AUTO_CACHE_TTL: int = 300  # 保険 TTL (delete 失敗時の orphan 上限)
    # 応答後の保持秒数。0 (既定) なら従来どおり保険 TTL で create して応答直後に手動 delete。
    # 1 以上なら TTL をこの値にして create し、手動 delete はせず Gemini の TTL 失効に任せる。
    _AUTO_CACHE_KEEP_SECONDS: int = clamp_auto_cache_keep_seconds(
        os.getenv("SAIVERSE_GEMINI_AUTO_CACHE_KEEP_SECONDS", "0")
    )
    # 継続キャッシュ (高速応答モード、docs/intent/cache_lifecycle_control.md §3.5)。
    # 自動キャッシュが ON のときだけ効く (UI では「オフ / 自動 / 継続」の三択)。効くのは
    # ユーザーの発言への返事の LLM 呼び出し (SEA が continuous_cache= を渡したもの) だけで、
    # それ以外の呼び出しは自動キャッシュと同じ動きをする。
    _CONTINUOUS_CACHE_ENABLED: bool = _env_flag("SAIVERSE_GEMINI_CONTINUOUS_CACHE")
    _CONTINUOUS_CACHE_TTL_SECONDS: int = clamp_continuous_cache_ttl_seconds(
        os.getenv("SAIVERSE_GEMINI_CONTINUOUS_CACHE_TTL_SECONDS", str(CONTINUOUS_CACHE_TTL_DEFAULT))
    )

    @classmethod
    def continuous_cache_ttl_seconds(cls) -> int:
        """継続キャッシュの TTL 秒数 (丸め済み)。"""
        return clamp_continuous_cache_ttl_seconds(cls._CONTINUOUS_CACHE_TTL_SECONDS)

    @classmethod
    def continuous_cache_active(cls) -> bool:
        """継続キャッシュが実際に効く設定か (自動キャッシュ ON が前提)。"""
        return bool(cls._AUTO_CACHE_ENABLED and cls._CONTINUOUS_CACHE_ENABLED)

    @classmethod
    def auto_cache_keep_seconds(cls) -> int:
        """現在の保持秒数 (丸め済み)。クラス属性を直接読むと未検証の値が混じるため。"""
        return clamp_auto_cache_keep_seconds(cls._AUTO_CACHE_KEEP_SECONDS)

    @classmethod
    def auto_cache_create_ttl(cls) -> int:
        """caches.create に渡す TTL 秒数。

        保持秒数が 0 のときは「delete が失敗したときの孤児キャッシュの寿命上限」
        としての保険 TTL、1 以上のときは保持秒数そのものが TTL になる。
        """
        keep = cls.auto_cache_keep_seconds()
        return keep if keep > 0 else cls._AUTO_CACHE_TTL

    def _auto_cache_wrap(
        self,
        sys_msg: str,
        contents: list,
    ) -> Tuple[Optional[str], list, Optional[str]]:
        """Auto cache mode: system + contents[:-1] を cache に焼き、tail だけ返す。

        Returns (cache_name, send_contents, cache_name_for_cleanup).
        cache_name_for_cleanup は finally で delete するために呼び出し元が保持する。
        保持秒数が 1 以上のときは手動削除をしない (TTL 失効に任せる) ので None を返す。
        """
        if not self._AUTO_CACHE_ENABLED:
            return (None, contents, None)
        if self._cache_unavailable_on_free_tier():
            return (None, contents, None)
        if not contents or len(contents) < 2:
            return (None, contents, None)

        from llm_clients.gemini_cache import (
            get_gemini_cache_controller, parse_ttl_seconds, CacheEnsureResult,
        )
        controller = get_gemini_cache_controller()
        cached_contents = contents[:-1]
        tail = contents[-1:]

        if controller._approx_chars(sys_msg, cached_contents) < self._AUTO_CACHE_MIN_TOKENS:
            return (None, contents, None)

        keep_seconds = self.auto_cache_keep_seconds()
        ttl_seconds = self.auto_cache_create_ttl()

        result = controller.ensure(
            self.client, self.model, sys_msg, ttl_seconds,
            contents=cached_contents, min_tokens=self._AUTO_CACHE_MIN_TOKENS,
        )
        if not result or not result.name:
            return (None, contents, None)

        if result.created:
            self._pending_cache_storage = (
                self.config_key or self.model,
                result.cached_tokens,
                ttl_seconds,
                result.timestamp,
            )

        logging.info(
            "[gemini][auto_cache] cache=%s prefix=%d msgs, tail=%d msgs, ttl=%ds keep=%ds",
            result.name, len(cached_contents), len(tail), ttl_seconds, keep_seconds,
        )
        # keep_seconds > 0 のときは応答後に削除せず、Gemini 側の TTL 失効に任せる。
        cleanup_name = result.name if keep_seconds <= 0 else None
        return (result.name, tail, cleanup_name)

    def _auto_cache_cleanup(self, cache_name: Optional[str]) -> None:
        """Auto cache mode: generate 完了後に cache を即 delete。"""
        if not cache_name:
            return
        from llm_clients.gemini_cache import get_gemini_cache_controller
        # NOTE: 継続キャッシュと同じ穴が残っている — 作成時に TTL の全額を計上して
        # いるのに、ここで早く消しても残りを返金していない (この経路はどのペルソナの
        # 要求かを知らない)。docs/issues/gemini_auto_cache_storage_accounting_gaps.md
        get_gemini_cache_controller().delete(self.client, cache_name)

    # ── キャッシュを使えなかった回の作り直し ──
    # cached_content を付けた要求が、そのキャッシュのせいで拒まれる (サーバー側で
    # 消えた・長い生成の途中で失効した・cachedContents が見つからない / 権限が無い・
    # 中身が不正) と、キャッシュは任意の高速化なのに返事そのものが失敗していた。
    # その回だけ、キャッシュを外して (contents 全部 + system_instruction を戻して)
    # 1 回だけ送り直す。継続キャッシュ・自動キャッシュ・モデル設定の explicit cache の
    # どれでも同じ失敗の形なので、三つとも同じ経路で扱う。

    _CACHE_ERROR_CODES = (400, 403, 404)
    _CACHE_ERROR_STATUSES = ("INVALID_ARGUMENT", "PERMISSION_DENIED", "NOT_FOUND")

    @classmethod
    def _cache_use_error_reason(cls, exc: BaseException) -> Optional[str]:
        """``exc`` が「付けたキャッシュが使えない」ことによる失敗なら理由の短い文字列。

        保守的に判定する: google-genai の ``errors.APIError`` (4xx は ``ClientError``)
        で、``code`` が 400/403/404 か ``status`` が INVALID_ARGUMENT /
        PERMISSION_DENIED / NOT_FOUND、かつメッセージがキャッシュ (cached content /
        cachedContents) に触れているときだけ。レート制限・安全性フィルター等は
        対象外で、従来どおりの扱いに任せる。
        """
        try:
            from google.genai import errors as genai_errors
        except Exception:  # pragma: no cover - SDK は必ずある
            return None
        if not isinstance(exc, genai_errors.APIError):
            return None
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", None) or "").upper()
        if code not in cls._CACHE_ERROR_CODES and status not in cls._CACHE_ERROR_STATUSES:
            return None
        text = " ".join(
            str(part) for part in (getattr(exc, "message", None), exc) if part
        ).lower()
        if "cached" not in text:
            return None
        message =str(getattr(exc, "message", None) or "").strip()
        return f"{code} {status or '-'}: {message[:160]}" if message else f"{code} {status or '-'}"

    def _drop_unusable_cache(self, cache_name: str, reason: str, held: List[str]) -> None:
        """使えなかったキャッシュを、二度と選ばれないように台帳から外す。

        - 継続キャッシュ (この要求が参照を ``held`` に持っている): 参照を 1 つ返し
          ながら retired にする (参照が 0 になったら削除、成功すれば残りを返金)。
        - 自動キャッシュ / explicit cache: 再利用の台帳 (``_entries``) から外す。
          自動キャッシュの「応答後に消す」予約がこの name なら、作り直しの要求の
          使用量の記録 (無関係な後続の要求のこともある) を待たずに、ここで消す。
        """
        from llm_clients.gemini_cache import get_gemini_cache_controller
        controller = get_gemini_cache_controller()
        if cache_name in held:
            held.remove(cache_name)
            controller.continuous_discard(cache_name, reason=reason)
            return
        controller.forget(cache_name)
        if getattr(self, "_auto_cache_pending_cleanup", None) == cache_name:
            self._auto_cache_pending_cleanup = None
            self._auto_cache_cleanup(cache_name)

    def _uncached_retry_config(
        self, cfg_kwargs: Dict[str, Any], sys_msg: str, reason: str, held: List[str],
    ) -> Dict[str, Any]:
        """キャッシュを外した送り直し用の cfg を返す (台帳からの除去とログも行う)。"""
        cache_name = cfg_kwargs.get("cached_content")
        logging.warning(
            "[gemini] cached_content=%s unusable (%s) -> retrying once without cache",
            cache_name, reason,
        )
        if cache_name:
            self._drop_unusable_cache(cache_name, reason, held)
        retry_cfg = dict(cfg_kwargs)
        retry_cfg.pop("cached_content", None)
        retry_cfg["system_instruction"] = sys_msg
        return retry_cfg

    def _apply_request_cache(
        self,
        sys_msg: str,
        contents: list,
        boundary_idx: Optional[int],
        enable_cache: bool,
        cache_ttl: Any,
        continuous_cache: Optional[Dict[str, Any]],
        held: List[str],
    ) -> Tuple[Optional[str], list, Optional[str]]:
        """この呼び出しのキャッシュを決める。``(cache_name, 送る contents, 応答後に消す name)``。

        - 自動キャッシュ ON + 継続キャッシュ ON + 返事の呼び出し (``continuous_cache``):
          継続キャッシュ。使えない回 (境目なし・最小トークン未満・無料枠・作成失敗) は
          自動キャッシュへ落ちる。
        - 自動キャッシュ ON: 自動キャッシュ (従来どおり)。
        - それ以外: モデル設定の explicit cache (B 戦略、env opt-in)。

        継続キャッシュで確保した参照の name は ``held`` に積む。呼び出し元は応答が
        終わったら :meth:`_continuous_release_held` で返す。
        """
        # 明示キャッシュ非対応のモデル (cache.explicit_supported: false) は caches.create が
        # 毎回拒否されるだけなので、三経路とも試みず Google 側の暗黙キャッシュに任せる。
        if not self._explicit_cache_supported:
            return (None, contents, None)
        if self._AUTO_CACHE_ENABLED:
            if continuous_cache:
                cc = self._continuous_cache_wrap(
                    sys_msg, contents, boundary_idx, continuous_cache, held,
                )
                if cc is not None:
                    return (cc[0], cc[1], None)
            return self._auto_cache_wrap(sys_msg, contents)
        name, send = self._resolve_explicit_cache(sys_msg, contents, enable_cache, cache_ttl)
        return (name, send, None)

    @staticmethod
    def _continuous_release_held(held: List[str]) -> None:
        """継続キャッシュの参照を返す (置き換え済みならここで削除される)。"""
        if not held:
            return
        from llm_clients.gemini_cache import get_gemini_cache_controller
        controller = get_gemini_cache_controller()
        while held:
            name = held.pop()
            try:
                controller.continuous_release(name)
            except Exception:
                logging.warning(
                    "[gemini][continuous_cache] release failed name=%s", name, exc_info=True,
                )

    def _continuous_cache_wrap(
        self,
        sys_msg: str,
        contents: list,
        boundary_idx: Optional[int],
        cc_info: Dict[str, Any],
        held: List[str],
    ) -> Optional[Tuple[str, list]]:
        """継続キャッシュ (高速応答モード、docs/intent/cache_lifecycle_control.md §3.5)。

        キャッシュの範囲は「system_instruction + contents[:B+1]」(B = 境目、履歴の中の
        最後の AI の返事)。生きたキャッシュのうち今回の並びの先頭と一致する最長のものを
        使い、キャッシュに入っていない残り (contents[k:]) だけを送る。今回の境目までの
        キャッシュはまだ無ければ裏で作り始める (返事は待たせない)。一致が無い回だけ、
        今回の境目までを同期で作ってから送る。

        使えない回は None (呼び出し元が自動キャッシュへ落とす)。
        """
        if not self.continuous_cache_active():
            return None
        if self._cache_unavailable_on_free_tier():
            return None
        if boundary_idx is None:
            logging.info(
                "[gemini][continuous_cache] no boundary (no AI reply in history) -> auto cache",
            )
            return None
        if boundary_idx < 0 or boundary_idx + 1 >= len(contents):
            logging.info(
                "[gemini][continuous_cache] nothing after the boundary (boundary=%d contents=%d) -> auto cache",
                boundary_idx, len(contents),
            )
            return None

        from llm_clients.gemini_cache import get_gemini_cache_controller, prefix_digests

        controller = get_gemini_cache_controller()
        prefix = contents[: boundary_idx + 1]
        if controller._approx_chars(sys_msg, prefix) < self._AUTO_CACHE_MIN_TOKENS:
            logging.info(
                "[gemini][continuous_cache] prefix below min tokens (prefix=%d msgs) -> auto cache",
                len(prefix),
            )
            return None

        digests = prefix_digests(sys_msg, prefix)
        chain = str(cc_info.get("persona_id") or "")
        ttl_seconds = self.continuous_cache_ttl_seconds()

        acquired = controller.continuous_acquire(self.model, chain, digests)
        if acquired is not None:
            name, k = acquired
            held.append(name)
            send = contents[k:]
            logging.info(
                "[gemini][continuous_cache] use=%s prefix=%d sent=%d msgs boundary=%d",
                name, k, len(send), len(prefix),
            )
            if k != len(prefix):
                self._continuous_start_background(
                    controller, sys_msg, prefix, digests, chain, ttl_seconds, cc_info,
                )
            return (name, send)

        reason = (
            "no cache yet for this conversation"
            if controller.continuous_chain_size(self.model, chain) == 0
            else "no live cache matches the current prefix"
        )
        logging.info(
            "[gemini][continuous_cache] no match -> sync create (%s) prefix=%d msgs",
            reason, len(prefix),
        )
        account_model = self.config_key or self.model
        persona_id = cc_info.get("persona_id")
        building_id = cc_info.get("building_id")
        result = controller.continuous_create_sync(
            self.client, self.model, chain, sys_msg, prefix, digests, ttl_seconds,
            min_tokens=self._AUTO_CACHE_MIN_TOKENS,
            account_model=account_model, persona_id=persona_id, building_id=building_id,
        )
        if not result or not result.name:
            logging.info("[gemini][continuous_cache] sync create failed -> auto cache")
            return None
        held.append(result.name)
        if result.created:
            # 作った瞬間に、この要求のペルソナ・建物で計上する (裏での作成と同じ)。
            # クライアント全体で一つの _pending_cache_storage に置くと、生成が失敗した
            # 回に漏れ、要求が重なると別の要求の記録にくっつくため。
            self._record_continuous_cache_storage(
                account_model, result.cached_tokens, result.ttl_seconds,
                timestamp=result.timestamp,
                persona_id=persona_id, building_id=building_id,
            )
        send = contents[len(prefix):]
        logging.info(
            "[gemini][continuous_cache] use=%s prefix=%d sent=%d msgs boundary=%d (sync)",
            result.name, len(prefix), len(send), len(prefix),
        )
        return (result.name, send)

    def _continuous_start_background(
        self,
        controller: Any,
        sys_msg: str,
        prefix: list,
        digests: List[str],
        chain: str,
        ttl_seconds: int,
        cc_info: Dict[str, Any],
    ) -> None:
        """今回の境目までの継続キャッシュを裏で作り始める (既に有るか作成中なら何もしない)。"""
        reservation = controller.continuous_reserve(self.model, chain, digests[-1])
        if reservation is None:
            return
        client = self.client
        model = self.model
        account_model = self.config_key or self.model
        persona_id = cc_info.get("persona_id")
        building_id = cc_info.get("building_id")
        min_tokens = self._AUTO_CACHE_MIN_TOKENS
        logging.info(
            "[gemini][continuous_cache] create(bg) started prefix=%d msgs ttl=%ds",
            len(prefix), ttl_seconds,
        )

        def _run() -> None:
            try:
                result = controller.continuous_create_reserved(
                    client, model, chain, sys_msg, prefix, digests, ttl_seconds,
                    reservation, min_tokens=min_tokens,
                    account_model=account_model, persona_id=persona_id,
                    building_id=building_id,
                )
                if result and result.created:
                    # 裏での作成はどの呼び出しの usage レコードにも相乗りできない
                    # (完了が応答の前か後か決まらない) ので、同じ区分で単独に計上する。
                    self._record_continuous_cache_storage(
                        account_model, result.cached_tokens, result.ttl_seconds,
                        timestamp=result.timestamp,
                        persona_id=persona_id, building_id=building_id,
                    )
            except Exception:
                logging.warning(
                    "[gemini][continuous_cache] background create crashed", exc_info=True,
                )

        controller.spawn_background(_run)

    @staticmethod
    def _record_continuous_cache_storage(
        model_id: str,
        cached_tokens: int,
        ttl_seconds: int,
        *,
        timestamp: Optional[float] = None,
        persona_id: Optional[str],
        building_id: Optional[str],
    ) -> None:
        """作った継続キャッシュ (同期・裏とも) の保存料金を、作った時点で計上する。

        区分は cache_storage。TTL 前に消したときの残りの返金は、削除の経路
        (``GeminiCacheController._delete_continuous``) が同じ値で記録する。
        """
        if not cached_tokens:
            return
        from datetime import datetime
        from saiverse.usage_tracker import get_usage_tracker
        get_usage_tracker().record_cache_storage(
            model_id=model_id,
            cached_tokens=cached_tokens,
            ttl_seconds=ttl_seconds,
            timestamp=datetime.fromtimestamp(timestamp) if timestamp is not None else None,
            persona_id=persona_id,
            building_id=building_id,
        )

    def _cache_unavailable_on_free_tier(self) -> bool:
        """いま使っているクライアントが無料枠 (free_client) かを判定する。

        無料枠キーは explicit cache のストレージ上限が 0
        (``TotalCachedContentStorageTokensPerModelFreeTier limit=0``) で、
        ``caches.create`` が必ず 429 RESOURCE_EXHAUSTED になる。しかも SDK の
        HttpRetryOptions が 429 を再試行対象に含むため、毎コール create が 5 回
        リトライしてから失敗する (無駄な往復 + backoff)。そもそも無料枠は課金が
        無くキャッシュの節約効果も無いので、free_client 使用時は create を試みない。

        free / paid はモデル API 名 (free/paid 共通) では区別できないため、
        クライアントオブジェクトの identity で厳密に判定する。
        """
        return self.free_client is not None and self.client is self.free_client

    def consume_stream_error(self) -> Optional[Dict[str, Any]]:
        """Return and clear the last SSE stream error (e.g. 504 DEADLINE_EXCEEDED).

        Returns a dict with keys 'code', 'message', 'status', or None if no error occurred.
        """
        err = self._last_stream_error
        self._last_stream_error = None
        return err

    def _resolve_explicit_cache(
        self,
        sys_msg: str,
        contents: list,
        enable_cache: bool,
        cache_ttl: Any,
    ) -> Tuple[Optional[str], list]:
        """explicit cache (gemini_explicit) を解決し ``(cache_name, 送信すべき contents)``
        を返す (Phase 3 / B 戦略)。

        キャッシュ対象 = **system_instruction + contents の「最新入力を除く全部」**
        (= contents[:-1])。リクエストは最新の1メッセージ (contents[-1:]) だけ送る。
        毎ターン prefix が伸びるので毎回 create (Gemini の create は無料)、古い cache は
        TTL / orphan cleanup (M2) で消える。

        張れない (対象外モデル / env 未設定 / トークン不足 / API エラー) 場合は
        ``(None, contents)`` を返し、system_instruction inline の通常コールにフォールバック。
        """
        if not enable_cache:
            return (None, contents)
        if (self._cache_config or {}).get("type") != "gemini_explicit":
            return (None, contents)
        # M1 安全ゲート: 実験段階 (orphan cleanup / pulse 終了 delete 未実装) のため、
        # env で明示 opt-in したときだけ有効化する。M2-M4 完了後にこのゲートは外す。
        if os.getenv("SAIVERSE_GEMINI_EXPLICIT_CACHE", "").lower() not in ("1", "true", "yes", "on"):
            return (None, contents)
        # 無料枠キーは cache storage 上限 0 で create が必ず 429 になるためスキップ。
        if self._cache_unavailable_on_free_tier():
            return (None, contents)
        # 最新入力以外にキャッシュできる中身が無い (履歴ゼロ) ならスキップ。
        if not contents or len(contents) < 2:
            return (None, contents)

        cached_contents = contents[:-1]
        tail = contents[-1:]
        try:
            from llm_clients.gemini_cache import get_gemini_cache_controller, parse_ttl_seconds
            min_tokens = int((self._cache_config or {}).get("min_tokens", 1024))
            ttl_seconds = parse_ttl_seconds(cache_ttl, default=300)
            result = get_gemini_cache_controller().ensure(
                self.client, self.model, sys_msg, ttl_seconds,
                contents=cached_contents, min_tokens=min_tokens,
            )
        except Exception:
            logging.warning("[gemini] explicit cache resolve failed; inline fallback", exc_info=True)
            return (None, contents)

        name = result.name if result else None
        if result and result.created:
            self._pending_cache_storage = (
                self.config_key or self.model,
                result.cached_tokens,
                result.ttl_seconds,
                result.timestamp,
            )
        if name:
            # backend.log に出す ([gemini_cache] created と同じ場所に揃える。
            # get_llm_logger は llm_io.log 行きで created と別ファイルになり紛らわしいため)。
            def _txt_chars(cs: list) -> int:
                n = 0
                for c in cs:
                    for p in (getattr(c, "parts", None) or []):
                        n += len(getattr(p, "text", "") or "")
                return n

            def _media_count(cs: list) -> int:
                n = 0
                for c in cs:
                    for p in (getattr(c, "parts", None) or []):
                        if getattr(p, "inline_data", None) is not None:
                            n += 1
                return n

            # 計測ログ: 「キャッシュに入れる prefix」と「送る tail」のサイズを直接出す。
            # response の cached_content_token_count / prompt_token_count と突き合わせれば、
            # uncached の正体 (tail が大きい=正常 / cache が prefix を取りこぼし=partial) が分かる。
            logging.info(
                "[gemini] cache split: prefix=%d msgs (~%d text chars, %d media) | "
                "tail=%d msgs (~%d text chars, %d media) | cache=%s ttl=%s",
                len(cached_contents), _txt_chars(cached_contents), _media_count(cached_contents),
                len(tail), _txt_chars(tail), _media_count(tail), name, cache_ttl,
            )
            return (name, tail)
        return (None, contents)

    def _build_thinking_config(self) -> Optional[types.ThinkingConfig]:
        """Build ThinkingConfig from current settings."""
        if not self._include_thoughts and not self._thinking_level and self._thinking_budget is None:
            return None

        # include_thoughts alone (without budget or level) causes silent failures
        # on some models (e.g., Flash Lite ignores structured output).
        # Only send thinking config when a control parameter is present.
        if not self._thinking_level and self._thinking_budget is None:
            logging.warning(
                "[gemini] include_thoughts is True but no thinking_budget or thinking_level set "
                "for model %s. Skipping thinking config to avoid API issues. "
                "Add thinking_budget or thinking_level to the model config file.",
                self.model,
            )
            return None

        kwargs: Dict[str, Any] = {}
        kwargs["include_thoughts"] = True
        if self._thinking_level:
            kwargs["thinking_level"] = self._thinking_level
        if self._thinking_budget is not None:
            kwargs["thinking_budget"] = self._thinking_budget

        try:
            return types.ThinkingConfig(**kwargs)
        except Exception as e:
            logging.warning("Failed to create ThinkingConfig: %s", e)
            return None

    def _build_safety_settings(self) -> List[types.SafetySetting]:
        """Build safety settings from defaults and overrides."""
        # Start with defaults
        settings = dict(GEMINI_DEFAULT_SAFETY_SETTINGS)

        # Apply overrides from configure_parameters
        for param_key, category in SAFETY_PARAM_TO_CATEGORY.items():
            if param_key in self._safety_overrides:
                settings[category] = self._safety_overrides[param_key]

        result = []
        for category_name, threshold_name in settings.items():
            category = getattr(types.HarmCategory, category_name, None)
            threshold = getattr(types.HarmBlockThreshold, threshold_name, None)
            if category and threshold:
                result.append(types.SafetySetting(category=category, threshold=threshold))
        return result

    def prefer_minimal_reasoning(self) -> None:
        """一撃の判定として使われるので、思考を最小にする (契約は LLMClient 側)。

        世代ごとに「最小にする」の形が違うので、ここで分ける:

        - **Gemini 3 以降** (thinking_level の世代、4 Argon も含む): :data:`MINIMAL_THINKING_LEVEL`
          を指定する。"minimal" は 3.7 以降で廃止されていて送るとエラーになるため、
          世代を跨いで安全な "low" を使う。
        - **Gemini 2.5 の Flash / Flash-Lite** (thinking_budget の世代): 予算 0 =
          思考なし。この形はモデル設定の "off" (configure_parameters が 0 に直す)
          と同じもので、それぞれのモデル設定ファイルが選択肢として宣言している。
        - **Gemini 2.5 Pro**: 思考を切れない (モデル設定の選択肢に "off" が無く、
          予算の下限は 128)。何もしない。
        - **それ以外の世代**: 思考のつまみ自体が無いので何もしない。

        既に thinking_level / thinking_budget が決まっている場合は、それが
        モデル設定の既定であれ画面からの上書きであれ尊重して何もしない。
        """
        if self._thinking_level or self._thinking_budget is not None:
            logging.debug(
                "[gemini] prefer_minimal_reasoning: model=%s already has an explicit "
                "thinking setting (level=%s budget=%s); leaving it alone",
                self.model, self._thinking_level, self._thinking_budget,
            )
            return

        if self._is_gemini_3_or_later:
            self._thinking_level = MINIMAL_THINKING_LEVEL
            logging.debug(
                "[gemini] prefer_minimal_reasoning: model=%s thinking_level=%s",
                self.model, self._thinking_level,
            )
            return

        model_lower = (self.model or "").lower()
        if "2.5" in model_lower and "pro" not in model_lower:
            self._thinking_budget = 0
            logging.debug(
                "[gemini] prefer_minimal_reasoning: model=%s thinking_budget=0", self.model,
            )
            return

        logging.debug(
            "[gemini] prefer_minimal_reasoning: model=%s has no thinking control that "
            "is safe to minimise; leaving the request as it is", self.model,
        )

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        """Configure model parameters from UI settings."""
        if not isinstance(parameters, dict):
            return
        for key, value in parameters.items():
            if key in GEMINI_ALLOWED_REQUEST_PARAMS:
                if key in GEMINI_SAMPLING_REQUEST_PARAMS and not self._supports_sampling_parameters:
                    self._request_params.pop(key, None)
                    if value is not None:
                        logging.debug(
                            "[gemini] Ignoring deprecated sampling parameter %s for model=%s",
                            key,
                            self.model,
                        )
                    continue
                if value is None:
                    self._request_params.pop(key, None)
                else:
                    self._request_params[key] = value
            elif key == "thinking_level":
                self._thinking_level = value if value else None
            elif key == "thinking_budget":
                if not value or value == "default":
                    self._thinking_budget = None
                elif value == "off":
                    self._thinking_budget = 0
                else:
                    try:
                        self._thinking_budget = int(value)
                    except (ValueError, TypeError):
                        logging.warning("Invalid thinking_budget value: %s", value)
                        self._thinking_budget = None
            elif key in ("multi_turn_thinking", "thought_signature_echo"):
                # 3.5+ は "multi_turn_thinking" (全ターン推論の引き継ぎ。GenerateContent
                # では 3.5 Flash から), 3.5 未満は "thought_signature_echo"
                # (関数呼び出し連続性のための署名 echo) と UI 上の名前が異なるが,
                # SAIVerse 側の制御対象はどちらも thought_signature の echo 可否のみ。
                # 詳細は docs/intent/thought_signature_persistence.md
                self._multi_turn_thinking = value != "off"
            elif key in SAFETY_PARAM_TO_CATEGORY:
                if value:
                    self._safety_overrides[key] = value
                else:
                    self._safety_overrides.pop(key, None)

    @staticmethod
    def _schema_from_json(js: Optional[Dict[str, Any]]) -> Optional[types.Schema]:
        if js is None:
            return None

        def _to_schema(node: Any) -> types.Schema:
            if not isinstance(node, dict):
                return types.Schema(type=types.Type.OBJECT)

            kwargs: Dict[str, Any] = {}
            value_type = node.get("type")
            if isinstance(value_type, list):
                if len(value_type) == 1:
                    value_type = value_type[0]
                else:
                    kwargs["any_of"] = [_to_schema({**node, "type": t}) for t in value_type]
                    value_type = None
            if isinstance(value_type, str):
                kwargs["type"] = {
                    "string": types.Type.STRING,
                    "number": types.Type.NUMBER,
                    "integer": types.Type.INTEGER,
                    "boolean": types.Type.BOOLEAN,
                    "array": types.Type.ARRAY,
                    "object": types.Type.OBJECT,
                    "null": types.Type.NULL,
                }.get(value_type, types.Type.TYPE_UNSPECIFIED)

            if "description" in node:
                kwargs["description"] = node["description"]
            if "enum" in node and isinstance(node["enum"], list):
                kwargs["enum"] = node["enum"]
            if "const" in node:
                kwargs["enum"] = [node["const"]]

            has_props = False
            if "properties" in node and isinstance(node["properties"], dict):
                props = {k: _to_schema(v) for k, v in node["properties"].items()}
                if props:
                    kwargs["properties"] = props
                    kwargs["property_ordering"] = list(node["properties"].keys())
                    has_props = True
            # Gemini rejects OBJECT type with empty/missing properties
            if not has_props and kwargs.get("type") == types.Type.OBJECT:
                kwargs["type"] = types.Type.STRING
            if "additionalProperties" in node:
                ap = node["additionalProperties"]
                if isinstance(ap, dict):
                    kwargs["additional_properties"] = _to_schema(ap)
                elif isinstance(ap, bool):
                    kwargs["additional_properties"] = ap
            if "required" in node and isinstance(node["required"], list):
                kwargs["required"] = node["required"]

            if "items" in node:
                kwargs["items"] = _to_schema(node["items"])

            if "anyOf" in node and isinstance(node["anyOf"], list):
                kwargs["any_of"] = [_to_schema(sub) for sub in node["anyOf"]]

            if "oneOf" in node and isinstance(node["oneOf"], list):
                kwargs["any_of"] = [_to_schema(sub) for sub in node["oneOf"]]

            if "allOf" in node and isinstance(node["allOf"], list):
                kwargs["all_of"] = [_to_schema(sub) for sub in node["allOf"]]

            return types.Schema(**kwargs)

        return _to_schema(js)

    @staticmethod
    def _to_response_json_schema(node: Any) -> Any:
        """Rewrite a raw JSON Schema into what ``response_json_schema`` accepts.

        The raw path supports ``enum`` only for strings and numbers and has no
        ``const`` at all (google-genai ``GenerateContentConfig.response_json_schema``
        docstring). So a string/number ``const`` becomes a one-value ``enum``;
        a ``const`` or ``enum`` holding booleans / null is dropped and written
        into ``description`` so the model still sees the constraint.
        Keys inside ``properties`` are property names, not keywords, and are
        never rewritten.
        """
        if isinstance(node, list):
            return [GeminiClient._to_response_json_schema(item) for item in node]
        if not isinstance(node, dict):
            return node

        def representable(value: Any) -> bool:
            return isinstance(value, str) or (
                isinstance(value, (int, float)) and not isinstance(value, bool)
            )

        out: Dict[str, Any] = {}
        notes: List[str] = []
        for key, value in node.items():
            if key in ("properties", "patternProperties", "$defs", "definitions") and isinstance(value, dict):
                out[key] = {
                    name: GeminiClient._to_response_json_schema(sub)
                    for name, sub in value.items()
                }
            elif key in ("items", "prefixItems", "anyOf", "oneOf", "allOf", "additionalProperties"):
                out[key] = GeminiClient._to_response_json_schema(value)
            elif key == "enum" and isinstance(value, list):
                if all(representable(v) for v in value):
                    out[key] = value
                else:
                    notes.append(
                        "Allowed values: "
                        + ", ".join(json.dumps(v, ensure_ascii=False) for v in value)
                        + "."
                    )
            elif key == "const":
                if representable(value) and "enum" not in node:
                    out["enum"] = [value]
                else:
                    notes.append(f"Must be {json.dumps(value, ensure_ascii=False)}.")
            else:
                out[key] = value
        if notes:
            base = out.get("description")
            out["description"] = " ".join(([base] if isinstance(base, str) and base else []) + notes)
        return out

    @staticmethod
    def _requires_json_schema(node: Any) -> bool:
        if isinstance(node, dict):
            # types.Schema.enum is list[str]; a non-string enum/const would
            # make _schema_from_json raise, so send the raw JSON Schema instead
            # (keeps the constraint rather than dropping it).
            enum_val = node.get("enum")
            if isinstance(enum_val, list) and any(not isinstance(v, str) for v in enum_val):
                return True
            if "const" in node and not isinstance(node["const"], str):
                return True
            if "additionalProperties" in node:
                ap_val = node.get("additionalProperties")
                if ap_val not in (None, False):
                    return True
            for key in ("properties", "patternProperties"):
                props = node.get(key)
                if isinstance(props, dict) and any(
                    GeminiClient._requires_json_schema(value) for value in props.values()
                ):
                    return True
            for key in ("items", ):
                child = node.get(key)
                if child and GeminiClient._requires_json_schema(child):
                    return True
            for key in ("anyOf", "oneOf", "allOf"):
                child_list = node.get(key)
                if isinstance(child_list, list) and any(GeminiClient._requires_json_schema(item) for item in child_list):
                    return True
        elif isinstance(node, list):
            return any(GeminiClient._requires_json_schema(item) for item in node)
        return False

    @staticmethod
    def _is_rate_limit_error(err: Exception) -> bool:
        msg = str(err).lower()
        return "rate" in msg or "429" in msg or "quota" in msg

    @staticmethod
    def _is_timeout_error(err: Exception) -> bool:
        """Check if the error is a timeout that should be retried."""
        return isinstance(err, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout))

    @staticmethod
    def _is_server_error(err: Exception) -> bool:
        """Check if the error is a server error (5xx)."""
        msg = str(err).lower()
        return (
            "500" in msg or "502" in msg or "503" in msg or "504" in msg
            or "internal" in msg or "unavailable" in msg or "overload" in msg
        )

    @staticmethod
    def _is_authentication_error(err: Exception) -> bool:
        """Check if the error is an authentication error."""
        msg = str(err).lower()
        return "401" in msg or "403" in msg or "invalid" in msg and "key" in msg or "authentication" in msg

    @staticmethod
    def _is_model_not_found_error(err: Exception) -> bool:
        """Check if the error is a model-not-found error (404)."""
        msg = str(err).lower()
        return "404" in msg and ("not found" in msg or "not_found" in msg)

    @staticmethod
    def _extract_retry_delay(err: Exception) -> Optional[float]:
        """Extract retryDelay from a Gemini API 429 error response.

        Returns seconds to wait, or None if not parseable.
        """
        from google.genai.errors import APIError as GenaiAPIError
        if not isinstance(err, GenaiAPIError):
            return None
        details = getattr(err, "details", None)
        if not isinstance(details, dict):
            return None
        error_block = details.get("error", details)
        for detail in error_block.get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                delay_str = detail.get("retryDelay", "")
                if isinstance(delay_str, str) and delay_str.endswith("s"):
                    try:
                        return float(delay_str[:-1])
                    except ValueError:
                        pass
        return None

    @staticmethod
    def _is_payment_error(err: Exception) -> bool:
        """Check if the error is a payment/billing error (402), NOT a rate limit (429).

        Gemini 429 RESOURCE_EXHAUSTED messages include "billing" in the text,
        which would false-positive here. Use the HTTP status code to distinguish.
        """
        from google.genai.errors import APIError as GenaiAPIError
        if isinstance(err, GenaiAPIError) and err.code == 429:
            return False
        msg = str(err).lower()
        return (
            "402" in msg
            or "payment required" in msg
            or "spend limit" in msg
            or "billing" in msg
            or "insufficient_quota" in msg
        )

    def _convert_to_llm_error(self, err: Exception, context: str = "API call") -> LLMError:
        """Convert a generic exception to an appropriate LLMError subclass."""
        if self._is_payment_error(err):
            return PaymentError(f"Gemini {context} failed: payment required", err)
        elif self._is_rate_limit_error(err):
            return RateLimitError(f"Gemini {context} failed: rate limit exceeded", err)
        elif self._is_timeout_error(err):
            return LLMTimeoutError(f"Gemini {context} failed: timeout", err)
        elif self._is_server_error(err):
            return ServerError(f"Gemini {context} failed: server error", err)
        elif self._is_model_not_found_error(err):
            return ModelNotFoundError(
                f"Gemini {context} failed: model not found",
                err,
                user_message=f"指定されたモデルが見つかりません: {self.model}",
            )
        elif self._is_authentication_error(err):
            return AuthenticationError(f"Gemini {context} failed: authentication error", err)
        else:
            return LLMError(f"Gemini {context} failed: {err}", err)

    def _convert_messages(
        self,
        msgs: List[Dict[str, str] | types.Content],
    ) -> Tuple[str, List[types.Content]]:
        sys_msg, contents, _boundary = self._convert_messages_with_boundary(msgs)
        return sys_msg, contents

    def _convert_messages_with_boundary(
        self,
        msgs: List[Dict[str, str] | types.Content],
    ) -> Tuple[str, List[types.Content], Optional[int]]:
        """``_convert_messages`` と同じ変換に、継続キャッシュの境目の位置を添えて返す。

        境目は SEA が履歴の中の最後の AI の返事に付けた内部印
        (``metadata[CACHE_BOUNDARY_METADATA_KEY]``)。変換は system を抜き出し、
        中身の無い tool_calls を落とすので、メッセージの番号と contents の番号は
        一致しない — 印の付いたメッセージが**実際に落ちた contents の番号**を返す
        (印が無い・そのメッセージが contents を作らなかったなら None)。印そのものは
        metadata にあるだけで、Content には載らない。
        """
        system_lines: List[str] = []
        # 境目の印が付いたメッセージの番号 (複数あれば最後のもの) と、各メッセージの
        # 変換を始めた時点の contents の長さ。
        boundary_msg_idx: Optional[int] = None
        content_starts: List[int] = []
        contents: List[types.Content] = []
        from .utils import attachment_limit_is_off, parse_attachment_limit
        # リアルタイム情報と自動想起を残すモデルの列では上限を使わない
        # (llm_clients/utils.ATTACHMENT_LIMIT_OFF_KEY)。
        max_image_embeds = (
            None if attachment_limit_is_off(msgs) else parse_attachment_limit("GEMINI")
        )
        logging.debug(
            "[gemini] attachment limit=%s",
            "∞" if max_image_embeds is None else max_image_embeds,
        )

        attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
        audio_cache: Dict[int, List[Dict[str, Any]]] = {}
        video_cache: Dict[int, List[Dict[str, Any]]] = {}
        # Track messages that are exempt from attachment limits (visual context)
        exempt_message_indices: Set[int] = set()
        # Track messages where summary generation should be skipped per media type
        # (e.g. summary generation requests themselves, to prevent infinite recursion)
        skip_summary_indices: Set[int] = set()
        skip_audio_summary_indices: Set[int] = set()
        skip_video_summary_indices: Set[int] = set()
        for idx, message in enumerate(msgs):
            if not isinstance(message, dict):
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get(CACHE_BOUNDARY_METADATA_KEY):
                boundary_msg_idx = idx
            # Check for visual context marker - these are exempt from limits
            if metadata.get("__visual_context__"):
                exempt_message_indices.add(idx)
            # Per-media skip flags (set by media_summary module to avoid recursion)
            if metadata.get("__skip_image_summary__"):
                skip_summary_indices.add(idx)
            if metadata.get("__skip_audio_summary__"):
                skip_audio_summary_indices.add(idx)
            if metadata.get("__skip_video_summary__"):
                skip_video_summary_indices.add(idx)
            # 画像は画像非対応のモデルでも集める。非対応なら下の per-message の
            # 分岐が説明の文に置き換える (2026-10-14 まで、ここで絞っていたため
            # その分岐に届かず、画像が説明も無しに消えていた)。
            media_items = iter_image_media(metadata)
            if media_items:
                attachment_cache[idx] = media_items
            # Audio / video are always cached; per-message loop decides whether to
            # send inline_data (supports_*=True) or inject summary text (False).
            audio_items = iter_audio_media(metadata)
            if audio_items:
                audio_cache[idx] = audio_items
            video_items = iter_video_media(metadata)
            if video_items:
                video_cache[idx] = video_items
        allowed_attachment_keys: Optional[Set[Tuple[int, int]]] = None
        if max_image_embeds is not None and attachment_cache:
            ordered: List[Tuple[int, int]] = []
            for msg_idx in sorted(attachment_cache.keys(), reverse=True):
                # Skip exempt (visual context) messages when counting towards limit
                if msg_idx in exempt_message_indices:
                    continue
                items = attachment_cache[msg_idx]
                for att_idx in range(len(items)):
                    ordered.append((msg_idx, att_idx))
            if max_image_embeds == 0:
                allowed_attachment_keys = set()
            else:
                allowed_attachment_keys = set(ordered[:max_image_embeds])
            # Always allow visual context attachments (exempt from limit)
            for msg_idx in exempt_message_indices:
                if msg_idx in attachment_cache:
                    for att_idx in range(len(attachment_cache[msg_idx])):
                        allowed_attachment_keys.add((msg_idx, att_idx))
                    logging.debug(
                        "[gemini] visual context at idx=%d exempted from attachment limit (%d images)",
                        msg_idx,
                        len(attachment_cache[msg_idx]),
                    )
        elif max_image_embeds is not None:
            allowed_attachment_keys = set()

        for idx, message in enumerate(msgs):
            content_starts.append(len(contents))
            if isinstance(message, types.Content):
                contents.append(message)
                continue

            role = message.get("role", "")
            if role == "system":
                system_lines.append(message.get("content", "") or "")
                continue

            # Handle assistant messages with tool_calls (function calling protocol)
            if "tool_calls" in message:
                parts: List[types.Part] = []
                # Include text content if present (for "both" type responses)
                msg_text = content_to_text(message.get("content", "")) or ""
                if msg_text:
                    parts.append(types.Part(text=msg_text))
                for tc in message["tool_calls"]:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    fn_name = fn.get("name", "")
                    fn_args_raw = fn.get("arguments", "{}")
                    fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                    fn_id = tc.get("id") if isinstance(tc, dict) else None
                    fc_part = types.Part(
                        function_call=types.FunctionCall(id=fn_id or None, name=fn_name, args=fn_args)
                    )
                    if self._multi_turn_thinking or self._is_gemini_3_or_later:
                        _ts = tc.get("thought_signature") if isinstance(tc, dict) else None
                        if _ts:
                            fc_part.thought_signature = _ts
                    parts.append(fc_part)
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue

            # Handle tool result messages (function response)
            if role == "tool":
                fn_name = message.get("name", "")
                fn_id = message.get("tool_call_id")
                fn_content = message.get("content", "")
                # Wrap content as a dict for FunctionResponse.response
                try:
                    response_data = json.loads(fn_content) if isinstance(fn_content, str) else fn_content
                except (json.JSONDecodeError, TypeError):
                    response_data = {"result": fn_content}
                if not isinstance(response_data, dict):
                    response_data = {"result": response_data}
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(
                        function_response=types.FunctionResponse(
                            id=fn_id or None,
                            name=fn_name,
                            response=response_data,
                        )
                    )]
                ))
                continue

            text = content_to_text(message.get("content", "")) or ""
            text_content = text
            g_role = "user" if role == "user" else "model"
            attachments = attachment_cache.get(idx, [])
            audio_attachments = audio_cache.get(idx, [])
            video_attachments = video_cache.get(idx, [])
            media_parts: List[types.Part] = []

            # === Image processing ===
            if attachments and self.supports_images:
                selected_attachments: List[Dict[str, Any]] = []
                skipped_attachments: List[Dict[str, Any]] = []
                attachment_records: List[Tuple[int, Dict[str, Any], Optional[str]]] = []
                skip_summary = idx in skip_summary_indices
                for att_idx, attachment in enumerate(attachments):
                    summary_text = (
                        None if skip_summary
                        else ensure_image_summary(attachment["path"], attachment["mime_type"])
                    )
                    key = (idx, att_idx)
                    if allowed_attachment_keys is not None and key not in allowed_attachment_keys:
                        attachment_records.append((att_idx, attachment, summary_text))
                        skipped_attachments.append(attachment)
                        continue
                    attachment_records.append((att_idx, attachment, summary_text))
                    selected_attachments.append(attachment)
                logging.debug(
                    "[gemini] embedding %d image attachment(s) for role=%s (skipped=%d)",
                    len(selected_attachments),
                    role,
                    len(skipped_attachments),
                )
                if skipped_attachments:
                    for att_idx, attachment, summary_text in attachment_records:
                        if attachment not in skipped_attachments:
                            continue
                        summary = summary_text or "(要約を取得できませんでした)"
                        note = f"[画像参照のみ: {attachment['uri']}] {summary}"
                        text_content = f"{text_content}\n{note}" if text_content else note
                for attachment in selected_attachments:
                    data, effective_mime = load_image_bytes_for_llm(
                        attachment["path"], attachment["mime_type"],
                        max_bytes=self.max_image_bytes,
                    )
                    if not data or not effective_mime:
                        logging.warning(
                            "Failed to load image for Gemini payload: %s", attachment["uri"]
                        )
                        # 読めなかった画像も黙って捨てず、説明の文で届ける
                        # (Anthropic・OpenAI 互換・xAI と同じ扱い)
                        summary = next(
                            (s for _i, att, s in attachment_records if att is attachment),
                            None,
                        ) or "(要約を取得できませんでした)"
                        note = f"[画像: {attachment['uri']}] {summary}"
                        text_content = f"{text_content}\n{note}" if text_content else note
                        continue
                    logging.debug(
                        "[gemini] image att=%s orig_mime=%s effective_mime=%s "
                        "raw_bytes=%d magic=%s",
                        attachment.get("uri"),
                        attachment.get("mime_type"),
                        effective_mime,
                        len(data),
                        data[:8].hex(),
                    )
                    media_parts.append(types.Part.from_bytes(data=data, mime_type=effective_mime))
            elif attachments:
                # supports_images=False: inject image summaries as text only
                logging.debug(
                    "[gemini] image attachments present but not embedded (supports_images=%s)",
                    self.supports_images,
                )
                for attachment in attachments:
                    if idx in skip_summary_indices:
                        summary = None
                    else:
                        summary = ensure_image_summary(attachment["path"], attachment["mime_type"])
                    summary_note = summary or "(要約を取得できませんでした)"
                    note = f"[画像: {attachment['uri']}] {summary_note}"
                    text_content = f"{text_content}\n{note}" if text_content else note

            # === Audio processing ===
            if audio_attachments and self.supports_audio:
                logging.debug(
                    "[gemini] embedding %d audio attachment(s) for role=%s",
                    len(audio_attachments), role,
                )
                for attachment in audio_attachments:
                    data, effective_mime = load_audio_bytes_for_llm(
                        attachment["path"], attachment["mime_type"],
                    )
                    if not data or not effective_mime:
                        logging.warning(
                            "Failed to load audio for Gemini payload: %s", attachment["uri"]
                        )
                        continue
                    media_parts.append(types.Part.from_bytes(data=data, mime_type=effective_mime))
            elif audio_attachments:
                # supports_audio=False: inject audio summaries as text only
                skip_audio_summary = idx in skip_audio_summary_indices
                for attachment in audio_attachments:
                    summary = (
                        None if skip_audio_summary
                        else ensure_audio_summary(attachment["path"], attachment["mime_type"])
                    )
                    summary_note = summary or "(要約を取得できませんでした)"
                    note = f"[音声: {attachment['uri']}] {summary_note}"
                    text_content = f"{text_content}\n{note}" if text_content else note

            # === Video processing ===
            if video_attachments and self.supports_video:
                logging.debug(
                    "[gemini] embedding %d video attachment(s) for role=%s",
                    len(video_attachments), role,
                )
                for attachment in video_attachments:
                    data, effective_mime = load_video_bytes_for_llm(
                        attachment["path"], attachment["mime_type"],
                    )
                    if not data or not effective_mime:
                        logging.warning(
                            "Failed to load video for Gemini payload: %s", attachment["uri"]
                        )
                        continue
                    media_parts.append(types.Part.from_bytes(data=data, mime_type=effective_mime))
            elif video_attachments:
                # supports_video=False: inject video summaries as text only
                skip_video_summary = idx in skip_video_summary_indices
                for attachment in video_attachments:
                    summary = (
                        None if skip_video_summary
                        else ensure_video_summary(attachment["path"], attachment["mime_type"])
                    )
                    summary_note = summary or "(要約を取得できませんでした)"
                    note = f"[動画: {attachment['uri']}] {summary_note}"
                    text_content = f"{text_content}\n{note}" if text_content else note

            # === Final assembly: text first, then media parts ===
            final_parts: List[types.Part] = []
            if text_content:
                text_part = types.Part(text=text_content)
                # 2026-05-20: assistant role の text-only message に thought_signature
                # があれば最初の text Part に乗せて echo する (Gemini 3.x 公式仕様:
                # 最初のパートまたは function_call パートに付与)。tool_calls 経路は
                # 上記 line 682-684 で別途 echo 済み。
                # 詳細は docs/intent/thought_signature_persistence.md
                if g_role == "model" and self._multi_turn_thinking:
                    _msg_thought_sig = message.get("thought_signature")
                    if _msg_thought_sig:
                        text_part.thought_signature = _msg_thought_sig
                final_parts.append(text_part)
            final_parts.extend(media_parts)
            if not final_parts:
                final_parts.append(types.Part(text=""))
            contents.append(types.Content(parts=final_parts, role=g_role))

        boundary_contents_idx: Optional[int] = None
        if boundary_msg_idx is not None and boundary_msg_idx < len(content_starts):
            start = content_starts[boundary_msg_idx]
            end = (
                content_starts[boundary_msg_idx + 1]
                if boundary_msg_idx + 1 < len(content_starts)
                else len(contents)
            )
            if end > start:
                boundary_contents_idx = end - 1
        return "\n".join(system_lines), contents, boundary_contents_idx

    def _validate_contents(self, contents: List[types.Content]) -> None:
        """Reject requests that the selected Gemini contract cannot accept."""
        if self._supports_model_prefill:
            return
        for content in reversed(contents):
            if not _content_has_payload(content):
                continue
            if content.role == "model":
                logging.warning(
                    "[gemini] Rejecting request ending in a non-empty model turn: model=%s",
                    self.model,
                )
                raise InvalidRequestError(
                    f"Gemini model {self.model} does not accept a prefilled model turn",
                    user_message=(
                        "このGeminiモデルでは、会話コンテキストの末尾をモデル発話にできません。"
                        "呼び出し元のコンテキスト構築を確認してください。"
                    ),
                )
            return

    def _apply_generation_parameters(
        self,
        cfg_kwargs: Dict[str, Any],
        temperature: float | None,
        *,
        max_output_tokens: int | None = None,
    ) -> None:
        """Apply request parameters while enforcing model capabilities.

        ``max_output_tokens`` is a per-call override that wins over the model
        config (``self._request_params``). Callers that must cap a single call
        — e.g. the sluice, whose structured output must not run away into a
        digit loop (docs/issues/sluice_structured_output_digit_loop.md) —
        pass it as a keyword to :meth:`generate`; every other call is
        unaffected.
        """
        if self._supports_sampling_parameters:
            effective_temp = (
                temperature if temperature is not None else self._request_params.get("temperature")
            )
            if effective_temp is not None:
                cfg_kwargs["temperature"] = effective_temp
            for param in ("top_p", "top_k"):
                if param in self._request_params:
                    cfg_kwargs[param] = self._request_params[param]
        elif temperature is not None:
            logging.debug(
                "[gemini] Dropping deprecated temperature override for model=%s",
                self.model,
            )

        for param in ("max_output_tokens", "stop_sequences"):
            if param in self._request_params:
                cfg_kwargs[param] = self._request_params[param]

        if max_output_tokens is not None:
            cfg_kwargs["max_output_tokens"] = max_output_tokens

    def _last_user(self, messages: List[Any]) -> str:
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                return content_to_text(message.get("content", ""))
            if isinstance(message, types.Content) and message.role == "user":
                if message.parts and message.parts[0].text:
                    return message.parts[0].text
        return ""

    def _call_with_client(
        self,
        client: genai.Client,
        messages: List[Any],
        tools_spec: Optional[list],
        tool_cfg: Optional[types.ToolConfig],
    ):
        sys_msg, contents = self._convert_messages(messages)
        self._validate_contents(contents)
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": self._build_safety_settings(),
        }
        if tools_spec:
            cfg_kwargs["tools"] = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tool_config"] = tool_cfg
        # Always disable AFC - SAIVerse handles function calls manually
        cfg_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            cfg_kwargs["thinking_config"] = thinking_config
        return client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] = None,
        history_snippets: Optional[List[str]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        enable_cache: bool = False,
        cache_ttl: Any = None,
        max_output_tokens: int | None = None,
        continuous_cache: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> str | Dict[str, Any]:
        """Unified generate method (see ``_generate_impl``).

        ``continuous_cache``: SEA がユーザーの発言への返事の呼び出しにだけ渡す
        ``{"persona_id": ..., "building_id": ...}``。継続キャッシュ (高速応答モード) が
        ON のとき、この呼び出しを継続キャッシュで送る (それ以外の呼び出しは None)。
        """
        held: List[str] = []
        try:
            return self._generate_impl(
                messages,
                tools,
                history_snippets,
                response_schema,
                temperature=temperature,
                enable_cache=enable_cache,
                cache_ttl=cache_ttl,
                max_output_tokens=max_output_tokens,
                continuous_cache=continuous_cache,
                _cc_held=held,
            )
        finally:
            # 応答 (再試行を含む) が終わるまで参照を持ち、置き換え済みのキャッシュを
            # 使用中に消さない。
            self._continuous_release_held(held)

    def _generate_impl(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] = None,
        history_snippets: Optional[List[str]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        enable_cache: bool = False,
        cache_ttl: Any = None,
        max_output_tokens: int | None = None,
        continuous_cache: Optional[Dict[str, Any]] = None,
        _cc_held: Optional[List[str]] = None,
    ) -> str | Dict[str, Any]:
        """Unified generate method.

        Args:
            messages: Conversation messages
            tools: Tool specifications. If provided, returns Dict with tool detection.
                   If None or empty, returns str with text response.
            history_snippets: Optional history context
            response_schema: Optional JSON schema for structured output
            temperature: Optional temperature override
            max_output_tokens: Per-call output cap. Overrides the model config
                for this call only; other calls are untouched.

        Returns:
            str: Text response when tools is None or empty
            Dict: Tool detection result when tools is provided, with keys:
                  - type: "text" | "tool_call" | "both"
                  - content: Generated text (if any)
                  - tool_name: Tool name (if tool_call or both)
                  - tool_args: Tool arguments dict (if tool_call or both)
        """
        tools_spec = tools or []
        use_tools = bool(tools_spec)
        history_snippets = history_snippets or []
        self._store_reasoning([])
        # 2026-05-20: 前回呼び出しの signature 残りを必ずクリア (retry や次ターン汚染防止)
        self._store_thought_signature(None)

        active_client = self.client
        sys_msg, contents, _boundary_idx = self._convert_messages_with_boundary(messages)
        self._validate_contents(contents)

        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": self._build_safety_settings(),
        }

        self._apply_generation_parameters(
            cfg_kwargs, temperature, max_output_tokens=max_output_tokens,
        )

        # Tool configuration
        if use_tools:
            merged_tools = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tools"] = merged_tools
            cfg_kwargs["tool_config"] = types.ToolConfig(
                functionCallingConfig=types.FunctionCallingConfig(mode="AUTO")
            )
            logging.info("[gemini] Sending %d Tool objects to API", len(merged_tools))

        # Response schema configuration
        if response_schema:
            cfg_kwargs["response_mime_type"] = "application/json"
            if isinstance(response_schema, dict) and self._requires_json_schema(response_schema):
                cfg_kwargs["response_json_schema"] = self._to_response_json_schema(response_schema)
            else:
                schema_obj = self._schema_from_json(response_schema)
                if schema_obj is not None:
                    cfg_kwargs["response_schema"] = schema_obj

        # Always disable AFC - SAIVerse handles function calls manually
        cfg_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)

        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            cfg_kwargs["thinking_config"] = thinking_config

        # Auto cache mode: 全呼び出しで create→use→delete を自動適用。
        # ON なら B 戦略 (_resolve_explicit_cache) をバイパスする。返事の呼び出しで
        # 継続キャッシュが ON なら継続キャッシュ (_apply_request_cache の docstring)。
        cc_held: List[str] = _cc_held if _cc_held is not None else []
        uncached_contents = contents  # キャッシュが使えなかった回の送り直し用
        _cache_name, contents, _auto_cleanup_name = self._apply_request_cache(
            sys_msg, contents, _boundary_idx, enable_cache, cache_ttl,
            continuous_cache, cc_held,
        )
        if _cache_name:
            cfg_kwargs["cached_content"] = _cache_name
            cfg_kwargs.pop("system_instruction", None)

        get_llm_logger().debug(
            "Gemini generate config model=%s use_tools=%s cfg=%s",
            self.model, use_tools, cfg_kwargs,
        )
        logging.debug(
            "[gemini] SENDING %d contents msgs (~%d text chars) cached_content=%s",
            len(contents),
            sum(len(getattr(p, "text", "") or "") for c in contents for p in (getattr(c, "parts", None) or [])),
            cfg_kwargs.get("cached_content"),
        )

        if _auto_cleanup_name:
            self._auto_cache_pending_cleanup = _auto_cleanup_name

        max_retries = 3
        model_id = self.model
        last_retry_exc: Optional[Exception] = None

        # Structured output (response_schema) is buffered server-side by Gemini
        # — no chunks arrive until the full JSON is ready, so streaming offers
        # no latency benefit and previously triggered spurious timeouts.
        use_non_streaming = use_tools or response_schema is not None
        # キャッシュを外した送り直し (1 回だけ) は、再試行の回数を消費しない。
        attempts = _AttemptCounter(max_retries)
        cache_fallback_done = False
        for attempt in attempts:
            try:
                if use_non_streaming:
                    try:
                        resp = active_client.models.generate_content(
                            model=model_id,
                            contents=contents,
                            config=types.GenerateContentConfig(**cfg_kwargs),
                        )
                    except Exception as sdk_exc:
                        # Structured-output parse failures happen inside the SDK
                        # and would otherwise take the response body with them.
                        from_sdk_parse, raw_text = _sdk_structured_parse_failure(sdk_exc)
                        if not from_sdk_parse:
                            raise
                        if raw_text is not None:
                            get_llm_logger().warning(
                                "Gemini structured output failed to parse inside the SDK "
                                "(model=%s, %s: %s). Raw body (%d chars) follows:\n%s",
                                model_id, type(sdk_exc).__name__, sdk_exc,
                                len(raw_text), raw_text,
                            )
                            logging.warning(
                                "[gemini] schema 付き応答の解析に失敗 (model=%s, %s) — "
                                "生の本文 %d 字を llm_io.log に残しました",
                                model_id, type(sdk_exc).__name__, len(raw_text),
                            )
                            detail = f"raw body kept in llm_io.log ({len(raw_text)} chars)"
                        else:
                            logging.warning(
                                "[gemini] schema 付き応答の解析に失敗 (model=%s, %s) — "
                                "本文は SDK 内で失われた",
                                model_id, type(sdk_exc).__name__,
                            )
                            detail = "本文は SDK 内で失われた"
                        raise InvalidRequestError(
                            f"schema 付き応答の解析に失敗 ({detail}): {sdk_exc}",
                            sdk_exc,
                            user_message="LLMからの応答を解析できませんでした。再度お試しください。",
                        ) from sdk_exc
                else:
                    stream = active_client.models.generate_content_stream(
                        model=model_id,
                        contents=contents,
                        config=types.GenerateContentConfig(**cfg_kwargs),
                    )
                    all_parts: List[Any] = []
                    saw_any_chunk = False
                    last_chunk = None
                    last_finish_reason = None
                    last_safety_ratings = None
                    prompt_block_reason = None

                    for chunk in stream:
                        last_chunk = chunk
                        saw_any_chunk = True
                        get_llm_logger().debug("Gemini stream chunk:\n%s", chunk)

                        # Check prompt-level block (PROHIBITED_CONTENT etc.)
                        pf = getattr(chunk, "prompt_feedback", None)
                        if pf:
                            br = getattr(pf, "block_reason", None)
                            if br is not None:
                                prompt_block_reason = br

                        if chunk.candidates:
                            candidate = chunk.candidates[0]
                            fr = getattr(candidate, "finish_reason", None)
                            if fr is not None:
                                last_finish_reason = fr
                            sr = getattr(candidate, "safety_ratings", None)
                            if sr is not None:
                                last_safety_ratings = sr
                            if candidate.content and candidate.content.parts:
                                all_parts.extend(candidate.content.parts)

                    if not saw_any_chunk:
                        raise EmptyResponseError("No chunks received from stream")

                    # Check for server error detected in SSE stream (e.g. 504 DEADLINE_EXCEEDED)
                    # For call() / structured output, treat as timeout → retry
                    _sse_err = getattr(_sse_error_local, "error", None)
                    if _sse_err is not None:
                        _sse_error_local.error = None
                        logging.warning(
                            "[gemini] SSE server error in call() path: code=%s status=%s — retrying",
                            _sse_err.get("code"), _sse_err.get("status", ""),
                        )
                        from .exceptions import LLMTimeoutError as _LLMTimeoutError
                        raise _LLMTimeoutError(
                            f"Gemini stream interrupted by server: {_sse_err.get('status', '')} "
                            f"({_sse_err.get('code', '')}): {_sse_err.get('message', '')}",
                        )

                    # Prompt-level block: input rejected before generation
                    if prompt_block_reason:
                        block_str = str(prompt_block_reason)
                        _block_usage = getattr(last_chunk, "usage_metadata", None) if last_chunk else None
                        logging.warning(
                            "[gemini] Prompt blocked: block_reason=%s, prompt_tokens=%s",
                            block_str,
                            getattr(_block_usage, "prompt_token_count", None) if _block_usage else None,
                        )
                        raise SafetyFilterError(
                            f"Prompt blocked by Gemini: {block_str}",
                            user_message=_prompt_block_user_message(block_str),
                        )

                    if not all_parts:
                        # Log usage even for empty responses (did tokens get consumed?)
                        _empty_usage = getattr(last_chunk, "usage_metadata", None) if last_chunk else None
                        logging.warning(
                            "[gemini] Stream had chunks but no content parts. "
                            "finish_reason=%s, safety_ratings=%s, "
                            "prompt_tokens=%s, total_tokens=%s",
                            last_finish_reason,
                            [str(r) for r in (last_safety_ratings or [])],
                            getattr(_empty_usage, "prompt_token_count", None) if _empty_usage else None,
                            getattr(_empty_usage, "total_token_count", None) if _empty_usage else None,
                        )
                        if last_finish_reason and "SAFETY" in str(last_finish_reason).upper():
                            blocked = [
                                str(getattr(r, "category", "unknown"))
                                for r in (last_safety_ratings or [])
                                if str(getattr(r, "probability", "")).upper() in ("HIGH", "MEDIUM")
                            ]
                            raise SafetyFilterError(
                                f"Content blocked by safety filter (streaming). Blocked: {blocked}",
                                user_message=_RESPONSE_BLOCK_USER_MESSAGE,
                            )
                        raise EmptyResponseError(
                            f"No parts in stream response (finish_reason={last_finish_reason})"
                        )

                    # Store usage from last chunk (uses self.config_key for pricing)
                    if last_chunk:
                        usage = getattr(last_chunk, "usage_metadata", None)
                        if usage:
                            # Debug: log available fields in usage_metadata
                            logging.info("[DEBUG] Gemini usage_metadata attrs: %s",
                                        [a for a in dir(usage) if not a.startswith('_')])
                            logging.info("[DEBUG] Gemini usage_metadata values: prompt=%s, candidates=%s, cached=%s, total=%s",
                                        getattr(usage, "prompt_token_count", None),
                                        getattr(usage, "candidates_token_count", None),
                                        getattr(usage, "cached_content_token_count", None),
                                        getattr(usage, "total_token_count", None))
                            cached = getattr(usage, "cached_content_token_count", 0) or 0
                            self._store_usage(
                                input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                                output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                                cached_tokens=cached,
                            )

                    # Debug: log what's in all_parts before processing
                    for i, part in enumerate(all_parts):
                        logging.info(
                            "[gemini] all_parts[%d]: type=%s, thought=%s, text=%r, attrs=%s",
                            i,
                            type(part).__name__,
                            getattr(part, "thought", "N/A"),
                            (getattr(part, "text", None) or "")[:100] if getattr(part, "text", None) else None,
                            [a for a in dir(part) if not a.startswith("_")]
                        )

                    text, reasoning_entries, text_thought_sig = self._separate_parts(all_parts)
                    self._store_reasoning(reasoning_entries)
                    # 2026-05-20: text part の thought_signature を保持。
                    # 次ターンで echo するために SEA runtime が consume する。
                    self._store_thought_signature(text_thought_sig)

                    if response_schema:
                        logging.info("[gemini] Structured output: text=%r, len=%d", text if text else "(empty)", len(text))
                        try:
                            parsed = json.loads(text)
                            if isinstance(parsed, dict):
                                return parsed
                        except json.JSONDecodeError as e:
                            logging.warning("[gemini] Failed to parse structured output: %s", e)
                            from .exceptions import InvalidRequestError
                            raise InvalidRequestError(
                                "Failed to parse JSON response from structured output",
                                e,
                                user_message="LLMからの応答を解析できませんでした。再度お試しください。"
                            ) from e

                    # Check for empty streaming response (no response_schema and empty/whitespace-only text)
                    if not text.strip():
                        logging.error(
                            "[gemini] Empty streaming response (attempt %d/%d). "
                            "Received parts but text is empty/whitespace-only. "
                            "finish_reason=%s, parts_count=%d, reasoning_count=%d",
                            attempt + 1, max_retries,
                            last_finish_reason, len(all_parts), len(reasoning_entries),
                        )
                        continue

                    prefix = "\n".join(history_snippets)
                    return prefix + ("\n" if prefix and text else "") + text

                # Process non-streaming response (tool mode)
                get_llm_logger().debug("Gemini raw:\n%s", resp)

                # Store usage information (uses self.config_key for pricing)
                usage = getattr(resp, "usage_metadata", None)
                logging.info("[DEBUG] Gemini non-stream usage_metadata: %s", usage)
                if usage:
                    logging.info("[DEBUG] Gemini non-stream usage attrs: %s",
                                [a for a in dir(usage) if not a.startswith('_')])
                    logging.info("[DEBUG] Gemini non-stream usage values: prompt=%s, candidates=%s, cached=%s",
                                getattr(usage, "prompt_token_count", None),
                                getattr(usage, "candidates_token_count", None),
                                getattr(usage, "cached_content_token_count", None))
                    cached = getattr(usage, "cached_content_token_count", 0) or 0
                    self._store_usage(
                        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                        cached_tokens=cached,
                    )

                # Check prompt-level block (PROHIBITED_CONTENT etc.)
                pf = getattr(resp, "prompt_feedback", None)
                if pf:
                    br = getattr(pf, "block_reason", None)
                    if br is not None:
                        block_str = str(br)
                        logging.warning(
                            "[gemini] Prompt blocked (non-streaming): block_reason=%s, prompt_tokens=%s",
                            block_str,
                            getattr(usage, "prompt_token_count", None) if usage else None,
                        )
                        raise SafetyFilterError(
                            f"Prompt blocked by Gemini: {block_str}",
                            user_message=_prompt_block_user_message(block_str),
                        )

                if not resp.candidates:
                    logging.warning("[gemini] No candidates (attempt %d/%d)", attempt + 1, max_retries)
                    continue
                    
                candidate = resp.candidates[0]

                # Check for safety filter block
                finish_reason = getattr(candidate, "finish_reason", None)
                if finish_reason is not None:
                    finish_reason_str = str(finish_reason).upper()
                    if "SAFETY" in finish_reason_str:
                        safety_ratings = getattr(candidate, "safety_ratings", [])
                        blocked_categories = [
                            str(getattr(r, "category", "unknown"))
                            for r in (safety_ratings or [])
                            if str(getattr(r, "probability", "")).upper() in ("HIGH", "MEDIUM")
                        ]
                        detail = f"Blocked categories: {blocked_categories}" if blocked_categories else ""
                        logging.warning("[gemini] Response blocked by safety filter: %s", detail)
                        raise SafetyFilterError(
                            f"Content blocked by safety filter. {detail}",
                            user_message=_RESPONSE_BLOCK_USER_MESSAGE,
                        )

                if not candidate.content or not candidate.content.parts:
                    logging.warning("[gemini] No content/parts (attempt %d/%d)", attempt + 1, max_retries)
                    continue

                # Extract text, reasoning, and function calls
                text_parts = []
                reasoning_entries = []
                function_call_part = None

                _sync_thought_sig: Optional[str] = None
                # 2026-05-20: text part (function_call 以外) の thought_signature。
                # 公式 doc は text 空でも signature だけ乗るケースを認めるため、
                # text の有無に関わらず最後の非 None 値を採用する。
                _text_thought_sig: Optional[str] = None
                for part in candidate.content.parts:
                    part_fcall = getattr(part, "function_call", None)
                    if part_fcall:
                        function_call_part = part_fcall
                        _ts = getattr(part, "thought_signature", None)
                        if _ts:
                            _sync_thought_sig = _ts
                        continue

                    is_thought = is_truthy_flag(getattr(part, "thought", None))
                    # thought=True (思考ブロック) 以外の text part から signature を取得。
                    if not is_thought:
                        _ts_text = getattr(part, "thought_signature", None)
                        if _ts_text:
                            _text_thought_sig = _ts_text
                    part_text = getattr(part, "text", None)
                    if not part_text:
                        continue
                    if is_thought:
                        reasoning_entries.append({"title": "Thought", "text": part_text.strip()})
                    else:
                        text_parts.append(part_text)

                text = "".join(text_parts)
                self._store_reasoning(reasoning_entries)

                if response_schema:
                    logging.info("[gemini] Structured output (non-stream): text=%r, len=%d", text if text else "(empty)", len(text))
                    if not text.strip():
                        logging.warning(
                            "[gemini] Empty structured output response (attempt %d/%d). finish_reason=%s",
                            attempt + 1, max_retries, finish_reason,
                        )
                        continue
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError as e:
                        logging.warning("[gemini] Failed to parse structured output: %s", e)
                        from .exceptions import InvalidRequestError
                        raise InvalidRequestError(
                            "Failed to parse JSON response from structured output",
                            e,
                            user_message="LLMからの応答を解析できませんでした。再度お試しください。"
                        ) from e
                    if not isinstance(parsed, dict):
                        from .exceptions import InvalidRequestError
                        raise InvalidRequestError(
                            f"Structured output returned non-dict JSON: {type(parsed).__name__}",
                            user_message="LLMからの構造化出力が想定の形式ではありません。"
                        )
                    return parsed

                # Check candidate-level function_call for backwards compatibility
                if not function_call_part:
                    function_call_part = getattr(candidate, "function_call", None)

                # Return appropriate type based on what was found
                if function_call_part:
                    fcall_name = getattr(function_call_part, "name", None)
                    fcall_args = getattr(function_call_part, "args", {}) or {}
                    fcall_id = getattr(function_call_part, "id", None)
                    _fc_base: Dict[str, Any] = {
                        "tool_name": fcall_name,
                        "tool_args": dict(fcall_args),
                        "raw_function_call": function_call_part,
                    }
                    if fcall_id:
                        _fc_base["tool_call_id"] = fcall_id
                    if _sync_thought_sig:
                        _fc_base["thought_signature"] = _sync_thought_sig

                    if fcall_name and isinstance(fcall_name, str):
                        if text:
                            logging.info("[gemini] Returning both: text + tool_call (%s)", fcall_name)
                            return {"type": "both", "content": text, **_fc_base}
                        else:
                            logging.info("[gemini] Returning tool_call: %s", fcall_name)
                            return {"type": "tool_call", **_fc_base}

                # Text-only response in tool mode
                # Check for empty text response (no tool call and empty/whitespace-only text)
                if not text.strip():
                    logging.error(
                        "[gemini] Empty text response without tool call (attempt %d/%d). "
                        "Model returned Part(text='') with finish_reason=STOP. "
                        "prompt_tokens=%s, response_id=%s",
                        attempt + 1, max_retries,
                        getattr(usage, "prompt_token_count", None) if usage else None,
                        getattr(resp, "response_id", None)
                    )
                    continue

                # 2026-05-20: text-only 応答の thought_signature を保持。
                # 次ターンで Gemini に echo するため SEA runtime が consume する。
                # tool_call 経路と一貫させて result dict にも乗せる (SEA runtime は
                # result.get("thought_signature") で取得して state に保存する)。
                self._store_thought_signature(_text_thought_sig)
                logging.info("[gemini] Returning text response")
                _text_result: Dict[str, Any] = {"type": "text", "content": text}
                if _text_thought_sig:
                    _text_result["thought_signature"] = _text_thought_sig
                return _text_result

            except EmptyResponseError as e:
                logging.warning("Gemini empty response (attempt %d/%d): %s", attempt + 1, max_retries, e)
                continue
            except LLMError:
                # LLMError subclasses (SafetyFilterError, etc.) already have
                # correct error_code and user_message — propagate as-is.
                raise
            except Exception as exc:
                # 付けたキャッシュが使えない (403/404 等) — 認証エラーやモデル無しと
                # 誤判定される前に見る。キャッシュを外して 1 回だけ送り直す。
                if cfg_kwargs.get("cached_content") and not cache_fallback_done:
                    cache_reason = self._cache_use_error_reason(exc)
                    if cache_reason is not None:
                        cache_fallback_done = True
                        cfg_kwargs = self._uncached_retry_config(
                            cfg_kwargs, sys_msg, cache_reason, cc_held,
                        )
                        contents = uncached_contents
                        attempts.repeat_current()
                        continue
                if self._is_timeout_error(exc):
                    logging.warning(
                        "Gemini timeout (attempt %d/%d): %s",
                        attempt + 1, max_retries, type(exc).__name__
                    )
                    try:
                        total_chars = sum(
                            len(m.get("content", "") or "") if isinstance(m, dict) else 0
                            for m in messages
                        )
                        image_count = sum(
                            len(iter_image_media(m.get("metadata")) or [])
                            if isinstance(m, dict) else 0
                            for m in messages
                        )
                        client_type = "paid" if active_client is self.paid_client else "free"
                        log_timeout_event(
                            timeout_type=type(exc).__name__,
                            model=model_id,
                            wait_duration_sec=600.0,
                            message_count=len(messages),
                            total_chars=total_chars,
                            image_count=image_count,
                            has_tools=use_tools,
                            use_stream=not use_tools,
                            client_type=client_type,
                            retry_attempt=attempt + 1,
                        )
                    except Exception as log_exc:
                        logging.debug("Failed to log timeout diagnostics: %s", log_exc)
                    if active_client is self.free_client and self.paid_client:
                        logging.info("Switching to paid Gemini API key after timeout")
                        active_client = self.paid_client
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                    last_retry_exc = exc
                    continue
                if self._is_payment_error(exc) or self._is_authentication_error(exc):
                    raise self._convert_to_llm_error(exc, "API call") from exc
                if self._is_rate_limit_error(exc):
                    if active_client is self.free_client and self.paid_client:
                        logging.info("Retrying with paid Gemini API key due to rate limit")
                        active_client = self.paid_client
                        last_retry_exc = exc
                        continue
                    retry_delay = self._extract_retry_delay(exc)
                    if retry_delay and attempt < max_retries - 1:
                        capped_delay = min(retry_delay, 60.0)
                        logging.info(
                            "Rate limited (model=%s), waiting %.1fs before retry (attempt %d/%d)",
                            model_id, capped_delay, attempt + 1, max_retries,
                        )
                        time.sleep(capped_delay)
                        last_retry_exc = exc
                        continue
                    logging.warning("Rate limit exceeded for model=%s, no retries left", model_id)
                    raise self._convert_to_llm_error(exc, "API call") from exc
                logging.exception("Gemini call failed")
                raise self._convert_to_llm_error(exc, "API call") from exc

        logging.error("Gemini API call failed after %d retries", max_retries)
        if last_retry_exc is not None:
            raise self._convert_to_llm_error(last_retry_exc, f"API call (after {max_retries} retries)") from last_retry_exc
        if use_tools:
            return {"type": "text", "content": ""}
        raise LLMEmptyResponseError(
            f"Gemini API call failed after {max_retries} retries with empty responses",
            user_message="何度も空の応答が返されました。しばらく待ってから再度お試しください。"
        )

    def _separate_parts(self, parts: List[Any]) -> Tuple[str, List[Dict[str, str]], Optional[str]]:
        """Separate text / reasoning / thought_signature from response parts.

        2026-05-20: 戻り値に thought_signature を追加。Gemini 公式 doc は
        「最終チャンクの空 text part に signature だけ乗る」ケースを認めるため、
        text が空でも signature だけ抽出する。複数現れた場合は最後の非 None 値を採用。
        詳細は docs/intent/thought_signature_persistence.md
        """
        reasoning_entries: List[Dict[str, str]] = []
        text_segments: List[str] = []
        latest_signature: Optional[str] = None
        counter = 1
        for part in parts or []:
            if part is None:
                continue
            is_thought = is_truthy_flag(getattr(part, "thought", None))
            # thought=True (= 思考ブロック) の part に signature が乗っていても、
            # ペルソナの発話用ではないため text part 由来のみを採用する。
            if not is_thought:
                sig = getattr(part, "thought_signature", None)
                if sig:
                    latest_signature = sig
            text = getattr(part, "text", None)
            if not text:
                continue
            if is_thought:
                reasoning_entries.append({"title": f"Thought {counter}", "text": text.strip()})
                counter += 1
            else:
                text_segments.append(text)
        return "".join(text_segments), reasoning_entries, latest_signature

    def generate_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        enable_cache: bool = False,
        cache_ttl: Any = None,
        max_output_tokens: int | None = None,
        continuous_cache: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> Iterator[str]:
        """Streaming generate (see ``_generate_stream_impl``).

        ``continuous_cache`` は :meth:`generate` と同じ。ストリームを読み終える
        (または閉じられる) まで継続キャッシュの参照を持つ — 要求の送信は最初の
        chunk を読むときに起きるので、``_start_stream`` の戻りで返すと早すぎる。
        """
        held: List[str] = []
        try:
            yield from self._generate_stream_impl(
                messages,
                tools,
                history_snippets,
                response_schema,
                temperature=temperature,
                enable_cache=enable_cache,
                cache_ttl=cache_ttl,
                max_output_tokens=max_output_tokens,
                continuous_cache=continuous_cache,
                _cc_held=held,
            )
        finally:
            self._continuous_release_held(held)

    def _generate_stream_impl(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        enable_cache: bool = False,
        cache_ttl: Any = None,
        max_output_tokens: int | None = None,
        continuous_cache: Optional[Dict[str, Any]] = None,
        _cc_held: Optional[List[str]] = None,
    ) -> Iterator[str]:
        cc_held: List[str] = _cc_held if _cc_held is not None else []
        disable_stream = os.getenv("SAIVERSE_DISABLE_GEMINI_STREAMING")
        if disable_stream and disable_stream.lower() not in {"0", "false", "off"}:
            logging.info("SAIVERSE_DISABLE_GEMINI_STREAMING set; using non-streaming Gemini call")
            result = self.generate(
                messages,
                tools=tools or [],
                history_snippets=history_snippets,
                response_schema=response_schema,
                temperature=temperature,
                enable_cache=enable_cache,
                cache_ttl=cache_ttl,
                max_output_tokens=max_output_tokens,
                continuous_cache=continuous_cache,
            )
            yield result
            return

        tools_spec = GEMINI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        history_snippets = history_snippets or []
        self._store_reasoning([])
        # 2026-05-20: 前回呼び出しの signature 残りを必ずクリア (retry や次ターン汚染防止)
        self._store_thought_signature(None)
        reasoning_chunks: List[str] = []

        # Clear any leftover SSE error from a previous call on this thread
        _sse_error_local.error = None
        self._last_stream_error = None

        if use_tools:
            if tools is not None:
                # Explicit tools from runtime — let LLM decide natively (AUTO)
                tool_cfg = types.ToolConfig(
                    functionCallingConfig=types.FunctionCallingConfig(mode="AUTO")
                )
            else:
                # Legacy mode (tools=None → GEMINI_TOOLS_SPEC) — use router
                decision = route(self._last_user(messages), tools_spec)
                logging.info(
                    "Router decision:\n%s",
                    json.dumps(decision, indent=2, ensure_ascii=False),
                )
                tool_cfg = types.ToolConfig(
                    functionCallingConfig=(
                        types.FunctionCallingConfig(
                            mode="ANY",
                            allowedFunctionNames=[decision["tool"]],
                        )
                        if decision["call"] == "yes"
                        else types.FunctionCallingConfig(mode="AUTO")
                    )
                )
        else:
            tool_cfg = None

        active_client = self.client
        # 利用者へ何か yield したら立てる (キャッシュが使えなかった回の送り直しの可否)。
        stream_state: Dict[str, Any] = {"emitted": False}
        try:
            stream = self._start_stream(active_client, messages, tools_spec, tool_cfg, use_tools, temperature, response_schema, enable_cache=enable_cache, cache_ttl=cache_ttl, max_output_tokens=max_output_tokens, continuous_cache=continuous_cache, cc_held=cc_held, stream_state=stream_state)
        except Exception as exc:
            if self._is_payment_error(exc) or self._is_authentication_error(exc):
                raise self._convert_to_llm_error(exc, "streaming")
            if self._is_rate_limit_error(exc):
                if active_client is self.free_client and self.paid_client:
                    logging.info("Retrying with paid Gemini API key due to rate limit")
                    active_client = self.paid_client
                    stream = self._start_stream(active_client, messages, tools_spec, tool_cfg, use_tools, temperature, response_schema, enable_cache=enable_cache, cache_ttl=cache_ttl, max_output_tokens=max_output_tokens, continuous_cache=continuous_cache, cc_held=cc_held, stream_state=stream_state)
                else:
                    retry_delay = self._extract_retry_delay(exc)
                    if retry_delay:
                        capped_delay = min(retry_delay, 60.0)
                        logging.info(
                            "Rate limited (model=%s), waiting %.1fs before stream retry",
                            self.model, capped_delay,
                        )
                        time.sleep(capped_delay)
                        stream = self._start_stream(active_client, messages, tools_spec, tool_cfg, use_tools, temperature, response_schema, enable_cache=enable_cache, cache_ttl=cache_ttl, max_output_tokens=max_output_tokens, continuous_cache=continuous_cache, cc_held=cc_held, stream_state=stream_state)
                    else:
                        logging.warning("Rate limit exceeded for model=%s", self.model)
                        raise self._convert_to_llm_error(exc, "streaming")
            else:
                logging.exception("Gemini call failed")
                raise self._convert_to_llm_error(exc, "streaming")

        fcall: Optional[types.FunctionCall] = None
        fcall_thought_signature: Optional[str] = None
        # 2026-05-20: text part (function_call 以外) の thought_signature。
        # ストリームに複数現れた場合は最後の非 None 値を採用。
        text_thought_signature: Optional[str] = None
        prefix_yielded = False
        seen_stream_texts: Dict[int, str] = {}
        thought_seen: Dict[int, str] = {}

        saw_chunks = False
        stream_completed = False
        last_chunk = None

        # ── Streaming timing instrumentation ──
        stream_start = time.monotonic()
        first_text_chunk_time: float | None = None
        last_text_chunk_time: float | None = None
        finish_reason_time: float | None = None
        finish_reason_value: str | None = None
        chunk_count = 0
        text_chunk_count = 0
        post_finish_chunk_count = 0

        try:
            for chunk in stream:
                chunk_count += 1
                last_chunk = chunk
                get_llm_logger().debug("Gemini stream chunk:\n%s", chunk)
                if finish_reason_time is not None:
                    post_finish_chunk_count += 1
                # プロンプト段階のブロック (PROHIBITED_CONTENT 等)。この時 candidates は
                # None なので、下の candidates 判定より先に見ないと黙って読み飛ばされ、
                # 空のストリームとして理由なく終わる (2026-09-24 修正)。
                _pf = getattr(chunk, "prompt_feedback", None)
                _br = getattr(_pf, "block_reason", None) if _pf else None
                if _br is not None:
                    block_str = str(_br)
                    _block_usage = getattr(chunk, "usage_metadata", None)
                    logging.warning(
                        "[gemini_stream] Prompt blocked: block_reason=%s, prompt_tokens=%s",
                        block_str,
                        getattr(_block_usage, "prompt_token_count", None) if _block_usage else None,
                    )
                    raise SafetyFilterError(
                        f"Prompt blocked by Gemini: {block_str}",
                        user_message=_prompt_block_user_message(block_str),
                    )
                if not chunk.candidates:
                    continue
                candidate = chunk.candidates[0]
                get_llm_logger().debug("Gemini stream candidate:\n%s", candidate)
                saw_chunks = True

                # Check for safety filter block in streaming
                finish_reason = getattr(candidate, "finish_reason", None)
                if finish_reason is not None:
                    if finish_reason_time is None:
                        finish_reason_value = str(finish_reason)
                        finish_reason_time = time.monotonic()
                        logging.info(
                            "[gemini_stream] finish_reason='%s' received at +%.2fs (chunk #%d)",
                            finish_reason_value, finish_reason_time - stream_start, chunk_count,
                        )
                    finish_reason_str = str(finish_reason).upper()
                    if "SAFETY" in finish_reason_str:
                        safety_ratings = getattr(candidate, "safety_ratings", [])
                        blocked_categories = [
                            str(getattr(r, "category", "unknown"))
                            for r in (safety_ratings or [])
                            if str(getattr(r, "probability", "")).upper() in ("HIGH", "MEDIUM")
                        ]
                        detail = f"Blocked categories: {blocked_categories}" if blocked_categories else ""
                        logging.warning("[gemini] Stream blocked by safety filter: %s", detail)
                        raise SafetyFilterError(
                            f"Content blocked by safety filter. {detail}",
                            user_message=_RESPONSE_BLOCK_USER_MESSAGE,
                        )

                if not candidate.content or not candidate.content.parts:
                    continue
                candidate_index = getattr(candidate, "index", 0)
                for part_idx, part in enumerate(candidate.content.parts):
                    if getattr(part, "function_call", None) and fcall is None:
                        get_llm_logger().debug("Gemini function_call (part %s): %s", part_idx, part.function_call)
                        fcall = part.function_call
                        # Capture thought_signature for thinking-enabled models (required by Gemini API)
                        _ts = getattr(part, "thought_signature", None)
                        if _ts:
                            fcall_thought_signature = _ts
                    elif is_truthy_flag(getattr(part, "thought", None)):
                        text_val = getattr(part, "text", None) or ""
                        if text_val:
                            previous = thought_seen.get(candidate_index, "")
                            if text_val.startswith(previous):
                                delta = text_val[len(previous) :]
                                if delta:
                                    reasoning_chunks.append(delta)
                                    thought_seen[candidate_index] = previous + delta
                                    stream_state["emitted"] = True
                                    yield {"type": "thinking", "content": delta}
                            else:
                                reasoning_chunks.append(text_val)
                                thought_seen[candidate_index] = previous + text_val
                                stream_state["emitted"] = True
                                yield {"type": "thinking", "content": text_val}
                    else:
                        # 2026-05-20: text part (function_call なし thought なし) の signature 取得。
                        # 公式 doc は「最終チャンクの空 text part に signature だけ乗る」
                        # ケースを認めるため、text の有無に関わらず取得する。
                        _ts_text = getattr(part, "thought_signature", None)
                        if _ts_text:
                            text_thought_signature = _ts_text
                            logging.debug(
                                "[gemini_stream][sig-trace] text part signature captured: %d bytes (chunk=%d)",
                                len(_ts_text) if isinstance(_ts_text, (bytes, str)) else -1,
                                chunk_count,
                            )

                combined_text = "".join(
                    getattr(part, "text", None) or ""
                    for part in candidate.content.parts
                    if getattr(part, "text", None) and not is_truthy_flag(getattr(part, "thought", None))
                )
                if not combined_text:
                    continue

                previous_text = seen_stream_texts.get(candidate_index, "")
                new_text = (
                    combined_text[len(previous_text) :]
                    if combined_text.startswith(previous_text)
                    else combined_text
                )
                if not new_text:
                    continue

                text_chunk_count += 1
                now = time.monotonic()
                if first_text_chunk_time is None:
                    first_text_chunk_time = now
                    logging.debug("[gemini_stream] First text chunk at +%.2fs", now - stream_start)
                last_text_chunk_time = now

                get_llm_logger().debug("Gemini text delta: %s", new_text)
                stream_state["emitted"] = True
                if not prefix_yielded and history_snippets:
                    yield "\n".join(history_snippets) + "\n"
                    prefix_yielded = True
                yield new_text
                seen_stream_texts[candidate_index] = previous_text + new_text
                finish_reason = getattr(candidate, "finish_reason", None)
                if finish_reason:
                    stream_completed = True
        except LLMError:
            raise
        except Exception as exc:
            logging.exception("Gemini stream iteration failed")
            raise self._convert_to_llm_error(exc, "streaming") from exc

        # ── Stream completed: log timing summary ──
        stream_end = time.monotonic()
        elapsed = stream_end - stream_start
        post_finish_wait = (stream_end - finish_reason_time) if finish_reason_time else 0
        logging.info(
            "[gemini_stream] Stream completed: total=%.2fs, chunks=%d, text_chunks=%d, "
            "first_text=+%.2fs, last_text=+%.2fs, finish_reason='%s' at +%.2fs, "
            "post_finish_chunks=%d, post_finish_wait=%.2fs",
            elapsed, chunk_count, text_chunk_count,
            (first_text_chunk_time - stream_start) if first_text_chunk_time else -1,
            (last_text_chunk_time - stream_start) if last_text_chunk_time else -1,
            finish_reason_value,
            (finish_reason_time - stream_start) if finish_reason_time else -1,
            post_finish_chunk_count, post_finish_wait,
        )

        # Check if SSE patch detected a server error (e.g. 504 DEADLINE_EXCEEDED)
        sse_error = getattr(_sse_error_local, "error", None)
        if sse_error is not None:
            _sse_error_local.error = None
            self._last_stream_error = sse_error
            logging.warning(
                "[gemini_stream] Stream interrupted by server error: code=%s status=%s — partial content retained",
                sse_error.get("code"), sse_error.get("status", ""),
            )

        if fcall is None and saw_chunks and not stream_completed and self._last_stream_error is None:
            # finish_reason not received — known Gemini API quirk, not an error
            logging.debug("Gemini stream ended without finish_reason (known Gemini API behavior).")

        # Store usage from last chunk (uses self.config_key for pricing)
        logging.info("[DEBUG] generate_stream last_chunk exists: %s", last_chunk is not None)
        if last_chunk:
            usage = getattr(last_chunk, "usage_metadata", None)
            logging.info("[DEBUG] generate_stream usage_metadata: %s", usage)
            if usage:
                logging.info("[DEBUG] generate_stream usage attrs: %s",
                            [a for a in dir(usage) if not a.startswith('_')])
                logging.info("[DEBUG] generate_stream usage values: prompt=%s, candidates=%s, cached=%s",
                            getattr(usage, "prompt_token_count", None),
                            getattr(usage, "candidates_token_count", None),
                            getattr(usage, "cached_content_token_count", None))
                cached = getattr(usage, "cached_content_token_count", 0) or 0
                self._store_usage(
                    input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                    cached_tokens=cached,
                )

        # Store reasoning
        self._store_reasoning(merge_reasoning_strings(reasoning_chunks))
        # 2026-05-20: text part の thought_signature を保持。tool_call 経由の
        # signature は _td_base 経由で SEA runtime に伝わるため、ここでは text 用のみ。
        # 後段の "if fcall is not None" 分岐内で None に上書きする (function_call
        # detected 時は text 用 signature を保存しない方針)。
        if fcall is None:
            self._store_thought_signature(text_thought_signature)
            logging.debug(
                "[gemini_stream][sig-trace] stored thought_signature on stream end: %s (%d bytes)",
                "present" if text_thought_signature else "None",
                len(text_thought_signature) if isinstance(text_thought_signature, (bytes, str)) else 0,
            )
        else:
            self._store_thought_signature(None)
            logging.debug("[gemini_stream][sig-trace] fcall path; cleared thought_signature")

        # Store tool detection result if function call was detected
        if fcall is not None:
            # Collect all text that was yielded
            all_text = "".join(seen_stream_texts.values())
            fcall_name = getattr(fcall, "name", None)
            fcall_args = getattr(fcall, "args", {}) or {}
            fcall_id = getattr(fcall, "id", None)

            _td_base = {
                "tool_name": fcall_name,
                "tool_args": dict(fcall_args),
                "raw_function_call": fcall,
            }
            if fcall_id:
                _td_base["tool_call_id"] = fcall_id
            if fcall_thought_signature:
                _td_base["thought_signature"] = fcall_thought_signature
            if all_text.strip():
                self._store_tool_detection({
                    "type": "both",
                    "content": all_text,
                    **_td_base,
                })
            else:
                self._store_tool_detection({
                    "type": "tool_call",
                    **_td_base,
                })
            logging.info("[gemini] Tool detection stored: %s", fcall_name)
        else:
            # No tool call - store text-only result
            all_text = "".join(seen_stream_texts.values())
            self._store_tool_detection({
                "type": "text",
                "content": all_text,
            })

    def _start_stream(
        self,
        client: genai.Client,
        messages: List[Any],
        tools_spec: Optional[list],
        tool_cfg: Optional[types.ToolConfig],
        use_tools: bool,
        temperature: float | None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        enable_cache: bool = False,
        cache_ttl: Any = None,
        max_output_tokens: int | None = None,
        continuous_cache: Optional[Dict[str, Any]] = None,
        cc_held: Optional[List[str]] = None,
        stream_state: Optional[Dict[str, Any]] = None,
    ):
        """要求を組み立ててストリームを開く。

        ``stream_state`` は呼び出し元と共有する dict で、呼び出し元が chunk の中身を
        利用者へ yield したら ``emitted=True`` を立てる。キャッシュが使えなかった回の
        送り直しは、まだ何も yield していないときだけ行う
        (:meth:`_stream_with_cache_fallback`)。
        """
        held: List[str] = cc_held if cc_held is not None else []
        sys_msg, contents, _boundary_idx = self._convert_messages_with_boundary(messages)
        self._validate_contents(contents)
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": self._build_safety_settings(),
        }
        if use_tools:
            cfg_kwargs["tools"] = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tool_config"] = tool_cfg
        # Response schema configuration
        if response_schema and not use_tools:
            cfg_kwargs["response_mime_type"] = "application/json"
            if isinstance(response_schema, dict) and self._requires_json_schema(response_schema):
                cfg_kwargs["response_json_schema"] = self._to_response_json_schema(response_schema)
            else:
                schema_obj = self._schema_from_json(response_schema)
                if schema_obj is not None:
                    cfg_kwargs["response_schema"] = schema_obj
        # Always disable AFC - SAIVerse handles function calls manually via Playbook
        cfg_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            cfg_kwargs["thinking_config"] = thinking_config

        self._apply_generation_parameters(
            cfg_kwargs, temperature, max_output_tokens=max_output_tokens,
        )

        # Auto cache mode / 継続キャッシュ / B 戦略 (_apply_request_cache の docstring)
        uncached_contents = contents  # キャッシュが使えなかった回の送り直し用
        _cache_name, contents, _auto_cleanup_name = self._apply_request_cache(
            sys_msg, contents, _boundary_idx, enable_cache, cache_ttl,
            continuous_cache, held,
        )
        if _cache_name:
            cfg_kwargs["cached_content"] = _cache_name
            cfg_kwargs.pop("system_instruction", None)
        if _auto_cleanup_name:
            self._auto_cache_pending_cleanup = _auto_cleanup_name

        get_llm_logger().debug(
            "Gemini stream config model=%s use_tools=%s cfg=%s",
            self.model,
            use_tools,
            cfg_kwargs,
        )
        logging.debug(
            "[gemini] SENDING %d contents msgs (~%d text chars) cached_content=%s",
            len(contents),
            sum(len(getattr(p, "text", "") or "") for c in contents for p in (getattr(c, "parts", None) or [])),
            cfg_kwargs.get("cached_content"),
        )
        if use_tools:
            cfg_kwargs.setdefault("tool_config", tool_cfg)
        if not cfg_kwargs.get("cached_content"):
            return client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        try:
            stream = client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        except Exception as exc:
            # 要求を開いた時点で拒まれた (SDK が先に送る場合)。まだ何も返していない。
            reason = self._cache_use_error_reason(exc)
            if reason is None:
                raise
            retry_cfg = self._uncached_retry_config(cfg_kwargs, sys_msg, reason, held)
            return client.models.generate_content_stream(
                model=self.model,
                contents=uncached_contents,
                config=types.GenerateContentConfig(**retry_cfg),
            )
        return self._stream_with_cache_fallback(
            stream, client, cfg_kwargs, sys_msg, uncached_contents, held, stream_state,
        )

    def _stream_with_cache_fallback(
        self,
        stream: Any,
        client: genai.Client,
        cfg_kwargs: Dict[str, Any],
        sys_msg: str,
        uncached_contents: list,
        held: List[str],
        stream_state: Optional[Dict[str, Any]],
    ) -> Iterator[Any]:
        """キャッシュ付きのストリームを読み、キャッシュのせいで拒まれたら 1 回だけ送り直す。

        google-genai のストリームは最初の chunk を読むときに要求を送るので、
        キャッシュの失敗もそこで出る。送り直してよいのは、呼び出し元がまだ利用者へ
        何も yield していないとき (``stream_state["emitted"]`` が立っていない) だけ。
        既に出していたら従来どおり例外を上げる (キャッシュは台帳から外す)。
        """
        try:
            for chunk in stream:
                yield chunk
            return
        except Exception as exc:
            reason = self._cache_use_error_reason(exc)
            if reason is None:
                raise
            if stream_state is not None and stream_state.get("emitted"):
                cache_name = cfg_kwargs.get("cached_content")
                logging.warning(
                    "[gemini] cached_content=%s unusable (%s) after output was already "
                    "emitted -> not retrying",
                    cache_name, reason,
                )
                if cache_name:
                    self._drop_unusable_cache(cache_name, reason, held)
                raise
            retry_cfg = self._uncached_retry_config(cfg_kwargs, sys_msg, reason, held)
        yield from client.models.generate_content_stream(
            model=self.model,
            contents=uncached_contents,
            config=types.GenerateContentConfig(**retry_cfg),
        )

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """DEPRECATED: Use generate(messages, tools=[...]) instead.
        
        This method is kept for backward compatibility with existing code.
        It simply delegates to generate() with tools specified.
        """
        import warnings
        warnings.warn(
            "generate_with_tool_detection() is deprecated. Use generate(messages, tools=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        tools_spec = tools or []
        if not tools_spec:
            # If no tools, return text-only format for compatibility
            result = self.generate(messages, temperature=temperature)
            if isinstance(result, str):
                return {"type": "text", "content": result}
            return result
        return self.generate(messages, tools=tools_spec, temperature=temperature)

__all__ = [
    "GeminiClient",
    "GEMINI_SAFETY_CONFIG",
    "merge_tools_for_gemini",
    "GROUNDING_TOOL",
    "genai",
]
