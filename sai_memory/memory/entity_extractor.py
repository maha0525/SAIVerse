"""Extract entities and per-entity knowledge from conversation messages.

Replaces the old note_extractor + note_organizer pipeline.
Instead of extracting free-form notes and grouping them later,
this module identifies named entities (people, AIs, projects, etc.)
and extracts knowledge specific to each entity in a single LLM call.

The extracted entities are directly reflected in Memopedia:
- New entities get a new page created
- Existing entities get notes appended to their page
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sai_memory.memory.storage import Message

LOGGER = logging.getLogger(__name__)

# Valid Memopedia categories
VALID_CATEGORIES = {"people", "terms", "plans", "events"}
DEFAULT_CATEGORY = "terms"

# Category root page IDs
CATEGORY_ROOT_IDS = {
    "people": "root_people",
    "terms": "root_terms",
    "plans": "root_plans",
    "events": "root_events",
}


@dataclass
class ExtractedEntity:
    """An entity recognized from conversation with associated knowledge notes."""
    name: str
    category: str
    summary: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class EntityExtractionResult:
    """Result of entity extraction and Memopedia reflection."""
    entity_name: str
    page_id: str
    is_new_page: bool
    notes_appended: int


def _build_extraction_prompt(
    conversation: str,
    *,
    episode_context: str = "",
    existing_pages: str = "",
) -> str:
    """Build the entity extraction prompt."""
    parts = [
        "あなたは知識ベースの整理係です。以下の会話に登場する固有の対象（エンティティ）を認識し、"
        "各エンティティについて新たに判明した情報を抽出してください。",
        "",
        "## エンティティとは",
        "",
        "Memopedia（知識ベース）でページを持つべき固有の対象物です。",
        "- 人物: ユーザー、友人、家族など（例: まはー、ナチュレ）",
        "- AI: AIペルソナ、AIプロジェクト（例: エイド、エリス）",
        "- プロジェクト/作品: ソフトウェア、ゲーム、創作物（例: SAIVerse、Project N.E.K.O.）",
        "- 用語/概念: 固有の技術用語、独自概念（例: Playbook、マザーAI法）",
        "- 場所: 固有名を持つ場所（例: 創造の祭壇）",
        "- イベント: 固有の出来事（例: ぽこあポケモン発売）",
        "",
        "**エンティティではないもの**:",
        "- 一般名詞や抽象概念",
        "- Wikipediaや検索エンジンで調べれば誰でも分かるもの"
        "（広く知られたサービス、技術用語、プログラミング言語、ツール名など）",
        "- ファイル名、パス、CLIコマンド名",
        "- IDやハッシュ値、ランダムな文字列、メールアドレス",
        "- APIスコープ名、認証トークン、パスワード等の技術的な識別子",
        "- その場限りのエラーメッセージやステータス",
        "",
        "**判定基準**: 「これは会話の当事者たちの間でだけ通じる固有の対象か？」",
        "世間一般に知られている事物は、この人たちの個人的な知識ベースに記録する必要はありません。",
        "当事者たちの関係性・創作・プロジェクトに固有の対象だけを記録してください。",
        "",
        "## 会話当事者についての注意",
        "",
        "会話の当事者（ペルソナ自身、ユーザー、頻繁に登場するAI）については、"
        "**6ヶ月後でも変わらない恒久的な属性のみ**を記録してください。",
        "",
        "記録すべき（恒久的な属性）:",
        "- 誕生日、好き嫌い、価値観、信念、性格的特徴",
        "- 人間関係や関係性の定義",
        "- 恒久的な習慣や癖",
        "",
        "記録すべきでない（行動記録・一時的な状態）:",
        "- 「〜した」「〜を行った」「〜を試みた」「〜に成功した」のような行動の記録",
        "- 「〜に直面している」「〜を感じている」のような一時的な状態",
        "- 作業の詳細や手順",
        "",
        "悪い例（行動記録であり、Chronicleの仕事）:",
        "- 「設定ファイルを編集して問題を解決した」",
        "- 「長時間の作業により疲労を感じている」",
        "",
        "良い例（恒久的な属性）:",
        "- 「几帳面な性格で、細部にこだわる傾向がある」",
        "- 「物語の中に驚きや成長の要素があることを好む」",
        "",
    ]

    if episode_context:
        parts.extend([
            "## これまでの流れ（参考）",
            episode_context,
            "",
        ])

    if existing_pages:
        parts.extend([
            "## 既存のMemopediaページ一覧",
            "以下のページが既に存在します。同名のエンティティが会話に登場した場合は、"
            "既に記録済みの情報と重複しないよう、新たに判明した情報のみを抽出してください。"
            "一覧にないエンティティが会話に登場した場合は新規として抽出してください。",
            existing_pages,
            "",
        ])

    parts.extend([
        "## 今回の会話",
        conversation,
        "",
        "## 指示",
        "",
        "- 会話に登場するエンティティ（固有名詞を持つ対象）を列挙してください",
        "- 各エンティティについて **summary**（そのエンティティが何であるかの一文定義）を書いてください",
        "  - 例: 「ソフィーの一人であるAIペルソナ」「まはーが開発しているAIプラットフォーム」",
        "  - 既存ページにあるエンティティでも、summaryは毎回書いてください",
        "- 各エンティティについて、この会話で新たに判明した事実・属性・状態変化を **notes** として記録してください",
        "- **notesは、その情報単体で意味が通じるように書いてください**。エンティティ名のページに記載される情報なので、",
        "  「このエンティティは何か」という前提知識がない読者でも理解できる文脈を含めてください",
        "  - 悪い例: 「元コードネームは「γ」である」（何の文脈か不明）",
        "  - 良い例: 「SAIVerseの開発初期にはコードネーム「γ」で呼ばれていた」",
        "- notesは短い1文で記述してください（「〜である」「〜が判明した」等）",
        "- 会話の中で明確に述べられた事実のみを抽出してください（推測や解釈は含めない）",
        "- 既存ページに記録済みの情報と重複するnotesは除外してください",
        "- 抽出すべきエンティティがない場合は空のリストを返してください",
        "- categoryは people / terms / plans / events のいずれかを指定してください",
        "- 日本語で出力してください",
        "",
        "## 出力形式",
        "",
        "以下のJSON形式で出力してください。",
        "```json",
        json.dumps({
            "entities": [
                {
                    "name": "エンティティ名",
                    "category": "people",
                    "summary": "このエンティティが何であるかの一文定義",
                    "notes": ["文脈を含む事実1", "文脈を含む事実2"],
                },
            ]
        }, ensure_ascii=False, indent=2),
        "```",
        "",
        "JSONのみを出力してください。",
    ])

    return "\n".join(parts)


def _format_messages(messages: List[Message]) -> str:
    """Format messages for the extraction prompt."""
    lines: List[str] = []
    for msg in messages:
        role = msg.role
        if role == "model":
            role = "assistant"
        content = (msg.content or "").strip()
        if not content:
            continue
        ts_str = datetime.fromtimestamp(msg.created_at).strftime("%Y-%m-%d %H:%M") if msg.created_at else "?"
        lines.append(f"[{ts_str}] [{role}]: {content}")
    return "\n\n".join(lines)


def _parse_extraction_response(response: str) -> List[ExtractedEntity]:
    """Parse LLM response into ExtractedEntity list."""
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
        LOGGER.warning("Failed to parse entity extraction response as JSON: %s", text[:300])
        return []

    entities_raw = parsed.get("entities", []) if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []

    results = []
    for item in entities_raw:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        category = (item.get("category") or DEFAULT_CATEGORY).strip().lower()
        if category not in VALID_CATEGORIES:
            category = DEFAULT_CATEGORY
        summary = (item.get("summary") or "").strip()
        notes = []
        for note in item.get("notes", []):
            note_str = str(note).strip()
            if note_str:
                notes.append(note_str)
        if notes or summary:
            results.append(ExtractedEntity(name=name, category=category, summary=summary, notes=notes))

    return results


def _format_page_list(memopedia) -> str:
    """Format existing Memopedia pages as a compact list for the prompt."""
    try:
        tree = memopedia.get_tree()
    except Exception:
        return ""

    lines = []
    for category_key in ("people", "terms", "plans", "events"):
        pages = tree.get(category_key, [])
        if not pages:
            continue
        lines.append(f"[{category_key}]")
        for page in pages:
            title = page.get("title", "?")
            lines.append(f"  - {title}")
            for child in page.get("children", []):
                lines.append(f"    - {child.get('title', '?')}")
    return "\n".join(lines)


def extract_entities(
    client,
    messages: List[Message],
    *,
    episode_context: str = "",
    existing_pages: str = "",
    persona_id: Optional[str] = None,
) -> List[ExtractedEntity]:
    """Extract entities and per-entity knowledge from conversation messages.

    Args:
        client: LLM client.
        messages: Conversation messages to process.
        episode_context: Chronicle context for surrounding events.
        existing_pages: Formatted list of existing Memopedia pages.
        persona_id: Optional persona ID for usage tracking.

    Returns:
        List of ExtractedEntity with names, categories, and notes.
    """
    if not messages:
        return []

    conversation = _format_messages(messages)
    if not conversation.strip():
        return []

    prompt = _build_extraction_prompt(
        conversation,
        episode_context=episode_context,
        existing_pages=existing_pages,
    )

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
    except Exception as exc:
        LOGGER.error("Entity extraction LLM call failed: %s", exc)
        return []

    if not response:
        LOGGER.debug("Entity extraction returned empty response")
        return []

    entities = _parse_extraction_response(response)
    LOGGER.info(
        "Extracted %d entities from %d messages",
        len(entities), len(messages),
    )
    for ent in entities:
        LOGGER.debug("  Entity '%s' [%s]: %d notes", ent.name, ent.category, len(ent.notes))

    return entities



def reflect_to_memopedia(
    entities: List[ExtractedEntity],
    memopedia,
    *,
    source_time: Optional[int] = None,
    chronicle_entry_id: Optional[str] = None,
) -> List[EntityExtractionResult]:
    """Reflect extracted entities into Memopedia pages and create Fragments.

    For each entity:
    - Search for existing page by title
    - If found, refresh its summary when changed and create Fragments from notes
    - If not found, create a new page and create Fragments from notes

    Notes are recorded as Fragments, not appended to page content (content is
    reserved for manual edits).

    Args:
        entities: Extracted entities from extract_entities().
        memopedia: Memopedia instance.
        source_time: Timestamp used as the source date on created Fragments.
        chronicle_entry_id: ID of the Chronicle Lv-1 entry that triggered this extraction.

    Returns:
        List of results describing what was done.
    """
    results = []
    date_str = datetime.fromtimestamp(source_time).strftime("%Y-%m-%d") if source_time else datetime.now().strftime("%Y-%m-%d")

    for entity in entities:
        if not entity.notes and not entity.summary:
            continue

        # Search for existing page
        page = memopedia.find_by_title(entity.name)

        if page:
            # Update summary to reflect latest understanding
            if entity.summary and entity.summary != page.summary:
                from sai_memory.memopedia.storage import update_page as _update_page
                _update_page(
                    memopedia.conn, page.id,
                    summary=entity.summary,
                )
            page_id = page.id
            is_new = False
        else:
            # Create new page (content is manual-edit only; notes go to Fragments)
            root_id = CATEGORY_ROOT_IDS.get(entity.category, CATEGORY_ROOT_IDS[DEFAULT_CATEGORY])
            new_page = memopedia.create_page(
                parent_id=root_id,
                title=entity.name,
                summary=entity.summary,
                content="",
                edit_source="entity_extractor",
            )
            page_id = new_page.id
            is_new = True

        # Create Fragments for each note
        for note in entity.notes:
            memopedia.create_fragment(
                entity_id=page_id,
                content=note,
                chronicle_entry_id=chronicle_entry_id,
                source_date=date_str,
            )

        results.append(EntityExtractionResult(
            entity_name=entity.name,
            page_id=page_id,
            is_new_page=is_new,
            notes_appended=len(entity.notes),
        ))
        LOGGER.info(
            "%s page '%s' (%s): %d fragments created",
            "Created new" if is_new else "Updated",
            entity.name, page_id, len(entity.notes),
        )

    return results


def extract_and_reflect(
    client,
    conn: sqlite3.Connection,
    messages: List[Message],
    *,
    episode_context: str = "",
    persona_id: Optional[str] = None,
    chronicle_entry_id: Optional[str] = None,
) -> List[EntityExtractionResult]:
    """Extract entities from messages and reflect them to Memopedia in one call.

    Convenience function that combines extract_entities() + reflect_to_memopedia().

    Args:
        client: LLM client.
        conn: Database connection (for Memopedia access).
        messages: Conversation messages to process.
        episode_context: Chronicle context for surrounding events.
        persona_id: Optional persona ID for usage tracking.
        chronicle_entry_id: ID of the Chronicle Lv-1 entry for Fragment linkage.

    Returns:
        List of results describing what was created/updated.
    """
    from sai_memory.memopedia import Memopedia, init_memopedia_tables
    init_memopedia_tables(conn)
    memopedia = Memopedia(conn)

    existing_pages = _format_page_list(memopedia)

    entities = extract_entities(
        client, messages,
        episode_context=episode_context,
        existing_pages=existing_pages,
        persona_id=persona_id,
    )

    if not entities:
        return []

    source_time = max((m.created_at for m in messages), default=None)

    return reflect_to_memopedia(
        entities, memopedia,
        source_time=int(source_time) if source_time else None,
        chronicle_entry_id=chronicle_entry_id,
    )


def make_batch_callback(
    client,
    conn: sqlite3.Connection,
    *,
    persona_id: Optional[str] = None,
) -> Callable[[List[Message], Optional[str]], None]:
    """Create a batch callback for Chronicle generation.

    Returns a callback that can be passed to ArasujiGenerator.generate_unprocessed()
    as batch_callback. Each time a batch of messages is processed for Chronicle,
    this callback extracts entities and reflects them to Memopedia.

    Args:
        client: LLM client for entity extraction.
        conn: Database connection.
        persona_id: Optional persona ID for usage tracking.

    Returns:
        Callback function accepting (batch_messages, chronicle_entry_id).
    """
    def callback(batch_messages: List[Message], chronicle_entry_id: Optional[str] = None) -> None:
        if not batch_messages:
            return

        try:
            from sai_memory.arasuji.context import get_episode_context_for_timerange
            start_time = min(m.created_at for m in batch_messages)
            end_time = max(m.created_at for m in batch_messages)

            ep_ctx = ""
            try:
                ep_ctx = get_episode_context_for_timerange(
                    conn, start_time=start_time, end_time=end_time, max_entries=10,
                )
            except Exception:
                pass

            results = extract_and_reflect(
                client, conn, batch_messages,
                episode_context=ep_ctx,
                persona_id=persona_id,
                chronicle_entry_id=chronicle_entry_id,
            )

            if results:
                new_count = sum(1 for r in results if r.is_new_page)
                update_count = sum(1 for r in results if not r.is_new_page)
                total_notes = sum(r.notes_appended for r in results)
                LOGGER.info(
                    "Entity extraction batch complete: %d entities (%d new, %d updated), "
                    "%d notes total, chronicle_entry=%s",
                    len(results), new_count, update_count, total_notes,
                    chronicle_entry_id[:12] if chronicle_entry_id else None,
                )
        except Exception as exc:
            LOGGER.warning("Entity extraction batch callback failed: %s", exc, exc_info=True)

    return callback
