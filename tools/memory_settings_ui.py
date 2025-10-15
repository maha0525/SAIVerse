from __future__ import annotations

import logging
import json
import textwrap
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import gradio as gr
import pandas as pd

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memory.chunking import chunk_text
from sai_memory.memory.storage import (
    compose_message_content,
    get_messages_paginated,
    replace_message_embeddings,
)
from scripts.import_chatgpt_conversations import (
    build_summary_rows,
    format_datetime,
    load_export,
    parse_roles,
    resolve_selection,
    resolve_thread_suffix,
)

LOGGER = logging.getLogger(__name__)


def _persona_choices(manager) -> List[tuple[str, str]]:
    choices: List[tuple[str, str]] = []
    if not manager:
        return choices
    for persona_id, persona in manager.personas.items():
        display = persona.persona_name or persona_id
        choices.append((display, persona_id))
    choices.sort(key=lambda item: item[0])
    return choices


def _load_chatgpt_summary(export_path: Optional[str], preview: int) -> tuple[pd.DataFrame, str, Dict[str, Any]]:
    if not export_path:
        empty = pd.DataFrame(columns=["Idx", "ID", "Title", "Created (UTC)", "Updated (UTC)", "Msgs", "Preview"])
        return empty, "ファイルを選ぶとここに一覧が出るよ。", {"path": None, "count": 0}
    try:
        export = load_export(export_path)
    except Exception as exc:
        LOGGER.warning("Failed to load ChatGPT export: %s", exc, exc_info=True)
        empty = pd.DataFrame(columns=["Idx", "ID", "Title", "Created (UTC)", "Updated (UTC)", "Msgs", "Preview"])
        return empty, f"読み込みでエラーが出たよ: {exc}", {"path": None, "count": 0}

    records = export.conversations
    if not records:
        empty = pd.DataFrame(columns=["Idx", "ID", "Title", "Created (UTC)", "Updated (UTC)", "Msgs", "Preview"])
        return empty, "会話が見つからなかったよ。", {"path": export_path, "count": 0}

    headers, rows = build_summary_rows(records, preview)
    table = pd.DataFrame(rows, columns=headers)
    message = f"{len(records)}件の会話を読み込んだよ。'Idx'列の番号を使ってね。"
    return table, message, {"path": export_path, "count": len(records)}


def _split_selectors(raw_value: str) -> List[str]:
    if not raw_value:
        return []
    tokens: List[str] = []
    for frag in raw_value.replace("\n", ",").split(","):
        frag = frag.strip()
        if frag:
            tokens.append(frag)
    return tokens


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
    return pd.DataFrame(columns=["Idx", "Message ID", "Role", "Timestamp", "Preview"])


def _refresh_threads(manager, persona_id: str) -> tuple[pd.DataFrame, gr.Update, Dict[str, Any], str, pd.DataFrame, Dict[str, Any], gr.Update, str, str, str, gr.Update]:
    if not persona_id:
        message = "ペルソナを選んでから更新してね。"
        return (
            _empty_thread_table(),
            gr.update(),
            {"threads": []},
            message,
            _empty_message_table(),
            {"thread_id": None, "messages": {}},
            gr.update(choices=[], value=None, interactive=False),
            "",
            "",
            "",
            gr.update(interactive=False),
        )

    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        return (
            _empty_thread_table(),
            gr.update(),
            {"threads": []},
            "SAIMemoryが利用できなかったよ。",
            _empty_message_table(),
            {"thread_id": None, "messages": {}},
            gr.update(choices=[], value=None, interactive=False),
            "",
            "",
            "",
            gr.update(interactive=False),
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
                LOGGER.debug("Failed to close temporary adapter after refresh", exc_info=True)

    rows: List[List[Any]] = []
    dropdown_choices: List[tuple[str, str]] = []
    for summary in summaries:
        suffix = summary.get("suffix") or summary.get("thread_id") or ""
        label = suffix
        if summary.get("active"):
            label = f"{suffix} (active)"
        dropdown_choices.append((label, summary.get("thread_id", suffix)))
        rows.append(
            [
                summary.get("thread_id", ""),
                suffix,
                "✓" if summary.get("active") else "",
                summary.get("preview", ""),
            ]
        )

    table = pd.DataFrame(rows, columns=["Thread ID", "Suffix", "Active", "Preview"]) if rows else _empty_thread_table()
    dropdown_update = gr.update(choices=dropdown_choices, value=dropdown_choices[0][1] if dropdown_choices else None, interactive=bool(dropdown_choices))
    message = f"{len(summaries)}件のスレッドを取得したよ。" if summaries else "スレッドがまだ無いみたい。"
    return (
        table,
        dropdown_update,
        {"threads": summaries},
        message,
        _empty_message_table(),
        {"thread_id": None, "messages": {}},
        gr.update(choices=[], value=None, interactive=False),
        "",
        "",
        "",
        gr.update(interactive=bool(dropdown_choices)),
    )


def _load_thread_messages(manager, persona_id: str, thread_id: Optional[str]) -> tuple[pd.DataFrame, Dict[str, Any], gr.Update, str, str, str]:
    if not persona_id:
        return _empty_message_table(), {"thread_id": None, "messages": {}}, gr.update(), "ペルソナを選んでね。", "", ""
    if not thread_id:
        return _empty_message_table(), {"thread_id": None, "messages": {}}, gr.update(interactive=False), "スレッドを選んでね。", "", ""

    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        return _empty_message_table(), {"thread_id": None, "messages": {}}, gr.update(interactive=False), "SAIMemoryが利用できなかったよ。", "", ""

    try:
        with adapter._db_lock:  # type: ignore[attr-defined]
            rows = get_messages_paginated(adapter.conn, thread_id, page=0, page_size=200)
            messages_map: Dict[str, Dict[str, Any]] = {}
            table_rows: List[List[Any]] = []
            for idx, msg in enumerate(rows):
                content = compose_message_content(adapter.conn, msg) or ""
                ts = datetime.fromtimestamp(msg.created_at, tz=timezone.utc)
                iso = format_datetime(ts)
                preview = textwrap.shorten(content.replace("\n", " ").strip(), width=80, placeholder="…")
                table_rows.append([idx, msg.id, msg.role, iso or "-", preview])
                messages_map[msg.id] = {"content": content, "role": msg.role, "timestamp": iso or "-", "thread_id": msg.thread_id}
    except Exception as exc:
        LOGGER.warning("Failed to load messages for thread %s: %s", thread_id, exc, exc_info=True)
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after load", exc_info=True)
        return _empty_message_table(), {"thread_id": thread_id, "messages": {}}, gr.update(interactive=False), f"メッセージ取得でエラー: {exc}", "", ""
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after load", exc_info=True)

    table = pd.DataFrame(table_rows, columns=["Idx", "Message ID", "Role", "Timestamp", "Preview"]) if table_rows else _empty_message_table()
    dropdown_choices = [(f"{item['role']} @ {item['timestamp']}", mid) for mid, item in messages_map.items()]
    dropdown_update = gr.update(choices=dropdown_choices, value=dropdown_choices[0][1] if dropdown_choices else None, interactive=bool(dropdown_choices))
    first_content = dropdown_choices[0][1] if dropdown_choices else None
    current_text = messages_map[first_content]["content"] if first_content else ""
    state = {"thread_id": thread_id, "messages": messages_map}
    note = f"{len(messages_map)}件のメッセージを読み込んだよ。" if messages_map else "このスレッドにはメッセージが無いみたい。"
    return table, state, dropdown_update, note, current_text, current_text


def _select_message(message_id: Optional[str], message_state: Dict[str, Any]) -> tuple[str, str]:
    if not message_id:
        return "", ""
    info = (message_state or {}).get("messages", {}).get(message_id)
    if not info:
        return "", ""
    content = info.get("content", "")
    return content, content


def _delete_message(manager, persona_id: str, message_id: Optional[str], message_state: Dict[str, Any]) -> tuple[pd.DataFrame, Dict[str, Any], gr.Update, str, str, str]:
    if not persona_id:
        return _empty_message_table(), message_state, gr.update(interactive=False), "ペルソナを選んでね。", "", ""
    if not message_id:
        return _empty_message_table(), message_state, gr.update(interactive=False), "削除するメッセージを選んでね。", "", ""
    thread_id = (message_state or {}).get("thread_id")
    if not thread_id:
        return _empty_message_table(), message_state, gr.update(interactive=False), "スレッドを先に読み込んでね。", "", ""

    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        return _empty_message_table(), message_state, gr.update(interactive=False), "SAIMemoryが利用できなかったよ。", "", ""

    try:
        with adapter._db_lock:  # type: ignore[attr-defined]
            adapter.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))
            adapter.conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
            adapter.conn.commit()
    except Exception as exc:
        LOGGER.warning("Failed to delete message %s: %s", message_id, exc, exc_info=True)
        note = f"削除に失敗したよ: {exc}"
    else:
        note = f"メッセージ {message_id} を削除したよ。"
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after delete", exc_info=True)

    table, state, dropdown_update, load_note, current_text, edit_text = _load_thread_messages(manager, persona_id, thread_id)
    full_note = f"{note}\n{load_note}".strip()
    return table, state, dropdown_update, full_note, current_text, edit_text


def _update_message(manager, persona_id: str, message_id: Optional[str], new_content: str, message_state: Dict[str, Any]) -> tuple[pd.DataFrame, Dict[str, Any], gr.Update, str, str, str]:
    if not persona_id:
        return _empty_message_table(), message_state, gr.update(interactive=False), "ペルソナを選んでね。", "", ""
    if not message_id:
        return _empty_message_table(), message_state, gr.update(interactive=False), "更新するメッセージを選んでね。", "", ""
    thread_id = (message_state or {}).get("thread_id")
    if not thread_id:
        return _empty_message_table(), message_state, gr.update(interactive=False), "スレッドを先に読み込んでね。", "", ""
    if new_content is None:
        new_content = ""

    adapter, release_adapter = _acquire_adapter(manager, persona_id)
    if not adapter or not adapter.is_ready():
        return _empty_message_table(), message_state, gr.update(interactive=False), "SAIMemoryが利用できなかったよ。", "", ""

    try:
        with adapter._db_lock:  # type: ignore[attr-defined]
            cur = adapter.conn.execute(
                "SELECT role, metadata FROM messages WHERE id=?",
                (message_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("対象のメッセージが見つからなかったよ。")
            role, metadata_json = row
            metadata = json.loads(metadata_json) if metadata_json else None
            adapter.conn.execute(
                "UPDATE messages SET content=?, metadata=? WHERE id=?",
                (new_content, json.dumps(metadata, ensure_ascii=False) if metadata else None, message_id),
            )
            adapter.conn.execute("DELETE FROM message_embeddings WHERE message_id=?", (message_id,))
            if new_content.strip() and adapter.embedder is not None:
                chunks = chunk_text(
                    new_content,
                    min_chars=adapter.settings.chunk_min_chars,
                    max_chars=adapter.settings.chunk_max_chars,
                )
                payload = [chunk.strip() for chunk in chunks if chunk and chunk.strip()]
                if payload:
                    vectors = adapter.embedder.embed(payload)
                    replace_message_embeddings(adapter.conn, message_id, vectors)
            adapter.conn.commit()
    except Exception as exc:
        LOGGER.warning("Failed to update message %s: %s", message_id, exc, exc_info=True)
        note = f"更新でエラーが出たよ: {exc}"
    else:
        note = f"メッセージ {message_id} を更新したよ。"
    finally:
        if release_adapter and adapter:
            try:
                adapter.close()
            except Exception:
                LOGGER.debug("Failed to close temporary adapter after update", exc_info=True)

    table, state, dropdown_update, load_note, current_text, edit_text = _load_thread_messages(manager, persona_id, thread_id)
    full_note = f"{note}\n{load_note}".strip()
    return table, state, dropdown_update, full_note, current_text, edit_text


def _import_chatgpt_conversations(
    manager,
    persona_id: str,
    export_info: Dict[str, Any],
    selectors_text: str,
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
        selected = list(records)
    else:
        selectors = _split_selectors(selectors_text)
        if not selectors:
            return "インポートする会話を番号かIDで指定してね。"
        try:
            selected = resolve_selection(records, selectors)
        except ValueError as exc:
            return f"選択内容に誤りがあったよ: {exc}"

    if not selected:
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
    try:
        for record in selected:
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
                results.append(
                    f"[dry-run] {record.title} ({len(payloads)}件) thread={thread_suffix}"
                )
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
    persona_dropdown.change(
        fn=_update_persona,
        inputs=[persona_dropdown, persona_id_state],
        outputs=[persona_id_state, persona_status],
        show_progress="hidden",
    )

    gr.Markdown("#### ChatGPTエクスポートからのインポート")
    with gr.Row():
        chatgpt_file = gr.File(label="conversations.json または ZIP", type="filepath", interactive=bool(choices))
        preview_slider = gr.Slider(40, 240, value=120, step=10, label="プレビュー文字数", interactive=bool(choices))
    chatgpt_table = gr.DataFrame(headers=["Idx", "ID", "Title", "Created (UTC)", "Updated (UTC)", "Msgs", "Preview"], interactive=False)
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

    selection_box = gr.Textbox(label="インポートするIdxまたはID (カンマ区切り)", placeholder="例: 0,2,4 または 会話ID", interactive=bool(choices))
    with gr.Row():
        roles_box = gr.Textbox(value="user,assistant", label="取得する役割", placeholder="カンマ区切り (空ならすべて)", interactive=bool(choices))
        thread_suffix_box = gr.Textbox(label="スレッド接尾辞 (任意)", placeholder="空なら会話IDを使うよ", interactive=bool(choices))
    with gr.Row():
        header_checkbox = gr.Checkbox(value=True, label="システムヘッダーを追加する", interactive=bool(choices))
        dry_run_checkbox = gr.Checkbox(value=False, label="dry-run (書き込まない)", interactive=bool(choices))
    import_feedback = gr.Textbox(label="結果", lines=6, interactive=False)

    import_button = gr.Button("選択した会話をインポート", variant="primary", interactive=bool(choices))
    import_all_button = gr.Button("全件インポート", variant="secondary", interactive=bool(choices))

    import_button.click(
        fn=lambda persona_id, state, selectors, roles, suffix, header, dry_run: _import_chatgpt_conversations(
            manager,
            persona_id,
            state,
            selectors,
            roles,
            suffix,
            bool(header),
            bool(dry_run),
            False,
        ),
        inputs=[persona_id_state, export_state, selection_box, roles_box, thread_suffix_box, header_checkbox, dry_run_checkbox],
        outputs=import_feedback,
        show_progress=True,
    )
    import_all_button.click(
        fn=lambda persona_id, state, roles, suffix, header, dry_run: _import_chatgpt_conversations(
            manager,
            persona_id,
            state,
            "",
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

    thread_state = gr.State({"threads": []})
    message_state = gr.State({"thread_id": None, "messages": {}})

    gr.Markdown("#### SAIMemoryスレッド管理")
    with gr.Accordion("長期記憶のスレッドとメッセージを管理する", open=False):
        refresh_threads_btn = gr.Button("スレッド一覧を更新", variant="secondary", interactive=bool(choices))
        thread_feedback = gr.Markdown("")
        thread_table = gr.DataFrame(headers=["Thread ID", "Suffix", "Active", "Preview"], interactive=False)
        thread_selector = gr.Dropdown(label="スレッド", choices=[], interactive=False)
        load_thread_btn = gr.Button("このスレッドを読み込む", interactive=False)
        message_table = gr.DataFrame(headers=["Idx", "Message ID", "Role", "Timestamp", "Preview"], interactive=False)
        message_feedback = gr.Markdown("")
        message_selector = gr.Dropdown(label="メッセージ", choices=[], interactive=False)
        current_message_box = gr.Textbox(label="現在の内容", lines=6, interactive=False)
        edit_message_box = gr.Textbox(label="編集後の内容", lines=6, interactive=bool(choices))
        with gr.Row():
            update_message_btn = gr.Button("内容を更新", variant="primary", interactive=bool(choices))
            delete_message_btn = gr.Button("メッセージを削除", variant="stop", interactive=bool(choices))

    refresh_threads_btn.click(
        fn=lambda persona_id: _refresh_threads(manager, persona_id),
        inputs=[persona_id_state],
        outputs=[
            thread_table,
            thread_selector,
            thread_state,
            thread_feedback,
            message_table,
            message_state,
            message_selector,
            current_message_box,
            edit_message_box,
            message_feedback,
            load_thread_btn,
        ],
        show_progress=True,
    )

    def _load_thread_wrapper(persona_id: str, thread_id: Optional[str]):
        table, state, dropdown, note, current, edit = _load_thread_messages(manager, persona_id, thread_id)
        return table, state, dropdown, note, current, edit

    load_thread_btn.click(
        fn=_load_thread_wrapper,
        inputs=[persona_id_state, thread_selector],
        outputs=[message_table, message_state, message_selector, message_feedback, current_message_box, edit_message_box],
        show_progress=True,
    )
    thread_selector.change(
        fn=_load_thread_wrapper,
        inputs=[persona_id_state, thread_selector],
        outputs=[message_table, message_state, message_selector, message_feedback, current_message_box, edit_message_box],
        show_progress="hidden",
    )

    message_selector.change(
        fn=lambda mid, state: _select_message(mid, state),
        inputs=[message_selector, message_state],
        outputs=[current_message_box, edit_message_box],
        show_progress="hidden",
    )

    update_message_btn.click(
        fn=lambda persona_id, mid, new_text, state: _update_message(manager, persona_id, mid, new_text, state),
        inputs=[persona_id_state, message_selector, edit_message_box, message_state],
        outputs=[message_table, message_state, message_selector, message_feedback, current_message_box, edit_message_box],
        show_progress=True,
    )

    delete_message_btn.click(
        fn=lambda persona_id, mid, state: _delete_message(manager, persona_id, mid, state),
        inputs=[persona_id_state, message_selector, message_state],
        outputs=[message_table, message_state, message_selector, message_feedback, current_message_box, edit_message_box],
        show_progress=True,
    )
