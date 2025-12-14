"""Memopedia UI - Visualize and browse persona knowledge pages."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr

LOGGER = logging.getLogger(__name__)


def create_memopedia_ui(manager) -> None:
    """Create the Memopedia visualization UI components."""

    # Get persona choices
    personas = manager.personas
    persona_choices: List[Tuple[str, str]] = []
    for pid, persona in personas.items():
        label = f"{persona.persona_name} ({pid})"
        persona_choices.append((label, pid))

    initial_label = persona_choices[0][0] if persona_choices else None

    def _resolve_persona_id(label: str) -> str:
        for display, pid in persona_choices:
            if display == label:
                return pid
        return label

    def _get_memopedia(persona_id: str):
        """Get Memopedia instance for a persona."""
        from sai_memory.memory.storage import init_db
        from sai_memory.memopedia import Memopedia

        db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
        if not db_path.exists():
            return None

        conn = init_db(str(db_path), check_same_thread=False)
        return Memopedia(conn)

    def load_tree_markdown(selected_label: str) -> str:
        """Load the page tree as Markdown."""
        if not selected_label:
            return "*ペルソナを選択してください*"

        persona_id = _resolve_persona_id(selected_label)
        memopedia = _get_memopedia(persona_id)

        if memopedia is None:
            return f"*{persona_id} のメモリDBが見つかりません*"

        try:
            return memopedia.get_tree_markdown() or "*ページがありません*"
        except Exception as e:
            LOGGER.exception("Failed to load tree for %s", persona_id)
            return f"*エラー: {e}*"

    def load_full_export(selected_label: str) -> str:
        """Load all pages as a single Markdown document."""
        if not selected_label:
            return "*ペルソナを選択してください*"

        persona_id = _resolve_persona_id(selected_label)
        memopedia = _get_memopedia(persona_id)

        if memopedia is None:
            return f"*{persona_id} のメモリDBが見つかりません*"

        try:
            return memopedia.export_all_markdown() or "*ページがありません*"
        except Exception as e:
            LOGGER.exception("Failed to export for %s", persona_id)
            return f"*エラー: {e}*"

    def load_page_list(selected_label: str) -> List[List[str]]:
        """Load page list as table data."""
        if not selected_label:
            return []

        persona_id = _resolve_persona_id(selected_label)
        memopedia = _get_memopedia(persona_id)

        if memopedia is None:
            return []

        try:
            tree = memopedia.get_tree()
            rows: List[List[str]] = []

            def _flatten(pages: List[Dict], category: str, depth: int = 0):
                for page in pages:
                    indent = "  " * depth
                    rows.append([
                        page["id"],
                        category,
                        f"{indent}{page['title']}",
                        page.get("summary", ""),
                    ])
                    children = page.get("children", [])
                    if children:
                        _flatten(children, category, depth + 1)

            category_names = {"people": "人物", "events": "出来事", "plans": "予定"}
            for cat_key in ["people", "events", "plans"]:
                pages = tree.get(cat_key, [])
                _flatten(pages, category_names.get(cat_key, cat_key))

            return rows
        except Exception as e:
            LOGGER.exception("Failed to load page list for %s", persona_id)
            return []

    def load_page_content(selected_label: str, page_id: str) -> str:
        """Load a single page's content."""
        if not selected_label or not page_id:
            return "*ページを選択してください*"

        persona_id = _resolve_persona_id(selected_label)
        memopedia = _get_memopedia(persona_id)

        if memopedia is None:
            return f"*{persona_id} のメモリDBが見つかりません*"

        try:
            return memopedia.get_page_markdown(page_id) or "*ページが見つかりません*"
        except Exception as e:
            LOGGER.exception("Failed to load page %s for %s", page_id, persona_id)
            return f"*エラー: {e}*"

    def on_table_select(selected_label: str, evt: gr.SelectData) -> str:
        """Handle table row selection."""
        if evt is None:
            return "*ページを選択してください*"

        # Get the row index
        idx = evt.index
        if isinstance(idx, (list, tuple)):
            row_idx = idx[0]
        else:
            row_idx = idx

        # Reload table data to get the page_id
        rows = load_page_list(selected_label)
        if row_idx < len(rows):
            page_id = rows[row_idx][0]  # First column is page_id
            return load_page_content(selected_label, page_id)

        return "*ページが見つかりません*"

    # --- UI Components ---
    gr.Markdown("## Memopedia - ペルソナ知識ベース")

    with gr.Row():
        persona_dropdown = gr.Dropdown(
            choices=[label for label, _ in persona_choices],
            label="ペルソナ選択",
            interactive=True,
            value=initial_label,
            scale=3,
        )
        refresh_btn = gr.Button("更新", scale=1)

    with gr.Tabs():
        with gr.Tab("ツリービュー"):
            tree_display = gr.Markdown(
                value=load_tree_markdown(initial_label) if initial_label else "*ペルソナを選択してください*",
                label="ページツリー",
            )

        with gr.Tab("ページ一覧"):
            with gr.Row():
                with gr.Column(scale=1):
                    page_table = gr.Dataframe(
                        headers=["ID", "カテゴリ", "タイトル", "概要"],
                        value=load_page_list(initial_label) if initial_label else [],
                        interactive=False,
                        label="ページ一覧（クリックで詳細表示）",
                    )
                with gr.Column(scale=2):
                    page_content_display = gr.Markdown(
                        value="*左のテーブルからページを選択してください*",
                        label="ページ内容",
                    )

        with gr.Tab("全ページエクスポート"):
            export_display = gr.Markdown(
                value=load_full_export(initial_label) if initial_label else "*ペルソナを選択してください*",
                label="全ページ（Markdown）",
            )

    # --- Event Handlers ---
    persona_dropdown.change(
        fn=load_tree_markdown,
        inputs=[persona_dropdown],
        outputs=[tree_display],
    )
    persona_dropdown.change(
        fn=load_page_list,
        inputs=[persona_dropdown],
        outputs=[page_table],
    )
    persona_dropdown.change(
        fn=load_full_export,
        inputs=[persona_dropdown],
        outputs=[export_display],
    )
    persona_dropdown.change(
        fn=lambda _: "*ページを選択してください*",
        inputs=[persona_dropdown],
        outputs=[page_content_display],
    )

    refresh_btn.click(
        fn=load_tree_markdown,
        inputs=[persona_dropdown],
        outputs=[tree_display],
    )
    refresh_btn.click(
        fn=load_page_list,
        inputs=[persona_dropdown],
        outputs=[page_table],
    )
    refresh_btn.click(
        fn=load_full_export,
        inputs=[persona_dropdown],
        outputs=[export_display],
    )

    page_table.select(
        fn=on_table_select,
        inputs=[persona_dropdown],
        outputs=[page_content_display],
    )
