"""chatlog の会話を ChatGPT 公式エクスポートの conversations.json 形式へ写す。

正典: docs/intent/persona_pack.md §4 の ``chatgpt_compat/`` 行。他アプリ向けの
コピーで、SAIVerse 側の取り込みでは読まない。形式は既存の取り込み口
``tools/utilities/chatgpt_importer.py`` (``ChatGPTExport``) がそのまま読める
mapping ツリー (各スレッドが一本道の連鎖) に合わせる。

写すのは会話のやりとりだけ。判定は :func:`is_conversation_message`:

- role が ``user`` / ``assistant``
- scope が ``committed`` (NULL は committed とみなす。``volatile`` /
  ``discardable`` は履歴に残さない行なので写さない)
- metadata.tags に ``conversation`` を持ち、``internal`` / ``tool`` /
  ``handy_tool`` / ``spell`` を持たない (想起が会話として拾う範囲と同じ除外。
  saiverse_memory/adapter.py の recall 呼び出しの ``exclude_tags`` を参照)
- 本文が空でない

本文は無改変で写す (intent §3-2)。
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Dict, Iterable, Optional, TextIO, Tuple

_CONVERSATION_ROLES = {"user", "assistant"}
_EXCLUDED_TAGS = {"internal", "tool", "handy_tool", "spell"}
_ROOT_NODE_ID = "client-created-root"


def is_conversation_message(
    role: Any, scope: Any, metadata: Any, content: Any
) -> bool:
    """会話のやりとりとして chatgpt_compat に写す行か (判定はモジュール docstring)。"""
    if role not in _CONVERSATION_ROLES:
        return False
    if (scope or "committed") != "committed":
        return False
    if not isinstance(content, str) or not content.strip():
        return False
    if not isinstance(metadata, dict):
        return False
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        return False
    tag_set = {t for t in tags if isinstance(t, str)}
    if "conversation" not in tag_set:
        return False
    return not (tag_set & _EXCLUDED_TAGS)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _node(
    node_id: str, item: Optional[Dict[str, Any]], parent_id: Optional[str], child_id: Optional[str]
) -> Dict[str, Any]:
    """mapping の 1 ノード。item が None なら根 (_ROOT_NODE_ID)。"""
    message = None
    if item is not None:
        created = item.get("created_at")
        create_time = float(created) if isinstance(created, (int, float)) else None
        message = {
            "id": node_id,
            "author": {"role": item["role"], "name": None, "metadata": {}},
            "create_time": create_time,
            "update_time": None,
            "content": {"content_type": "text", "parts": [item["content"]]},
            "status": "finished_successfully",
            "end_turn": True if item["role"] == "assistant" else None,
            "weight": 1.0,
            "metadata": {},
            "recipient": "all",
        }
    return {
        "id": node_id,
        "message": message,
        "parent": parent_id,
        "children": [child_id] if child_id is not None else [],
    }


def write_conversation(
    out: TextIO,
    thread_id: str,
    title: Optional[str],
    messages: Iterable[Dict[str, Any]],
) -> None:
    """1 スレッドぶんの会話を ChatGPT エクスポートの 1 会話 (mapping ツリー) として
    ``out`` へ逐次書きする。

    ``messages`` は正典順 (created_at, seq) に並んだ ``{"id", "role", "content",
    "created_at"}`` (created_at は epoch 秒) で、1 件以上あること。mapping は根から
    一本道で、各ノードの children は次のノードの id 一つだけなので、ノードは次の
    メッセージが来た時点で書ける。メモリに持つのは書き待ちの 1 ノードぶんだけ。
    create_time / update_time / current_node は全部読むまで決まらないので、mapping の
    後ろに書く (JSON のオブジェクトなのでキーの順は意味を持たない)。
    """
    items = iter(messages)
    first = next(items, None)
    if first is None:
        raise ValueError(f"write_conversation needs at least one message: {thread_id}")
    display_title = title or (thread_id.split(":", 1)[1] if ":" in thread_id else thread_id)
    out.write(
        f'{{"id": {_dumps(thread_id)}, "conversation_id": {_dumps(thread_id)}, '
        f'"title": {_dumps(display_title)}, "mapping": {{'
    )
    first_time: Optional[float] = None
    last_time: Optional[float] = None
    # 書き待ちのノード (id, メッセージ, 親の id)。根は message を持たない
    pending: Tuple[str, Optional[Dict[str, Any]], Optional[str]] = (_ROOT_NODE_ID, None, None)
    for item in itertools.chain((first,), items):
        node_id = str(item["id"])
        pending_id, pending_item, pending_parent = pending
        if pending_item is not None:
            out.write(", ")
        out.write(f"{_dumps(pending_id)}: {_dumps(_node(pending_id, pending_item, pending_parent, node_id))}")
        pending = (node_id, item, pending_id)
        created = item.get("created_at")
        if isinstance(created, (int, float)):
            t = float(created)
            first_time = t if first_time is None else min(first_time, t)
            last_time = t if last_time is None else max(last_time, t)
    pending_id, pending_item, pending_parent = pending
    out.write(f", {_dumps(pending_id)}: {_dumps(_node(pending_id, pending_item, pending_parent, None))}")
    out.write(
        f'}}, "create_time": {_dumps(first_time)}, "update_time": {_dumps(last_time)}, '
        f'"current_node": {_dumps(pending_id)}, "default_model_slug": null}}'
    )
