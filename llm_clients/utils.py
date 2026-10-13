"""Utility helpers shared by LLM client implementations."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

_log = logging.getLogger(__name__)

#: 履歴の中の「最後の AI の返事」に付ける内部印 (message の ``metadata`` のキー)。
#: 継続キャッシュ (高速応答モード) のキャッシュの境目を、SEA から LLM クライアントへ
#: 伝えるためのもの (docs/intent/cache_lifecycle_control.md §3.5)。各クライアントは
#: metadata を API へ送らないので、この印が payload に載ることはない。印を読むのは
#: Gemini クライアントだけで、他のクライアントは素通しする。
CACHE_BOUNDARY_METADATA_KEY = "__cache_boundary__"


def content_to_text(content: Any) -> str:
    """Extract plain text from SDK message payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("content")
            if text:
                texts.append(str(text))
        return "".join(texts)
    return ""


def obj_to_dict(obj: Any) -> Any:
    """Best-effort conversion to plain dict for SDK objects."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return obj.model_dump()
        except Exception:
            _log.warning("obj_to_dict: model_dump() failed for %s", type(obj).__name__, exc_info=True)
    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        try:
            return obj.to_dict()
        except Exception:
            _log.warning("obj_to_dict: to_dict() failed for %s", type(obj).__name__, exc_info=True)
    return obj


def is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def positive_token_count(value: Any) -> Optional[int]:
    """設定やパラメータに書かれたトークン数を、正の整数として読む。

    読めない値 (None・真偽値・数字でない文字列) と 0 以下は None。応答の上限を
    答える ``LLMClient.response_token_limit`` の実装が使う。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def merge_reasoning_strings(chunks: List[str]) -> List[Dict[str, str]]:
    if not chunks:
        return []
    text = "".join(chunks).strip()
    if not text:
        return []
    return [{"title": "", "text": text}]


# ---------------------------------------------------------------------------
# Image attachment helpers (shared across providers)
# ---------------------------------------------------------------------------

DEFAULT_ATTACHMENT_LIMIT = 4

#: この列には画像の枚数の上限を使わない、という列ごとの印 (metadata のキー)。
#: 列の中のどれか一つのメッセージが持てば、その列の画像はすべて画像のまま送る。
#: 付けるのは、リアルタイム情報と自動想起を残して送り続けるモデルの列
#: (docs/intent/persist_realtime_and_recall.md §4.2 — sea/runtime_context.
#: prepare_context が、組んだときのモデルで判定して列の先頭のメッセージに付ける)。
#: 上限があると、新しい画像が来るたびに一番古い画像が文字に置き換わって前回の
#: 送信の中身が書き換わり、「送る中身は毎ターン後ろに足されるだけ」という
#: その設定の約束と根本でぶつかる。印はメッセージ一つ一つではなく列に付ける —
#: 画像を運ぶメッセージ (ユーザー・知覚・ツール・スペル…) ごとに印を付ける形は、
#: 印の付け漏れが同じ型の欠陥として何度も出たため。
ATTACHMENT_LIMIT_OFF_KEY = "__attachment_limit_off__"


def attachment_limit_is_off(messages: Any) -> bool:
    """この列で画像の枚数の上限を使わないか (全プロバイダと送る量の見積もりで共通)。

    列のどれか一つのメッセージが :data:`ATTACHMENT_LIMIT_OFF_KEY` を持てば真。
    各クライアントは system のメッセージを抜き出す**前の**列でこれを判定する
    (印は列の先頭 = たいてい system のメッセージにあるため)。
    """
    if not isinstance(messages, (list, tuple)):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        metadata = msg.get("metadata")
        if isinstance(metadata, dict) and metadata.get(ATTACHMENT_LIMIT_OFF_KEY):
            return True
    return False


def mark_attachment_limit_off(messages: List[Dict[str, Any]]) -> None:
    """列に「画像の枚数の上限を使わない」の印を付ける (先頭のメッセージに、写しで)。

    元の dict は書き換えず、先頭の 1 件だけを複製して差し替える (履歴の dict は
    他の経路と共有されうる — mark_cache_boundary と同じ扱い)。空の列には何もしない。
    印は metadata にあるだけで、どのクライアントも API へは送らない。
    """
    if not messages or not isinstance(messages[0], dict):
        return
    first = messages[0]
    metadata = first.get("metadata")
    new_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    new_metadata[ATTACHMENT_LIMIT_OFF_KEY] = True
    messages[0] = {**first, "metadata": new_metadata}


def parse_attachment_limit(provider: str = "") -> int:
    """Parse image attachment embed limit from environment variables.

    Checks ``SAIVERSE_{PROVIDER}_ATTACHMENT_LIMIT`` first, then the
    universal ``SAIVERSE_ATTACHMENT_LIMIT`` as fallback.
    Returns ``DEFAULT_ATTACHMENT_LIMIT`` (4) when no limit is configured.
    """
    limit_str = None
    if provider:
        limit_str = os.getenv(f"SAIVERSE_{provider.upper()}_ATTACHMENT_LIMIT")
    if limit_str is None:
        limit_str = os.getenv("SAIVERSE_ATTACHMENT_LIMIT")
    if limit_str is None:
        return DEFAULT_ATTACHMENT_LIMIT
    try:
        val = int(limit_str.strip())
        return max(val, 0)
    except ValueError:
        _log.warning("Invalid attachment limit '%s'; ignoring", limit_str)
        return DEFAULT_ATTACHMENT_LIMIT


def compute_allowed_attachment_keys(
    attachment_cache: Dict[int, list],
    max_embeds: Optional[int],
    exempt_indices: Optional[Set[int]] = None,
) -> Optional[Set[Tuple[int, int]]]:
    """Determine which ``(msg_idx, att_idx)`` pairs should be embedded.

    Selects the *max_embeds* most-recent attachments (by message index,
    most recent first).  Attachments in *exempt_indices* (e.g.
    ``__visual_context__`` messages) are always allowed and do not count
    towards the limit.

    Returns ``None`` when *max_embeds* is ``None`` (unlimited).
    """
    if max_embeds is None:
        return None
    if not attachment_cache:
        return set()

    exempt = exempt_indices or set()
    ordered: List[Tuple[int, int]] = []
    for msg_idx in sorted(attachment_cache.keys(), reverse=True):
        if msg_idx in exempt:
            continue
        for att_idx in range(len(attachment_cache[msg_idx])):
            ordered.append((msg_idx, att_idx))

    allowed: Set[Tuple[int, int]] = set(ordered[:max_embeds]) if max_embeds > 0 else set()

    # Exempt attachments are always allowed
    for msg_idx in exempt:
        if msg_idx in attachment_cache:
            for att_idx in range(len(attachment_cache[msg_idx])):
                allowed.add((msg_idx, att_idx))
            _log.debug(
                "visual context at idx=%d exempted from attachment limit (%d images)",
                msg_idx,
                len(attachment_cache[msg_idx]),
            )

    return allowed


def image_summary_note(
    path: str,
    mime_type: str,
    uri: str,
    skip_summary: bool = False,
) -> str:
    """Build a text note for an image that will not be embedded as binary.

    Tries ``ensure_image_summary()`` for a meaningful description.
    Falls back to a bare ``[画像: {uri}]`` reference with a warning
    when the summary is unavailable.
    """
    if not skip_summary:
        try:
            from pathlib import Path as _Path

            from saiverse.media_summary import ensure_image_summary

            summary = ensure_image_summary(_Path(path), mime_type)
            if summary:
                return f"[画像: {uri}] {summary}"
        except Exception:
            _log.exception("Image summary generation error for %s", path)
        _log.warning(
            "Image summary unavailable for %s; using URI-only reference",
            path,
        )
    return f"[画像: {uri}]"


def audio_summary_note(
    path: str,
    mime_type: str,
    uri: str,
    skip_summary: bool = False,
) -> str:
    """Build a text note for an audio file that will not be embedded as binary.

    Used by LLM clients that do not support audio inline_data (Anthropic, OpenAI, etc.).
    """
    if not skip_summary:
        try:
            from pathlib import Path as _Path

            from saiverse.media_summary import ensure_audio_summary

            summary = ensure_audio_summary(_Path(path), mime_type)
            if summary:
                return f"[音声: {uri}] {summary}"
        except Exception:
            _log.exception("Audio summary generation error for %s", path)
        _log.warning(
            "Audio summary unavailable for %s; using URI-only reference", path,
        )
    return f"[音声: {uri}]"


def video_summary_note(
    path: str,
    mime_type: str,
    uri: str,
    skip_summary: bool = False,
) -> str:
    """Build a text note for a video file that will not be embedded as binary."""
    if not skip_summary:
        try:
            from pathlib import Path as _Path

            from saiverse.media_summary import ensure_video_summary

            summary = ensure_video_summary(_Path(path), mime_type)
            if summary:
                return f"[動画: {uri}] {summary}"
        except Exception:
            _log.exception("Video summary generation error for %s", path)
        _log.warning(
            "Video summary unavailable for %s; using URI-only reference", path,
        )
    return f"[動画: {uri}]"


def inject_audio_video_summaries(
    metadata: Any,
    text: str,
    skip_audio: bool = False,
    skip_video: bool = False,
) -> str:
    """Append audio/video summary notes to a text string based on metadata.media[].

    This is the common fallback path for LLM clients that do not natively support
    audio or video inline data. Returns the original text with `[音声: ...] <summary>`
    and `[動画: ...] <summary>` lines appended for each audio/video media descriptor.
    """
    from saiverse.media_utils import iter_audio_media, iter_video_media

    result = text or ""
    for att in iter_audio_media(metadata):
        note = audio_summary_note(
            str(att["path"]), att["mime_type"], att["uri"], skip_summary=skip_audio,
        )
        result = f"{result}\n{note}" if result else note
    for att in iter_video_media(metadata):
        note = video_summary_note(
            str(att["path"]), att["mime_type"], att["uri"], skip_summary=skip_video,
        )
        result = f"{result}\n{note}" if result else note
    return result
