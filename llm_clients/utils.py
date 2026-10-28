"""Utility helpers shared by LLM client implementations."""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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

#: 画像の枚数の上限は既定では無い (None = 枚数では絞らない)。``SAIVERSE_{PROVIDER}_
#: ATTACHMENT_LIMIT`` / ``SAIVERSE_ATTACHMENT_LIMIT`` を明示したときだけ、新しい順に
#: その枚数だけを画像のまま送る (:func:`parse_attachment_limit`)。以前の既定 4 枚は、
#: 新しい画像が来るたびに一番古い画像を要約の文字へ置き換え、キャッシュの前方一致を
#: 毎回割っていた (docs/issues/image_attachment_limit_breaks_cache_prefix.md)。
#: 送る量の歯止めは、枚数ではなくバイトの非常弁 (:func:`select_embedded_attachment_keys`)
#: が持つ。

#: 天井の既定 (バイト)。主語: 1 回の API 送信に埋め込む画像の合計バイト
#: (base64 後の見積もり、:func:`estimate_inline_image_bytes`) の上限。プロバイダの
#: 要求サイズ上限 (Gemini は 1 回の送信で合計約 20MB、本文・音声・動画も同じ枠を
#: 使う) に当たらないための余裕を残した値。画像の合計がこれを超える送信でだけ、
#: 非常弁が古い画像を要約の文字へ置き換える。
DEFAULT_ATTACHMENT_BYTE_CEILING = 12 * 1024 * 1024

#: 床の既定 (バイト)。主語: 非常弁が開いたとき、埋め込む画像の合計バイト (base64
#: 後の見積もり) をどこまで下げておくか。天井ぎりぎりまでしか下げないと、次の
#: 画像 1 枚で再び開いて前方一致がまた割れる。床まで下げて、次に開くまでの間隔を
#: 稼ぐ。
DEFAULT_ATTACHMENT_BYTE_FLOOR = 6 * 1024 * 1024

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


def parse_attachment_limit(provider: str = "") -> Optional[int]:
    """Parse image attachment embed limit from environment variables.

    Checks ``SAIVERSE_{PROVIDER}_ATTACHMENT_LIMIT`` first, then the
    universal ``SAIVERSE_ATTACHMENT_LIMIT`` as fallback.
    Returns ``None`` (no count limit) when neither is set, or when the value
    is not an integer. The byte valve (:func:`select_embedded_attachment_keys`)
    still applies either way.
    """
    limit_str = None
    if provider:
        limit_str = os.getenv(f"SAIVERSE_{provider.upper()}_ATTACHMENT_LIMIT")
    if limit_str is None:
        limit_str = os.getenv("SAIVERSE_ATTACHMENT_LIMIT")
    if limit_str is None:
        return None
    try:
        val = int(limit_str.strip())
        return max(val, 0)
    except ValueError:
        _log.warning("Invalid attachment limit '%s'; ignoring (no count limit)", limit_str)
        return None


def _parse_byte_env(provider: str, suffix: str, default: int) -> int:
    """``SAIVERSE_{PROVIDER}_{suffix}`` → ``SAIVERSE_{suffix}`` → ``default``。

    読めない値 (整数でない・負) は警告して ``default``。
    """
    raw = None
    if provider:
        raw = os.getenv(f"SAIVERSE_{provider.upper()}_{suffix}")
    if raw is None:
        raw = os.getenv(f"SAIVERSE_{suffix}")
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        _log.warning("Invalid %s '%s'; using default %d", suffix, raw, default)
        return default
    if value < 0:
        _log.warning("Negative %s '%s'; using default %d", suffix, raw, default)
        return default
    return value


def parse_attachment_byte_bounds(provider: str = "") -> Tuple[int, int]:
    """非常弁の (天井, 床) をバイトで返す。

    天井 = 1 回の API 送信に埋め込む画像の合計バイト (base64 後の見積もり) の
    上限 (:data:`DEFAULT_ATTACHMENT_BYTE_CEILING`)。床 = 弁が開いたとき、次に
    開くまでの間隔を稼ぐために下げておく水準 (:data:`DEFAULT_ATTACHMENT_BYTE_FLOOR`)。
    ``SAIVERSE_{PROVIDER}_ATTACHMENT_BYTE_CEILING`` → ``SAIVERSE_ATTACHMENT_BYTE_
    CEILING`` (床は ``_FLOOR``) で変えられる。床が天井を超える設定は、床を天井に
    揃える (弁が開いたら天井以下までは必ず下げる)。
    """
    ceiling = _parse_byte_env(provider, "ATTACHMENT_BYTE_CEILING", DEFAULT_ATTACHMENT_BYTE_CEILING)
    floor = _parse_byte_env(provider, "ATTACHMENT_BYTE_FLOOR", DEFAULT_ATTACHMENT_BYTE_FLOOR)
    if floor > ceiling:
        _log.warning(
            "Attachment byte floor %d exceeds ceiling %d; using the ceiling as the floor",
            floor, ceiling,
        )
        floor = ceiling
    return ceiling, floor


# 画像 1 枚の埋め込みの見積もり (base64 後のバイト) を、ファイルの版ごとに覚えておく。
# 非常弁は送る列の画像を毎回すべて測るので、測るたびに読み直す (長辺の縮小は
# 0.3 秒ほどかかる) と、画像の多い会話で毎回の送信が遅くなる。値は整数か None
# (読めなかった) だけなので、件数を多めに持つ。読めなかった結果も覚える — 壊れた
# 画像を毎ターン読み直さないため。ファイルが書き換われば版 (mtime・size) が変わり、
# 測り直す。
_INLINE_IMAGE_BYTES_CACHE: "OrderedDict[Tuple[str, int, int, str, Optional[int]], Optional[int]]" = OrderedDict()
_INLINE_IMAGE_BYTES_CACHE_MAX = 4096
_INLINE_IMAGE_BYTES_CACHE_LOCK = threading.Lock()


def estimate_inline_image_bytes(
    path: Any, mime_type: str, max_bytes: Optional[int] = None,
) -> Optional[int]:
    """画像 1 枚を埋め込んだときの送信上のバイト数の見積もり。

    埋め込みと同じ ``load_image_bytes_for_llm`` (長辺の縮小・``max_bytes`` での
    縮小を含む) で得たバイト列の長さを、base64 で約 4/3 倍になる分だけ増やす
    (``ceil(len * 4 / 3)``)。読めない画像は None (埋め込まれず注記になるので、
    合計に入らない)。
    """
    from saiverse.media_utils import load_image_bytes_for_llm

    file_path = Path(path)
    key: Optional[Tuple[str, int, int, str, Optional[int]]] = None
    try:
        st = file_path.stat()
        key = (str(file_path), st.st_mtime_ns, st.st_size, str(mime_type), max_bytes)
    except OSError:
        key = None
    if key is not None:
        with _INLINE_IMAGE_BYTES_CACHE_LOCK:
            if key in _INLINE_IMAGE_BYTES_CACHE:
                _INLINE_IMAGE_BYTES_CACHE.move_to_end(key)
                return _INLINE_IMAGE_BYTES_CACHE[key]
    data: Optional[bytes]
    try:
        data, _mime = load_image_bytes_for_llm(file_path, mime_type, max_bytes=max_bytes)
    except Exception:
        _log.warning("Failed to measure image for the attachment byte valve: %s", path, exc_info=True)
        data = None
    estimated: Optional[int] = -(-len(data) * 4 // 3) if data else None
    if key is not None:
        with _INLINE_IMAGE_BYTES_CACHE_LOCK:
            _INLINE_IMAGE_BYTES_CACHE[key] = estimated
            while len(_INLINE_IMAGE_BYTES_CACHE) > _INLINE_IMAGE_BYTES_CACHE_MAX:
                _INLINE_IMAGE_BYTES_CACHE.popitem(last=False)
    return estimated


def attachment_positions_to_replace(
    sizes: Sequence[Optional[int]], ceiling: int, floor: int,
) -> List[int]:
    """非常弁: 要約の文字へ置き換える画像の位置 (``sizes`` の番号、昇順)。

    ``sizes`` は埋め込み予定の画像の見積もり (:func:`estimate_inline_image_bytes`)
    を**古い順**に並べたもの (None は 0 として数える)。

    1. それ一枚で天井を超える画像は、弁の手前で必ず置き換える。他の画像の合計
       には最初から入れない — 入れると、その一枚のために古い画像が床まで道連れ
       になる。その画像単体の大きさだけで決まるので、列に何が足されても変わらない。
    2. 残りの画像 (どれも一枚では天井以下) を、古い順に一枚ずつ会話に入ってきた
       と見立てて、入るたびに「埋め込む合計が天井を超えたら、古い側から床以下に
       なるまで置き換える」を繰り返す。2 で置き換えるのは、残りの画像のうち古い
       側の連続した並び。

    だから

    - 埋め込む合計は必ず天井以下になる。
    - 列の後ろに画像が足されただけなら、置き換える画像の集まりは広がるだけで
      縮まない (一度文字にした画像が画像に戻らない)。弁が開かない送信では、
      埋め込む並びが後ろに伸びるだけなので、キャッシュの前方一致は割れない。
      前方一致が割れるのは弁が開いた回だけで、そのとき天井から床までの分を
      まとめて置き換える。
    - 列の状態だけで決まる (覚えておく状態が無い) ので、再起動しても同じ列には
      同じ答えを返す。

    一番新しい画像は、それ一枚で天井を超えるときだけ置き換える (床を超えるだけ
    なら残す)。
    """
    values = [max(int(s or 0), 0) for s in sizes]
    oversized = [pos for pos, size in enumerate(values) if size > ceiling]
    fitting = [pos for pos, size in enumerate(values) if size <= ceiling]
    start = 0
    total = 0
    for j, pos in enumerate(fitting):
        total += values[pos]
        if total <= ceiling:
            continue
        # どの画像も一枚では天井以下なので、床以下まで下げるか、いま入った
        # 一枚だけが残った時点で、合計は天井以下になっている。
        while total > floor and start < j:
            total -= values[fitting[start]]
            start += 1
    return sorted(oversized + fitting[:start])


def _message_metadata_flag(messages: Sequence[Any], idx: int, flag: str) -> bool:
    if idx < 0 or idx >= len(messages):
        return False
    msg = messages[idx]
    if not isinstance(msg, dict):
        return False
    metadata = msg.get("metadata")
    return isinstance(metadata, dict) and bool(metadata.get(flag))


def select_embedded_attachment_keys(
    attachment_cache: Dict[int, list],
    max_embeds: Optional[int],
    exempt_indices: Optional[Set[int]] = None,
    *,
    provider: str,
    messages: Optional[Sequence[Any]] = None,
    embeddable_indices: Optional[Set[int]] = None,
    max_image_bytes: Optional[int] = None,
) -> Optional[Set[Tuple[int, int]]]:
    """埋め込む画像 ``(msg_idx, att_idx)`` を決める (全クライアント共通の入口)。

    1. 枚数の上限 (``max_embeds``、None = 枚数では絞らない) を
       :func:`compute_allowed_attachment_keys` で当てる。
    2. 残った画像のうち、そのクライアントが実際に埋め込むメッセージ
       (``embeddable_indices``、None = 全部) の画像を古い順に並べ、バイトの
       非常弁 (:func:`attachment_positions_to_replace`、天井と床は
       :func:`parse_attachment_byte_bounds` の ``provider``) で置き換えるものを
       外す。

    ``exempt_indices`` (``__visual_context__``) の画像は枚数にも非常弁にも
    数えず、常に埋め込む。``messages`` (``attachment_cache`` と同じ番号の列) を
    渡すと、``__skip_image_summary__`` のメッセージ (saiverse/media_summary.py が
    画像の要約・説明を作るために送る依頼) の画像も、同じく枚数にも非常弁にも
    数えず常に埋め込む — 外すと要約のモデルが画像を見ないまま答え、その答え
    (または「(要約を取得できませんでした)」) が ``.summary.txt`` に保存されて
    残る。``__realtime_context__`` のメッセージの画像は非常弁には数えず、常に
    埋め込む — このメッセージは毎ターン作り直されて末尾近くに置かれるので、
    非常弁に入れると、その画像があるターンだけ弁が開き、次のターンで閉じて
    置き換えた画像が画像に戻る (前方一致が二回割れる)。数えない分は、天井と
    プロバイダの要求サイズ上限の間の余裕が受ける。非常弁は画像だけが対象
    (音声・動画は数えない)。
    リアルタイム情報と自動想起を残すモデルの列 (:func:`attachment_limit_is_off`)
    でも非常弁は効く — プロバイダの要求サイズ上限に当たると送信そのものが
    失敗するので、稀な一回のまとめ置き換えを優先する
    (docs/intent/persist_realtime_and_recall.md §4.2)。

    返り値は ``compute_allowed_attachment_keys`` と同じ形: None は「全部埋め込む」。
    """
    exempt: Set[int] = set(exempt_indices or ())
    if messages is not None:
        exempt.update(
            idx for idx in attachment_cache
            if _message_metadata_flag(messages, idx, "__skip_image_summary__")
        )
    allowed = compute_allowed_attachment_keys(attachment_cache, max_embeds, exempt)
    if not attachment_cache:
        return allowed
    if embeddable_indices is not None and not embeddable_indices:
        return allowed
    candidates: List[Tuple[int, int]] = []
    for msg_idx in sorted(attachment_cache.keys()):
        if msg_idx in exempt:
            continue
        if embeddable_indices is not None and msg_idx not in embeddable_indices:
            continue
        if messages is not None and _message_metadata_flag(
            messages, msg_idx, "__realtime_context__",
        ):
            continue
        for att_idx in range(len(attachment_cache[msg_idx])):
            key = (msg_idx, att_idx)
            if allowed is None or key in allowed:
                candidates.append(key)
    if not candidates:
        return allowed

    ceiling, floor = parse_attachment_byte_bounds(provider)
    sizes: List[Optional[int]] = []
    for msg_idx, att_idx in candidates:
        att = attachment_cache[msg_idx][att_idx]
        sizes.append(
            estimate_inline_image_bytes(att["path"], att["mime_type"], max_bytes=max_image_bytes)
        )
    positions = attachment_positions_to_replace(sizes, ceiling, floor)
    if not positions:
        return allowed

    replaced = {candidates[pos] for pos in positions}
    replaced_positions = set(positions)
    kept_bytes = sum(
        int(s or 0) for pos, s in enumerate(sizes) if pos not in replaced_positions
    )
    _log.info(
        "[attachment byte valve] provider=%s replaced %d image(s) with summaries "
        "(embedded images: %d, ~%d bytes after base64; ceiling=%d floor=%d)",
        provider or "-", len(replaced), len(candidates) - len(replaced),
        kept_bytes, ceiling, floor,
    )
    if allowed is None:
        allowed = {
            (msg_idx, att_idx)
            for msg_idx, items in attachment_cache.items()
            for att_idx in range(len(items))
        }
    return allowed - replaced


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
