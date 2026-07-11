"""Report Memopedia health status for autonomous decision-making.

P4 庭仕事ワーカーの素材として内部専用化 (concept_consolidation.md)。
"""
from __future__ import annotations

import logging

from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema
from sai_memory.memopedia.storage import category_keys
from saiverse.curation import HEALTH_LARGE_THRESHOLD, HEALTH_OVERSIZED_THRESHOLD

LOGGER = logging.getLogger(__name__)


def memopedia_health() -> str:
    """Return a compact health report of Memopedia pages.

    Includes: total page count, oversized pages (>HEALTH_OVERSIZED_THRESHOLD chars),
    pages without summaries, and category breakdown.

    閾値は saiverse/curation.py の HEALTH_OVERSIZED_THRESHOLD /
    HEALTH_LARGE_THRESHOLD から一元取得している。
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    from saiverse_memory import SAIMemoryAdapter
    persona_dir = get_active_persona_path()
    adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)

    if not adapter.is_ready():
        return "Memopedia: データベースにアクセスできません"

    from sai_memory.memopedia import Memopedia, init_memopedia_tables
    init_memopedia_tables(adapter.conn)
    memopedia = Memopedia(adapter.conn)

    tree = memopedia.get_tree()

    total_pages = 0
    oversized = []       # > HEALTH_OVERSIZED_THRESHOLD chars
    large = []           # > HEALTH_LARGE_THRESHOLD chars
    no_summary = []
    category_counts = {}

    def _scan(pages, category: str, depth: int = 0):
        nonlocal total_pages
        for p in pages:
            total_pages += 1
            title = p.get("title", "?")
            content = p.get("content", "")
            summary = p.get("summary", "")
            content_len = len(content)

            category_counts[category] = category_counts.get(category, 0) + 1

            if content_len > HEALTH_OVERSIZED_THRESHOLD:
                oversized.append(f"  - {title} ({content_len}字) [{category}]")
            elif content_len > HEALTH_LARGE_THRESHOLD:
                large.append(f"  - {title} ({content_len}字) [{category}]")

            if not summary.strip():
                no_summary.append(f"  - {title} [{category}]")

            children = p.get("children", [])
            if children:
                _scan(children, category, depth + 1)

    for cat_key in category_keys("in_tree"):
        _scan(tree.get(cat_key, []), cat_key)

    lines = ["## Memopedia ヘルスレポート", f"総ページ数: {total_pages}"]

    # Category breakdown
    cat_parts = [f"{k}: {v}" for k, v in sorted(category_counts.items())]
    if cat_parts:
        lines.append(f"カテゴリ別: {', '.join(cat_parts)}")

    # Oversized pages (need splitting)
    if oversized:
        lines.append(
            f"\n### 分割推奨 ({HEALTH_OVERSIZED_THRESHOLD}字超): {len(oversized)}件"
        )
        lines.extend(oversized)
    else:
        lines.append("\n分割推奨ページ: なし")

    # Large pages (approaching limit)
    if large:
        lines.append(
            f"\n### 注意 ({HEALTH_LARGE_THRESHOLD}-{HEALTH_OVERSIZED_THRESHOLD}字):"
            f" {len(large)}件"
        )
        lines.extend(large)

    # Pages without summaries
    if no_summary:
        lines.append(f"\n### 概要なし: {len(no_summary)}件")
        lines.extend(no_summary[:10])
        if len(no_summary) > 10:
            lines.append(f"  ... 他{len(no_summary) - 10}件")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_health",
        description=(
            "Memopediaの健康状態をレポートします。"
            "総ページ数、分割が必要な大きいページ、概要がないページなどを一覧表示します。"
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        result_type="string",
    )
