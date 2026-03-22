"""Memory note organization: exec phase.

Takes one planned group of notes and writes them to Memopedia.
Handles append_to_existing, create_child, and create_new actions.
Excludes notes that don't belong in the group (reverts them to unplanned).
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
    clear_note_plan,
    get_planned_notes_by_group,
    resolve_memory_notes,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class ExecResult:
    """Result of executing one group's organization."""
    group_label: str
    action: str
    page_id: str  # ID of the page that was created/updated
    resolved_count: int
    excluded_count: int
    excluded_ids: List[str]


def _build_append_prompt(
    notes: List[MemoryNote],
    page_title: str,
    page_content: str,
    page_summary: str,
) -> str:
    """Build prompt for appending notes to an existing page."""
    notes_text = "\n".join(f"- [{n.id}] {n.content}" for n in notes)

    return f"""\
以下のメモリーノートを、既存のMemopediaページに統合してください。

## 対象ページ
タイトル: {page_title}
概要: {page_summary}

### 現在の内容
{page_content}

## 統合するメモリーノート
{notes_text}

## 指示

1. ノートの情報を既存の内容に自然に統合した**更新後の全文**を書いてください
2. 矛盾する情報（例: 「帰省中」と「帰省完了」）がある場合は、最新の状態に統一してください
3. 既存の内容の構造や文体を維持してください
4. **グループに含まれているが、このページのトピックと明らかに無関係なノートがあれば、excluded_note_ids に含めてください**（これらは別のページに再振り分けされます）
5. 更新後の概要（summary）も必要に応じて更新してください
6. ページの内容は2000字程度を目安にしてください

## 出力形式

```json
{{
  "updated_content": "更新後のページ内容の全文",
  "updated_summary": "更新後の概要",
  "excluded_note_ids": ["無関係なノートのID（あれば）"],
  "exclude_reasons": "除外理由の説明（あれば）"
}}
```

JSONのみを出力してください。"""


def _build_create_prompt(
    notes: List[MemoryNote],
    suggested_title: str,
    parent_title: Optional[str] = None,
    parent_summary: Optional[str] = None,
) -> str:
    """Build prompt for creating a new page from notes."""
    notes_text = "\n".join(f"- [{n.id}] {n.content}" for n in notes)

    parent_info = ""
    if parent_title:
        parent_info = f"\n親ページ: {parent_title}"
        if parent_summary:
            parent_info += f"（{parent_summary}）"

    return f"""\
以下のメモリーノートから、新しいMemopediaページを作成してください。

## ページ情報
タイトル案: {suggested_title}{parent_info}

## メモリーノート
{notes_text}

## 指示

1. ノートの情報をまとめた**ページ内容**を書いてください
2. 矛盾する情報がある場合は、最新の状態に統一してください
3. **グループに含まれているが、このページのトピックと明らかに無関係なノートがあれば、excluded_note_ids に含めてください**
4. タイトルは提案されたものを使うか、より適切なものに変更してください
5. 概要（summary）を1〜2文で書いてください
6. 関連するキーワードを3〜5個挙げてください
7. ページの内容は2000字程度を目安にしてください

## 出力形式

```json
{{
  "title": "ページタイトル",
  "summary": "ページの概要（1〜2文）",
  "content": "ページの内容",
  "keywords": ["キーワード1", "キーワード2", "キーワード3"],
  "excluded_note_ids": ["無関係なノートのID（あれば）"],
  "exclude_reasons": "除外理由の説明（あれば）"
}}
```

JSONのみを出力してください。"""


def _parse_json_response(response: str) -> Optional[Dict[str, Any]]:
    """Parse LLM response as JSON, handling markdown code blocks."""
    if not response:
        return None

    text = response.strip()
    md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to parse exec response as JSON: %s", text[:300])
        return None


def _handle_exclusions(
    conn: sqlite3.Connection,
    excluded_ids: List[str],
    valid_note_ids: set,
) -> int:
    """Clear plan metadata from excluded notes so they return to unplanned."""
    valid_excluded = [nid for nid in excluded_ids if nid in valid_note_ids]
    if not valid_excluded:
        return 0
    count = clear_note_plan(conn, valid_excluded)
    LOGGER.info("Reverted %d excluded notes to unplanned", count)
    return count


def execute_group(
    client,
    conn: sqlite3.Connection,
    group_label: str,
    memopedia,
    *,
    persona_id: Optional[str] = None,
) -> Optional[ExecResult]:
    """Execute one planned group: write notes to Memopedia.

    Args:
        client: LLM client with generate() method.
        conn: Database connection (memory.db).
        group_label: The group to process.
        memopedia: Memopedia instance (from sai_memory.memopedia.core).
        persona_id: Optional persona ID for usage tracking.

    Returns:
        ExecResult on success, None on failure.
    """
    notes = get_planned_notes_by_group(conn, group_label)
    if not notes:
        LOGGER.info("No notes found for group '%s'", group_label)
        return None

    action = notes[0].action
    target_page_id = notes[0].target_page_id
    suggested_title = notes[0].suggested_title or group_label
    target_category = notes[0].target_category
    valid_note_ids = {n.id for n in notes}

    LOGGER.info(
        "Executing group '%s': %d notes, action=%s, target=%s",
        group_label, len(notes), action, target_page_id or suggested_title,
    )

    if action == "append_to_existing":
        return _exec_append(
            client, conn, notes, target_page_id, memopedia,
            valid_note_ids=valid_note_ids, group_label=group_label,
            persona_id=persona_id,
        )
    elif action == "create_child":
        return _exec_create_child(
            client, conn, notes, target_page_id, suggested_title, memopedia,
            valid_note_ids=valid_note_ids, group_label=group_label,
            target_category=target_category, persona_id=persona_id,
        )
    elif action == "create_new":
        return _exec_create_new(
            client, conn, notes, suggested_title, target_category, memopedia,
            valid_note_ids=valid_note_ids, group_label=group_label,
            persona_id=persona_id,
        )
    else:
        LOGGER.warning("Unknown action '%s' for group '%s'", action, group_label)
        return None


def _exec_append(
    client, conn, notes, target_page_id, memopedia, *,
    valid_note_ids, group_label, persona_id,
) -> Optional[ExecResult]:
    """Append notes to an existing page."""
    page = memopedia.get_page(target_page_id)
    if page is None:
        LOGGER.warning("Target page '%s' not found, reverting group to unplanned", target_page_id)
        clear_note_plan(conn, [n.id for n in notes])
        return None

    prompt = _build_append_prompt(notes, page.title, page.content, page.summary)

    try:
        response = client.generate(messages=[{"role": "user", "content": prompt}], tools=[])
    except Exception as e:
        LOGGER.warning("LLM call failed for exec append: %s", e)
        return None

    parsed = _parse_json_response(response or "")
    if not parsed:
        return None

    # Update page
    updated_content = parsed.get("updated_content", "")
    updated_summary = parsed.get("updated_summary")
    if not updated_content:
        LOGGER.warning("Empty updated_content from LLM for group '%s'", group_label)
        return None

    memopedia.update_page(
        target_page_id,
        content=updated_content,
        summary=updated_summary if updated_summary else None,
        edit_source="memory_organize",
    )

    # Handle exclusions
    excluded_ids = parsed.get("excluded_note_ids", []) or []
    excluded_count = _handle_exclusions(conn, excluded_ids, valid_note_ids)

    # Resolve remaining notes
    resolved_ids = [n.id for n in notes if n.id not in set(excluded_ids)]
    resolve_memory_notes(conn, resolved_ids)

    LOGGER.info(
        "Appended to page '%s': %d resolved, %d excluded",
        page.title, len(resolved_ids), excluded_count,
    )

    return ExecResult(
        group_label=group_label,
        action="append_to_existing",
        page_id=target_page_id,
        resolved_count=len(resolved_ids),
        excluded_count=excluded_count,
        excluded_ids=excluded_ids,
    )


def _exec_create_child(
    client, conn, notes, parent_page_id, suggested_title, memopedia, *,
    valid_note_ids, group_label, target_category, persona_id,
) -> Optional[ExecResult]:
    """Create a child page under an existing parent."""
    parent = memopedia.get_page(parent_page_id)
    if parent is None:
        LOGGER.warning("Parent page '%s' not found, reverting to unplanned", parent_page_id)
        clear_note_plan(conn, [n.id for n in notes])
        return None

    prompt = _build_create_prompt(
        notes, suggested_title,
        parent_title=parent.title, parent_summary=parent.summary,
    )

    try:
        response = client.generate(messages=[{"role": "user", "content": prompt}], tools=[])
    except Exception as e:
        LOGGER.warning("LLM call failed for exec create_child: %s", e)
        return None

    parsed = _parse_json_response(response or "")
    if not parsed:
        return None

    title = parsed.get("title", suggested_title)
    summary = parsed.get("summary", "")
    content = parsed.get("content", "")
    keywords = parsed.get("keywords", [])

    if not content:
        LOGGER.warning("Empty content from LLM for group '%s'", group_label)
        return None

    new_page = memopedia.create_page(
        parent_id=parent_page_id,
        title=title,
        summary=summary,
        content=content,
        keywords=keywords,
        edit_source="memory_organize",
    )

    # Handle exclusions
    excluded_ids = parsed.get("excluded_note_ids", []) or []
    excluded_count = _handle_exclusions(conn, excluded_ids, valid_note_ids)

    # Resolve remaining
    resolved_ids = [n.id for n in notes if n.id not in set(excluded_ids)]
    resolve_memory_notes(conn, resolved_ids)

    LOGGER.info(
        "Created child page '%s' under '%s': %d resolved, %d excluded",
        title, parent.title, len(resolved_ids), excluded_count,
    )

    return ExecResult(
        group_label=group_label,
        action="create_child",
        page_id=new_page.id,
        resolved_count=len(resolved_ids),
        excluded_count=excluded_count,
        excluded_ids=excluded_ids,
    )


def _exec_create_new(
    client, conn, notes, suggested_title, target_category, memopedia, *,
    valid_note_ids, group_label, persona_id,
) -> Optional[ExecResult]:
    """Create a new top-level page under a category root."""
    # Determine parent (category root)
    category = target_category or "terms"
    root_id = f"root_{category}"

    # Verify root exists
    root = memopedia.get_page(root_id)
    if root is None:
        LOGGER.warning("Category root '%s' not found, falling back to root_terms", root_id)
        root_id = "root_terms"
        root = memopedia.get_page(root_id)
        if root is None:
            LOGGER.error("root_terms not found either, cannot create page")
            return None

    prompt = _build_create_prompt(notes, suggested_title)

    try:
        response = client.generate(messages=[{"role": "user", "content": prompt}], tools=[])
    except Exception as e:
        LOGGER.warning("LLM call failed for exec create_new: %s", e)
        return None

    parsed = _parse_json_response(response or "")
    if not parsed:
        return None

    title = parsed.get("title", suggested_title)
    summary = parsed.get("summary", "")
    content = parsed.get("content", "")
    keywords = parsed.get("keywords", [])

    if not content:
        LOGGER.warning("Empty content from LLM for group '%s'", group_label)
        return None

    new_page = memopedia.create_page(
        parent_id=root_id,
        title=title,
        summary=summary,
        content=content,
        keywords=keywords,
        edit_source="memory_organize",
    )

    # Handle exclusions
    excluded_ids = parsed.get("excluded_note_ids", []) or []
    excluded_count = _handle_exclusions(conn, excluded_ids, valid_note_ids)

    # Resolve remaining
    resolved_ids = [n.id for n in notes if n.id not in set(excluded_ids)]
    resolve_memory_notes(conn, resolved_ids)

    LOGGER.info(
        "Created new page '%s' in %s: %d resolved, %d excluded",
        title, category, len(resolved_ids), excluded_count,
    )

    return ExecResult(
        group_label=group_label,
        action="create_new",
        page_id=new_page.id,
        resolved_count=len(resolved_ids),
        excluded_count=excluded_count,
        excluded_ids=excluded_ids,
    )
