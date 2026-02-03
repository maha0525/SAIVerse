from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from .models import (
    MemoryRecallRequest,
    MemoryRecallResponse,
    MemoryRecallDebugRequest,
    MemoryRecallDebugResponse,
    MemoryRecallDebugHit,
)
from .utils import get_adapter

router = APIRouter()

@router.post("/{persona_id}/recall", response_model=MemoryRecallResponse)
def memory_recall(
    persona_id: str,
    request: MemoryRecallRequest,
    manager = Depends(get_manager)
):
    """Execute memory recall, similar to the memory_recall tool."""
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    with get_adapter(persona_id, manager) as adapter:
        try:
            result = adapter.recall_snippet(
                None,
                query_text=query,
                max_chars=request.max_chars,
                topk=request.topk,
            )
            return MemoryRecallResponse(
                query=query,
                result=result or "(no relevant memory)",
                topk=request.topk,
                max_chars=request.max_chars,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memory recall failed: {e}")


def _parse_date_range(start_date: str | None, end_date: str | None) -> tuple:
    """Parse date strings to Unix timestamps."""
    from datetime import datetime as dt
    start_ts = None
    end_ts = None

    if start_date:
        try:
            start_ts = int(dt.strptime(start_date, "%Y-%m-%d").timestamp())
        except ValueError:
            pass

    if end_date:
        try:
            # End of day
            end_ts = int(dt.strptime(end_date, "%Y-%m-%d").timestamp()) + 86400 - 1
        except ValueError:
            pass

    return start_ts, end_ts


@router.post("/{persona_id}/recall-debug", response_model=MemoryRecallDebugResponse)
def memory_recall_debug(
    persona_id: str,
    request: MemoryRecallDebugRequest,
    manager = Depends(get_manager)
):
    """Debug-friendly recall: returns raw search results with scores, no context expansion."""
    import logging
    logger = logging.getLogger(__name__)

    query = request.query.strip()
    keywords = [k.strip() for k in request.keywords if k.strip()]
    start_ts, end_ts = _parse_date_range(request.start_date, request.end_date)

    if not query and not keywords:
        raise HTTPException(status_code=400, detail="Query or keywords required")

    try:
        with get_adapter(persona_id, manager) as adapter:
            if request.use_hybrid and keywords:
                # Hybrid mode: keywords + semantic, combine with RRF
                hits = _recall_hybrid(
                    adapter, query, keywords, request.topk, request.rrf_k,
                    start_ts, end_ts
                )
            elif request.use_rrf and query:
                # RRF mode: split query and combine results
                hits = _recall_with_rrf(
                    adapter, query, request.topk, request.rrf_k,
                    start_ts, end_ts
                )
            elif query:
                # Normal mode: single query search
                hits = _recall_single_query(adapter, query, request.topk, start_ts, end_ts)
            else:
                # Keywords only mode
                hits = _recall_keywords_only(adapter, keywords, request.topk, start_ts, end_ts)

            print(f"[RECALL DEBUG] Building response with {len(hits)} hits", flush=True)
            response = MemoryRecallDebugResponse(
                query=query or ", ".join(keywords),
                topk=request.topk,
                total_hits=len(hits),
                hits=hits,
            )
            print("[RECALL DEBUG] Response built successfully", flush=True)
            return response
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Memory recall debug failed")
        raise HTTPException(status_code=500, detail=f"Memory recall debug failed: {e}")


def _recall_single_query(adapter, query: str, topk: int, start_ts=None, end_ts=None) -> list:
    """Single query search (original behavior)."""
    from sai_memory.memory.recall import semantic_recall_groups

    # Get more results if filtering by date
    search_topk = topk * 3 if (start_ts or end_ts) else topk

    with adapter._db_lock:
        groups_raw = semantic_recall_groups(
            adapter.conn,
            adapter.embedder,
            query,
            thread_id=None,
            resource_id=None,
            topk=search_topk,
            range_before=0,
            range_after=0,
            scope=adapter.settings.scope,
            exclude_message_ids=set(),
            required_tags=["conversation"],
        )

    hits = []
    for seed, bundle, score in groups_raw:
        # Date range filter
        if start_ts and seed.created_at < start_ts:
            continue
        if end_ts and seed.created_at > end_ts:
            continue

        dt = datetime.fromtimestamp(seed.created_at)
        hits.append(MemoryRecallDebugHit(
            rank=0,  # Will be set later
            score=round(score, 4),
            message_id=seed.id,
            thread_id=seed.thread_id,
            role=seed.role,
            content=seed.content,
            created_at=seed.created_at,
            created_at_str=dt.strftime("%Y-%m-%d %H:%M:%S"),
        ))
        if len(hits) >= topk:
            break

    # Set ranks
    for i, hit in enumerate(hits, start=1):
        hit.rank = i

    return hits


def _recall_with_rrf(adapter, query: str, topk: int, rrf_k: int, start_ts=None, end_ts=None) -> list:
    """Reciprocal Rank Fusion: split query by spaces, search each, combine with RRF."""
    from collections import defaultdict
    from sai_memory.memory.recall import semantic_recall_groups

    print(f"[RRF DEBUG] _recall_with_rrf called with query='{query}', topk={topk}, rrf_k={rrf_k}", flush=True)

    # Split query into sub-queries (by whitespace)
    sub_queries = [q.strip() for q in query.split() if q.strip()]
    print(f"[RRF DEBUG] Split into {len(sub_queries)} sub-queries: {sub_queries}", flush=True)

    if not sub_queries:
        return []

    # If only one term, fall back to single query
    if len(sub_queries) == 1:
        return _recall_single_query(adapter, query, topk, start_ts, end_ts)

    # Collect ranked lists for each sub-query
    # message_id -> {message data, rrf_score, sub_query_ranks}
    message_data: dict = {}
    rrf_scores: dict = defaultdict(float)

    # Search more results per sub-query to ensure good coverage
    per_query_topk = min(topk * 2, 100)

    # Run each sub-query separately (don't hold lock across all queries)
    for i, sq in enumerate(sub_queries):
        print(f"[RRF DEBUG] Starting sub-query {i+1}/{len(sub_queries)}: '{sq}'", flush=True)
        try:
            with adapter._db_lock:
                print(f"[RRF DEBUG] Lock acquired for '{sq}'", flush=True)
                groups_raw = semantic_recall_groups(
                    adapter.conn,
                    adapter.embedder,
                    sq,
                    thread_id=None,
                    resource_id=None,
                    topk=per_query_topk,
                    range_before=0,
                    range_after=0,
                    scope=adapter.settings.scope,
                    exclude_message_ids=set(),
                    required_tags=["conversation"],
                )
            print(f"[RRF DEBUG] Got {len(groups_raw)} results for '{sq}'", flush=True)
        except Exception as e:
            print(f"[RRF DEBUG] Exception for '{sq}': {e}", flush=True)
            raise

        rank_counter = 0
        for seed, bundle, score in groups_raw:
            # Date range filter
            if start_ts and seed.created_at < start_ts:
                continue
            if end_ts and seed.created_at > end_ts:
                continue

            rank_counter += 1
            msg_id = seed.id
            # Store message data if not seen
            if msg_id not in message_data:
                message_data[msg_id] = {
                    "seed": seed,
                    "original_scores": {},
                }
            # Record this sub-query's rank and score
            message_data[msg_id]["original_scores"][sq] = score
            # RRF contribution: 1 / (k + rank)
            rrf_scores[msg_id] += 1.0 / (rrf_k + rank_counter)

    print(f"[RRF DEBUG] Total unique messages: {len(message_data)}", flush=True)

    # Sort by RRF score
    sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)

    # Build result list
    hits = []
    for rank, msg_id in enumerate(sorted_ids[:topk], start=1):
        data = message_data[msg_id]
        seed = data["seed"]

        # Safely get created_at
        created_at_val = float(seed.created_at) if seed.created_at else 0.0
        dt = datetime.fromtimestamp(created_at_val)

        # Show RRF score (normalized to 0-1 range for display)
        max_possible = len(sub_queries) / (rrf_k + 1)
        normalized_score = rrf_scores[msg_id] / max_possible if max_possible > 0 else 0

        # Ensure content is a string
        content = str(seed.content) if seed.content else ""

        hits.append(MemoryRecallDebugHit(
            rank=rank,
            score=round(normalized_score, 4),
            message_id=str(seed.id),
            thread_id=str(seed.thread_id) if seed.thread_id else "",
            role=str(seed.role) if seed.role else "unknown",
            content=content,
            created_at=created_at_val,
            created_at_str=dt.strftime("%Y-%m-%d %H:%M:%S"),
        ))

    print(f"[RRF DEBUG] Returning {len(hits)} hits", flush=True)
    return hits


def _recall_keywords_only(adapter, keywords: list, topk: int, start_ts=None, end_ts=None) -> list:
    """Keyword-based search using substring matching."""
    from sai_memory.memory.storage import get_all_messages_for_search

    print(f"[KEYWORD DEBUG] Searching for keywords: {keywords}", flush=True)

    with adapter._db_lock:
        # Get all messages with conversation tag
        messages = get_all_messages_for_search(
            adapter.conn,
            required_tags=["conversation"],
        )

    print(f"[KEYWORD DEBUG] Got {len(messages)} messages to search", flush=True)

    # Score each message by keyword matches
    scored = []
    for msg in messages:
        # Date range filter
        if start_ts and msg.created_at < start_ts:
            continue
        if end_ts and msg.created_at > end_ts:
            continue

        content_lower = msg.content.lower() if msg.content else ""
        match_count = sum(1 for kw in keywords if kw.lower() in content_lower)
        if match_count > 0:
            # Score = match_count / total_keywords (0 to 1)
            score = match_count / len(keywords)
            scored.append((msg, score, match_count))

    # Sort by score (descending), then by match_count
    scored.sort(key=lambda x: (x[1], x[2]), reverse=True)

    print(f"[KEYWORD DEBUG] Found {len(scored)} messages with keyword matches", flush=True)

    # Build result
    hits = []
    for rank, (msg, score, match_count) in enumerate(scored[:topk], start=1):
        created_at_val = float(msg.created_at) if msg.created_at else 0.0
        dt = datetime.fromtimestamp(created_at_val)
        hits.append(MemoryRecallDebugHit(
            rank=rank,
            score=round(score, 4),
            message_id=str(msg.id),
            thread_id=str(msg.thread_id) if msg.thread_id else "",
            role=str(msg.role) if msg.role else "unknown",
            content=str(msg.content) if msg.content else "",
            created_at=created_at_val,
            created_at_str=dt.strftime("%Y-%m-%d %H:%M:%S"),
        ))

    return hits


def _recall_hybrid(adapter, query: str, keywords: list, topk: int, rrf_k: int, start_ts=None, end_ts=None) -> list:
    """Hybrid search: combine keyword matching and semantic search with RRF."""
    from collections import defaultdict
    from sai_memory.memory.recall import semantic_recall_groups
    from sai_memory.memory.storage import get_all_messages_for_search

    print(f"[HYBRID DEBUG] query='{query}', keywords={keywords}", flush=True)
    if start_ts or end_ts:
        print(f"[HYBRID DEBUG] date range: {start_ts} - {end_ts}", flush=True)

    message_data: dict = {}
    rrf_scores: dict = defaultdict(float)

    # 1. Keyword search
    if keywords:
        print("[HYBRID DEBUG] Running keyword search...", flush=True)
        with adapter._db_lock:
            messages = get_all_messages_for_search(
                adapter.conn,
                required_tags=["conversation"],
            )

        # Score and rank by keyword matches
        keyword_scored = []
        for msg in messages:
            # Date range filter
            if start_ts and msg.created_at < start_ts:
                continue
            if end_ts and msg.created_at > end_ts:
                continue

            content_lower = msg.content.lower() if msg.content else ""
            match_count = sum(1 for kw in keywords if kw.lower() in content_lower)
            if match_count > 0:
                keyword_scored.append((msg, match_count))

        # Sort by match count
        keyword_scored.sort(key=lambda x: x[1], reverse=True)
        print(f"[HYBRID DEBUG] Keyword search found {len(keyword_scored)} matches", flush=True)

        # Add to RRF scores (use match count as rank basis - more matches = lower rank number)
        for rank, (msg, match_count) in enumerate(keyword_scored[:topk * 2], start=1):
            msg_id = msg.id
            if msg_id not in message_data:
                message_data[msg_id] = {"seed": msg}
            rrf_scores[msg_id] += 1.0 / (rrf_k + rank)

    # 2. Semantic search
    if query:
        print("[HYBRID DEBUG] Running semantic search...", flush=True)
        # Get more results if filtering by date
        search_topk = topk * 4 if (start_ts or end_ts) else topk * 2
        with adapter._db_lock:
            groups_raw = semantic_recall_groups(
                adapter.conn,
                adapter.embedder,
                query,
                thread_id=None,
                resource_id=None,
                topk=search_topk,
                range_before=0,
                range_after=0,
                scope=adapter.settings.scope,
                exclude_message_ids=set(),
                required_tags=["conversation"],
            )
        print(f"[HYBRID DEBUG] Semantic search found {len(groups_raw)} results", flush=True)

        rank_counter = 0
        for seed, bundle, score in groups_raw:
            # Date range filter
            if start_ts and seed.created_at < start_ts:
                continue
            if end_ts and seed.created_at > end_ts:
                continue

            rank_counter += 1
            msg_id = seed.id
            if msg_id not in message_data:
                message_data[msg_id] = {"seed": seed}
            rrf_scores[msg_id] += 1.0 / (rrf_k + rank_counter)

    print(f"[HYBRID DEBUG] Total unique messages: {len(message_data)}", flush=True)

    # Sort by RRF score
    sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)

    # Determine number of sources for normalization
    num_sources = (1 if keywords else 0) + (1 if query else 0)

    # Build result
    hits = []
    for rank, msg_id in enumerate(sorted_ids[:topk], start=1):
        seed = message_data[msg_id]["seed"]
        created_at_val = float(seed.created_at) if seed.created_at else 0.0
        dt = datetime.fromtimestamp(created_at_val)

        # Normalize score
        max_possible = num_sources / (rrf_k + 1)
        normalized_score = rrf_scores[msg_id] / max_possible if max_possible > 0 else 0

        hits.append(MemoryRecallDebugHit(
            rank=rank,
            score=round(normalized_score, 4),
            message_id=str(seed.id),
            thread_id=str(seed.thread_id) if seed.thread_id else "",
            role=str(seed.role) if seed.role else "unknown",
            content=str(seed.content) if seed.content else "",
            created_at=created_at_val,
            created_at_str=dt.strftime("%Y-%m-%d %H:%M:%S"),
        ))

    print(f"[HYBRID DEBUG] Returning {len(hits)} hits", flush=True)
    return hits
