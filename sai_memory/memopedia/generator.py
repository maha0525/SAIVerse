"""On-demand Memopedia page generation using memory recall.

Uses a Deep Research-style loop:
1. Search with memory_recall for relevant messages
2. Expand context around found messages  
3. Extract knowledge via LLM
4. Check if information is sufficient
5. Repeat with different queries if needed
6. Save as Memopedia page
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from sai_memory.memory.storage import Message, get_message
from sai_memory.memopedia import Memopedia

LOGGER = logging.getLogger(__name__)


@dataclass
class GenerationContext:
    """Tracks state across generation loops."""
    keyword: str
    category: Optional[str]
    accumulated_info: str = ""
    processed_message_ids: Set[str] = field(default_factory=set)
    loop_count: int = 0
    queries_tried: List[str] = field(default_factory=list)


def get_messages_around(
    conn,
    message_id: str,
    window: int = 5,
) -> List[Message]:
    """Get messages around a specific message (before and after).
    
    Args:
        conn: Database connection
        message_id: Center message ID
        window: Number of messages before/after to fetch
        
    Returns:
        List of messages ordered by created_at
    """
    # First get the center message to find its timestamp and thread
    center_msg = get_message(conn, message_id)
    if not center_msg:
        return []
    
    # Get messages in same thread around that time
    cur = conn.execute("""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        WHERE thread_id = ?
        ORDER BY created_at ASC
    """, (center_msg.thread_id,))
    
    import json
    all_msgs = []
    center_idx = -1
    for i, row in enumerate(cur.fetchall()):
        msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
        metadata = {}
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except:
                pass
        msg = Message(
            id=msg_id,
            thread_id=tid,
            role=role,
            content=content,
            resource_id=resource_id,
            created_at=created_at,
            metadata=metadata,
        )
        all_msgs.append(msg)
        if msg_id == message_id:
            center_idx = i
    
    if center_idx < 0:
        return []
    
    # Extract window around center
    start_idx = max(0, center_idx - window)
    end_idx = min(len(all_msgs), center_idx + window + 1)
    
    return all_msgs[start_idx:end_idx]


def recall_relevant_messages(
    conn,
    query: str,
    persona_id: str,
    persona_dir: str,
    topk: int = 5,
) -> List[str]:
    """Use semantic recall to find relevant message IDs.
    
    Uses sai_memory.memory.recall.semantic_recall_groups directly
    to get message IDs that semantically match the query.
    
    Returns:
        List of message IDs that match the query
    """
    from sai_memory.config import load_settings
    from sai_memory.memory.recall import Embedder, semantic_recall_groups
    
    try:
        settings = load_settings()
        
        # Initialize embedder
        embedder = Embedder(
            model=settings.embed_model,
            local_model_path=settings.embed_model_path,
            model_dim=settings.embed_model_dim,
        )
        
        # Run semantic recall - search across all threads (thread_id=None, resource_id=None)
        groups = semantic_recall_groups(
            conn,
            embedder,
            query,
            thread_id=None,  # Search all threads
            resource_id=None,  # Search all resources
            topk=topk,
            range_before=0,  # Just get the seed message, not surrounding
            range_after=0,
            scope="all",
            required_tags=["conversation"],
        )
        
        # Extract message IDs from results
        message_ids = []
        for seed, bundle, score in groups:
            message_ids.append(seed.id)
            LOGGER.debug(f"Recall hit: {seed.id} (score={score:.3f})")
        
        return message_ids
        
    except Exception as e:
        LOGGER.error(f"Recall failed: {e}")
        return []


def format_messages_for_extraction(messages: List[Message]) -> str:
    """Format messages for LLM prompt."""
    lines = []
    for msg in messages:
        role = "assistant" if msg.role == "model" else msg.role
        content = (msg.content or "").strip()
        if content:
            lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


def generate_memopedia_page(
    conn,
    client,
    keyword: str,
    directions: Optional[str],
    category: Optional[str],
    persona_id: str,
    persona_dir: str,
    max_loops: int = 5,
    context_window: int = 5,
    with_chronicle: bool = True,
    progress_callback=None,
) -> Optional[Dict[str, Any]]:
    """Generate a Memopedia page by iteratively collecting information.
    
    Args:
        conn: Database connection
        client: LLM client
        keyword: Topic to create page about
        directions: Optional directions for what to research or how to summarize
        category: Optional category (people, terms, plans)
        persona_id: Persona ID
        persona_dir: Path to persona directory
        max_loops: Maximum search iterations
        context_window: Messages to fetch around each hit
        with_chronicle: Whether to include Chronicle context for better understanding
        progress_callback: Optional callback(loop, max_loops, message)
        
    Returns:
        Page data dict or None if failed
    """
    ctx = GenerationContext(
        keyword=keyword,
        category=category,
    )
    
    # Get Chronicle context if enabled
    chronicle_context = ""
    if with_chronicle:
        try:
            from sai_memory.arasuji.context import get_episode_context, format_episode_context
            entries = get_episode_context(conn, max_entries=20)
            if entries:
                formatted = format_episode_context(entries, include_level_info=True)
                # Truncate to reasonable size
                chronicle_context = formatted[:3000] if len(formatted) > 3000 else formatted
                LOGGER.info(f"Loaded Chronicle context ({len(chronicle_context)} chars)")
        except Exception as e:
            LOGGER.warning(f"Failed to load Chronicle context: {e}")
    
    memopedia = Memopedia(conn)
    
    for loop in range(max_loops):
        ctx.loop_count = loop + 1
        
        if progress_callback:
            progress_callback(loop + 1, max_loops, f"Search loop {loop + 1}/{max_loops}")
        
        # Build search query
        if loop == 0:
            query = keyword
        else:
            # Ask LLM for next query based on what we have
            query = _build_next_query(client, keyword, ctx.accumulated_info, ctx.queries_tried)
            if not query or query in ctx.queries_tried:
                LOGGER.info("No new query to try, stopping search")
                break
        
        ctx.queries_tried.append(query)
        LOGGER.info(f"[Loop {loop + 1}] Searching with query: {query}")
        
        # Recall relevant messages
        message_ids = recall_relevant_messages(
            conn, query, persona_id, persona_dir, topk=5
        )
        
        if not message_ids:
            LOGGER.info("No messages found for query")
            continue
        
        # Filter out already processed
        new_ids = [mid for mid in message_ids if mid not in ctx.processed_message_ids]
        if not new_ids:
            LOGGER.info("All found messages already processed")
            continue
        
        # Get surrounding context for each hit
        all_context_messages = []
        for msg_id in new_ids[:3]:  # Limit to top 3 hits per loop
            ctx.processed_message_ids.add(msg_id)
            context_msgs = get_messages_around(conn, msg_id, window=context_window)
            for m in context_msgs:
                if m.id not in ctx.processed_message_ids:
                    ctx.processed_message_ids.add(m.id)
                    all_context_messages.append(m)
        
        if not all_context_messages:
            continue
        
        # Sort by time
        all_context_messages.sort(key=lambda m: m.created_at)
        
        # Extract knowledge from this batch
        conversation = format_messages_for_extraction(all_context_messages)
        extracted = _extract_knowledge(client, keyword, directions, conversation, ctx.accumulated_info, chronicle_context)
        
        if extracted:
            if ctx.accumulated_info:
                ctx.accumulated_info += "\n\n---\n\n" + extracted
            else:
                ctx.accumulated_info = extracted
            LOGGER.info(f"Accumulated {len(ctx.accumulated_info)} chars of info")
        
        # Check if we have enough information
        is_sufficient = _check_sufficiency(client, keyword, ctx.accumulated_info)
        if is_sufficient:
            LOGGER.info("Information sufficient, stopping search")
            break
    
    if not ctx.accumulated_info:
        LOGGER.warning("No information collected for keyword")
        return None
    
    # Generate final page
    page_data = _generate_final_page(
        client, keyword, directions, category, ctx.accumulated_info, memopedia, chronicle_context
    )
    
    if not page_data:
        return None
    
    # Save to Memopedia
    existing = memopedia.find_by_title(page_data["title"], page_data.get("category"))
    
    if existing:
        # Update existing page
        memopedia.update_page(
            existing.id,
            content=existing.content + "\n\n" + page_data["content"],
            summary=page_data.get("summary", existing.summary),
        )
        page_data["page_id"] = existing.id
        page_data["action"] = "updated"
        LOGGER.info(f"Updated existing page: {page_data['title']}")
    else:
        # Create new page
        category_root = {
            "people": "root_people",
            "terms": "root_terms",
            "plans": "root_plans",
        }.get(page_data.get("category", "terms"), "root_terms")
        
        new_page = memopedia.create_page(
            parent_id=category_root,
            title=page_data["title"],
            summary=page_data.get("summary", ""),
            content=page_data["content"],
            keywords=page_data.get("keywords", [keyword]),
        )
        page_data["page_id"] = new_page.id
        page_data["action"] = "created"
        LOGGER.info(f"Created new page: {page_data['title']}")
    
    return page_data


def _build_next_query(
    client,
    keyword: str,
    accumulated_info: str,
    queries_tried: List[str],
) -> Optional[str]:
    """Ask LLM for the next search query."""
    prompt = f"""トピック「{keyword}」について情報を集めています。

これまで試したクエリ:
{chr(10).join(f'- {q}' for q in queries_tried) if queries_tried else '(なし)'}

収集済み情報:
{accumulated_info[:2000] if accumulated_info else '(まだなし)'}

上記を踏まえて、まだ足りない情報を探すための新しい検索クエリを1つ提案してください。
既に試したクエリと重複しないものにしてください。
これ以上探す必要がなければ「完了」と答えてください。

クエリのみを返してください（説明不要）。"""

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        query = response.strip().strip('"\'')
        if not query or query == "完了" or len(query) > 100:
            return None
        return query
    except Exception as e:
        LOGGER.error(f"Failed to build next query: {e}")
        return None


def _extract_knowledge(
    client,
    keyword: str,
    directions: Optional[str],
    conversation: str,
    accumulated_info: str,
    chronicle_context: str = "",
) -> Optional[str]:
    """Extract knowledge about keyword from conversation."""
    
    # Build directions section
    directions_section = ""
    if directions:
        directions_section = f"\n=== 調査の方向性 ===\n{directions}\n"
    
    # Build chronicle section
    chronicle_section = ""
    if chronicle_context:
        chronicle_section = f"\n=== これまでの出来事の流れ（参考） ===\n{chronicle_context[:1500]}\n"
    
    prompt = f"""以下の会話から「{keyword}」に関する情報を抽出してください。
{directions_section}{chronicle_section}
=== 会話 ===
{conversation}

=== これまでに集めた情報 ===
{accumulated_info[:1500] if accumulated_info else '(なし)'}

【指示】
- 「{keyword}」に直接関連する情報のみを抽出
- 既に集めた情報と重複する内容は省略
- 事実、定義、特徴、関連する出来事などを箇条書きまたは短い段落で記述
{'- 調査の方向性に沿った情報を優先的に抽出' if directions else ''}
- 関連情報がなければ「関連情報なし」と返答

抽出した情報のみを返してください（説明や前置き不要）。"""

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        text = response.strip()
        if "関連情報なし" in text or len(text) < 20:
            return None
        return text
    except Exception as e:
        LOGGER.error(f"Failed to extract knowledge: {e}")
        return None


def _check_sufficiency(
    client,
    keyword: str,
    accumulated_info: str,
) -> bool:
    """Check if we have enough information."""
    if not accumulated_info or len(accumulated_info) < 100:
        return False
    
    prompt = f"""トピック「{keyword}」について以下の情報が集まっています。

{accumulated_info}

この情報量で「{keyword}」についてのページを作成するのに十分ですか？
「はい」または「いいえ」のみで答えてください。"""

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        return "はい" in response or "yes" in response.lower()
    except:
        return len(accumulated_info) > 500  # Fallback: 500+ chars is probably enough


def _generate_final_page(
    client,
    keyword: str,
    directions: Optional[str],
    category: Optional[str],
    accumulated_info: str,
    memopedia: Memopedia,
    chronicle_context: str = "",
) -> Optional[Dict[str, Any]]:
    """Generate the final Memopedia page from accumulated info."""
    existing_pages = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
    
    category_hint = ""
    if category:
        category_hint = f"カテゴリは「{category}」を使用してください。"
    else:
        category_hint = "適切なカテゴリ（people=人物、terms=用語・概念、plans=計画・予定）を選んでください。"
    
    # Build directions section
    directions_section = ""
    if directions:
        directions_section = f"\n=== 調査の方向性・まとめ方 ===\n{directions}\n"
    
    # Build chronicle section
    chronicle_section = ""
    if chronicle_context:
        chronicle_section = f"\n=== 参考：これまでの出来事の流れ ===\n{chronicle_context[:1500]}\n"
    
    prompt = f"""以下の情報を元に「{keyword}」についてのMemopediaページを作成してください。
{directions_section}{chronicle_section}
=== 収集した情報 ===
{accumulated_info}

=== 既存ページ一覧 ===
{existing_pages[:2000] if existing_pages else '(なし)'}

【指示】
- {category_hint}
- タイトルは簡潔に（キーワードそのまま、または少し補足）
- 要約は1-2文で
- 本文は収集した情報を整理して読みやすく構成
{'- 調査の方向性・まとめ方の指示に沿って記述' if directions else ''}
- キーワード（検索用）を3-5個

以下のJSON形式で返してください:
```json
{{
  "category": "people|terms|plans",
  "title": "ページタイトル",
  "summary": "1-2文の要約",
  "content": "本文（Markdown可）",
  "keywords": ["キーワード1", "キーワード2", ...]
}}
```"""

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        
        # Parse JSON from response
        import json
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        
        data = json.loads(text.strip())
        
        # Validate required fields
        if not all(k in data for k in ["category", "title", "content"]):
            LOGGER.error("Missing required fields in page data")
            return None
        
        return data
        
    except Exception as e:
        LOGGER.error(f"Failed to generate final page: {e}")
        return None


__all__ = ["generate_memopedia_page", "GenerationContext"]
