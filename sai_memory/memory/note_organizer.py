"""Memory note organization: plan phase.

Groups unplanned memory notes and determines where each group
should be placed in Memopedia (append to existing page, create child,
or create new page).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sai_memory.memory.storage import (
    MemoryNote,
    get_unplanned_notes,
    resolve_memory_notes,
    set_note_plan,
)

LOGGER = logging.getLogger(__name__)

# Page size thresholds (characters)
PAGE_TARGET_SIZE = 2000
PAGE_COMPACT_THRESHOLD = 3000

# Valid action values
ACTION_APPEND = "append_to_existing"
ACTION_CHILD = "create_child"
ACTION_NEW = "create_new"
VALID_ACTIONS = {ACTION_APPEND, ACTION_CHILD, ACTION_NEW}

CATEGORY_EVENTS = "events"


def _format_memopedia_tree_for_plan(tree: Dict[str, Any]) -> str:
    """Format Memopedia tree for the plan prompt.

    Includes page ID and content length for each page.
    """
    lines: List[str] = []
    category_names = {
        "people": "人物",
        "terms": "用語",
        "plans": "予定",
        "events": "出来事",
    }

    def _render(page: Dict[str, Any], depth: int = 0) -> None:
        # Skip root pages
        if page.get("id", "").startswith("root_"):
            for child in page.get("children", []):
                _render(child, depth)
            return

        indent = "  " * depth
        title = page.get("title", "")
        summary = page.get("summary", "")
        content = page.get("content", "")
        content_len = len(content) if content else 0
        page_id = page.get("id", "")

        summary_part = f": {summary}" if summary else ""
        lines.append(f"{indent}- [{page_id}] {title}{summary_part} ({content_len}字)")

        for child in page.get("children", []):
            _render(child, depth + 1)

    for category_key in ["people", "terms", "plans", "events"]:
        pages = tree.get(category_key, [])
        if pages:
            category_name = category_names.get(category_key, category_key)
            lines.append(f"\n### {category_name} ({category_key})")
            for page in pages:
                _render(page, depth=0)

    if not lines:
        return "(まだページはありません)"

    return "\n".join(lines)


def _format_notes_for_plan(notes: List[MemoryNote]) -> str:
    """Format unplanned notes for the plan prompt."""
    lines: List[str] = []
    for note in notes:
        lines.append(f"- [{note.id}] {note.content}")
    return "\n".join(lines)


def _build_plan_prompt(
    notes_text: str,
    tree_text: str,
) -> str:
    """Build the LLM prompt for organizing memory notes."""
    return f"""\
あなたは記憶の整理係です。以下のメモリーノート（新しく記録された情報）を、既存のMemopedia（知識ベース）に整理する計画を立ててください。

## 既存のMemopediaページ一覧

各ページは `[ページID] タイトル: 概要 (現在の文字数)` の形式です。
{tree_text}

## 未整理のメモリーノート

各ノートは `[ノートID] 内容` の形式です。
{notes_text}

## カテゴリの説明

- **people**: 関わりのある人物（ユーザー、他AI、言及された第三者）
- **terms**: 対話の中で特別な意味を持つ言葉や概念
- **plans**: 進行中や計画中のプロジェクト・予定
- **events**: 出来事、体験、時事的な話題

## 指示

1. メモリーノートを**トピック別にグルーピング**してください
2. 各グループについて、以下のいずれかの**アクション**を選んでください：
   - `append_to_existing`: 既存ページに追記する（関連するページが既にあり、そのページの文字数に余裕がある場合）
   - `create_child`: 既存ページの子ページとして新規作成する（親ページが既にあるが、内容が別トピックとして独立すべき場合、または親ページが2000字を超えそうな場合）
   - `create_new`: カテゴリ直下に新規の親ページを作成する（関連する既存ページがない場合）
3. **ページサイズの目安**: 1ページ2000字程度。既に2000字を超えているページへの追記は避け、子ページ作成を検討してください
4. 矛盾する情報（「帰省中」と「帰省完了」など）は同じグループにまとめてください（実行時に最新の状態に統一します）

## 出力形式

以下のJSON形式で出力してください。
```json
{{
  "groups": [
    {{
      "group_label": "グループ名",
      "note_ids": ["ノートID1", "ノートID2"],
      "action": "append_to_existing | create_child | create_new",
      "target_page_id": "対象ページID（append/childの場合）",
      "suggested_title": "ページタイトル案（child/newの場合）",
      "target_category": "カテゴリ名（newの場合: people/terms/plans/events）"
    }}
  ]
}}
```

JSONのみを出力してください。"""


def _parse_plan_response(response: str, valid_note_ids: set) -> List[Dict[str, Any]]:
    """Parse LLM response into a list of group plans.

    Validates note_ids and action values. Drops invalid entries with warnings.
    """
    if not response:
        return []

    text = response.strip()

    # Strip markdown code block if present
    md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to parse plan response as JSON: %s", text[:300])
        return []

    groups_raw = parsed.get("groups", []) if isinstance(parsed, dict) else []
    if not isinstance(groups_raw, list):
        LOGGER.warning("Plan response 'groups' is not a list")
        return []

    result: List[Dict[str, Any]] = []
    for group in groups_raw:
        if not isinstance(group, dict):
            continue

        group_label = group.get("group_label", "").strip()
        note_ids = group.get("note_ids", [])
        action = group.get("action", "").strip()
        target_page_id = group.get("target_page_id")
        suggested_title = group.get("suggested_title")
        target_category = group.get("target_category")

        if not group_label:
            LOGGER.warning("Skipping group with empty label")
            continue

        if action not in VALID_ACTIONS:
            LOGGER.warning("Skipping group '%s': invalid action '%s'", group_label, action)
            continue

        # Filter to valid note IDs only
        valid_ids = [nid for nid in note_ids if nid in valid_note_ids]
        if not valid_ids:
            LOGGER.warning("Skipping group '%s': no valid note IDs", group_label)
            continue

        # Validate required fields per action
        if action == ACTION_APPEND and not target_page_id:
            LOGGER.warning("Skipping group '%s': append_to_existing requires target_page_id", group_label)
            continue
        if action == ACTION_CHILD and not target_page_id:
            LOGGER.warning("Skipping group '%s': create_child requires target_page_id", group_label)
            continue
        if action == ACTION_NEW and not target_category:
            LOGGER.warning("Skipping group '%s': create_new requires target_category", group_label)
            continue

        result.append({
            "group_label": group_label,
            "note_ids": valid_ids,
            "action": action,
            "target_page_id": target_page_id,
            "suggested_title": suggested_title,
            "target_category": target_category,
        })

    return result


def _format_append_block(notes: List[MemoryNote]) -> str:
    """Format notes as an append block for a Memopedia page.

    Includes date header and bullet points.
    """
    from datetime import datetime

    # Group by date
    dates: Dict[str, List[str]] = {}
    for note in notes:
        ts = note.source_time or note.created_at
        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        dates.setdefault(date_str, []).append(note.content)

    parts: List[str] = []
    for date_str, items in dates.items():
        parts.append(f"## {date_str} 追記")
        for item in items:
            parts.append(f"- {item}")
    return "\n".join(parts)


@dataclass
class OrganizeResult:
    """Result of organizing notes into Memopedia."""
    group_label: str
    action: str
    page_id: str
    note_count: int


def organize_notes(
    client,
    conn: sqlite3.Connection,
    memopedia,
    *,
    limit: int = 200,
    persona_id: Optional[str] = None,
) -> List[OrganizeResult]:
    """Group unplanned notes, assign targets, and write directly to Memopedia.

    This combines the former plan + exec phases into a single operation.
    Notes are appended as-is (with timestamps) to avoid hallucination.
    LLM is only used for grouping and target assignment, not for content generation.

    Args:
        client: LLM client with generate() method.
        conn: Database connection (memory.db).
        memopedia: Memopedia instance (from sai_memory.memopedia.core).
        limit: Max number of unplanned notes to process.
        persona_id: Optional persona ID for usage tracking.

    Returns:
        List of OrganizeResult for each group processed.
    """
    notes = get_unplanned_notes(conn, limit=limit)
    if not notes:
        LOGGER.info("No unplanned notes to organize")
        return []

    LOGGER.info("Organizing %d unplanned notes", len(notes))

    # Build note ID → MemoryNote lookup
    note_map = {n.id: n for n in notes}

    # Get Memopedia tree for plan prompt
    memopedia_tree = memopedia.get_tree()
    tree_text = _format_memopedia_tree_for_plan(memopedia_tree)
    notes_text = _format_notes_for_plan(notes)
    prompt = _build_plan_prompt(notes_text, tree_text)

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
    except Exception as e:
        LOGGER.warning("LLM call failed for note organization: %s", e)
        return []

    # Track usage
    if persona_id and hasattr(client, "last_usage"):
        try:
            from sai_memory.arasuji.generator import _record_llm_usage
            _record_llm_usage(client, persona_id, "memory_note_plan")
        except Exception:
            pass

    valid_note_ids = {n.id for n in notes}
    groups = _parse_plan_response(response or "", valid_note_ids)

    if not groups:
        LOGGER.info("No valid groups extracted from plan response")
        return []

    # Process each group: append to Memopedia + resolve notes
    results: List[OrganizeResult] = []
    for group in groups:
        group_notes = [note_map[nid] for nid in group["note_ids"] if nid in note_map]
        if not group_notes:
            continue

        action = group["action"]
        target_page_id = group.get("target_page_id")
        suggested_title = group.get("suggested_title") or group["group_label"]
        target_category = group.get("target_category")

        append_block = _format_append_block(group_notes)
        page_id = None

        if action == ACTION_APPEND:
            # Append to existing page
            page = memopedia.get_page(target_page_id)
            if page is None:
                LOGGER.warning("Target page '%s' not found, skipping group '%s'",
                               target_page_id, group["group_label"])
                continue
            memopedia.append_to_content(
                target_page_id, append_block, edit_source="memory_organize",
            )
            page_id = target_page_id
            LOGGER.info("Appended %d notes to page '%s'", len(group_notes), page.title)

        elif action == ACTION_CHILD:
            # Create child page
            parent = memopedia.get_page(target_page_id)
            if parent is None:
                LOGGER.warning("Parent page '%s' not found, skipping group '%s'",
                               target_page_id, group["group_label"])
                continue
            new_page = memopedia.create_page(
                parent_id=target_page_id,
                title=suggested_title,
                summary="",
                content=append_block,
                edit_source="memory_organize",
            )
            page_id = new_page.id
            LOGGER.info("Created child page '%s' under '%s'", suggested_title, parent.title)

        elif action == ACTION_NEW:
            # Create new top-level page
            category = target_category or "terms"
            root_id = f"root_{category}"
            root = memopedia.get_page(root_id)
            if root is None:
                LOGGER.warning("Category root '%s' not found, falling back to root_terms", root_id)
                root_id = "root_terms"
                root = memopedia.get_page(root_id)
                if root is None:
                    LOGGER.error("root_terms not found, cannot create page")
                    continue
            new_page = memopedia.create_page(
                parent_id=root_id,
                title=suggested_title,
                summary="",
                content=append_block,
                edit_source="memory_organize",
            )
            page_id = new_page.id
            LOGGER.info("Created new page '%s' in %s", suggested_title, category)

        if page_id:
            # Resolve notes
            resolve_memory_notes(conn, [n.id for n in group_notes])
            results.append(OrganizeResult(
                group_label=group["group_label"],
                action=action,
                page_id=page_id,
                note_count=len(group_notes),
            ))

    LOGGER.info("Organization complete: %d groups processed, %d notes resolved",
                len(results), sum(r.note_count for r in results))
    return results


# Keep plan_notes for backward compatibility / dry-run use
def plan_notes(
    client,
    conn: sqlite3.Connection,
    memopedia_tree: Dict[str, Any],
    *,
    limit: int = 200,
    persona_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run the plan phase only (grouping + target assignment, no writing).

    Useful for dry-run / preview. Does NOT write to Memopedia or resolve notes.
    Sets plan metadata on notes for inspection.

    Args:
        client: LLM client with generate() method.
        conn: Database connection (memory.db).
        memopedia_tree: Output of Memopedia.get_tree() (includes content for char count).
        limit: Max number of unplanned notes to process.
        persona_id: Optional persona ID for usage tracking.

    Returns:
        List of group plan dicts.
    """
    notes = get_unplanned_notes(conn, limit=limit)
    if not notes:
        LOGGER.info("No unplanned notes to organize")
        return []

    LOGGER.info("Planning organization for %d unplanned notes", len(notes))

    tree_text = _format_memopedia_tree_for_plan(memopedia_tree)
    notes_text = _format_notes_for_plan(notes)
    prompt = _build_plan_prompt(notes_text, tree_text)

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
    except Exception as e:
        LOGGER.warning("LLM call failed for note organization plan: %s", e)
        return []

    # Track usage
    if persona_id and hasattr(client, "last_usage"):
        try:
            from sai_memory.arasuji.generator import _record_llm_usage
            _record_llm_usage(client, persona_id, "memory_note_plan")
        except Exception:
            pass

    valid_note_ids = {n.id for n in notes}
    groups = _parse_plan_response(response or "", valid_note_ids)

    if not groups:
        LOGGER.info("No valid groups extracted from plan response")
        return []

    # Write plan metadata (for inspection only, no Memopedia write)
    written_groups: List[Dict[str, Any]] = []
    for group in groups:
        count = set_note_plan(
            conn,
            group["note_ids"],
            group_label=group["group_label"],
            action=group["action"],
            target_page_id=group.get("target_page_id"),
            suggested_title=group.get("suggested_title"),
            target_category=group.get("target_category"),
        )
        if count > 0:
            written_groups.append(group)
            LOGGER.info(
                "Planned group '%s': %d notes → %s (target=%s)",
                group["group_label"], count, group["action"],
                group.get("target_page_id") or group.get("suggested_title"),
            )

    LOGGER.info("Plan complete: %d groups planned", len(written_groups))
    return written_groups
