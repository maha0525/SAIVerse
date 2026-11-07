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
# 参照アドレッシング統一: 単語 prefix に揃え、messagelog → message。
PERSONA_RESOURCE_TYPES = {"message", "memopedia", "chronicle"}


@dataclass
class SaiUri:
    """パース済みSAIVerse URI。"""

    raw: str
    scheme: str  # "message", "memopedia", "chronicle", "item", "building", "web", "image", "document", "persona"
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

        # saiverse://self/message/msg/recent?depth=N
        if len(path) >= 2 and path[0] == "msg" and path[1] == "recent":
            from sai_memory.memory.storage import _row_to_message

            # depth=0 や負値を通すと「行があるのに空」の答えになるのでクランプする
            depth = max(1, min(int(params.get("depth", 5)), 100))
            thread_id = self._get_active_thread_id(adapter)
            visible_ids, current_pulse = self._presentation_exclusions()

            # 「直近」は、いまプロンプトに載っている行 (visible_ids) と、この
            # スペルを唱えた Pulse 自身の行を除いた中での直近。見えている行を
            # そのまま返しても想起にならない (2026-11-07 まはー指摘)。スペル
            # 以外の経路 (API / CLI) では両方空なので、従来どおり素の直近になる。
            sql = (
                "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                "FROM messages WHERE thread_id = ? "
                "AND (scope IS NULL OR scope != 'discardable') "
            )
            sql_params: List[Any] = [thread_id]
            if current_pulse:
                sql += "AND (pulse_id IS NULL OR pulse_id != ?) "
                sql_params.append(current_pulse)
            sql += "ORDER BY created_at DESC, rowid DESC"

            msgs = []
            with adapter._db_lock:
                cursor = adapter.conn.execute(sql, sql_params)
                while len(msgs) < depth:
                    rows = cursor.fetchmany(64)
                    if not rows:
                        break
                    for row in rows:
                        if str(row[0]) in visible_ids:
                            continue
                        msgs.append(_row_to_message(row))
                        if len(msgs) >= depth:
                            break
            msgs.reverse()  # oldest first (従来の並びと同じ)

            if msgs:
                content = self._format_messages(msgs)
            elif visible_ids:
                content = "(記録に残る直近のメッセージは、すべて現在のコンテキストに載っています)"
            else:
                content = "(no recent messages)"
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log",
                metadata={
                    "depth": depth,
                    "count": len(msgs),
                    "excluded_presented": len(visible_ids),
                },
            )

        # saiverse://self/message/msg?contain=TEXT&window=N&limit=N&order=new|old
        if len(path) >= 1 and path[0] == "msg" and "contain" in params:
            from sai_memory.memory.storage import (
                _row_to_message,
                author_settled_sql,
                get_window_with_anchor,
            )

            query_text = params["contain"]
            window = int(params.get("window", 0))
            limit = max(1, min(int(params.get("limit", 1)), 20))
            order = params.get("order", "new")
            if order not in ("new", "old"):
                return self._error(parsed.raw, f"Invalid order (use 'new' or 'old'): {order}")
            thread_id = self._get_active_thread_id(adapter)
            visible_ids, current_pulse = self._presentation_exclusions()

            # 本文での検索なので、書き手が決まっていない行は当たりにも周辺にも
            # 入れない (storage.author_settled_sql — 記憶の検索の経路で共有)。
            # さらに、いまプロンプトに載っている行とこのスペルを唱えた Pulse
            # 自身の行は当たりから外す — contain で検索すると、検索語を含む
            # 自分の唱え文自身が最新の当たりになってしまうため。
            direction = "ASC" if order == "old" else "DESC"
            sql = (
                "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                f"FROM messages WHERE thread_id = ? AND content LIKE ? AND {author_settled_sql()} "
            )
            sql_params = [thread_id, f"%{query_text}%"]
            if current_pulse:
                sql += "AND (pulse_id IS NULL OR pulse_id != ?) "
                sql_params.append(current_pulse)
            sql += f"ORDER BY created_at {direction}, rowid {direction}"

            hits = []
            with adapter._db_lock:
                cursor = adapter.conn.execute(sql, sql_params)
                while len(hits) < limit:
                    rows = cursor.fetchmany(64)
                    if not rows:
                        break
                    for row in rows:
                        if str(row[0]) in visible_ids:
                            continue
                        hits.append(_row_to_message(row))
                        if len(hits) >= limit:
                            break

            if not hits:
                note = ""
                if visible_ids or current_pulse:
                    note = " (現在のコンテキストに見えているメッセージは検索の対象外です)"
                return self._error(
                    parsed.raw, f"Message not found containing: {query_text}{note}",
                )

            blocks = []
            for msg in hits:
                if window > 0:
                    # 当たりを前後で挟む (前と後を別々に取る正典順。周辺は書き手が
                    # 決まっていない行を外す)
                    with adapter._db_lock:
                        surrounding = get_window_with_anchor(
                            adapter.conn, msg, window, window, settled_only=True,
                        )
                    blocks.append(self._format_messages(surrounding, highlight_id=msg.id))
                else:
                    blocks.append(self._format_messages([msg]))
            content = "\n---\n".join(blocks)

            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log" if (window > 0 or len(hits) > 1) else "message",
                metadata={
                    "message_id": hits[0].id,
                    "message_ids": [m.id for m in hits],
                    "created_at": hits[0].created_at,
                    "window": window,
                    "order": order,
                    "count": len(hits),
                },
            )

        # saiverse://self/message/{message_id}  (平坦化した単一メッセージ)
        # または saiverse://self/message/msg/{message_id} (旧クエリ面の単一形)。
        if (len(path) >= 2 and path[0] == "msg") or (
            len(path) == 1 and path[0] not in ("msg", "range", "thread", "summary")
        ):
            from sai_memory.memory.storage import get_message, get_window_with_anchor

            message_id = path[1] if path[0] == "msg" else path[0]
            window = int(params.get("window", 0))

            with adapter._db_lock:
                msg = get_message(adapter.conn, message_id)
            if not msg:
                return self._error(parsed.raw, f"Message not found: {message_id}")

            if window > 0:
                # 前と後を別々に取って anchor を挟む (正典順 created_at, rowid のまま —
                # created_at の比較で挿し込むと、同じ秒の後ろの行が anchor の前に並ぶ)
                with adapter._db_lock:
                    all_msgs = get_window_with_anchor(adapter.conn, msg, window, window)
                content = self._format_messages(all_msgs, highlight_id=message_id)
            else:
                content = self._format_messages([msg])

            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="message_log" if window > 0 else "message",
                metadata={"message_id": message_id, "window": window},
            )

        # saiverse://self/message/range?from={ts}&to={ts}
        if (len(path) == 0 or (len(path) >= 1 and path[0] == "range")) and "from" in params:
            from sai_memory.memory.storage import get_messages_last

            start_ts = int(params["from"])
            end_ts = int(params.get("to", "9999999999"))

            with adapter._db_lock:
                # 全スレッドから時間範囲のメッセージ取得
                cursor = adapter.conn.execute(
                    "SELECT id, thread_id, role, content, resource_id, created_at, metadata "
                    "FROM messages WHERE created_at >= ? AND created_at <= ? "
                    "ORDER BY created_at ASC, rowid ASC LIMIT 100",
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

        # saiverse://self/message/thread/{suffix}?last=N
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

        # saiverse://self/message/summary/{uuid}
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

        return self._error(parsed.raw, f"Unknown message path: {'/'.join(path)}")

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

        # saiverse://self/memopedia/{key}  (key = AI 可視の short_id が主、UUID も可)
        # または ?title=... 。旧 page/ 階層は平坦化済み。
        # ページ・本文・子一覧は page_snapshot で**同じロック区間**から撮る
        # (ばらばらに読むと混成表示になる — Sol レビュー 2026-08-06 F8)。
        snapshot = None
        identifier = "?"
        if len(path) >= 1:
            # 平坦化した末尾セグメントがキー。旧 `page/{id}` も後方互換で拾う。
            key = path[1] if path[0] == "page" and len(path) >= 2 else path[0]
            identifier = key
            if key.isdigit():
                key = f"memopedia:{key}"
            snapshot = memopedia.page_snapshot(page_ref=key)
        elif "title" in params:
            identifier = params.get("title", "?")
            snapshot = memopedia.page_snapshot(title=unquote(params["title"]))

        if not snapshot:
            return self._error(parsed.raw, f"Memopedia page not found: {identifier}")

        page = snapshot["page"]
        content = self._format_memopedia_page(snapshot)
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

    def _resolve_chronicle(self, parsed: SaiUri) -> ResolvedContent:
        """Chronicleエントリの解決。"""
        conn = self._get_memory_conn(parsed.persona_id)
        if not conn:
            return self._error(parsed.raw, f"Memory DB not available for {parsed.persona_id}")

        from sai_memory.arasuji.storage import get_entry, get_all_entries_ordered

        path = parsed.path_parts
        params = parsed.params

        # saiverse://self/chronicle?contain=TEXT  (旧 entry?contain も後方互換)
        if (len(path) == 0 or path[0] == "entry") and "contain" in params:
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

        # saiverse://self/chronicle/{entry_id}  (平坦化。旧 entry/{id} も後方互換)
        if (len(path) >= 2 and path[0] == "entry") or (
            len(path) == 1 and path[0] not in ("entry", "range", "recent")
        ):
            entry_id = path[1] if path[0] == "entry" else path[0]
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
            from sai_memory.arasuji.storage import get_entries_by_level

            depth = max(1, min(int(params.get("depth", 3)), 50))
            adapter = self._get_adapter(parsed.persona_id)

            # 「直近のあらすじ」は、いま見えているあらすじ (head のあらすじ枠と、
            # 提示ウィンドウで digest として見えている畳んだ範囲) に**出ていない**
            # 一番細かいあらすじ (Lv1) を新しい順に返す。head の枠は全履歴を
            # 予算内で覆っているので、見えているものをそのまま返しても想起に
            # ならない (2026-11-07 まはー指摘)。粗く束ねられて中身が隠れている
            # 範囲の、畳まれる前の細かいあらすじだけが「見えていない直近」。
            presented = self._presented_chronicle_entry_ids(
                parsed.persona_id, adapter, conn,
            )

            if adapter:
                with adapter._db_lock:
                    lv1 = get_entries_by_level(adapter.conn, 1, order_by_time=True)
            else:
                lv1 = get_entries_by_level(conn, 1, order_by_time=True)
            lv1 = [e for e in lv1 if e.origin_track_id is None]
            lv1.sort(key=lambda e: e.end_time or 0, reverse=True)
            unseen = [e for e in lv1 if e.id not in presented][:depth]

            if not unseen:
                if presented:
                    content = (
                        "(直近のあらすじは、すべて現在のコンテキストの"
                        "あらすじ枠に載っています)"
                    )
                else:
                    content = "(no chronicle data)"
            else:
                unseen.reverse()  # oldest first で読みやすく
                content = "\n---\n".join(
                    self._format_chronicle_entry(e) for e in unseen
                )
            return ResolvedContent(
                uri=parsed.raw,
                content=content,
                content_type="chronicle_context",
                metadata={
                    "depth": depth,
                    "entries": len(unseen),
                    "excluded_presented": len(presented),
                },
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
                # item_id は AI 可視の short_id が主。UUID も裏方フォールバックで受ける。
                item_service = self.manager.item_service
                item = item_service.item_dict_by_key(item_id)
                if not item:
                    return self._error(parsed.raw, f"Item not found: {item_id}")
                # 以降の href/metadata は裏方の UUID を使う。
                item_id = item.get("item_id", item_id)

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
            from manager.items import item_db_filter

            with get_session() as session:
                # short_id 主・UUID フォールバックの両対応。
                item = session.query(Item).filter(item_db_filter(item_id)).first()
                if not item:
                    return self._error(parsed.raw, f"Item not found: {item_id}")
                item_id = item.ITEM_ID  # 以降は裏方の UUID で引く

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
        "message": _resolve_messagelog,
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

    def _presentation_exclusions(self) -> Tuple[set, Optional[str]]:
        """「いま見えている行」の id 集合と、実行中 Pulse の id を返す。

        ペルソナがスペルとして resolve_uri を唱えたときだけ値が入る — スペル
        ループが実行時に tools.context へ「これから送る messages の列」と
        PulseContext を置くので、列に載っている記録の行 id (= プロンプトに実際に
        入っている行) と、唱えた Pulse の id をそこから読む。API / CLI 経由では
        両方空で、除外は掛からない。
        """
        visible: set = set()
        pulse_id: Optional[str] = None
        try:
            from tools.context import get_active_llm_messages, get_active_pulse_context

            msgs = get_active_llm_messages()
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, dict) and m.get("id"):
                        visible.add(str(m["id"]))
            pulse_ctx = get_active_pulse_context()
            raw = getattr(pulse_ctx, "pulse_id", None)
            if raw:
                pulse_id = str(raw)
        except Exception:
            LOGGER.debug("Failed to read presentation context for exclusion", exc_info=True)
        return visible, pulse_id

    def _presented_chronicle_entry_ids(self, persona_id, adapter, conn) -> set:
        """いまペルソナに見えているあらすじ entry の id 集合 (近似)。

        供給源は二つ:

        1. 提示ウィンドウの畳んだ範囲 (fold) — その範囲はあらすじ entry の本文が
           digest としてそのまま窓に見えている (sea/session_window.py)。
        2. head のあらすじ枠 (memory weave) — head が使うのと同じ選定
           (sai_memory/arasuji/context.get_episode_context、ペルソナの文字数予算、
           fold の除外名簿) をいまの記録で回して再現する。head の実物は
           Metabolism 時点の凍結スナップショットなので厳密には一致しないことが
           あるが、ズレるのは「凍結後に増えた最新 entry」で、その期間の中身は
           窓の生ログとして見えている。

        weave が無効なペルソナは 1 だけ。manager が無い経路 (CLI / 単体テスト)
        では空集合 — 除外なしの素の一覧になる。
        """
        presented: set = set()
        if not self.manager or not persona_id:
            return presented

        folded: set = set()
        try:
            from sea.session_lifecycle import collect_folded_chronicle_entry_ids

            collected = collect_folded_chronicle_entry_ids(self.manager, persona_id)
            # None = 照会失敗 (fold の有無が分からない)。ここは提示の近似なので
            # 空として続行する (吸収と違い、ズレても読み物が少し重複するだけ)。
            folded = {str(x) for x in (collected or ())}
            presented |= folded
        except Exception:
            LOGGER.debug(
                "Failed to collect folded chronicle entry ids for %s",
                persona_id, exc_info=True,
            )

        if not self._memory_weave_enabled(persona_id):
            return presented

        try:
            from builtin_data.tools.get_memory_weave_context import (
                _resolve_persona_chronicle_budget,
            )
            from sai_memory.arasuji.context import USE_DEFAULT_BUDGET, get_episode_context

            budget = _resolve_persona_chronicle_budget(persona_id, USE_DEFAULT_BUDGET)
            if adapter:
                with adapter._db_lock:
                    ctx = get_episode_context(
                        adapter.conn, max_entries=10_000, char_budget=budget,
                        exclude_entry_ids=folded or None,
                    )
            else:
                ctx = get_episode_context(
                    conn, max_entries=10_000, char_budget=budget,
                    exclude_entry_ids=folded or None,
                )
            presented |= {str(e.source_id) for e in ctx if e.level >= 1}
        except Exception:
            LOGGER.warning(
                "Failed to reproduce presented chronicle selection for %s",
                persona_id, exc_info=True,
            )
        return presented

    def _memory_weave_enabled(self, persona_id) -> bool:
        """head のあらすじ枠 (memory weave) がこのペルソナで有効か。

        sea/head_pipeline/sections/memory_weave.py の _resolve_enabled と同じ
        判定 (AI.MEMORY_WEAVE_CONTEXT)。読めなければ False (= 枠は見えていない
        扱い — 除外が減る側に倒す)。
        """
        session_factory = getattr(self.manager, "SessionLocal", None)
        if not session_factory:
            return False
        db = session_factory()
        try:
            from database.models import AI as AIModel

            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return bool(ai.MEMORY_WEAVE_CONTEXT) if ai else False
        except Exception:
            LOGGER.warning(
                "Failed to resolve MEMORY_WEAVE_CONTEXT for %s", persona_id,
                exc_info=True,
            )
            return False
        finally:
            db.close()

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

    def _format_memopedia_page(self, snapshot: dict) -> str:
        """Memopediaページをフォーマット。

        本文は ``render_page_body`` で描く——v0.3.x はページの知識が Fragment
        側にあり、``page.content`` を直に出すと変換済みページが全部 "(empty)"
        に見える。子ページの一覧も親子関係からここで組み立てる（本文には
        導線を書かない設計の表示側、memopedia_body_to_fragment.md §7 (a)）。

        受け取るのは :meth:`Memopedia.page_snapshot` の戻り——ページ・本文・
        子一覧が同じ時点の姿であることは、撮る側が保証している。
        """
        page = snapshot["page"]
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
        lines.append(snapshot["body"] or "(empty)")
        children = snapshot["children"]
        if children:
            lines.append("")
            lines.append("子ページ:")
            for child in children:
                ref = f"m:{child.short_id}" if getattr(child, "short_id", None) else child.id
                lines.append(f"- {child.title} ({ref})")
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
