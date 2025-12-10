from __future__ import annotations

import json
import logging
import math
import textwrap
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import gradio as gr
import pandas as pd
from gradio.events import SelectData

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memory.chunking import chunk_text
from sai_memory.memory.storage import compose_message_content, get_messages_paginated, replace_message_embeddings
from scripts.import_chatgpt_conversations import (
    build_summary_rows,
    format_datetime,
    load_export,
    parse_roles,
    resolve_selection,
    resolve_thread_suffix,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 100
PAGE_SIZE_CHOICES = [50, 100, 200, 500]
IMPORT_DATATYPES = ["bool", "number", "str", "str", "str", "number", "str"]


def _persona_choices(manager) -> List[tuple[str, str]]:
    choices: List[tuple[str, str]] = []
    if not manager:
        return choices
    for persona_id, persona in manager.personas.items():
        display = persona.persona_name or persona_id
        choices.append((display, persona_id))
    choices.sort(key=lambda item: item[0])
    return choices


def _empty_import_table() -> pd.DataFrame:
    return pd.DataFrame(columns=["Import", "Idx", "ID", "Title", "Created (UTC)", "Updated (UTC)", "Msgs", "Preview"])


def _load_chatgpt_summary(export_path: Optional[str], preview: int) -> tuple[pd.DataFrame, str, Dict[str, Any]]:
    if not export_path:
        empty = _empty_import_table()
        return empty, "ファイルを選ぶとここに一覧が出るよ。", {"path": None, "count": 0}
    try:
        export = load_export(export_path)
    except Exception as exc:
        LOGGER.warning("Failed to load ChatGPT export: %s", exc, exc_info=True)
        empty = _empty_import_table()
        return empty, f"読み込みでエラーが出たよ: {exc}", {"path": None, "count": 0}

    records = export.conversations
    if not records:
        empty = _empty_import_table()
        return empty, "会話が見つからなかったよ。", {"path": export_path, "count": 0}

    headers, rows = build_summary_rows(records, preview)
    table = pd.DataFrame(rows, columns=headers)
    table.insert(0, "Import", False)
    message = "インポートしたい行の『Import』列にチェックを入れてね。"
    return table, message, {"path": export_path, "count": len(records)}


def _extract_selected_indices(table_data: Any) -> List[int]:
    if table_data is None:
        return []
    if isinstance(table_data, pd.DataFrame):
        df = table_data.copy()
    else:
        try:
            df = pd.DataFrame(table_data)
        except Exception:
            return []

    # Normalize column names when Gradio sends without headers
    if "Import" not in df.columns and df.shape[1] >= 2:
        df.columns = ["Import", "Idx"] + [f"col_{i}" for i in range(2, df.shape[1])]

    if "Import" not in df.columns or "Idx" not in df.columns:
        return []

    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        text = str(value).strip().lower()
        return text in {"true", "1", "yes", "y", "on"}

    df["Import"] = df["Import"].map(_as_bool)

    selected = df[df["Import"]]
    indices: List[int] = []
    for raw in selected["Idx"].tolist():
        try:
            indices.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    return indices


def _acquire_adapter(manager, persona_id: str) -> tuple[Optional[SAIMemoryAdapter], bool]:
    if not manager or not persona_id:
        return None, False
    persona = manager.personas.get(persona_id)
    if persona and getattr(persona, "sai_memory", None) and persona.sai_memory.is_ready():
        return persona.sai_memory, False
    try:
        adapter = SAIMemoryAdapter(persona_id)
    except Exception as exc:
        LOGGER.error("Failed to initialise SAIMemory adapter for %s: %s", persona_id, exc, exc_info=True)
        return None, False
    return adapter, True


def _empty_thread_table() -> pd.DataFrame:
    return pd.DataFrame(columns=["Thread ID", "Suffix", "Active", "Preview"])


def _empty_message_table() -> pd.DataFrame:
    return pd.DataFrame(columns=["Idx", "Message ID", "Role", "Tags", "Timestamp", "Preview"])


def _initial_message_state() -> Dict[str, Any]:
    return {
        "thread_id": None,
        "messages": {},
        "order": [],
        "page": 1,
        "page_size": DEFAULT_PAGE_SIZE,
        "total": 0,
        "total_pages": 0,
        "selected_id": None,
        "selected_info": "メッセージを選んでね。",
        "selected_content": "",
    }


def _format_page_summary(total: int, page: int, total_pages: int) -> str:
    if total <= 0:
        return "メッセージを読み込むと表示するよ。"
    if total_pages <= 0:
        total_pages = 1
    page = max(1, min(page, total_pages))
    return f"全{total}件 / {total_pages}ページ (現在 {page}/{total_pages})"


def _sanitize_page_size(value: Any) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    if size <= 0:
        size = DEFAULT_PAGE_SIZE
    if size > 1000:
        size = 1000
    return size


def _update_page_metadata(state: Dict[str, Any]) -> tuple[gr.Update, str, gr.Update, gr.Update, gr.Update, gr.Update]:
    total = int(state.get("total") or 0)
    page_size = _sanitize_page_size(state.get("page_size"))
    state["page_size"] = page_size
    total_pages = int(state.get("total_pages") or (math.ceil(total / page_size) if page_size else 0))
    page = int(state.get("page") or 1)
    if total_pages == 0 and total > 0:
        total_pages = max(1, math.ceil(total / page_size))
    if total_pages and page > total_pages:
        page = total_pages
    if page < 1:
        page = 1
    state["total"] = total
    state["total_pages"] = total_pages
    state["page"] = page
    summary = _format_page_summary(total, page, total_pages)
    page_update = gr.update(value=page, interactive=total > 0)
    prev_update = gr.update(interactive=total > 0 and page > 1)
    next_update = gr.update(interactive=total > 0 and total_pages > 0 and page < total_pages)
    page_size_update = gr.update(value=str(page_size), interactive=bool(state.get("thread_id")))
    go_update = gr.update(interactive=total > 0)
    return page_update, summary, prev_update, next_update, page_size_update, go_update


def _count_thread_messages(conn, thread_id: str) -> int:
    try:
        cur = conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?", (thread_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        LOGGER.exception("Failed to count messages for thread %s", thread_id)
        return 0


def _message_noop_response(state: Dict[str, Any], note: str):
    page_update, summary, prev_update, next_update, page_size_update, go_update = _update_page_metadata(state)
    selected_info = state.get("selected_info", "メッセージを選んでね。")
    content = state.get("selected_content", "")
    edit_interactive = bool(state.get("selected_id"))
    result = (
        gr.update(),  # message_table
        state,
        selected_info,
        gr.update(value=content),
        gr.update(value=content, interactive=edit_interactive),
        note,
        page_update,
        summary,
        prev_update,
        next_update,
        page_size_update,
        go_update,
    )
    LOGGER.debug(
        "[memory-settings] message noop",
        extra={
            "thread_id": state.get("thread_id"),
            "note": note,
            "selected_id": state.get("selected_id"),
        },
    )
    return result


def _refresh_threads(manager, persona_id: str, page_size_value: Any):
    page_size = _sanitize_page_size(page_size_value)
    if not persona_id:
        state = _initial_message_state()
        LOGGER.debug("[memory-settings] refresh skipped: persona missing")
        return (
            _empty_thread_table(),
            [],
            [],
            gr.update(value=None),
            "ペルソナを選んでから更新してね。",
            *_message_noop_response(state, "ペルソナを選んでから更新してね。"),
        )

    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        state = _initial_message_state()
        LOGGER.debug("[memory-settings] refresh skipped: adapter unavailable", extra={"persona_id": persona_id})
        return (
            _empty_thread_table(),
            [],
            [],
            None,
            "SAIMemoryが利用できなかったよ。",
            *_message_noop_response(state, "SAIMemoryが利用できなかったよ。"),
        )

    try:
        summaries = adapter.list_thread_summaries()
    except Exception as exc:
        LOGGER.warning("Failed to list threads for %s: %s", persona_id, exc, exc_info=True)
        summaries = []
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after listing threads", exc_info=True)

    rows: List[List[Any]] = []
    active_value: Optional[str] = None
    for summary in summaries:
        thread_id = summary.get("thread_id") or summary.get("suffix") or ""
        suffix = summary.get("suffix") or thread_id
        if summary.get("active") and not active_value:
            active_value = thread_id
        rows.append(
            [
                thread_id,
                suffix,
                "✓" if summary.get("active") else "",
                summary.get("preview", ""),
            ]
        )

    LOGGER.debug(
        "[memory-settings] refresh summaries prepared",
        extra={
            "persona_id": persona_id,
            "summary_count": len(summaries),
            "sample_ids": [rows[0][0]] if rows else [],
        },
    )

    table = pd.DataFrame(rows, columns=["Thread ID", "Suffix", "Active", "Preview"]) if rows else _empty_thread_table()
    message = f"{len(summaries)}件のスレッドを取得したよ。" if summaries else "スレッドがまだ無いみたい。"
    default_value = active_value or (rows[0][0] if rows else None)

    if default_value:
        LOGGER.debug(
            "[memory-settings] refresh default thread",
            extra={"persona_id": persona_id, "thread_id": default_value, "summary_count": len(summaries)},
        )
        message_tuple = _load_thread_messages(manager, persona_id, default_value, -1, page_size)
    else:
        empty_state = _initial_message_state()
        LOGGER.debug(
            "[memory-settings] refresh has no threads",
            extra={"persona_id": persona_id, "summary_count": len(summaries)},
        )
        message_tuple = _message_noop_response(empty_state, "スレッドがまだ無いみたい。")

    table_preview = None
    try:
        table_preview = table.head(3).to_dict() if hasattr(table, "head") else None
    except Exception:
        table_preview = None

    LOGGER.debug(
        "[memory-settings] refresh outputs snapshot",
        extra={
            "persona_id": persona_id,
            "table_rows": len(rows),
            "summary_count": len(summaries),
            "default_thread": default_value,
            "message_note": message,
            "table_preview": table_preview,
        },
    )

    return (
        table,
        summaries,
        default_value,
        message,
        *message_tuple,
    )


def _load_thread_messages(manager, persona_id: str, thread_id: Optional[str], page: int, page_size_value: Any):
    if not persona_id or not thread_id:
        state = _initial_message_state()
        state["thread_id"] = thread_id
        LOGGER.debug(
            "[memory-settings] load thread skipped",
            extra={"persona_id": persona_id, "thread_id": thread_id, "reason": "missing persona or thread"},
        )
        return _message_noop_response(state, "スレッドを先に選んでね。")

    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        state = _initial_message_state()
        state["thread_id"] = thread_id
        LOGGER.debug(
            "[memory-settings] load thread skipped",
            extra={"persona_id": persona_id, "thread_id": thread_id, "reason": "adapter unavailable"},
        )
        return _message_noop_response(state, "SAIMemoryが利用できなかったよ。")

    page_size = _sanitize_page_size(page_size_value)
    page = int(page if page is not None else 1)
    load_latest = page <= 0
    if page < 1:
        page = 1
    state = _initial_message_state()
    state["thread_id"] = thread_id
    state["page_size"] = page_size

    total_count = 0
    total_count = 0
    try:
        LOGGER.debug(
            "[memory-settings] loading thread",
            extra={"persona_id": persona_id, "thread_id": thread_id, "page": page, "page_size": page_size},
        )
        with adapter._db_lock:  # type: ignore[attr-defined]
            total_count = _count_thread_messages(adapter.conn, thread_id)
            total_pages = max(1, math.ceil(total_count / page_size)) if total_count > 0 else 0
            if total_pages and load_latest:
                page = total_pages
            elif total_pages and page > total_pages:
                page = total_pages
            if page < 1:
                page = 1
            effective_page = page - 1 if total_count > 0 else 0
            rows = get_messages_paginated(adapter.conn, thread_id, page=effective_page, page_size=page_size)

            messages_map: Dict[str, Dict[str, Any]] = {}
            table_rows: List[List[Any]] = []
            order: List[str] = []
            for idx, msg in enumerate(rows):
                content = compose_message_content(adapter.conn, msg) or ""
                ts = datetime.fromtimestamp(msg.created_at, tz=timezone.utc)
                iso = format_datetime(ts)
                normalized = " ".join(content.split())
                preview = normalized[:80] + ("…" if len(normalized) > 80 else "")
                tags_value = "-"
                raw_tags = None
                if isinstance(msg.metadata, dict):
                    raw_tags = msg.metadata.get("tags")
                elif isinstance(msg.metadata, list):
                    raw_tags = msg.metadata
                tag_list: List[str] = []
                if isinstance(raw_tags, list):
                    tag_list = [str(tag) for tag in raw_tags if tag]
                elif isinstance(raw_tags, str):
                    tag_list = [raw_tags]
                if tag_list:
                    tags_value = ", ".join(tag_list)
                order.append(msg.id)
                table_rows.append([idx + 1 + (page - 1) * page_size, msg.id, msg.role, tags_value, iso or "-", preview])
                messages_map[msg.id] = {"content": content, "role": msg.role, "timestamp": iso or "-", "thread_id": msg.thread_id}
    except Exception as exc:
        LOGGER.warning("Failed to load messages for thread %s: %s", thread_id, exc, exc_info=True)
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after load", exc_info=True)
        return _message_noop_response(state, f"メッセージ取得でエラー: {exc}")
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after load", exc_info=True)

    state["messages"] = messages_map
    state["order"] = order
    state["page"] = page
    state["page_size"] = page_size
    state["total"] = total_count
    state["total_pages"] = total_pages

    selected_id = order[0] if order else None
    state["selected_id"] = selected_id

    if selected_id:
        info = messages_map[selected_id]
        selected_info = f"選択中: {info['role']} @ {info['timestamp']} (ID: {selected_id})"
        selected_content = info["content"]
    else:
        selected_info = "メッセージを選んでね。"
        selected_content = ""

    state["selected_info"] = selected_info
    state["selected_content"] = selected_content

    table = pd.DataFrame(table_rows, columns=["Idx", "Message ID", "Role", "Tags", "Timestamp", "Preview"]) if table_rows else _empty_message_table()
    note = f"{len(order)}件のメッセージを読み込んだよ。" if order else "このスレッドにはメッセージが無いみたい。"

    LOGGER.debug(
        "[memory-settings] thread messages loaded",
        extra={
            "persona_id": persona_id,
            "thread_id": thread_id,
            "page": page,
            "page_size": page_size,
            "total": total_count,
            "rows_loaded": len(order),
        },
    )

    page_update, summary, prev_update, next_update, page_size_update, go_update = _update_page_metadata(state)
    current_update = gr.update(value=selected_content)
    edit_update = gr.update(value=selected_content, interactive=bool(order))

    return (
        table,
        state,
        selected_info,
        current_update,
        edit_update,
        note,
        page_update,
        summary,
        prev_update,
        next_update,
        page_size_update,
        go_update,
    )


def _on_message_select(select_data: SelectData, message_state_value: Dict[str, Any]):
    LOGGER.debug(
        "[memory-settings] message select handler - type=%s has_order=%s",
        type(select_data).__name__,
        bool((message_state_value or {}).get("order")),
    )

    payload = select_data if isinstance(select_data, SelectData) else None
    state = dict(message_state_value or _initial_message_state())
    order = state.get("order") or []

    if not order or not payload or payload.index is None:

        return state, state.get("selected_info", "メッセージを選んでね。"), gr.update(value=state.get("selected_content", "")), gr.update(value=state.get("selected_content", ""), interactive=bool(state.get("selected_id")))

    idx = payload.index
    if isinstance(idx, (list, tuple)):
        row_key = idx[0]
    else:
        row_key = idx

    try:
        row_pos = int(row_key)
    except (TypeError, ValueError):
        if isinstance(row_key, str) and row_key in order:
            row_pos = order.index(row_key)
        else:
            return state, state.get("selected_info", "メッセージを選んでね。"), gr.update(value=state.get("selected_content", "")), gr.update(value=state.get("selected_content", ""), interactive=bool(state.get("selected_id")))

    if row_pos < 0 or row_pos >= len(order):
        return state, state.get("selected_info", "メッセージを選んでね。"), gr.update(value=state.get("selected_content", "")), gr.update(value=state.get("selected_content", ""), interactive=bool(state.get("selected_id")))

    message_id = order[row_pos]
    info = state.get("messages", {}).get(message_id)
    if not info:
        return state, state.get("selected_info", "メッセージを選んでね。"), gr.update(value=state.get("selected_content", "")), gr.update(value=state.get("selected_content", ""), interactive=bool(state.get("selected_id")))
    state["selected_id"] = message_id
    state["selected_info"] = f"選択中: {info['role']} @ {info['timestamp']} (ID: {message_id})"
    state["selected_content"] = info["content"]
    LOGGER.debug(
        "[memory-settings] message selected",
        extra={
            "message_id": message_id,
            "order_size": len(order),
            "row_position": row_pos,
        },
    )
    return state, state["selected_info"], gr.update(value=info["content"]), gr.update(value=info["content"], interactive=True)


def _change_page(manager, persona_id: str, message_state: Dict[str, Any], *, delta: int = 0, explicit_page: Optional[int] = None, new_page_size: Optional[int] = None):
    state = dict(message_state or _initial_message_state())
    thread_id = state.get("thread_id")
    if not thread_id:
        return _message_noop_response(state, "スレッドを先に読み込んでね。")
    page_size = _sanitize_page_size(new_page_size if new_page_size is not None else state.get("page_size", DEFAULT_PAGE_SIZE))
    page = int(state.get("page", 1))
    if explicit_page is not None:
        page = max(1, int(explicit_page))
    else:
        page = max(1, page + delta)
    return _load_thread_messages(manager, persona_id, thread_id, page, page_size)


def _update_message(manager, persona_id: str, message_state: Dict[str, Any], new_content: str):
    state = dict(message_state or _initial_message_state())
    thread_id = state.get("thread_id")
    message_id = state.get("selected_id")
    if not persona_id or not thread_id:
        return _message_noop_response(state, "スレッドを先に読み込んでね。")
    if not message_id:
        return _message_noop_response(state, "更新するメッセージを選んでね。")
    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        return _message_noop_response(state, "SAIMemoryが利用できなかったよ。")
    note = ""
    try:
        with adapter._db_lock:  # type: ignore[attr-defined]
            cur = adapter.conn.execute("SELECT role, metadata FROM messages WHERE id=?", (message_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError("対象のメッセージが見つからなかったよ。")
            _, metadata_json = row
            metadata = json.loads(metadata_json) if metadata_json else None
            adapter.conn.execute(
                "UPDATE messages SET content=?, metadata=? WHERE id=?",
                (new_content, json.dumps(metadata, ensure_ascii=False) if metadata else None, message_id),
            )
            adapter.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))
            content_strip = new_content.strip()
            if content_strip and adapter.embedder is not None:
                chunks = chunk_text(
                    content_strip,
                    min_chars=adapter.settings.chunk_min_chars,
                    max_chars=adapter.settings.chunk_max_chars,
                )
                payload = [chunk.strip() for chunk in chunks if chunk and chunk.strip()]
                if payload:
                    vectors = adapter.embedder.embed(payload, is_query=False)
                    replace_message_embeddings(adapter.conn, message_id, vectors)
            adapter.conn.commit()
            note = "メッセージを更新したよ。"
    except Exception as exc:
        LOGGER.warning("Failed to update message %s: %s", message_id, exc, exc_info=True)
        note = f"更新でエラーが出たよ: {exc}"
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after update", exc_info=True)

    page = state.get("page", 1)
    page_size = state.get("page_size", DEFAULT_PAGE_SIZE)
    table_tuple = _load_thread_messages(manager, persona_id, thread_id, page, page_size)
    # inject note
    table, new_state, info, current_update, edit_update, load_note, page_update, summary, prev_update, next_update, page_size_update, go_update = table_tuple
    combined_note = f"{note}\n{load_note}".strip() if load_note else note
    return (
        table,
        new_state,
        info,
        current_update,
        edit_update,
        combined_note,
        page_update,
        summary,
        prev_update,
        next_update,
        page_size_update,
        go_update,
    )


def _delete_message(manager, persona_id: str, message_state: Dict[str, Any]):
    state = dict(message_state or _initial_message_state())
    thread_id = state.get("thread_id")
    message_id = state.get("selected_id")
    if not persona_id or not thread_id:
        return _message_noop_response(state, "スレッドを先に読み込んでね。")
    if not message_id:
        return _message_noop_response(state, "削除するメッセージを選んでね。")
    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        return _message_noop_response(state, "SAIMemoryが利用できなかったよ。")
    note = ""
    try:
        with adapter._db_lock:  # type: ignore[attr-defined]
            adapter.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))
            adapter.conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
            adapter.conn.commit()
            note = "メッセージを削除したよ。"
    except Exception as exc:
        LOGGER.warning("Failed to delete message %s: %s", message_id, exc, exc_info=True)
        note = f"削除に失敗したよ: {exc}"
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after delete", exc_info=True)

    page = state.get("page", 1)
    page_size = state.get("page_size", DEFAULT_PAGE_SIZE)
    table_tuple = _load_thread_messages(manager, persona_id, thread_id, page, page_size)
    table, new_state, info, current_update, edit_update, load_note, page_update, summary, prev_update, next_update, page_size_update, go_update = table_tuple
    combined_note = f"{note}\n{load_note}".strip() if load_note else note
    return (
        table,
        new_state,
        info,
        current_update,
        edit_update,
        combined_note,
        page_update,
        summary,
        prev_update,
        next_update,
        page_size_update,
        go_update,
    )


def _import_chatgpt_conversations(
    manager,
    persona_id: str,
    export_info: Dict[str, Any],
    table_data: Any,
    roles_text: str,
    thread_suffix_override: str,
    include_header: bool,
    dry_run: bool,
    select_all: bool,
) -> str:
    export_path = export_info.get("path") if isinstance(export_info, dict) else None
    if not persona_id:
        return "先にペルソナを選んでね。"
    if not export_path:
        return "ChatGPTのエクスポートファイルを先に読み込もう。"

    try:
        export = load_export(export_path)
    except Exception as exc:
        LOGGER.warning("Failed to re-load ChatGPT export: %s", exc, exc_info=True)
        return f"エクスポートの読み直しで失敗したよ: {exc}"

    records = export.conversations
    if not records:
        return "会話が一件も見つからなかったよ。"

    if select_all:
        selected_records = list(records)
    else:
        selected_indices = _extract_selected_indices(table_data)
        if not selected_indices:
            return "インポートする行の『Import』列にチェックを入れてね。"
        selectors = [str(idx) for idx in selected_indices]
        try:
            selected_records = resolve_selection(records, selectors)
        except ValueError as exc:
            return f"選択内容に誤りがあったよ: {exc}"

    if not selected_records:
        return "条件に合う会話が見つからなかったよ。"

    allowed_roles = parse_roles(roles_text)
    include_roles: Optional[Sequence[str]] = list(allowed_roles) if allowed_roles else None

    adapter: Optional[SAIMemoryAdapter] = None
    release_adapter = False
    if not dry_run:
        adapter, release_adapter = _acquire_adapter(manager, persona_id)
        if not adapter or not adapter.is_ready():
            return "SAIMemoryが使えなかったよ。設定を確認してみて。"

    utc_now = datetime.now(timezone.utc)
    results: List[str] = []
    LOGGER.debug(
        "Import request: persona=%s selected=%s select_all=%s roles=%s dry_run=%s",
        persona_id,
        [rec.identifier for rec in selected_records],
        select_all,
        include_roles,
        dry_run,
    )
    try:
        for record in selected_records:
            payloads = list(record.iter_memory_payloads(include_roles=include_roles))
            thread_suffix = resolve_thread_suffix(record, thread_suffix_override or None)
            header_ts = record.create_time or record.update_time or utc_now
            header_timestamp = format_datetime(header_ts)
            origin_id = record.conversation_id or record.identifier
            if include_header:
                header_text = (
                    f"[Imported ChatGPT conversation \"{record.title}\" "
                    f"({origin_id}) created {format_datetime(header_ts)}]"
                )
                payloads.insert(
                    0,
                    {
                        "role": "system",
                        "content": header_text,
                        "timestamp": header_timestamp,
                    },
                )

            if dry_run:
                results.append(f"[dry-run] {record.title} ({len(payloads)}件) thread={thread_suffix}")
                continue

            if not adapter:
                return "インポート用のアダプターが準備できなかったよ。"

            for payload in payloads:
                adapter.append_persona_message(payload, thread_suffix=thread_suffix)
            results.append(f"[imported] {record.title} ({len(payloads)}件) thread={thread_suffix}")
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary SAIMemory adapter for %s", persona_id, exc_info=True)

    header_note = "実際には書き込んでいないよ (dry-run)" if dry_run else "SAIMemoryに書き込んだよ"
    joined = "\n".join(results)
    return f"{header_note}:\n{joined}"


def _coerce_summaries(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _format_select_payload(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {"type": None}
    data = {"type": type(payload).__name__}
    index = getattr(payload, "index", None)
    value = getattr(payload, "value", None)
    data["index"] = index
    data["value"] = value
    return data


def _format_table_preview(table_value: Any, limit: int = 3) -> Dict[str, Any]:
    preview: Dict[str, Any] = {"type": type(table_value).__name__}
    if isinstance(table_value, list):
        preview["rows"] = table_value[:limit]
    elif hasattr(table_value, "value"):
        try:
            preview["rows"] = table_value.value[:limit]
        except Exception:
            preview["rows"] = None
    else:
        preview["rows"] = None
    return preview


def _load_selected_thread(manager, select_data: SelectData, summaries: List[Dict[str, Any]], persona_id: str, page_size_value: Any):
    payload = select_data if isinstance(select_data, SelectData) else None
    summaries_list = _coerce_summaries(summaries)
    preview_ids = [item.get("thread_id") for item in summaries_list[:3]]
    payload_info = _format_select_payload(payload)
    LOGGER.debug(
        "[memory-settings] _load_selected_thread - persona_id=%s summary_count=%s",
        persona_id,
        len(summaries_list),
    )
    if not payload or payload.index is None:
        LOGGER.debug(
            "[memory-settings] thread select ignored",
            extra={
                "persona_id": persona_id,
                "reason": "missing payload/index",
                "summary_count": len(summaries_list),
                "select_payload": payload_info,
            },
        )
        state = _initial_message_state()
        return (None, *_message_noop_response(state, "スレッドを選んでね。"))

    idx = payload.index
    row_index = idx[0] if isinstance(idx, (tuple, list)) else idx
    if not isinstance(row_index, int) or row_index >= len(summaries_list):
        LOGGER.debug(
            "[memory-settings] thread select invalid index",
            extra={
                "persona_id": persona_id,
                "row_index": row_index,
                "summary_count": len(summaries_list),
                "select_payload": payload_info,
            },
        )
        state = _initial_message_state()
        return (None, *_message_noop_response(state, "スレッドを選んでね。"))

    item = summaries_list[row_index]
    thread_id = item.get("thread_id") or item.get("suffix")
    if not thread_id:
        LOGGER.debug(
            "[memory-settings] thread select missing id",
            extra={"persona_id": persona_id, "row_index": row_index},
        )
        state = _initial_message_state()
        return (None, *_message_noop_response(state, "スレッドIDを取得できなかったよ。"))

    page_size = _sanitize_page_size(page_size_value)
    if not persona_id:
        state = _initial_message_state()
        state["thread_id"] = thread_id
        return (thread_id, *_message_noop_response(state, "ペルソナを選んでから更新してね。"))

    LOGGER.debug(
        "[memory-settings] thread selected",
        extra={
            "persona_id": persona_id,
            "thread_id": thread_id,
            "row_index": row_index,
            "summary_count": len(summaries_list),
        },
    )

    return (
        thread_id,
        *_load_thread_messages(manager, persona_id, thread_id, -1, page_size),
    )


def create_memory_settings_ui(manager) -> None:
    choices = _persona_choices(manager)
    persona_ids = [pid for _, pid in choices]
    default_persona = persona_ids[0] if persona_ids else None

    gr.Markdown("### メモリー設定\nペルソナごとの長期記憶を管理するよ。最初に対象のペルソナを選んでね。")
    persona_dropdown = gr.Dropdown(
        choices=[label for label, _ in choices] if choices else [],
        value=choices[0][0] if choices else None,
        label="ペルソナ",
        interactive=bool(choices),
    )
    persona_id_state = gr.State(default_persona)

    def _update_persona(selected_label: Optional[str], current_id: Optional[str]):
        if not choices:
            return current_id, "ペルソナが見つからなかったよ。"
        mapping = {label: pid for label, pid in choices}
        persona_id = mapping.get(selected_label) if selected_label else None
        if not persona_id:
            return current_id, "そのペルソナは今使えないみたい。"
        return persona_id, f"対象ペルソナ: {selected_label} ({persona_id})"

    persona_status = gr.Markdown(f"対象ペルソナ: {choices[0][0]} ({choices[0][1]})" if choices else "対象ペルソナがまだ無いよ。")
    persona_change_event = persona_dropdown.change(
        fn=_update_persona,
        inputs=[persona_dropdown, persona_id_state],
        outputs=[persona_id_state, persona_status],
        show_progress="hidden",
    )

    with gr.Accordion("ChatGPTエクスポートからのインポート", open=False):
        with gr.Row():
            chatgpt_file = gr.File(label="conversations.json または ZIP", type="filepath", interactive=bool(choices))
            preview_slider = gr.Slider(40, 240, value=120, step=10, label="プレビュー文字数", interactive=bool(choices))
        chatgpt_table = gr.DataFrame(
            value=_empty_import_table(),
            interactive=bool(choices),
            datatype=IMPORT_DATATYPES,
            type="pandas",
        )
        chatgpt_info = gr.Markdown("ファイルを選ぶとここに一覧が出るよ。")
        export_state = gr.State({"path": None, "count": 0})

        chatgpt_file.change(
            fn=lambda path, preview: _load_chatgpt_summary(path, int(preview)),
            inputs=[chatgpt_file, preview_slider],
            outputs=[chatgpt_table, chatgpt_info, export_state],
            show_progress=True,
        )
        preview_slider.change(
            fn=lambda preview, state: _load_chatgpt_summary(state.get("path"), int(preview)),
            inputs=[preview_slider, export_state],
            outputs=[chatgpt_table, chatgpt_info, export_state],
            show_progress="hidden",
        )

        with gr.Row():
            roles_box = gr.Textbox(value="user,assistant", label="取得する役割", placeholder="カンマ区切り (空ならすべて)", interactive=bool(choices))
            thread_suffix_box = gr.Textbox(label="スレッド接尾辞 (任意)", placeholder="空なら会話IDを使うよ", interactive=bool(choices))
        with gr.Row():
            header_checkbox = gr.Checkbox(value=True, label="システムヘッダーを追加する", interactive=bool(choices))
            dry_run_checkbox = gr.Checkbox(value=False, label="dry-run (書き込まない)", interactive=bool(choices))
        import_feedback = gr.Textbox(label="結果", lines=6, interactive=False)

        import_button = gr.Button("チェックした会話をインポート", variant="primary", interactive=bool(choices))
        import_all_button = gr.Button("全件インポート", variant="secondary", interactive=bool(choices))

        import_button.click(
            fn=lambda persona_id, state, table, roles, suffix, header, dry_run: _import_chatgpt_conversations(
                manager,
                persona_id,
                state,
                table,
                roles,
                suffix,
                bool(header),
                bool(dry_run),
                False,
            ),
            inputs=[persona_id_state, export_state, chatgpt_table, roles_box, thread_suffix_box, header_checkbox, dry_run_checkbox],
            outputs=import_feedback,
            show_progress=True,
        )
        import_all_button.click(
            fn=lambda persona_id, state, roles, suffix, header, dry_run: _import_chatgpt_conversations(
                manager,
                persona_id,
                state,
                None,
                roles,
                suffix,
                bool(header),
                bool(dry_run),
                True,
            ),
            inputs=[persona_id_state, export_state, roles_box, thread_suffix_box, header_checkbox, dry_run_checkbox],
            outputs=import_feedback,
            show_progress=True,
        )

    thread_summaries_state = gr.State([])
    thread_selected_state = gr.State(None)
    message_state = gr.State(_initial_message_state())

    gr.Markdown("#### SAIMemoryスレッド管理")
    with gr.Accordion("長期記憶のスレッドとメッセージを管理する", open=False):
        refresh_threads_btn = gr.Button("スレッド一覧を更新", variant="secondary", interactive=bool(choices))
        thread_feedback = gr.Markdown("")
        thread_table = gr.DataFrame(value=_empty_thread_table(), interactive=False)

        with gr.Row():
            page_number_input = gr.Number(value=1, precision=0, label="ページ", interactive=False)
            page_size_dropdown = gr.Dropdown(
                choices=[str(v) for v in PAGE_SIZE_CHOICES],
                value=str(DEFAULT_PAGE_SIZE),
                label="1ページの件数",
                interactive=bool(choices),
            )
            message_page_summary = gr.Markdown("メッセージを読み込むと表示するよ。")
        with gr.Row():
            prev_page_btn = gr.Button("← 前", interactive=False)
            next_page_btn = gr.Button("次 →", interactive=False)
            go_page_btn = gr.Button("指定ページへ", interactive=False)

        message_table = gr.DataFrame(value=_empty_message_table(), interactive=False)
        selected_message_info = gr.Markdown("メッセージを選ぶとここに表示するよ。")
        current_message_box = gr.Textbox(label="現在の内容", lines=6, interactive=False)
        edit_message_box = gr.Textbox(label="編集後の内容", lines=8, interactive=bool(choices))
        with gr.Row():
            update_message_btn = gr.Button("内容を更新", variant="primary", interactive=bool(choices))
            delete_message_btn = gr.Button("メッセージを削除", variant="stop", interactive=bool(choices))
        message_feedback = gr.Markdown("")

    load_outputs = [
        message_table,
        message_state,
        selected_message_info,
        current_message_box,
        edit_message_box,
        message_feedback,
        page_number_input,
        message_page_summary,
        prev_page_btn,
        next_page_btn,
        page_size_dropdown,
        go_page_btn,
    ]

    thread_refresh_outputs = [
        thread_table,
        thread_summaries_state,
        thread_selected_state,
        thread_feedback,
        message_table,
        message_state,
        selected_message_info,
        current_message_box,
        edit_message_box,
        message_feedback,
        page_number_input,
        message_page_summary,
        prev_page_btn,
        next_page_btn,
        page_size_dropdown,
        go_page_btn,
    ]

    refresh_threads_btn.click(
        fn=lambda persona_id, page_size: _refresh_threads(manager, persona_id, page_size),
        inputs=[persona_id_state, page_size_dropdown],
        outputs=thread_refresh_outputs,
        show_progress=True,
    )

    persona_change_event.then(
        fn=lambda persona_id, page_size: _refresh_threads(manager, persona_id, page_size),
        inputs=[persona_id_state, page_size_dropdown],
        outputs=thread_refresh_outputs,
        show_progress=True,
    )

    def _handle_thread_select(evt: SelectData, summaries, persona_id, page_size):
        LOGGER.debug(
            "[memory-settings] thread select handler - persona_id=%s SelectData=%s summaries_len=%s",
            persona_id,
            isinstance(evt, SelectData),
            len(summaries) if isinstance(summaries, list) else 'N/A',
        )

        clean_summaries = _coerce_summaries(summaries)
        result = _load_selected_thread(manager, evt, clean_summaries, persona_id, page_size)

        return result

    thread_select_event = thread_table.select(
        fn=_handle_thread_select,
        inputs=[thread_summaries_state, persona_id_state, page_size_dropdown],
        outputs=[thread_selected_state, *load_outputs],
        show_progress=True,
    )

    message_select_event = message_table.select(
        fn=_on_message_select,
        inputs=[message_state],
        outputs=[message_state, selected_message_info, current_message_box, edit_message_box],
        show_progress="hidden",
    )

    prev_page_btn.click(
        fn=lambda persona_id, state: _change_page(manager, persona_id, state, delta=-1),
        inputs=[persona_id_state, message_state],
        outputs=load_outputs,
        show_progress=True,
    )
    next_page_btn.click(
        fn=lambda persona_id, state: _change_page(manager, persona_id, state, delta=1),
        inputs=[persona_id_state, message_state],
        outputs=load_outputs,
        show_progress=True,
    )
    go_page_btn.click(
        fn=lambda persona_id, state, page: _change_page(manager, persona_id, state, explicit_page=page),
        inputs=[persona_id_state, message_state, page_number_input],
        outputs=load_outputs,
        show_progress=True,
    )
    page_size_dropdown.change(
        fn=lambda persona_id, state, size: _change_page(manager, persona_id, state, explicit_page=1, new_page_size=size),
        inputs=[persona_id_state, message_state, page_size_dropdown],
        outputs=load_outputs,
        show_progress=True,
    )

    update_message_btn.click(
        fn=lambda persona_id, state, new_content: _update_message(manager, persona_id, state, new_content),
        inputs=[persona_id_state, message_state, edit_message_box],
        outputs=load_outputs,
        show_progress=True,
    )
    delete_message_btn.click(
        fn=lambda persona_id, state: _delete_message(manager, persona_id, state),
        inputs=[persona_id_state, message_state],
        outputs=load_outputs,
        show_progress=True,
    )

    # Memory Recall テストセクション
    gr.Markdown("#### Memory Recall テスト")
    with gr.Accordion("memory_recall ツールを実行してみる", open=False):
        gr.Markdown(
            "memory_recall ツールと同じロジックでペルソナの長期記憶を検索できるよ。"
            "結果を確認してデバッグに使ってね。"
        )
        recall_query_box = gr.Textbox(
            label="検索クエリ",
            placeholder="検索したい内容を入力してね",
            interactive=bool(choices),
        )
        with gr.Row():
            recall_topk_slider = gr.Slider(
                minimum=1,
                maximum=20,
                value=4,
                step=1,
                label="topk (取得するシード数)",
                interactive=bool(choices),
            )
            recall_max_chars_slider = gr.Slider(
                minimum=100,
                maximum=10000,
                value=1200,
                step=100,
                label="max_chars (出力文字数上限)",
                interactive=bool(choices),
            )
        recall_execute_btn = gr.Button("Memory Recall を実行", variant="primary", interactive=bool(choices))
        recall_result_box = gr.Textbox(
            label="実行結果",
            lines=15,
            interactive=False,
            show_copy_button=True,
        )

        def _execute_memory_recall(persona_id: str, query: str, topk: int, max_chars: int) -> str:
            if not persona_id:
                return "ペルソナを先に選んでね。"
            if not query or not query.strip():
                return "検索クエリを入力してね。"

            adapter, release_adapter = _acquire_adapter(manager, persona_id)
            if not adapter or not adapter.is_ready():
                return "SAIMemoryが利用できなかったよ。"

            try:
                result = adapter.recall_snippet(
                    None,
                    query_text=query.strip(),
                    max_chars=int(max_chars),
                    topk=int(topk),
                )
                return result or "(no relevant memory)"
            except Exception as exc:
                LOGGER.warning("Memory recall failed for %s: %s", persona_id, exc, exc_info=True)
                return f"エラーが発生したよ: {exc}"
            finally:
                if release_adapter and adapter:
                    try:
                        adapter.close()
                    except Exception:
                        LOGGER.debug("Failed to close temporary adapter after recall", exc_info=True)

        recall_execute_btn.click(
            fn=_execute_memory_recall,
            inputs=[persona_id_state, recall_query_box, recall_topk_slider, recall_max_chars_slider],
            outputs=recall_result_box,
            show_progress=True,
        )
