"""
SAIVerse URI Resolver — 統一リソースアドレッシング & 解決

URI形式:
    saiverse://{city}/{persona_name}/{resource_type}/{path}?{params}
    saiverse://self/{resource_type}/{path}?{params}
    saiverse://image/{filename}       (既存互換)
    saiverse://document/{filename}    (既存互換)
    saiverse://item/{item_id}/...     (既存互換)
    saiverse://persona/{id}/...       (既存互換)
    saiverse://building/{id}/...      (既存互換)
    saiverse://web?url={encoded_url}

persona_id ↔ city/name 変換:
    air_city_a → city=city_a, name=air
    {name}_{city} (persona_idはname + "_" + cityで構成される)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

LOGGER = logging.getLogger(__name__)

URI_PREFIX = "saiverse://"

# 既存互換のグローバルスキーム (city/personaを含まない)
GLOBAL_SCHEMES = {"image", "document", "item", "persona", "building", "web"}

# ペルソナスコープのリソースタイプ
PERSONA_RESOURCE_TYPES = {"messagelog", "memopedia", "chronicle"}


@dataclass
class SaiUri:
    """パース済みSAIVerse URI。"""

    raw: str
    scheme: str  # "messagelog", "memopedia", "chronicle", "item", "building", "web", "image", "document", "persona"
    persona_id: Optional[str] = None  # 解決済み (selfは実IDに変換)
    city: Optional[str] = None
    persona_name: Optional[str] = None
    path_parts: list = field(default_factory=list)  # ["msg", "{message_id}"] 等
    params: dict = field(default_factory=dict)  # クエリパラメータ

    @property
    def is_persona_scoped(self) -> bool:
        return self.scheme in PERSONA_RESOURCE_TYPES


@dataclass
class ResolvedContent:
    """URI解決結果。"""

    uri: str
    content: str
    content_type: str  # "message", "message_log", "memopedia_page", "chronicle_entry", etc.
    char_count: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.char_count == 0:
            self.char_count = len(self.content)


def _persona_id_to_city_name(persona_id: str) -> Tuple[Optional[str], Optional[str]]:
    """persona_id (例: air_city_a) から city と persona_name を抽出。

    規則: persona_id = {name}_{city_name}
    city_name は "city_" で始まることが多いが、汎用的に最初の "_" で分割しない。
    実際には persona_name は先頭の短い名前で、残りが city_name。
    """
    # 末尾からcity部分を探す: "city_a", "city_b" などのパターン
    # まず "city_" を含む部分を探す
    idx = persona_id.find("_city_")
    if idx >= 0:
        name = persona_id[:idx]
        city = persona_id[idx + 1:]  # "city_a"
        return city, name

    # フォールバック: 最後の "_" で分割
    last_underscore = persona_id.rfind("_")
    if last_underscore > 0:
        name = persona_id[:last_underscore]
        city = persona_id[last_underscore + 1:]
        return city, name

    return None, persona_id


def _city_name_to_persona_id(city: str, persona_name: str) -> str:
    """city と persona_name から persona_id を構成。"""
    return f"{persona_name}_{city}"


def parse_sai_uri(uri: str, context_persona_id: Optional[str] = None) -> SaiUri:
    """SAIVerse URIをパースする。

    Args:
        uri: saiverse:// で始まるURI文字列
        context_persona_id: "self" 解決用の実行中ペルソナID

    Returns:
        パース済みSaiUriオブジェクト
    """
    if not uri.startswith(URI_PREFIX):
        raise ValueError(f"Invalid SAIVerse URI (must start with {URI_PREFIX}): {uri}")

    body = uri[len(URI_PREFIX):]

    # クエリパラメータ分離
    params = {}
    if "?" in body:
        body_part, query_part = body.split("?", 1)
        for key, values in parse_qs(query_part).items():
            params[key] = values[0] if len(values) == 1 else values
        body = body_part

    # パス分割
    parts = [p for p in body.split("/") if p]
    if not parts:
        raise ValueError(f"Empty URI path: {uri}")

    first = parts[0]

    # ── "self" 参照 ──
    if first == "self":
        if not context_persona_id:
            raise ValueError(f"Cannot resolve 'self' URI without context_persona_id: {uri}")
        if len(parts) < 2:
            raise ValueError(f"Missing resource type after 'self': {uri}")
        resource_type = parts[1]
        path_parts = parts[2:]
        city, name = _persona_id_to_city_name(context_persona_id)
        return SaiUri(
            raw=uri,
            scheme=resource_type,
            persona_id=context_persona_id,
            city=city,
            persona_name=name,
            path_parts=path_parts,
            params=params,
        )

    # ── 既存グローバルスキーム ──
    if first in GLOBAL_SCHEMES:
        return SaiUri(
            raw=uri,
            scheme=first,
            path_parts=parts[1:],
            params=params,
        )

    # ── ペルソナスコープ: {city}/{persona_name}/{resource_type}/... ──
    if len(parts) >= 3 and parts[2] in PERSONA_RESOURCE_TYPES:
        city = parts[0]
        persona_name = parts[1]
        resource_type = parts[2]
        path_parts = parts[3:]
        persona_id = _city_name_to_persona_id(city, persona_name)
        return SaiUri(
            raw=uri,
            scheme=resource_type,
            persona_id=persona_id,
            city=city,
            persona_name=persona_name,
            path_parts=path_parts,
            params=params,
        )

    # ── フォールバック: 不明なスキーム ──
    return SaiUri(
        raw=uri,
        scheme=first,
        path_parts=parts[1:],
        params=params,
    )


class UriResolver:
    """SAIVerse URIを実際のテキストコンテンツに解決するリゾルバ。"""

    def __init__(self, manager=None):
        """
        Args:
            manager: SAIVerseManager インスタンス (DBアクセス、item_service等に使用)
        """
        self.manager = manager

    def resolve(self, uri: str, *, persona_id: str = None) -> ResolvedContent:
        """単一URIを解決してコンテンツを返す。

        Args:
            uri: saiverse:// URI文字列
            persona_id: コンテキストペルソナID ("self" 解決用)

        Returns:
            ResolvedContent with content text
        """
        parsed = parse_sai_uri(uri, context_persona_id=persona_id)

        # ペルソナスコープURIのアクセス制御: 自分の記憶のみ参照可能
        if parsed.is_persona_scoped:
            if not persona_id:
                LOGGER.warning(
                    "Access denied: persona_id not provided for persona-scoped URI %s",
                    uri,
                )
                return ResolvedContent(
                    uri=uri,
                    content="(アクセス拒否: ペルソナスコープURIにはpersona_idが必要です)",
                    content_type="error",
                    metadata={"error": "access_denied", "reason": "persona_id_required"},
                )
            if parsed.persona_id != persona_id:
                LOGGER.warning(
                    "Access denied: persona %s tried to access %s's %s",
                    persona_id, parsed.persona_id, parsed.scheme,
                )
                return ResolvedContent(
                    uri=uri,
                    content="(アクセス拒否: 他ペルソナの記憶は参照できません)",
                    content_type="error",
                    metadata={"error": "access_denied", "target_persona": parsed.persona_id},
                )

        handler = self._handlers.get(parsed.scheme)
        if not handler:
            return ResolvedContent(
                uri=uri,
                content=f"(unsupported URI scheme: {parsed.scheme})",
                content_type="error",
                metadata={"error": f"unknown scheme: {parsed.scheme}"},
            )

        try:
            return handler(self, parsed)
        except Exception as exc:
            LOGGER.warning("Failed to resolve URI %s: %s", uri, exc)
            return ResolvedContent(
                uri=uri,
                content=f"(URI解決エラー: {exc})",
                content_type="error",
                metadata={"error": str(exc)},
            )

    def resolve_many(
        self,
        uris: list,
        *,
        persona_id: str = None,
        max_total_chars: int = 8000,
        priority: str = "first",
    ) -> list:
        """複数URIを解決し、合計文字数を制限内に収める。

        Args:
            uris: URI文字列のリスト
            persona_id: コンテキストペルソナID
            max_total_chars: 合計最大文字数
            priority: "first" (先頭優先でトリム) | "balanced" (均等配分)

        Returns:
            ResolvedContentのリスト
        """
        results = []
        for u in uris:
            resolved = self.resolve(u, persona_id=persona_id)
            results.append(resolved)

        total = sum(r.char_count for r in results)
        if total <= max_total_chars:
            return results

        # トリムが必要
        if priority == "balanced":
            per_item = max_total_chars // max(len(results), 1)
            for r in results:
                if r.char_count > per_item:
                    r.content = r.content[:per_item] + "\n... (truncated)"
                    r.char_count = len(r.content)
        else:  # "first" — 先頭から順に確保、後ろをトリム
            remaining = max_total_chars
            for r in results:
                if remaining <= 0:
                    r.content = "(skipped due to char limit)"
                    r.char_count = len(r.content)
                elif r.char_count > remaining:
                    r.content = r.content[:remaining] + "\n... (truncated)"
                    r.char_count = len(r.content)
                    remaining = 0
                else:
                    remaining -= r.char_count

        return results

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _resolve_messagelog(self, parsed: SaiUri) -> ResolvedContent:
        """過去ログ (messagelog) の解決。"""
        adapter = self._get_adapter(parsed.persona_id)
        if not adapter:
            return self._error(parsed.raw, f"SAIMemory not available for {parsed.persona_id}")

        path = parsed.path_parts
        params = parsed.params

        # saiverse://self/messagelog/msg/recent?depth=N
        if len(path) >= 2 and path[0] == "msg" and path[1] == "recent":
            from sai_memory.memory.storage import get_messages_last

            depth = int(params.get("depth", 5))
            thread_id = self._get_active_thread_id(adapter)

            with adapter._db_lock:
                msgs = get_messages_last(adapter.conn, thread_id, depth)
            content = self._format_messages(msgs) if msgs else "(no recent messages)"
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log",
                metadata={"depth": depth, "count": len(msgs)},
            )

        # saiverse://self/messagelog/msg?contain=TEXT&window=N
        if len(path) >= 1 and path[0] == "msg" and "contain" in params:
            from sai_memory.memory.storage import _row_to_message, get_messages_around

            query_text = params["contain"]
            window = int(params.get("window", 0))
            thread_id = self._get_active_thread_id(adapter)

            with adapter._db_lock:
                cursor = adapter.conn.execute(
                    "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                    "FROM messages WHERE thread_id = ? AND content LIKE ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (thread_id, f"%{query_text}%"),
                )
                row = cursor.fetchone()

            if not row:
                return self._error(parsed.raw, f"Message not found containing: {query_text}")

            msg = _row_to_message(row)

            if window > 0:
                with adapter._db_lock:
                    surrounding = get_messages_around(
                        adapter.conn, thread_id, msg.id, before=window, after=window
                    )
                content = self._format_messages(surrounding, highlight_id=msg.id)
            else:
                content = self._format_messages([msg])

            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log" if window > 0 else "message",
                metadata={
                    "message_id": msg.id,
                    "created_at": msg.created_at,
                    "window": window,
                },
            )

        # saiverse://self/messagelog/msg/{message_id}
        if len(path) >= 2 and path[0] == "msg":
            from sai_memory.memory.storage import get_message, get_messages_around

            message_id = path[1]
            window = int(params.get("window", 0))

            with adapter._db_lock:
                msg = get_message(adapter.conn, message_id)
            if not msg:
                return self._error(parsed.raw, f"Message not found: {message_id}")

            if window > 0:
                with adapter._db_lock:
                    surrounding = get_messages_around(
                        adapter.conn, msg.thread_id, message_id, before=window, after=window
                    )
                # Insert anchor
                insert_idx = 0
                for i, m in enumerate(surrounding):
                    if m.created_at <= msg.created_at and m.id != msg.id:
                        insert_idx = i + 1
                    elif m.created_at > msg.created_at:
                        break
                    else:
                        insert_idx = i + 1
                all_msgs = surrounding[:insert_idx] + [msg] + surrounding[insert_idx:]
                content = self._format_messages(all_msgs, highlight_id=message_id)
            else:
                content = self._format_messages([msg])

            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log" if window > 0 else "message",
                metadata={"message_id": message_id, "window": window},
            )

        # saiverse://self/messagelog/range?from={ts}&to={ts}
        if (len(path) == 0 or (len(path) >= 1 and path[0] == "range")) and "from" in params:
            from sai_memory.memory.storage import get_messages_last

            start_ts = int(params["from"])
            end_ts = int(params.get("to", "9999999999"))

            with adapter._db_lock:
                # 全スレッドから時間範囲のメッセージ取得
                cursor = adapter.conn.execute(
                    "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                    "FROM messages WHERE created_at >= ? AND created_at <= ? "
                    "ORDER BY created_at ASC LIMIT 100",
                    (start_ts, end_ts),
                )
                from sai_memory.memory.storage import _row_to_message

                msgs = [_row_to_message(row) for row in cursor.fetchall()]

            content = self._format_messages(msgs) if msgs else "(no messages in range)"
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log",
                metadata={"from": start_ts, "to": end_ts, "count": len(msgs)},
            )

        # saiverse://self/messagelog/thread/{suffix}?last=N
        if len(path) >= 2 and path[0] == "thread":
            from sai_memory.memory.storage import get_messages_last

            thread_suffix = path[1]
            thread_id = f"{parsed.persona_id}:{thread_suffix}"
            last_n = int(params.get("last", 20))

            with adapter._db_lock:
                msgs = get_messages_last(adapter.conn, thread_id, last_n)
            msgs.reverse()  # oldest first
            content = self._format_messages(msgs) if msgs else "(no messages in thread)"
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log",
                metadata={"thread_id": thread_id, "count": len(msgs)},
            )

        # saiverse://self/messagelog/summary/{uuid}
        if len(path) >= 2 and path[0] == "summary":
            summary_uuid = path[1]
            # summary_uuid はメモリ内のsummaryメッセージのUUID prefix
            persona = self._get_persona(parsed.persona_id)
            if persona:
                sai_mem = getattr(persona, "sai_memory", None)
                if sai_mem:
                    msgs = sai_mem.recent_persona_messages(
                        max_chars=100000, required_tags=["summary"]
                    )
                    for m in reversed(msgs):
                        meta = m.get("metadata", {})
                        if meta.get("summary_uuid", "").startswith(summary_uuid):
                            return ResolvedContent(
                                uri=parsed.raw,
                                content=m.get("content", ""),
                                content_type="summary",
                                metadata=meta,
                            )
            return self._error(parsed.raw, f"Summary not found: {summary_uuid}")

        return self._error(parsed.raw, f"Unknown messagelog path: {'/'.join(path)}")

    def _resolve_memopedia(self, parsed: SaiUri) -> ResolvedContent:
        """Memopediaページの解決。"""
        memopedia = self._get_memopedia(parsed.persona_id)
        if not memopedia:
            return self._error(parsed.raw, f"Memopedia not available for {parsed.persona_id}")

        path = parsed.path_parts
        params = parsed.params

        # saiverse://self/memopedia/tree
        if len(path) >= 1 and path[0] == "tree":
            content = memopedia.get_tree_markdown(thread_id=None)
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="memopedia_tree",
            )

        # saiverse://self/memopedia/page/{page_id} or ?title=...
        if len(path) >= 1 and path[0] == "page":
            page = None
            if len(path) >= 2:
                page = memopedia.get_page(path[1])
            elif "title" in params:
                page = memopedia.find_by_title(unquote(params["title"]))

            if not page:
                identifier = path[1] if len(path) >= 2 else params.get("title", "?")
                return self._error(parsed.raw, f"Memopedia page not found: {identifier}")

            content = self._format_memopedia_page(page)
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="memopedia_page",
                metadata={
                    "page_id": page.id,
                    "title": page.title,
                    "category": page.category,
                },
            )

        return self._error(parsed.raw, f"Unknown memopedia path: {'/'.join(path)}")

    def _resolve_chronicle(self, parsed: SaiUri) -> ResolvedContent:
        """Chronicleエントリの解決。"""
        conn = self._get_memory_conn(parsed.persona_id)
        if not conn:
            return self._error(parsed.raw, f"Memory DB not available for {parsed.persona_id}")

        from sai_memory.arasuji.storage import get_entry, get_all_entries_ordered

        path = parsed.path_parts
        params = parsed.params

        # saiverse://self/chronicle/entry?contain=TEXT
        if len(path) >= 1 and path[0] == "entry" and "contain" in params:
            from sai_memory.arasuji.storage import search_entries

            query_text = params["contain"]
            adapter = self._get_adapter(parsed.persona_id)
            if adapter:
                with adapter._db_lock:
                    entries = search_entries(adapter.conn, query_text, limit=1)
            else:
                entries = search_entries(conn, query_text, limit=1)

            if not entries:
                return self._error(parsed.raw, f"Chronicle entry not found containing: {query_text}")

            entry = entries[0]
            content = self._format_chronicle_entry(entry)
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="chronicle_entry",
                metadata={
                    "entry_id": entry.id,
                    "level": entry.level,
                    "start_time": entry.start_time,
                    "end_time": entry.end_time,
                    "message_count": entry.message_count,
                },
            )

        # saiverse://self/chronicle/entry/{entry_id}
        if len(path) >= 2 and path[0] == "entry":
            entry_id = path[1]
            adapter = self._get_adapter(parsed.persona_id)
            if adapter:
                with adapter._db_lock:
                    entry = get_entry(adapter.conn, entry_id)
            else:
                entry = get_entry(conn, entry_id)

            if not entry:
                return self._error(parsed.raw, f"Chronicle entry not found: {entry_id}")

            content = self._format_chronicle_entry(entry)
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="chronicle_entry",
                metadata={
                    "entry_id": entry.id,
                    "level": entry.level,
                    "start_time": entry.start_time,
                    "end_time": entry.end_time,
                    "message_count": entry.message_count,
                },
            )

        # saiverse://self/chronicle/range?from={ts}&to={ts}
        if (len(path) == 0 or (len(path) >= 1 and path[0] == "range")) and "from" in params:
            start_ts = int(params["from"])
            end_ts = int(params.get("to", "9999999999"))

            adapter = self._get_adapter(parsed.persona_id)
            if adapter:
                with adapter._db_lock:
                    all_entries = get_all_entries_ordered(adapter.conn)
            else:
                all_entries = get_all_entries_ordered(conn)

            filtered = [
                e for e in all_entries
                if e.start_time and e.end_time
                and e.start_time <= end_ts and e.end_time >= start_ts
            ]

            if not filtered:
                return self._error(parsed.raw, "No chronicle entries in range")

            lines = []
            for e in filtered:
                lines.append(self._format_chronicle_entry(e))
            content = "\n---\n".join(lines)
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="chronicle_range",
                metadata={"from": start_ts, "to": end_ts, "count": len(filtered)},
            )

        # saiverse://self/chronicle/recent?depth=N
        if len(path) >= 1 and path[0] == "recent":
            from sai_memory.arasuji.context import get_episode_context, format_episode_context

            depth = int(params.get("depth", 3))
            adapter = self._get_adapter(parsed.persona_id)
            if adapter:
                with adapter._db_lock:
                    ctx = get_episode_context(adapter.conn, max_entries=depth * 10)
            else:
                ctx = get_episode_context(conn, max_entries=depth * 10)

            content = format_episode_context(ctx) if ctx else "(no chronicle data)"
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="chronicle_context",
                metadata={"depth": depth, "entries": len(ctx)},
            )

        return self._error(parsed.raw, f"Unknown chronicle path: {'/'.join(path)}")

    def _resolve_item(self, parsed: SaiUri) -> ResolvedContent:
        """アイテムの解決。"""
        if not self.manager:
            return self._error(parsed.raw, "Manager not available")

        path = parsed.path_parts
        params = parsed.params

        if not path:
            return self._error(parsed.raw, "Missing item_id")

        item_id = path[0]

        # saiverse://item/{item_id}/content
        if len(path) >= 2 and path[1] == "content":
            try:
                # Read item content directly (no persona check needed for URI resolution)
                item_service = self.manager.item_service
                item = item_service.items.get(item_id)
                if not item:
                    return self._error(parsed.raw, f"Item not found: {item_id}")

                item_type = (item.get("type") or "").lower()
                file_path_str = item.get("file_path")
                if not file_path_str:
                    return self._error(parsed.raw, f"No file path for item: {item_id}")

                file_path = item_service._resolve_file_path(file_path_str)
                if not file_path.exists():
                    return self._error(parsed.raw, f"File not found: {file_path}")

                if item_type == "picture":
                    return ResolvedContent(
                        uri=parsed.raw,
                        content=f"/api/info/item/{item_id}",
                        content_type="image",
                        metadata={"item_id": item_id, "title": item.get("name", item_id), "type": item_type},
                    )
                elif item_type == "document":
                    content = file_path.read_text(encoding="utf-8")
                else:
                    content = f"アイテム: {item.get('name', item_id)} (type: {item_type})"

                # 行範囲フィルタ
                if "lines" in params:
                    content = self._filter_lines(content, params["lines"])

                return ResolvedContent(
                    uri=parsed.raw,
                    content=content,
                    content_type="item_content",
                    metadata={"item_id": item_id, "title": item.get("name", item_id), "type": item_type},
                )
            except Exception as exc:
                return self._error(parsed.raw, f"Failed to read item content: {exc}")

        # saiverse://item/{item_id} (情報のみ)
        try:
            from database.models import Item, ItemLocation
            from database.session import get_session

            with get_session() as session:
                item = session.query(Item).filter(Item.ITEM_ID == item_id).first()
                if not item:
                    return self._error(parsed.raw, f"Item not found: {item_id}")

                loc = session.query(ItemLocation).filter(
                    ItemLocation.ITEM_ID == item_id
                ).first()

                content = (
                    f"【Item】{item.NAME}\n"
                    f"Type: {item.TYPE}\n"
                    f"Description: {item.DESCRIPTION or '(none)'}\n"
                )
                if loc:
                    content += f"Location: {loc.OWNER_KIND}/{loc.OWNER_ID}\n"

                return ResolvedContent(
                    uri=parsed.raw,
                    content=content,
                    content_type="item_info",
                    metadata={"item_id": item_id, "name": item.NAME, "type": item.TYPE},
                )
        except Exception as exc:
            return self._error(parsed.raw, f"Failed to read item: {exc}")

    def _resolve_building(self, parsed: SaiUri) -> ResolvedContent:
        """ビルディング情報の解決。"""
        if not self.manager:
            return self._error(parsed.raw, "Manager not available")

        path = parsed.path_parts
        params = parsed.params

        if not path:
            return self._error(parsed.raw, "Missing building_id")

        building_id = path[0]

        # saiverse://building/{id}/items
        if len(path) >= 2 and path[1] == "items":
            try:
                items = self.manager.item_service.list_items_in_building(building_id)
                if not items:
                    return ResolvedContent(
                        uri=parsed.raw,
                        content="(no items in building)",
                        content_type="building_items",
                    )
                lines = [f"【Building Items】{building_id}"]
                for item in items:
                    name = getattr(item, "NAME", str(item))
                    item_type = getattr(item, "TYPE", "?")
                    desc = getattr(item, "DESCRIPTION", "")
                    lines.append(f"- {name} ({item_type}): {desc}")
                return ResolvedContent(
                    uri=parsed.raw,
                    content="\n".join(lines),
                    content_type="building_items",
                    metadata={"building_id": building_id, "count": len(items)},
                )
            except Exception as exc:
                return self._error(parsed.raw, f"Failed to list building items: {exc}")

        # saiverse://building/{id}/history?last=N
        if len(path) >= 2 and path[1] == "history":
            last_n = int(params.get("last", 20))
            try:
                building = self.manager.buildings.get(building_id)
                if not building:
                    return self._error(parsed.raw, f"Building not found: {building_id}")
                history = building.history[-last_n:] if hasattr(building, "history") else []
                if not history:
                    return ResolvedContent(
                        uri=parsed.raw,
                        content="(no history)",
                        content_type="building_history",
                    )
                lines = [f"【Building History】{building_id} (last {last_n})"]
                for msg in history:
                    role = msg.get("role", "?")
                    content_text = msg.get("content", "")[:200]
                    lines.append(f"[{role}] {content_text}")
                return ResolvedContent(
                    uri=parsed.raw,
                    content="\n".join(lines),
                    content_type="building_history",
                    metadata={"building_id": building_id, "count": len(history)},
                )
            except Exception as exc:
                return self._error(parsed.raw, f"Failed to get building history: {exc}")

        return self._error(parsed.raw, f"Unknown building path: {'/'.join(path)}")

    def _resolve_web(self, parsed: SaiUri) -> ResolvedContent:
        """Web URLの解決。"""
        url = parsed.params.get("url")
        if not url:
            return self._error(parsed.raw, "Missing url parameter")
        url = unquote(url)

        try:
            from builtin_data.tools.read_url_content import read_url_content

            result = read_url_content(url=url, max_chars=int(parsed.params.get("max_chars", 8000)))
            # read_url_content returns (content_str, ToolResult) tuple
            if isinstance(result, tuple):
                content = str(result[0])
            else:
                content = str(result)
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="web_page",
                metadata={"url": url},
            )
        except Exception as exc:
            return self._error(parsed.raw, f"Failed to fetch URL: {exc}")

    def _resolve_image(self, parsed: SaiUri) -> ResolvedContent:
        """画像URIの解決 (パスのみ返す)。"""
        from .media_utils import resolve_media_uri

        path = resolve_media_uri(parsed.raw)
        if path and path.exists():
            return ResolvedContent(
                uri=parsed.raw,
                content=f"[Image: {path}]",
                content_type="image_path",
                metadata={"path": str(path)},
            )
        return self._error(parsed.raw, "Image file not found")

    def _resolve_document(self, parsed: SaiUri) -> ResolvedContent:
        """ドキュメントファイルURIの解決。"""
        from .media_utils import resolve_media_uri

        path = resolve_media_uri(parsed.raw)
        if path and path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                return ResolvedContent(
                    uri=parsed.raw,
                    content=content,
                    content_type="document_file",
                    metadata={"path": str(path)},
                )
            except Exception as exc:
                return self._error(parsed.raw, f"Failed to read document: {exc}")
        return self._error(parsed.raw, "Document file not found")

    def _resolve_persona(self, parsed: SaiUri) -> ResolvedContent:
        """ペルソナ情報の解決 (基本的に既存互換)。"""
        # 主にimage用なのでパス返却のみ
        from .media_utils import resolve_extended_media_uri

        path = resolve_extended_media_uri(parsed.raw)
        if path and path.exists():
            return ResolvedContent(
                uri=parsed.raw,
                content=f"[Persona resource: {path}]",
                content_type="persona_resource",
                metadata={"path": str(path)},
            )
        return self._error(parsed.raw, "Persona resource not found")

    # Handler dispatch table
    _handlers = {
        "messagelog": _resolve_messagelog,
        "memopedia": _resolve_memopedia,
        "chronicle": _resolve_chronicle,
        "item": _resolve_item,
        "building": _resolve_building,
        "web": _resolve_web,
        "image": _resolve_image,
        "document": _resolve_document,
        "persona": _resolve_persona,
    }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_active_thread_id(self, adapter) -> str:
        """adapterからアクティブスレッドIDを取得。"""
        # SAIMemoryAdapter._thread_id() を引数なしで呼ぶとアクティブスレッドを返す
        getter = getattr(adapter, "_thread_id", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                LOGGER.warning("Failed to get active thread_id from adapter for %s", adapter.persona_id, exc_info=True)
        return f"{adapter.persona_id}:__persona__"

    def _get_adapter(self, persona_id: Optional[str]):
        """ペルソナのSAIMemoryAdapterを取得。"""
        if not persona_id:
            return None
        # manager経由でPersonaCoreからadapterを取得
        if self.manager:
            persona = self.manager.all_personas.get(persona_id)
            if persona:
                adapter = getattr(persona, "sai_memory", None)
                if adapter and adapter.is_ready():
                    return adapter

        # フォールバック: 直接adapter作成
        try:
            from .data_paths import get_saiverse_home
            from saiverse_memory import SAIMemoryAdapter

            persona_dir = get_saiverse_home() / "personas" / persona_id
            if persona_dir.exists():
                return SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
        except Exception:
            LOGGER.warning("Failed to create SAIMemoryAdapter for %s", persona_id, exc_info=True)
        return None

    def _get_persona(self, persona_id: Optional[str]):
        """PersonaCoreインスタンスを取得。"""
        if not persona_id or not self.manager:
            return None
        return self.manager.all_personas.get(persona_id)

    def _get_memopedia(self, persona_id: Optional[str]):
        """MemopediaインスタンスをPersonaCoreから取得。"""
        persona = self._get_persona(persona_id)
        if persona:
            mem = getattr(persona, "sai_memory", None)
            if mem:
                mp = getattr(mem, "memopedia", None)
                if mp:
                    return mp

        # フォールバック: 直接Memopedia作成
        adapter = self._get_adapter(persona_id)
        if adapter and adapter.conn:
            from sai_memory.memopedia.core import Memopedia

            return Memopedia(adapter.conn, db_lock=adapter._db_lock)
        return None

    def _get_memory_conn(self, persona_id: Optional[str]) -> Optional[sqlite3.Connection]:
        """memory.db の接続を取得。"""
        adapter = self._get_adapter(persona_id)
        if adapter:
            return adapter.conn
        return None

    def _format_messages(self, messages, highlight_id: str = None) -> str:
        """メッセージリストをフォーマット。"""
        lines = []
        for msg in messages:
            dt = datetime.fromtimestamp(msg.created_at)
            ts = dt.strftime("%Y-%m-%d %H:%M")
            role = msg.role if msg.role != "model" else "assistant"
            content = (msg.content or "").strip()
            marker = " <<<" if highlight_id and msg.id == highlight_id else ""
            lines.append(f"[{role}] {ts}: {content}{marker}")
        return "\n\n".join(lines) if lines else "(no messages)"

    def _format_memopedia_page(self, page) -> str:
        """Memopediaページをフォーマット。"""
        lines = [
            f"【Memopedia】{page.title}",
            f"ID: {page.id}",
            f"Category: {page.category}",
        ]
        if hasattr(page, "keywords") and page.keywords:
            kws = page.keywords if isinstance(page.keywords, str) else json.dumps(page.keywords, ensure_ascii=False)
            lines.append(f"Keywords: {kws}")
        if page.summary:
            lines.append(f"Summary: {page.summary}")
        lines.append("")
        lines.append(page.content or "(empty)")
        return "\n".join(lines)

    def _format_chronicle_entry(self, entry) -> str:
        """Chronicleエントリをフォーマット。"""
        start = datetime.fromtimestamp(entry.start_time).strftime("%Y-%m-%d %H:%M") if entry.start_time else "?"
        end = datetime.fromtimestamp(entry.end_time).strftime("%Y-%m-%d %H:%M") if entry.end_time else "?"
        return (
            f"【Chronicle】({entry.id[:8]}...) Lv.{entry.level} | {start} ~ {end} | "
            f"{entry.message_count}件\n{entry.content}"
        )

    def _filter_lines(self, content: str, line_spec: str) -> str:
        """行範囲でフィルタ (例: "10-50")。"""
        lines = content.split("\n")
        parts = line_spec.split("-")
        try:
            start = int(parts[0]) - 1  # 1-based → 0-based
            end = int(parts[1]) if len(parts) > 1 else start + 1
            return "\n".join(lines[start:end])
        except (ValueError, IndexError):
            LOGGER.debug("Failed to parse line range '%s', returning full content", line_spec, exc_info=True)
            return content

    def _error(self, uri: str, message: str) -> ResolvedContent:
        """エラーResolvedContentを生成。"""
        return ResolvedContent(
            uri=uri,
            content=f"({message})",
            content_type="error",
            metadata={"error": message},
        )
