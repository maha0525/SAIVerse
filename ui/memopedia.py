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

            # Vividness display mapping
            vividness_display = {
                "vivid": "鮮明",
                "rough": "概要",
                "faint": "淡い",
                "buried": "埋没",
            }

            def _flatten(pages: List[Dict], category: str, depth: int = 0):
                for page in pages:
                    indent = "  " * depth
                    vividness = page.get("vividness", "rough")
                    vividness_label = vividness_display.get(vividness, vividness)
                    rows.append([
                        page["id"],
                        category,
                        f"{indent}{page['title']}",
                        page.get("summary", ""),
                        vividness_label,
                    ])
                    children = page.get("children", [])
                    if children:
                        _flatten(children, category, depth + 1)

            category_names = {"people": "人物", "events": "出来事", "plans": "予定", "terms": "用語"}
            for cat_key in ["people", "terms", "plans"]:
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

    def on_table_select(selected_label: str, evt: gr.SelectData) -> Tuple[str, str, str]:
        """Handle table row selection. Returns (content, page_id, current_vividness_display)."""
        if evt is None:
            return "*ページを選択してください*", "", "概要（デフォルト）"

        # Get the row index
        idx = evt.index
        if isinstance(idx, (list, tuple)):
            row_idx = idx[0]
        else:
            row_idx = idx

        # Reload table data to get the page_id and vividness
        rows = load_page_list(selected_label)
        if row_idx < len(rows):
            page_id = rows[row_idx][0]  # First column is page_id
            vividness_display = rows[row_idx][4]  # Fifth column is vividness display

            # Convert table display to dropdown display
            dropdown_map = {
                "鮮明": "鮮明（全内容）",
                "概要": "概要（デフォルト）",
                "淡い": "淡い（タイトルのみ）",
                "埋没": "埋没（非表示）"
            }
            vividness_dropdown_display = dropdown_map.get(vividness_display, "概要（デフォルト）")

            content = load_page_content(selected_label, page_id)
            return content, page_id, vividness_dropdown_display

        return "*ページが見つかりません*", "", "概要（デフォルト）"

    def update_vividness(selected_label: str, page_id: str, new_vividness_display: str) -> Tuple[List[List[str]], str]:
        """Update page vividness and refresh table."""
        if not selected_label or not page_id:
            return [], "*ページを選択してください*"

        persona_id = _resolve_persona_id(selected_label)
        memopedia = _get_memopedia(persona_id)

        if memopedia is None:
            return [], f"*{persona_id} のメモリDBが見つかりません*"

        # Convert display name to internal value
        vividness_map = {"鮮明（全内容）": "vivid", "概要（デフォルト）": "rough", "淡い（タイトルのみ）": "faint", "埋没（非表示）": "buried"}
        new_vividness = vividness_map.get(new_vividness_display, "rough")

        try:
            # Update vividness
            memopedia.update_page(page_id, vividness=new_vividness)

            # Reload table
            new_table = load_page_list(selected_label)
            return new_table, f"✓ 鮮明度を更新しました: {new_vividness_display}"
        except Exception as e:
            LOGGER.exception("Failed to update vividness for page %s", page_id)
            return load_page_list(selected_label), f"*エラー: {e}*"

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
                        headers=["ID", "カテゴリ", "タイトル", "概要", "鮮明度"],
                        value=load_page_list(initial_label) if initial_label else [],
                        interactive=False,
                        label="ページ一覧（クリックで詳細表示）",
                    )
                with gr.Column(scale=2):
                    page_content_display = gr.Markdown(
                        value="*左のテーブルからページを選択してください*",
                        label="ページ内容",
                    )
                    with gr.Row():
                        vividness_dropdown = gr.Dropdown(
                            choices=["鮮明（全内容）", "概要（デフォルト）", "淡い（タイトルのみ）", "埋没（非表示）"],
                            label="鮮明度",
                            value="概要（デフォルト）",
                            interactive=True,
                            scale=3,
                        )
                        update_vividness_btn = gr.Button("更新", scale=1)
                    selected_page_id = gr.Textbox(value="", visible=False, interactive=False)
                    vividness_status = gr.Markdown(value="")

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
        fn=lambda _: ("*ページを選択してください*", "", "概要（デフォルト）", ""),
        inputs=[persona_dropdown],
        outputs=[page_content_display, selected_page_id, vividness_dropdown, vividness_status],
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
        outputs=[page_content_display, selected_page_id, vividness_dropdown],
    )

    update_vividness_btn.click(
        fn=update_vividness,
        inputs=[persona_dropdown, selected_page_id, vividness_dropdown],
        outputs=[page_table, vividness_status],
    )
