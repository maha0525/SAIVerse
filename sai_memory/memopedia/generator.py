"""On-demand Memopedia page generation using memory recall.

Uses a Deep Research-style loop with persistent conversation history:
1. Search with memory_recall for relevant messages (returns snippets)
2. LLM selects promising hits from search results
3. Expand context around selected messages only
4. Extract knowledge via LLM
5. Check if information is sufficient
6. Repeat with different queries if needed
7. Save as Memopedia page

All LLM calls share a single conversation history for better context
consistency and prompt caching.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from sai_memory.memory.storage import (
    Message,
    get_message,
    get_all_messages_for_search,
)
from sai_memory.memory.recall import semantic_recall_groups
from sai_memory.memopedia import Memopedia
from saiverse.usage_tracker import get_usage_tracker

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


@dataclass
class SearchHit:
    """A search result with snippet for LLM selection."""
    message_id: str
    score: float
    timestamp: int
    role: str
    snippet: str
    matched_keywords: List[str]


def get_messages_around(
    conn,
    message_id: str,
    window: int = 10,
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

    all_msgs = []
    center_idx = -1
    for i, row in enumerate(cur.fetchall()):
        msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
        metadata = {}
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except Exception:
                LOGGER.warning("Failed to parse metadata JSON for message %s", msg_id, exc_info=True)
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


def _extract_snippet(content: str, keywords: Optional[List[str]], max_chars: int = 100) -> str:
    """Extract a snippet centered on the first keyword match, or from the start."""
    if not content:
        return ""

    # Try to find a keyword match position
    if keywords:
        content_lower = content.lower()
        best_pos = -1
        for kw in keywords:
            pos = content_lower.find(kw.lower())
            if pos >= 0:
                if best_pos < 0 or pos < best_pos:
                    best_pos = pos

        if best_pos >= 0:
            # Center the snippet around the match
            half = max_chars // 2
            start = max(0, best_pos - half)
            end = min(len(content), start + max_chars)
            # Adjust start if we're near the end
            if end - start < max_chars:
                start = max(0, end - max_chars)
            snippet = content[start:end]
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(content) else ""
            return f"{prefix}{snippet}{suffix}"

    # No keyword match or no keywords: show from beginning
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def recall_with_snippets(
    conn,
    query: str,
    topk: int = 10,
    keywords: Optional[List[str]] = None,
    max_snippet_chars: int = 100,
) -> List[SearchHit]:
    """Use hybrid recall (semantic + keyword) to find relevant messages with snippets.

    Returns:
        List of SearchHit objects with snippets for LLM selection
    """
    from sai_memory.config import load_settings
    from sai_memory.memory.recall import Embedder

    rrf_k = 60
    message_scores: Dict[str, float] = defaultdict(float)
    message_data: Dict[str, Message] = {}
    keyword_matches: Dict[str, List[str]] = {}

    try:
        settings = load_settings()

        # 1. Keyword search
        if keywords:
            all_msgs = get_all_messages_for_search(
                conn, required_tags=["conversation"],
            )
            keyword_scored = []
            for msg in all_msgs:
                content_lower = (msg.content or "").lower()
                matched = [kw for kw in keywords if kw.lower() in content_lower]
                if matched:
                    keyword_scored.append((msg, len(matched)))
                    keyword_matches[msg.id] = matched

            keyword_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (msg, _count) in enumerate(keyword_scored[:topk * 2], start=1):
                if msg.id not in message_data:
                    message_data[msg.id] = msg
                message_scores[msg.id] += 1.0 / (rrf_k + rank)

        # 2. Semantic search
        if query and query.strip():
            embedder = Embedder(
                model=settings.embed_model,
                local_model_path=settings.embed_model_path,
                model_dim=settings.embed_model_dim,
            )

            groups = semantic_recall_groups(
                conn,
                embedder,
                query,
                thread_id=None,
                resource_id=None,
                topk=topk * 2,
                range_before=0,
                range_after=0,
                scope="all",
                required_tags=["conversation"],
            )

            for rank, (seed, _bundle, score) in enumerate(groups, start=1):
                if seed.id not in message_data:
                    message_data[seed.id] = seed
                message_scores[seed.id] += 1.0 / (rrf_k + rank)
                LOGGER.debug("Recall hit: %s (score=%.3f)", seed.id, score)

        if not message_scores:
            return []

        # Sort by RRF score and take top-k
        sorted_ids = sorted(
            message_scores.keys(),
            key=lambda x: message_scores[x],
            reverse=True,
        )[:topk]

        # Build SearchHit objects with snippets
        hits = []
        for msg_id in sorted_ids:
            msg = message_data[msg_id]
            content = (msg.content or "").strip().replace("\n", " ")
            matched_kws = keyword_matches.get(msg_id, [])
            snippet = _extract_snippet(content, matched_kws or keywords, max_snippet_chars)

            hits.append(SearchHit(
                message_id=msg_id,
                score=message_scores[msg_id],
                timestamp=msg.created_at,
                role=msg.role if msg.role != "model" else "assistant",
                snippet=snippet,
                matched_keywords=matched_kws,
            ))

        return hits

    except Exception as e:
        LOGGER.error("Recall failed: %s", e)
        return []


def format_search_results(hits: List[SearchHit], processed_ids: Set[str]) -> str:
    """Format search hits for LLM selection prompt."""
    lines = []
    for i, hit in enumerate(hits, start=1):
        dt = datetime.fromtimestamp(hit.timestamp)
        ts = dt.strftime("%Y-%m-%d %H:%M")
        processed_mark = " [処理済み]" if hit.message_id in processed_ids else ""
        lines.append(f"[{i}] ({hit.message_id}) {ts} {hit.role}: {hit.snippet} (score:{hit.score:.3f}){processed_mark}")
    return "\n".join(lines)


def format_messages_for_extraction(messages: List[Message]) -> str:
    """Format messages for LLM prompt."""
    lines = []
    for msg in messages:
        role = "assistant" if msg.role == "model" else msg.role
        content = (msg.content or "").strip()
        if content:
            dt = datetime.fromtimestamp(msg.created_at)
            ts = dt.strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{role}] {ts}: {content}")
    return "\n\n".join(lines)


def _build_system_message(
    keyword: str,
    directions: Optional[str],
    chronicle_context: str,
    existing_pages: str,
) -> str:
    """Build the system message with all static context."""
    parts = [
        "あなたはMemopedia（知識ベース）のページを作成するリサーチアシスタントです。",
        f"トピック「{keyword}」について、ユーザーの会話ログから情報を収集してページを作成します。",
    ]

    if directions:
        parts.append(f"\n【ユーザーの調査依頼】\n{directions}")

    if chronicle_context:
        parts.append(f"\n【これまでの出来事の流れ（Chronicle）】\n{chronicle_context[:2000]}")

    if existing_pages:
        parts.append(f"\n【既存のMemopediaページ】\n{existing_pages[:1500]}")

    parts.append("""
【あなたの役割】
- ユーザーからの指示に従って、検索クエリの提案、ヒットの選択、情報の抽出、充足判定、ページ作成を行います
- 一般的な知識ではなく、この会話ログに特有の個人的な記憶を探してください
- ChronicleやMemopediaの情報を参考に、関連する人名・キーワードを含めてください
""")

    return "\n".join(parts)


def _parse_json_response(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON from LLM response, handling markdown code blocks."""
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text.strip())
    except Exception:
        return None


def _record_llm_usage(client, persona_id: str, node_type: str) -> None:
    """Record LLM usage from the client to usage tracker."""
    try:
        usage = client.consume_usage()
        if usage:
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                node_type=node_type,
                category="memory_weave_generate",
            )
    except Exception as e:
        LOGGER.warning(f"Failed to record usage: {e}")


def generate_memopedia_page(
    conn,
    client,
    keyword: str,
    directions: Optional[str],
    category: Optional[str],
    persona_id: str,
    persona_dir: str,
    max_loops: int = 5,
    context_window: int = 10,
    with_chronicle: bool = True,
    progress_callback=None,
) -> Optional[Dict[str, Any]]:
    """Generate a Memopedia page by iteratively collecting information.

    Uses a persistent conversation history for all LLM calls, enabling
    prompt caching and better context consistency.

    Args:
        conn: Database connection
        client: LLM client
        keyword: Topic to create page about
        directions: Optional directions for what to research or how to summarize
        category: Optional category (people, terms, plans)
        persona_id: Persona ID
        persona_dir: Path to persona directory
        max_loops: Maximum search iterations
        context_window: Messages to fetch around each selected hit (default: 10)
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
                chronicle_context = formatted[:3000] if len(formatted) > 3000 else formatted
                LOGGER.info(f"Loaded Chronicle context ({len(chronicle_context)} chars)")
        except Exception as e:
            LOGGER.warning(f"Failed to load Chronicle context: {e}")

    memopedia = Memopedia(conn)

    # Get existing Memopedia pages for context
    existing_pages = ""
    try:
        existing_pages = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
        if existing_pages:
            LOGGER.info(f"Loaded existing Memopedia pages ({len(existing_pages)} chars)")
    except Exception as e:
        LOGGER.warning(f"Failed to load existing Memopedia pages: {e}")

    # Build system message and initialize conversation history
    system_message = _build_system_message(keyword, directions, chronicle_context, existing_pages)
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_message}]

    LOGGER.debug(f"System message length: {len(system_message)} chars")

    # Main research loop
    for loop in range(max_loops):
        ctx.loop_count = loop + 1

        if progress_callback:
            progress_callback(loop + 1, max_loops, f"Search loop {loop + 1}/{max_loops}")

        # === Step 1: Ask for next search query ===
        if loop == 0:
            query_prompt = """最初の検索クエリとキーワードを提案してください。

以下のJSON形式で返してください:
```json
{"query": "意味検索用のクエリ文", "keywords": ["キーワード1", "キーワード2"]}
```

- query: 思い出したい内容を自然文で（意味的な検索に使用）
- keywords: 会話ログに含まれているはずの具体的な単語・固有名詞・日付・人名など（部分一致検索に使用）"""
        else:
            queries_list = "\n".join(f"- {q}" for q in ctx.queries_tried)
            query_prompt = f"""これまでに試したクエリ:
{queries_list}

収集済み情報:
{ctx.accumulated_info[:1500] if ctx.accumulated_info else '(まだなし)'}

次の検索クエリとキーワードを提案してください。
まだ探すべき情報がある場合はJSON形式で、十分な場合は「完了」とだけ答えてください。

```json
{{"query": "意味検索用のクエリ文", "keywords": ["キーワード1", "キーワード2"]}}
```"""

        messages.append({"role": "user", "content": query_prompt})

        try:
            response = client.generate(messages=messages, tools=[])
            _record_llm_usage(client, persona_id, "memopedia_query")
            messages.append({"role": "assistant", "content": response})
            LOGGER.debug(f"[Loop {loop + 1}] Query generation response: {response}")
        except Exception as e:
            LOGGER.error(f"LLM call failed: {e}")
            break

        # Parse query response
        if "完了" in response and "query" not in response.lower():
            LOGGER.info(f"[Loop {loop + 1}] LLM indicated search is complete")
            break

        data = _parse_json_response(response)
        if not data:
            LOGGER.warning(f"[Loop {loop + 1}] Failed to parse query response")
            break

        query = data.get("query", "").strip()
        keywords = data.get("keywords", [])

        if not query:
            LOGGER.info(f"[Loop {loop + 1}] No query generated")
            break

        if query in ctx.queries_tried:
            LOGGER.info(f"[Loop {loop + 1}] Query already tried: {query}")
            break

        ctx.queries_tried.append(query)
        LOGGER.info(f"[Loop {loop + 1}] Searching with query: {query}, keywords: {keywords}")

        # === Step 2: Perform search (get snippets) ===
        hits = recall_with_snippets(conn, query, topk=10, keywords=keywords, max_snippet_chars=100)

        if not hits:
            LOGGER.info(f"[Loop {loop + 1}] No messages found for query")
            messages.append({
                "role": "user",
                "content": "検索結果が見つかりませんでした。別のアプローチでクエリを考えてください。"
            })
            continue

        LOGGER.info(f"[Loop {loop + 1}] Search returned {len(hits)} hits")

        # === Step 3: LLM selects promising hits ===
        search_results_text = format_search_results(hits, ctx.processed_message_ids)

        select_prompt = f"""以下の検索結果から、トピック「{keyword}」に関連する有望なメッセージを最大2件選んでください。

=== 検索結果 ===
{search_results_text}

【指示】
- [処理済み]マークのあるメッセージは除外してください
- 新しい情報が得られそうなメッセージを優先してください
- 選択するメッセージIDを配列で返してください

```json
{{"selected_ids": ["msg_xxx", "msg_yyy"], "reason": "選択理由"}}
```

有望なメッセージがない場合は空配列を返してください:
```json
{{"selected_ids": [], "reason": "理由"}}
```"""

        messages.append({"role": "user", "content": select_prompt})

        try:
            response = client.generate(messages=messages, tools=[])
            _record_llm_usage(client, persona_id, "memopedia_select")
            messages.append({"role": "assistant", "content": response})
            LOGGER.debug(f"[Loop {loop + 1}] Selection response: {response}")
        except Exception as e:
            LOGGER.error(f"Selection LLM call failed: {e}")
            continue

        selection_data = _parse_json_response(response)
        if not selection_data:
            LOGGER.warning(f"[Loop {loop + 1}] Failed to parse selection response")
            continue

        selected_ids = selection_data.get("selected_ids", [])
        if not selected_ids:
            LOGGER.info(f"[Loop {loop + 1}] LLM selected no hits: {selection_data.get('reason', 'no reason')}")
            messages.append({
                "role": "user",
                "content": "選択されたメッセージがありませんでした。別の観点からクエリを考えてください。"
            })
            continue

        # Filter to only valid, unprocessed IDs
        valid_ids = [mid for mid in selected_ids if mid not in ctx.processed_message_ids]
        if not valid_ids:
            LOGGER.info(f"[Loop {loop + 1}] All selected messages already processed")
            messages.append({
                "role": "user",
                "content": "選択されたメッセージは既に処理済みでした。別の観点からクエリを考えてください。"
            })
            continue

        LOGGER.info(f"[Loop {loop + 1}] LLM selected {len(valid_ids)} hits: {valid_ids}")

        # === Step 4: Expand context for selected hits only ===
        all_context_messages = []
        for msg_id in valid_ids[:2]:  # Max 2 hits per loop
            context_msgs = get_messages_around(conn, msg_id, window=context_window)
            LOGGER.debug(f"  Seed {msg_id[:8]}...: got {len(context_msgs)} context messages (window={context_window})")
            for m in context_msgs:
                if m.id not in ctx.processed_message_ids:
                    ctx.processed_message_ids.add(m.id)
                    all_context_messages.append(m)
            # Mark the seed as processed too
            ctx.processed_message_ids.add(msg_id)

        if not all_context_messages:
            LOGGER.warning(f"[Loop {loop + 1}] No context messages after filtering")
            continue

        all_context_messages.sort(key=lambda m: m.created_at)
        conversation = format_messages_for_extraction(all_context_messages)
        LOGGER.info(f"[Loop {loop + 1}] Extracting from {len(all_context_messages)} messages ({len(conversation)} chars)")

        # === Step 5: Extract knowledge ===
        extract_prompt = f"""以下の会話から「{keyword}」に関する情報を抽出してください。

=== 検索でヒットした会話 ===
{conversation}

【指示】
- 「{keyword}」に直接関連する情報のみを抽出
- 既に収集した情報と重複する内容は省略
- 事実、定義、特徴、関連する出来事などを箇条書きまたは短い段落で記述
- 関連情報がなければ「関連情報なし」と返答

抽出した情報のみを返してください（説明や前置き不要）。"""

        messages.append({"role": "user", "content": extract_prompt})

        try:
            response = client.generate(messages=messages, tools=[])
            _record_llm_usage(client, persona_id, "memopedia_extract")
            messages.append({"role": "assistant", "content": response})
        except Exception as e:
            LOGGER.error(f"Extraction LLM call failed: {e}")
            continue

        extracted = response.strip()
        has_no_info_marker = "関連情報なし" in extracted
        is_too_short = len(extracted) < 20

        if has_no_info_marker or is_too_short:
            reason = []
            if has_no_info_marker:
                reason.append("contains '関連情報なし'")
            if is_too_short:
                reason.append(f"too short ({len(extracted)} chars)")
            LOGGER.info(f"[Loop {loop + 1}] Extraction returned no relevant info: {', '.join(reason)}")
            LOGGER.debug(f"[Loop {loop + 1}] LLM extraction response: {extracted}")
        else:
            if ctx.accumulated_info:
                ctx.accumulated_info += "\n\n---\n\n" + extracted
            else:
                ctx.accumulated_info = extracted
            LOGGER.info(f"[Loop {loop + 1}] Accumulated {len(ctx.accumulated_info)} chars of info")

        # === Step 6: Check sufficiency ===
        if ctx.accumulated_info and len(ctx.accumulated_info) >= 100:
            sufficiency_prompt = f"""これまでに収集した情報:
{ctx.accumulated_info}

【質問】
上記の情報で、調査依頼に十分に答えられますか？
- 依頼された具体的な内容（年度別、人物ごとの詳細など）がカバーされていますか？
- まだ探すべき情報がありそうなら「いいえ」と答えてください

「はい」または「いいえ」のみで答えてください。"""

            messages.append({"role": "user", "content": sufficiency_prompt})

            try:
                response = client.generate(messages=messages, tools=[])
                _record_llm_usage(client, persona_id, "memopedia_sufficiency")
                messages.append({"role": "assistant", "content": response})
            except Exception as e:
                LOGGER.error(f"Sufficiency check LLM call failed: {e}")
                continue

            is_sufficient = "はい" in response or "yes" in response.lower()
            LOGGER.info(f"[Loop {loop + 1}] Sufficiency check: {is_sufficient} (response: {response})")

            if is_sufficient:
                LOGGER.info("Information sufficient, stopping search")
                break

    # === Final: Generate page or return diagnostic ===
    if not ctx.accumulated_info:
        LOGGER.warning(f"No information collected for keyword '{keyword}' after {ctx.loop_count} loops")
        LOGGER.warning(f"  Queries tried: {ctx.queries_tried}")
        LOGGER.warning(f"  Processed message IDs: {len(ctx.processed_message_ids)}")
        return {
            "error": "no_info_collected",
            "loops_completed": ctx.loop_count,
            "queries_tried": ctx.queries_tried,
            "messages_processed": len(ctx.processed_message_ids),
        }

    # Generate final page
    category_hint = ""
    if category:
        category_hint = f"カテゴリは「{category}」を使用してください。"
    else:
        category_hint = "適切なカテゴリ（people=人物、terms=用語・概念、plans=計画・予定）を選んでください。"

    page_prompt = f"""収集した情報を元に「{keyword}」についてのMemopediaページを作成してください。

=== 収集した情報 ===
{ctx.accumulated_info}

【指示】
- {category_hint}
- タイトルは簡潔に（キーワードそのまま、または少し補足）
- 要約は1-2文で
- 本文は収集した情報を整理して読みやすく構成
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

    messages.append({"role": "user", "content": page_prompt})

    try:
        response = client.generate(messages=messages, tools=[])
        _record_llm_usage(client, persona_id, "memopedia_compose")
        messages.append({"role": "assistant", "content": response})
    except Exception as e:
        LOGGER.error(f"Page generation LLM call failed: {e}")
        return None

    page_data = _parse_json_response(response)
    if not page_data:
        LOGGER.error("Failed to parse page data from response")
        return None

    if not all(k in page_data for k in ["category", "title", "content"]):
        LOGGER.error("Missing required fields in page data")
        return None

    # Save to Memopedia
    existing = memopedia.find_by_title(page_data["title"], page_data.get("category"))

    if existing:
        memopedia.update_page(
            existing.id,
            content=existing.content + "\n\n" + page_data["content"],
            summary=page_data.get("summary", existing.summary),
        )
        page_data["page_id"] = existing.id
        page_data["action"] = "updated"
        LOGGER.info(f"Updated existing page: {page_data['title']}")
    else:
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


__all__ = ["generate_memopedia_page", "GenerationContext"]
