"""ユーザーが添付した文書の本文を、送る直前にユーザーの発言の後ろへ付ける (クライアント層の変換)。

docs/intent/user_attachment_delivery.md「文書の本文は、ユーザーの発言の後ろに付けて送る」。

チャットに添付した文書の本文は、添付した時点で api/routes/chat.py の
``_store_document_attachment`` が抜き出し、発言の ``metadata["documents"]`` の各項目の
``text`` (と ``text_truncated``) へ写す。記録に残る発言の本文 (``content``) は
ユーザーの言葉のまま変えず、送る直前にだけ :func:`append_attached_documents` が
本文の後ろへ文書の塊を足す。呼ぶのは llm_clients/system_notice.py の
``present_system_notices`` の最初の段だけ — 全クライアントと sea/sluice.py
(送る量の見積もり) が同じ文面を見て、足した本文は user の発言として合言葉の
伏せ字を通る。

- 足すのは role=user のメッセージだけ。システム通知 (role=system / ``host``) や
  assistant が ``documents`` を持っていても触らない — ユーザーの文章を通知の本文に
  載せると、システムの言葉に化ける (system_notice_delivery.md 不変条件 2)。
- ``text`` の鍵を持たない項目 (この変更より前の記録。``content_preview`` だけを
  持つ) は何も足さない。古い発言から組む送信本文は一字も変わらない (キャッシュの
  前方一致を割らない)。
- 出力はメッセージの列だけから決まる (ファイルを読まない。同じ記録から同じ本文)。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 文書の塊の見出し (``[添付文書: <item_name>]``)。
_BLOCK_HEADER = "[添付文書: {name}]"
#: ``text_truncated`` が真のとき本文の後ろに置く行 (builtin_data/tools/get_visual_context.py
#: の部屋の様子の文書と同じ語)。
_TRUNCATED_LINE = "... (以下省略)"
#: 本文を取り出せなかった (``text`` が空) 文書の塊の本文。
_UNAVAILABLE_BODY = "(本文を取り出せませんでした)"


def _document_block(entry: Dict[str, Any]) -> str:
    """``metadata["documents"]`` の一項目 (``text`` を持つ) の塊。"""
    name = entry.get("item_name")
    if not isinstance(name, str) or not name:
        name = str(entry.get("uri") or entry.get("path") or "")
    text = entry.get("text")
    body = text if isinstance(text, str) and text else _UNAVAILABLE_BODY
    if entry.get("text_truncated") and isinstance(text, str) and text:
        body = f"{body}\n{_TRUNCATED_LINE}"
    return f"{_BLOCK_HEADER.format(name=name)}\n{body}"


def _document_blocks(metadata: Any) -> Optional[str]:
    """metadata の ``documents`` のうち ``text`` を持つ項目の塊を、空行で区切って並べた文 (無ければ None)。"""
    if not isinstance(metadata, dict):
        return None
    documents = metadata.get("documents")
    if not isinstance(documents, list):
        return None
    blocks = [
        _document_block(entry)
        for entry in documents
        if isinstance(entry, dict) and "text" in entry
    ]
    if not blocks:
        return None
    return "\n\n".join(blocks)


def append_attached_documents(messages: List[Any]) -> List[Any]:
    """role=user の発言の本文の後ろに、添付文書の本文の塊を足した新しい列を返す (入力は変えない)。

    形 (塊は ``metadata["documents"]`` の並びの順、塊どうしは空行で区切る)::

        <発言の本文>

        [添付文書: <item_name>]
        <text>
        ... (以下省略)          ← text_truncated が真のときだけ

    - ``text`` が空文字 (取り出せなかった) なら塊の本文は ``(本文を取り出せませんでした)``。
    - 発言の本文が空 (空白だけを含む) なら、本文は塊だけになる。
    - 本文が部品の並びなら、塊を一つの text 部品として末尾に足す。文字列でも
      並びでもない本文のメッセージは変えない。
    """
    out: List[Any] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            out.append(msg)
            continue
        blocks = _document_blocks(msg.get("metadata"))
        if blocks is None:
            out.append(msg)
            continue
        content = msg.get("content")
        if content is None or isinstance(content, str):
            said = content or ""
            new_content: Any = f"{said}\n\n{blocks}" if said.strip() else blocks
        elif isinstance(content, list):
            new_content = [*content, {"type": "text", "text": blocks}]
        else:
            out.append(msg)
            continue
        out.append({**msg, "content": new_content})
    return out


__all__ = ["append_attached_documents"]
