"""Navigate memory hierarchy: drill down or summarize from a URI."""

from __future__ import annotations

from typing import Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema


def recall_navigate(
    uri: str,
    direction: str = "detail",
    max_chars: int = 3000,
) -> str:
    """Navigate the memory hierarchy from a given URI.

    - direction="detail": Drill down (Chronicle → messages, Memopedia → content)
    - direction="summary": Go up (messages → Chronicle, Chronicle Lv1 → Lv2)

    Args:
        uri: A saiverse:// URI from recall_entry or a previous recall_navigate result.
        direction: "detail" to drill down, "summary" to go up.
        max_chars: Maximum characters in the response.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()

    from saiverse.uri_resolver import UriResolver
    resolver = UriResolver(manager=manager)

    if direction == "detail":
        return _navigate_detail(resolver, uri, persona_id, max_chars)
    elif direction == "summary":
        return _navigate_summary(resolver, uri, persona_id, max_chars)
    else:
        raise ValueError(f"Unknown direction: {direction}. Use 'detail' or 'summary'.")


def _store_recalled_id(persona_id: str, uri: str, content_type: str, title: str) -> None:
    """Store the navigated item in working memory."""
    try:
        from saiverse_memory import SAIMemoryAdapter
        from tools.context import get_active_persona_path

        persona_dir = get_active_persona_path()
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)

        # Map content_type to source_type
        source_type = "chronicle" if "chronicle" in content_type else "memopedia"
        # Extract source_id from URI
        parts = uri.rstrip("/").split("/")
        source_id = parts[-1] if parts else ""

        adapter.add_recalled_id(
            source_type=source_type,
            source_id=source_id,
            title=title,
            uri=uri,
        )
    except Exception:
        pass  # Non-critical, don't block navigation


def _navigate_detail(resolver, uri: str, persona_id: str, max_chars: int) -> str:
    """Drill down: show more detail for the given URI."""
    resolved = resolver.resolve(uri, persona_id=persona_id)
    if not resolved or not resolved.content:
        return f"URI の解決に失敗しました: {uri}"

    content = resolved.content
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n... (truncated, {len(resolved.content)} chars total)"

    # Store recalled ID in working memory
    metadata = resolved.metadata or {}
    title = metadata.get("title", "") or resolved.content_type or ""
    _store_recalled_id(persona_id, uri, resolved.content_type or "", title)

    # Add navigation hints based on content type
    hints = []

    if resolved.content_type == "chronicle_entry":
        source_ids = metadata.get("source_ids", [])
        level = metadata.get("level", 1)
        if level == 1 and source_ids:
            hints.append(f"このあらすじには{len(source_ids)}件のメッセージが含まれています。")
            hints.append("個別メッセージを見るには: saiverse://self/messagelog/msg/<message_id>")
        elif level >= 2:
            hints.append(f"この要約にはLv{level-1}のエントリが含まれています。")

    elif resolved.content_type == "memopedia_page":
        children = metadata.get("children", [])
        if children:
            hints.append(f"子ページ: {len(children)}件")
            for child in children[:5]:
                child_title = child.get("title", "?")
                child_id = child.get("id", "?")
                hints.append(f"  - {child_title}: saiverse://self/memopedia/page/{child_id}")

    result = content
    if hints:
        result += "\n\n---\n" + "\n".join(hints)

    return result


def _navigate_summary(resolver, uri: str, persona_id: str, max_chars: int) -> str:
    """Go up: show the summary/parent for the given URI."""
    from saiverse_memory import SAIMemoryAdapter
    from tools.context import get_active_persona_path

    persona_dir = get_active_persona_path()
    adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)

    parsed = resolver._parse_uri(uri)
    if not parsed:
        return f"URI のパースに失敗しました: {uri}"

    scheme = parsed.scheme

    if scheme == "chronicle":
        # Go up: find the parent Chronicle (higher level)
        entry_id = parsed.path_parts[-1] if parsed.path_parts else None
        if not entry_id:
            return "Chronicle entry ID が見つかりません"

        from sai_memory.arasuji.storage import get_entry
        entry = get_entry(adapter.conn, entry_id)
        if not entry:
            return f"Chronicle entry が見つかりません: {entry_id}"

        if entry.parent_id:
            parent = get_entry(adapter.conn, entry.parent_id)
            if parent:
                parent_uri = f"saiverse://self/chronicle/entry/{parent.id}"
                return (
                    f"親エントリ (Lv{parent.level}):\n"
                    f"URI: {parent_uri}\n\n"
                    f"{parent.content}"
                )

        # No parent — show Chronicle context at this level
        from sai_memory.arasuji.context import get_episode_context, format_episode_context
        context = get_episode_context(adapter.conn, max_entries=10)
        formatted = format_episode_context(context)
        if len(formatted) > max_chars:
            formatted = formatted[:max_chars] + "\n... (truncated)"
        return f"現在のエピソードコンテキスト:\n\n{formatted}"

    elif scheme == "memopedia":
        # Go up: show the parent page
        page_id = parsed.path_parts[-1] if parsed.path_parts else None
        if not page_id:
            return "Memopedia page ID が見つかりません"

        from sai_memory.memopedia.storage import get_page
        page = get_page(adapter.conn, page_id)
        if not page:
            return f"Memopedia page が見つかりません: {page_id}"

        if page.parent_id and not page.parent_id.startswith("root_"):
            parent = get_page(adapter.conn, page.parent_id)
            if parent:
                parent_uri = f"saiverse://self/memopedia/page/{parent.id}"
                return (
                    f"親ページ: {parent.title}\n"
                    f"URI: {parent_uri}\n\n"
                    f"概要: {parent.summary}\n\n"
                    f"{parent.content}"
                )

        # At root level — show category overview
        return f"このページはカテゴリ '{page.category}' の直下にあります。"

    elif scheme == "messagelog":
        # Go up from message: find covering Chronicle Lv1
        msg_id = parsed.path_parts[-1] if parsed.path_parts else None
        if not msg_id:
            return "Message ID が見つかりません"

        from sai_memory.arasuji.storage import search_entries

        # Get the message timestamp
        cur = adapter.conn.execute(
            "SELECT created_at FROM messages WHERE id = ?", (msg_id,)
        )
        row = cur.fetchone()
        if not row:
            return f"メッセージが見つかりません: {msg_id}"

        msg_time = row[0]
        # Find Chronicle entries covering this time
        entries = search_entries(
            adapter.conn,
            start_time=msg_time,
            end_time=msg_time,
            level=1,
            limit=1,
        )
        if entries:
            entry = entries[0]
            entry_uri = f"saiverse://self/chronicle/entry/{entry.id}"
            return (
                f"このメッセージを含むあらすじ (Lv{entry.level}):\n"
                f"URI: {entry_uri}\n\n"
                f"{entry.content}"
            )
        return "このメッセージを含むあらすじが見つかりません。"

    return f"summary ナビゲーションは {scheme} スキームに対応していません。"


def schema() -> ToolSchema:
    return ToolSchema(
        name="recall_navigate",
        description=(
            "記憶の階層をナビゲートします。recall_entryの結果URIや、"
            "以前のナビゲーション結果のURIを指定して、詳細を確認したり"
            "（detail）、概要に戻ったり（summary）できます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "saiverse:// URI（recall_entryの結果や前回のナビゲーション結果から）",
                },
                "direction": {
                    "type": "string",
                    "enum": ["detail", "summary"],
                    "description": "detail: 詳細を見る（掘り下げ）、summary: 概要に戻る（上位層へ）",
                    "default": "detail",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "レスポンスの最大文字数（デフォルト: 3000）",
                    "default": 3000,
                },
            },
            "required": ["uri"],
        },
        result_type="string",
    )
