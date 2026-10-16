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

from typing import Any, Dict, Iterable, List, Optional

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


def build_conversation(
    thread_id: str,
    title: Optional[str],
    messages: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """1 スレッドぶんの会話を ChatGPT エクスポートの 1 会話 (mapping ツリー) にする。

    ``messages`` は正典順 (created_at, seq) に並んだ ``{"id", "role", "content",
    "created_at"}`` (created_at は epoch 秒)。写す行が 1 つも無ければ None。
    """
    items = list(messages)
    if not items:
        return None
    mapping: Dict[str, Dict[str, Any]] = {
        _ROOT_NODE_ID: {
            "id": _ROOT_NODE_ID,
            "message": None,
            "parent": None,
            "children": [],
        }
    }
    parent_id = _ROOT_NODE_ID
    for item in items:
        node_id = str(item["id"])
        created = item.get("created_at")
        create_time = float(created) if isinstance(created, (int, float)) else None
        mapping[node_id] = {
            "id": node_id,
            "message": {
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
            },
            "parent": parent_id,
            "children": [],
        }
        mapping[parent_id]["children"].append(node_id)
        parent_id = node_id

    times: List[float] = [
        float(i["created_at"]) for i in items
        if isinstance(i.get("created_at"), (int, float))
    ]
    display_title = title or (thread_id.split(":", 1)[1] if ":" in thread_id else thread_id)
    return {
        "id": thread_id,
        "conversation_id": thread_id,
        "title": display_title,
        "create_time": min(times) if times else None,
        "update_time": max(times) if times else None,
        "mapping": mapping,
        "current_node": parent_id,
        "default_model_slug": None,
    }
