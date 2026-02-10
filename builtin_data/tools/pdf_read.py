"""Read text content from a PDF document item."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from tools.context import get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def pdf_read(
    item_id: str,
    pages: Optional[str] = None,
    max_chars: int = 8000,
) -> str:
    """Extract and read text from a PDF document item.

    Args:
        item_id: The item ID of the PDF document
        pages: Optional page range (e.g., "1-5", "3", "10-20"). 1-based. If not specified, reads all pages.
        max_chars: Maximum characters to return

    Returns:
        Extracted text content from the PDF
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return "(pypdf is not installed. Run: pip install pypdf)"

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    # Resolve item to file path
    item_service = manager.item_service
    file_path = item_service._resolve_file_path(item_id)
    if not file_path or not file_path.exists():
        return f"(PDF file not found for item: {item_id})"

    item_name = item_id
    try:
        from database.models import Item
        from database.session import get_session

        with get_session() as session:
            item = session.query(Item).filter(Item.ITEM_ID == item_id).first()
            if item:
                item_name = item.NAME
    except Exception:
        LOGGER.warning("Failed to get item name for %s", item_id, exc_info=True)

    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:
        return f"(Failed to open PDF: {exc})"

    total_pages = len(reader.pages)

    # Parse page range
    start_page = 0  # 0-based
    end_page = total_pages

    if pages:
        try:
            if "-" in pages:
                parts = pages.split("-")
                start_page = int(parts[0]) - 1  # 1-based to 0-based
                end_page = int(parts[1])
            else:
                start_page = int(pages) - 1
                end_page = start_page + 1
            start_page = max(0, start_page)
            end_page = min(total_pages, end_page)
        except ValueError:
            return f"(Invalid page range: {pages}. Use format like '1-5' or '3')"

    # Extract text
    parts = [
        f"【PDF】{item_name} (全{total_pages}ページ",
    ]
    if pages:
        parts[0] += f", {pages}を表示)"
    else:
        parts[0] += ")"
    parts.append("")

    total_chars = 0
    for page_num in range(start_page, end_page):
        page = reader.pages[page_num]
        text = (page.extract_text() or "").strip()
        parts.append(f"--- Page {page_num + 1} ---")
        parts.append(text)
        parts.append("")
        total_chars += len(text)
        if total_chars >= max_chars:
            parts.append(f"... (truncated at {max_chars} chars)")
            break

    result = "\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... (truncated)"

    return result


def schema() -> ToolSchema:
    return ToolSchema(
        name="pdf_read",
        description=(
            "Extract and read text from a PDF document item. "
            "Specify page range to read specific pages. "
            "Requires pypdf to be installed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The item ID of the PDF document",
                },
                "pages": {
                    "type": "string",
                    "description": "Page range to read (e.g., '1-5', '3', '10-20'). 1-based. Omit to read all.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return. Default: 8000.",
                    "default": 8000,
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
    )
