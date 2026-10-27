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
  (画像などの metadata.media は引き継ぐ)。
- それ以外のメッセージ: 本文に現れた合言葉を伏せる (モデルが合言葉を復唱して
  記録に残った場合の再利用を防ぐ)。

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
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set

from sai_memory.memory.storage import (
    HEAD_MESSAGE_KEY,
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


#: このプロセスで送る形に差し込んだ合言葉 (新しいものほど後ろ)。ログの書き出し
#: (saiverse/logging_config.py のハンドラの filter) が、どのクライアントのどの
#: ログ行 (送る形・応答・例外の文) からもこれを伏せる — 応答に復唱された合言葉は
#: 応答を書く側では見分けられないため (不変条件 3)。上限を超えたら古いものから
#: 忘れる (head の作り直しで合言葉は入れ替わる)。
_KNOWN_PASSPHRASES: "OrderedDict[str, None]" = OrderedDict()
# 同時に生きる head ((persona_id, model_key) ごとに一つ) の数を大きく超える余裕を
# 取る。1 件 12 文字なので、この数でも記憶量は無視できる。
_KNOWN_PASSPHRASES_LIMIT = 4096
_KNOWN_PASSPHRASES_LOCK = threading.Lock()


def _remember_passphrase(passphrase: str) -> None:
    if not passphrase:
        return
    with _KNOWN_PASSPHRASES_LOCK:
        _KNOWN_PASSPHRASES[passphrase] = None
        _KNOWN_PASSPHRASES.move_to_end(passphrase)
        while len(_KNOWN_PASSPHRASES) > _KNOWN_PASSPHRASES_LIMIT:
            _KNOWN_PASSPHRASES.popitem(last=False)


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


def _content_text(content: Any) -> str:
    from .utils import content_to_text

    return content_to_text(content or "")


def _without_passphrase(msg: Dict[str, Any]) -> Dict[str, Any]:
    meta = _metadata(msg)
    if meta is None or SYSTEM_NOTICE_PASSPHRASE_KEY not in meta:
        return dict(msg)
    new_meta = {k: v for k, v in meta.items() if k != SYSTEM_NOTICE_PASSPHRASE_KEY}
    return {**msg, "metadata": new_meta}


def _stored_image_summary(att: Dict[str, Any], metadata: Any) -> str:
    """記録の metadata (``media`` / ``images`` の項目の ``summary``) に保存済みの画像の要約。"""
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


def system_notice_image_note(att: Dict[str, Any], metadata: Any) -> str:
    """system のまま届ける通知の画像を置き換える説明の文 (決定的)。

    ``att`` は saiverse/media_utils.iter_image_media の項目。system メッセージは
    文字しか持てないので、画像は説明の文になる。記録の metadata に保存済みの
    要約があればそれを使い、無ければ ``[画像: <uri>]`` だけにする — その場で
    要約を生成する llm_clients/utils.image_summary_note を使うと、生成の成否で
    同じ記録から組む送信本文が変わる (不変条件 6)。
    """
    uri = att.get("uri") or str(att.get("path") or "unknown")
    summary = _stored_image_summary(att, metadata)
    return f"[画像: {uri}] {summary}" if summary else f"[画像: {uri}]"


def with_system_notice_image_notes(
    text: str, attachments: List[Dict[str, Any]], metadata: Any,
) -> str:
    """通知の本文 ``text`` の後ろに、各画像の :func:`system_notice_image_note` を一行ずつ足す。"""
    for att in attachments:
        note = system_notice_image_note(att, metadata)
        text = f"{text}\n{note}" if text else note
    return text


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


def present_system_notices(
    messages: List[Any], *, accepts_mid_system: bool,
) -> List[Any]:
    """messages 列を、送り先のモデルへ送る直前の形にする (新しい列を返す。入力は変えない)。

    ``accepts_mid_system`` はそのモデルが会話の途中の role=system を受け取れるか
    (:func:`accepts_mid_conversation_system`)。規則はモジュールの説明を参照。
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
            if accepts_mid_system:
                new_msg["role"] = "system"
                new_msg["content"] = _map_text(
                    msg.get("content"), lambda t: redact_passphrase(t, passphrase),
                )
            else:
                new_msg["role"] = "user"
                new_msg["content"] = tag_system_notice(
                    _content_text(msg.get("content")), passphrase,
                )
            out.append(new_msg)
            continue
        new_msg = _without_passphrase(msg)
        if passphrase and "content" in msg:
            new_msg["content"] = _map_text(
                msg.get("content"), lambda t: redact_passphrase(t, passphrase),
            )
        out.append(new_msg)
    return out


__all__ = [
    "PASSPHRASE_LENGTH",
    "accepts_mid_conversation_system",
    "generate_passphrase",
    "is_head_system_message",
    "model_accepts_mid_conversation_system",
    "present_system_notices",
    "redact_known_passphrases",
    "redact_passphrase",
    "system_notice_image_note",
    "system_notice_indices",
    "system_notice_passphrase",
    "tag_system_notice",
    "with_system_notice_image_notes",
]
