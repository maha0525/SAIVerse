"""会話の途中のシステム通知を、送り先のモデルが受け取れる形にする (クライアント層の変換)。

docs/intent/system_notice_delivery.md「クライアント層の変換と合言葉」・不変条件 3, 5, 6。

SAIVerse 層が組んだ messages 列には、head (プロンプト先頭部) の system メッセージ
(metadata に ``HEAD_MESSAGE_KEY``) と、会話の途中の role=system のシステム通知が
並ぶ。各 LLM クライアントは送る形を組む前に :func:`present_system_notices` を
通し、

- head の system メッセージ: 本文の仮置き文字列
  (``SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER``) を実際の合言葉に置き換える。
- head より後ろの role=system (と ``host``): 途中の system を受け取れるモデル
  (:func:`accepts_mid_conversation_system`) へはそのまま、受け取れないモデルへは
  元の位置で ``<system pass="合言葉">本文</system>`` の role=user に変える
  (画像などの metadata.media は引き継ぎ、写しの metadata に通知の印
  ``SYSTEM_NOTICE_KEY`` を必ず付ける)。
- それ以外のメッセージ: 本文と ``tool_calls`` の引数と、metadata の
  ``reasoning_details`` (前のターンの思考。OpenAI 互換のクライアントが次の
  リクエストへ送り返す) の思考の文に現れた合言葉を伏せる (モデルが合言葉を
  復唱して記録に残った場合の再利用を防ぐ)。本文が部品の並びなら、部品を
  またいで連結した文字列で伏せる。

head の印を持つメッセージが一つも無い列 (head を組まない要約・判定などの呼び出し)
は何も変えずに返す — その列の先頭の system は呼び出し側の指示で、各クライアントの
従来の扱い (先頭の system 欄へまとめる・OpenAI 互換の ``convert_system_to_user``)
のまま送る。

変換はどれも決定的 (同じ入力・同じ合言葉に同じ出力) — 同じ head を使い続けて
いる間、同じ記録から組む送信本文は一字も変わらない (不変条件 6)。
"""
from __future__ import annotations

import logging
import re
import secrets
import string
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set

from sai_memory.memory.storage import (
    HEAD_MESSAGE_KEY,
    SYSTEM_NOTICE_KEY,
    SYSTEM_NOTICE_PASSPHRASE_KEY,
    SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER,
)

_log = logging.getLogger(__name__)

#: 合言葉の長さと文字種 (英大小文字と数字)。
PASSPHRASE_LENGTH = 12
_PASSPHRASE_ALPHABET = string.ascii_letters + string.digits

#: 会話の途中の role=system をそのまま受け取れる protocol。
_DIRECT_PROTOCOLS = frozenset({"xai_native"})
#: モデル設定の ``convert_system_to_user`` で決まる protocol (真 = 受け取れない)。
_FLAG_PROTOCOLS = frozenset({"openai_compat", "nvidia_nim"})
# それ以外 (anthropic_native / gemini_native / openai_codex は API の仕様上、途中に
# system を置けない。ollama_compat はチャットテンプレート次第で途中の system が
# 落ちうる。未知の protocol も含む) は受け取れない側 = タグで囲む側に倒す。

_SYSTEM_TAG_RE = re.compile(r"<(/?)(system)", re.IGNORECASE)


def generate_passphrase() -> str:
    """head を作り直すときの合言葉 (英数 :data:`PASSPHRASE_LENGTH` 文字)。"""
    return "".join(
        secrets.choice(_PASSPHRASE_ALPHABET) for _ in range(PASSPHRASE_LENGTH)
    )


def accepts_mid_conversation_system(protocol: str, config: Optional[Dict[str, Any]]) -> bool:
    """その protocol とモデル設定のモデルが、会話の途中の role=system を受け取れるか。

    クライアント層 (各クライアントの変換) と head_pipeline (head に置く説明文の
    出し分け) の両方がこの一つの判定を使う — 説明文と実際の届き方が食い違わない
    ようにするため。
    """
    if protocol in _DIRECT_PROTOCOLS:
        return True
    if protocol in _FLAG_PROTOCOLS:
        return not (
            isinstance(config, dict) and config.get("convert_system_to_user") is True
        )
    return False


def model_accepts_mid_conversation_system(model_key: str) -> bool:
    """モデル設定キー ``model_key`` のモデルについての :func:`accepts_mid_conversation_system`。

    protocol の読み方は llm_clients/factory.py の get_llm_client と同じ
    (``resolve_protocol``。provider の既定は saiverse.model_configs.get_model_provider
    と同じ ``"ollama"``)。
    """
    from llm_clients.factory import resolve_protocol
    from saiverse.model_configs import get_model_config

    config = get_model_config(model_key) or {}
    provider = str(config.get("provider", "ollama"))
    return accepts_mid_conversation_system(resolve_protocol(provider, config), config)


def _metadata(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    meta = msg.get("metadata")
    return meta if isinstance(meta, dict) else None


def is_head_system_message(msg: Any) -> bool:
    """head が組んだ system メッセージか (metadata の ``HEAD_MESSAGE_KEY``)。"""
    if not isinstance(msg, dict):
        return False
    meta = _metadata(msg)
    return bool(meta and meta.get(HEAD_MESSAGE_KEY))


def _head_index(messages: List[Any]) -> Optional[int]:
    for idx, msg in enumerate(messages):
        if is_head_system_message(msg):
            return idx
    return None


def system_notice_passphrase(messages: List[Any]) -> str:
    """列の head の system メッセージが運んでいる合言葉 (無ければ空文字)。"""
    idx = _head_index(messages)
    if idx is None:
        return ""
    meta = _metadata(messages[idx]) or {}
    value = meta.get(SYSTEM_NOTICE_PASSPHRASE_KEY)
    return value if isinstance(value, str) else ""


def system_notice_indices(messages: List[Any]) -> Set[int]:
    """列の中の「会話の途中のシステム通知」の位置。

    head の system メッセージより後ろにある、head の印を持たない role=system /
    ``host`` のメッセージ。head の印を持つメッセージが無い列では空。
    """
    head = _head_index(messages)
    if head is None:
        return set()
    out: Set[int] = set()
    for idx in range(head + 1, len(messages)):
        msg = messages[idx]
        if not isinstance(msg, dict) or is_head_system_message(msg):
            continue
        role = msg.get("role")
        if isinstance(role, str) and role.lower() in ("system", "host"):
            out.add(idx)
    return out


def redact_passphrase(text: str, passphrase: str) -> str:
    """ログへ書く文字列から合言葉を伏せる (合言葉が空なら何もしない)。"""
    if not passphrase or not isinstance(text, str):
        return text
    return text.replace(passphrase, "*" * len(passphrase))


#: このプロセスで送る形に差し込んだ合言葉と、最後に差し込んだ時刻
#: (``time.monotonic()``。最後に使ったものほど後ろ)。ログの書き出し
#: (saiverse/logging_config.py のハンドラの filter) が、どのクライアントのどの
#: ログ行 (送る形・応答・例外の文) からもこれを伏せる — 応答に復唱された合言葉は
#: 応答を書く側では見分けられないため (不変条件 3)。件数が上限を超えたら、最後の
#: 使用が古いものから忘れる (head の作り直しで合言葉は入れ替わる)。ただし最後の
#: 使用から :data:`_KNOWN_PASSPHRASES_KEEP_SECONDS` 以内のものは上限を超えても
#: 忘れない — 長いリクエストの応答を待つ間に他の合言葉が上限まで回っても、
#: 応答待ちの合言葉が保護から外れないように。
_KNOWN_PASSPHRASES: "OrderedDict[str, float]" = OrderedDict()
# 同時に生きる head ((persona_id, model_key) ごとに一つ) の数を大きく超える余裕を
# 取る。1 件 12 文字なので、この数でも記憶量は無視できる。
_KNOWN_PASSPHRASES_LIMIT = 4096
_KNOWN_PASSPHRASES_KEEP_SECONDS = 3600.0
_KNOWN_PASSPHRASES_LOCK = threading.Lock()


def _remember_passphrase(passphrase: str) -> None:
    if not passphrase:
        return
    now = time.monotonic()
    with _KNOWN_PASSPHRASES_LOCK:
        _KNOWN_PASSPHRASES[passphrase] = now
        _KNOWN_PASSPHRASES.move_to_end(passphrase)
        while len(_KNOWN_PASSPHRASES) > _KNOWN_PASSPHRASES_LIMIT:
            oldest, last_used = next(iter(_KNOWN_PASSPHRASES.items()))
            # 先頭が最後の使用の最も古いもの。それが保持の期間内なら、残りも全部
            # 期間内なので、上限を超えたままにする。
            if now - last_used < _KNOWN_PASSPHRASES_KEEP_SECONDS:
                break
            del _KNOWN_PASSPHRASES[oldest]


def redact_known_passphrases(text: str) -> str:
    """このプロセスで送る形に差し込んだどの合言葉も ``text`` から伏せる。"""
    if not isinstance(text, str) or not _KNOWN_PASSPHRASES:
        return text
    with _KNOWN_PASSPHRASES_LOCK:
        known = tuple(_KNOWN_PASSPHRASES)
    for passphrase in known:
        if passphrase in text:
            text = redact_passphrase(text, passphrase)
    return text


def _neutralize_system_tags(text: str) -> str:
    """本文の中の ``<system`` / ``</system`` を全角の ``＜`` で始まる文字列にする。"""
    return _SYSTEM_TAG_RE.sub(lambda m: "＜" + m.group(1) + m.group(2), text)


def _map_text(content: Any, fn) -> Any:
    """本文 (文字列、または text 部品の並び) の文字の部分に ``fn`` を当てた写し。"""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part = {**part, "text": fn(part["text"])}
            out.append(part)
        return out
    return content


def _text_part_key(part: Any) -> Optional[str]:
    """部品の並びの中で文字を持つ欄の名前 (llm_clients/utils.content_to_text と同じ読み方)。"""
    if not isinstance(part, dict):
        return None
    for key in ("text", "content"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return key
    return None


def _map_joined_text(content: Any, fn) -> Any:
    """本文の文字の部分を、部品をまたいで連結した一続きの文字列として ``fn`` に通した写し。

    ``fn`` は文字数を変えない置換 (伏せ字・``<`` の全角化) に限る。本文が部品の
    並びのとき、各クライアントは文字の部品を連結して送る (content_to_text は
    画像などをまたいでも連結する)。部品ごとに置換すると、合言葉や ``<system`` を
    二つの部品に割った本文が素通りし、連結で元に戻る。連結した文字列で置換し、
    同じ長さで元の部品へ切り戻すので、部品の数・順序と文字以外の部品は変わらない。
    """
    if not isinstance(content, list):
        return _map_text(content, fn)
    slots = [(idx, key) for idx, part in enumerate(content) if (key := _text_part_key(part))]
    if not slots:
        return list(content)
    joined = "".join(content[idx][key] for idx, key in slots)
    mapped = fn(joined)
    if len(mapped) != len(joined):
        raise ValueError("_map_joined_text needs a length-preserving replacement")
    out = list(content)
    pos = 0
    for idx, key in slots:
        length = len(content[idx][key])
        out[idx] = {**content[idx], key: mapped[pos:pos + length]}
        pos += length
    return out


def _redact_tool_call_arguments(tool_calls: Any, passphrase: str) -> Any:
    """OpenAI 形式の ``tool_calls`` の各 ``function.arguments`` (文字列) から合言葉を伏せた写し。"""
    if not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for call in tool_calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
            call = {**call, "function": {**fn, "arguments": redact_passphrase(fn["arguments"], passphrase)}}
        out.append(call)
    return out


#: 思考の送り返し (metadata の ``reasoning_details`` — OpenAI 互換のクライアントが
#: ``reasoning_passback_field`` で次のリクエストへ同梱する) の各項目のうち、
#: 思考の文を持つ欄 (llm_clients/openai_reasoning.merge_streaming_reasoning_details
#: が連結する欄と同じ)。署名や暗号化された欄は伏せない (書き換えると送り返しが壊れる)。
_REASONING_TEXT_KEYS = ("text", "summary")


def _redact_reasoning_details(details: Any, passphrase: str) -> Any:
    """``reasoning_details`` の各項目の思考の文から合言葉を伏せた写し。"""
    if not isinstance(details, list):
        return details
    out = []
    for item in details:
        if isinstance(item, dict):
            redacted = {
                key: redact_passphrase(item[key], passphrase)
                for key in _REASONING_TEXT_KEYS
                if isinstance(item.get(key), str)
            }
            if redacted:
                item = {**item, **redacted}
        out.append(item)
    return out


def _content_text(content: Any) -> str:
    from .utils import content_to_text

    return content_to_text(content or "")


def _without_passphrase(msg: Dict[str, Any]) -> Dict[str, Any]:
    meta = _metadata(msg)
    if meta is None or SYSTEM_NOTICE_PASSPHRASE_KEY not in meta:
        return dict(msg)
    new_meta = {k: v for k, v in meta.items() if k != SYSTEM_NOTICE_PASSPHRASE_KEY}
    return {**msg, "metadata": new_meta}


def _stored_media_summary(att: Dict[str, Any], metadata: Any) -> str:
    """記録の metadata (``media`` / ``images`` の項目の ``summary``) に保存済みの画像・音声・動画の要約。"""
    if not isinstance(metadata, dict):
        return ""
    from pathlib import Path

    uri = att.get("uri")
    path = str(att.get("path") or "")
    for key in ("media", "images"):
        entries = metadata.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            same_uri = bool(uri) and entry.get("uri") == uri
            same_path = bool(entry.get("path")) and str(Path(entry["path"])) == path
            if not (same_uri or same_path):
                continue
            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
    return ""


#: :func:`system_notice_media_note` の種類ごとの見出し (llm_clients/utils の
#: image_summary_note / audio_summary_note / video_summary_note と同じ語)。
_MEDIA_NOTE_LABELS = {"image": "画像", "audio": "音声", "video": "動画"}


def system_notice_media_note(
    att: Dict[str, Any], metadata: Any, kind: str, *, passphrase: str,
) -> str:
    """システム通知の画像・音声・動画を文字で届けるときの説明の文 (決定的)。

    ``kind`` は ``"image"`` / ``"audio"`` / ``"video"``。``att`` は
    saiverse/media_utils の iter_image_media / iter_audio_media / iter_video_media
    の項目か、:func:`system_notice_image_entries` の項目。system のまま届ける通知は
    文字しか持てず、タグ化した通知でも埋め込めない (非対応のモデル・枚数の上限の
    外・ファイルが読めない) ときは説明の文になる。記録の metadata に保存済みの
    要約があればそれを使い、無ければ ``[画像: <uri>]`` (音声・動画も同じ形) だけに
    する — その場で要約を生成する llm_clients/utils の image_summary_note などを
    使うと、生成の成否で同じ記録から組む送信本文が変わる (不変条件 6)。

    system のまま届ける通知では、この文は合言葉の変換 (:func:`present_system_notices`)
    の後で本文へ足されるので、要約の文に現れた合言葉はここで伏せ、``<system`` も
    無害化する。伏せるのは ``passphrase`` (その送信の head の合言葉 —
    :func:`system_notice_passphrase`) だけ。プロセスが覚えている合言葉の全部で
    伏せると、別の head の合言葉が登録されただけで同じ記録から組む送信本文が
    変わる (不変条件 6)。
    """
    label = _MEDIA_NOTE_LABELS[kind]
    uri = att.get("uri") or str(att.get("path") or "unknown")
    summary = _stored_media_summary(att, metadata)
    if summary:
        summary = redact_passphrase(_neutralize_system_tags(summary), passphrase)
    return f"[{label}: {uri}] {summary}" if summary else f"[{label}: {uri}]"


def system_notice_image_note(att: Dict[str, Any], metadata: Any, *, passphrase: str) -> str:
    """システム通知の画像の説明の文 (:func:`system_notice_media_note` の画像版)。"""
    return system_notice_media_note(att, metadata, "image", passphrase=passphrase)


def system_notice_media_entries(metadata: Any, kind: str) -> List[Dict[str, Any]]:
    """記録の metadata にある画像・音声・動画の項目を、ファイルの有無を問わず並べる (``uri`` と ``path``)。

    ``kind`` は ``"image"`` / ``"audio"`` / ``"video"``。読み方は saiverse/media_utils の
    iter_image_media / iter_audio_media / iter_video_media と同じ (``media`` は種類で
    絞り、画像だけは ``images`` も全部画像とみなして読む) だが、ファイルを探さない。
    iter_*_media は読めないファイルの項目を返さないので、それだけを頼ると画像・
    音声・動画だけの通知が本文の空な通知として消える。``images`` の項目が
    ``media`` の項目と同じ URI・パスなら重ねて返さない。
    """
    if not isinstance(metadata, dict):
        return []
    candidates: List[Any] = []
    media = metadata.get("media")
    if isinstance(media, list):
        for item in media:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            item_mime = str(item.get("mime_type") or "").lower()
            if item_type:
                if item_type != kind:
                    continue
            elif not item_mime.startswith(f"{kind}/"):
                continue
            candidates.append(item)
    images = metadata.get("images") if kind == "image" else None
    if isinstance(images, list):
        candidates.extend(item for item in images if isinstance(item, dict))
    from pathlib import Path

    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in candidates:
        uri = item.get("uri") if isinstance(item.get("uri"), str) else None
        path = item.get("path")
        # iter_image_media の項目の path (Path) と同じ書き方に揃える
        path_s = str(Path(path)) if isinstance(path, (str, Path)) and str(path) else ""
        if not uri and not path_s:
            continue
        key = _media_entry_key({"uri": uri, "path": path_s})
        if key in seen:
            continue
        seen.add(key)
        out.append({"uri": uri, "path": path_s})
    return out


def system_notice_image_entries(metadata: Any) -> List[Dict[str, Any]]:
    """記録の metadata にある画像の項目 (:func:`system_notice_media_entries` の画像版)。"""
    return system_notice_media_entries(metadata, "image")


def _media_entry_key(att: Dict[str, Any]) -> str:
    """画像・音声・動画の項目を同定する文字列 (iter_*_media の項目の ``uri`` 欄と同じ作り方)。"""
    from pathlib import Path

    uri = att.get("uri")
    if uri:
        return str(uri)
    path = att.get("path")
    return str(Path(path)) if path else ""


def unresolved_system_notice_media(
    metadata: Any, attachments: List[Dict[str, Any]], kind: str,
) -> List[Dict[str, Any]]:
    """metadata の ``kind`` の項目のうち、iter_*_media の結果 ``attachments`` に無いもの (ファイルが読めない項目)。"""
    resolved = {_media_entry_key(att) for att in attachments}
    return [
        entry for entry in system_notice_media_entries(metadata, kind)
        if _media_entry_key(entry) not in resolved
    ]


def unresolved_system_notice_images(
    metadata: Any, attachments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """metadata の画像のうち、iter_image_media の結果 ``attachments`` に無いもの (ファイルが読めない画像)。"""
    return unresolved_system_notice_media(metadata, attachments, "image")


def with_system_notice_image_notes(text: str, metadata: Any, *, passphrase: str) -> str:
    """system のまま届ける通知の本文 ``text`` の後ろに、metadata の各画像の説明を一行ずつ足す。

    画像はファイルの有無を問わず metadata から並べる (:func:`system_notice_image_entries`)
    — system メッセージは画像を持てないので、どの画像も説明の文になり、ファイルが
    消えていても通知ごと落ちない。``passphrase`` はその送信の head の合言葉
    (:func:`system_notice_media_note`)。
    """
    for att in system_notice_image_entries(metadata):
        note = system_notice_image_note(att, metadata, passphrase=passphrase)
        text = f"{text}\n{note}" if text else note
    return text


def is_presented_system_notice(msg: Any) -> bool:
    """:func:`present_system_notices` を通した列の、会話の途中のシステム通知か。

    タグ化した通知は role=user になるので、role ではなく metadata の
    ``SYSTEM_NOTICE_KEY`` で見分ける (:func:`present_system_notices` が通知の
    写しに必ず付ける)。画像を文字に落とす分岐で、通知には生成した要約ではなく
    :func:`system_notice_image_note` を使うための判定。
    """
    if not isinstance(msg, dict):
        return False
    meta = _metadata(msg)
    return bool(meta and meta.get(SYSTEM_NOTICE_KEY) and not meta.get(HEAD_MESSAGE_KEY))


def tag_system_notice(body: str, passphrase: str) -> str:
    """システム通知の本文を、合言葉付きタグで囲んだ role=user の本文にする。

    本文に引用で入った ``<system`` / ``</system`` は無害化し、本文に現れた合言葉は
    伏せる (どちらも決定的な置換)。合言葉が空なら pass 属性を付けない (head の
    合言葉が無い列 — 正常な head では起きない)。
    """
    text = redact_passphrase(_neutralize_system_tags(body or ""), passphrase)
    if passphrase:
        return f'<system pass="{passphrase}">\n{text}\n</system>'
    return f"<system>\n{text}\n</system>"


#: タグ化した通知の写しの metadata に置く、タグで包む前の本文
#: (:func:`present_system_notices` の ``keep_notice_body``)。記録には保存されない
#: 写しだけに付く。
_NOTICE_BODY_KEY = "__system_notice_body__"


def tag_presented_system_notice(metadata: Any, notes: List[str], passphrase: str) -> str:
    """タグ化した通知の写しの最終本文: 元の本文に説明の文を足した全体をタグで包む。

    ``metadata`` はその写しの metadata。画像・音声・動画を埋め込めないときの説明の
    文 (:func:`system_notice_media_note`) は通知の本文の一部なので、閉じタグの
    後ろではなくタグの中へ入れる — 閉じタグの後ろに足すと、画像だけの通知では
    証明されたタグの中身が空で、説明は合言葉の外の普通の user の文章になる。
    ``notes`` が空なら :func:`present_system_notices` が組んだ本文と一字一致する。
    写しは ``keep_notice_body=True`` で :func:`present_system_notices` を通した
    ものでなければならない (無ければ例外 — タグの外へ説明を足す形へ黙って戻らない)。
    """
    meta = metadata if isinstance(metadata, dict) else {}
    if _NOTICE_BODY_KEY not in meta:
        raise ValueError(
            "tag_presented_system_notice needs a notice presented with keep_notice_body=True",
        )
    body = meta[_NOTICE_BODY_KEY]
    for note in notes:
        body = f"{body}\n{note}" if body else note
    return tag_system_notice(body, passphrase)


def present_system_notices(
    messages: List[Any], *, accepts_mid_system: bool, keep_notice_body: bool = False,
) -> List[Any]:
    """messages 列を、送り先のモデルへ送る直前の形にする (新しい列を返す。入力は変えない)。

    ``accepts_mid_system`` はそのモデルが会話の途中の role=system を受け取れるか
    (:func:`accepts_mid_conversation_system`)。規則はモジュールの説明を参照。

    ``keep_notice_body`` が真なら、タグ化した通知の写しの metadata に、タグで包む
    前の本文を残す。画像などの説明の文を足してからタグで包み直すクライアント
    (:func:`tag_presented_system_notice`) が使う。metadata ごと送るクライアント
    (Ollama) では、本文の写しがリクエストに載らないよう偽のまま使う。
    """
    head = _head_index(messages)
    if head is None:
        return list(messages)
    passphrase = system_notice_passphrase(messages)
    if not passphrase:
        _log.warning(
            "[system_notice] head message carries no passphrase; notices are tagged "
            "without one",
        )
    _remember_passphrase(passphrase)
    notices = system_notice_indices(messages)

    out: List[Any] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        if idx == head or is_head_system_message(msg):
            new_msg = _without_passphrase(msg)
            if passphrase:
                new_msg["content"] = _map_text(
                    msg.get("content"),
                    lambda t: t.replace(SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER, passphrase),
                )
            out.append(new_msg)
            continue
        if idx in notices:
            new_msg = _without_passphrase(msg)
            # 通知の写しには必ず通知の印を付ける (タグ化すると role では見分けられ
            # ない。is_presented_system_notice)。記録には保存されない写し。
            notice_meta = dict(_metadata(new_msg) or {})
            notice_meta.setdefault(SYSTEM_NOTICE_KEY, str(msg.get("role")).lower())
            new_msg["metadata"] = notice_meta
            if accepts_mid_system:
                new_msg["role"] = "system"
                new_msg["content"] = _map_joined_text(
                    msg.get("content"), lambda t: redact_passphrase(t, passphrase),
                )
            else:
                body = _content_text(msg.get("content"))
                new_msg["role"] = "user"
                new_msg["content"] = tag_system_notice(body, passphrase)
                if keep_notice_body:
                    notice_meta[_NOTICE_BODY_KEY] = body
            out.append(new_msg)
            continue
        new_msg = _without_passphrase(msg)
        if passphrase and "content" in msg:
            # 部品をまたいで連結した文字列で伏せる (_map_joined_text)
            new_msg["content"] = _map_joined_text(
                msg.get("content"), lambda t: redact_passphrase(t, passphrase),
            )
        if passphrase and "tool_calls" in msg:
            new_msg["tool_calls"] = _redact_tool_call_arguments(msg.get("tool_calls"), passphrase)
        new_meta = _metadata(new_msg)
        if passphrase and new_meta and "reasoning_details" in new_meta:
            new_msg["metadata"] = {
                **new_meta,
                "reasoning_details": _redact_reasoning_details(
                    new_meta["reasoning_details"], passphrase,
                ),
            }
        out.append(new_msg)
    return out


__all__ = [
    "PASSPHRASE_LENGTH",
    "accepts_mid_conversation_system",
    "generate_passphrase",
    "is_head_system_message",
    "is_presented_system_notice",
    "model_accepts_mid_conversation_system",
    "present_system_notices",
    "redact_known_passphrases",
    "redact_passphrase",
    "system_notice_image_entries",
    "system_notice_image_note",
    "system_notice_indices",
    "system_notice_media_entries",
    "system_notice_media_note",
    "system_notice_passphrase",
    "tag_presented_system_notice",
    "tag_system_notice",
    "unresolved_system_notice_images",
    "unresolved_system_notice_media",
    "with_system_notice_image_notes",
]
