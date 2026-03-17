#!/usr/bin/env python3
"""
Build Memopedia from existing SAIMemory conversation logs.

This script reads conversation messages from a persona's memory.db and uses an LLM
to extract knowledge into Memopedia pages.

Usage:
    python scripts/build_memopedia.py <persona_id> [--limit N] [--model MODEL] [--dry-run]

Examples:
    # Build from first 100 messages
    python scripts/build_memopedia.py air_city_a --limit 100

    # Preview what would be extracted without writing
    python scripts/build_memopedia.py air_city_a --limit 50 --dry-run

    # Use a specific model
    python scripts/build_memopedia.py air_city_a --limit 100 --model gemini-3.1-flash-lite-preview
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sai_memory.memory.storage import init_db, get_messages_paginated, Message
from sai_memory.memopedia import Memopedia, init_memopedia_tables, CATEGORY_PEOPLE, CATEGORY_TERMS, CATEGORY_PLANS
from saiverse.model_configs import get_model_config, find_model_config
from scripts._shared.config import load_prompt, load_runtime_config

# Import llm_clients lazily to avoid circular import
# (llm_clients imports tools which imports persona which imports llm_clients)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

# Prompt file paths
def _get_prompts_dir() -> Path:
    """Get prompts directory using data_paths or fallback to legacy."""
    try:
        from saiverse.data_paths import find_file, PROMPTS_DIR as DATA_PROMPTS_DIR, BUILTIN_DATA_DIR
        return BUILTIN_DATA_DIR / DATA_PROMPTS_DIR
    except ImportError:
        return Path(__file__).resolve().parents[1] / "system_prompts"

PROMPTS_DIR = _get_prompts_dir()




def category_to_root_id(category: str) -> str | None:
    return {"people": "root_people", "terms": "root_terms", "plans": "root_plans"}.get(category)


def filter_valid_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [p for p in pages if all(k in p for k in ["category", "title", "summary", "content"])]


def build_memopedia_actions(pages: List[Dict[str, Any]], existing_map: Dict[str, str]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for page_data in pages:
        root_id = category_to_root_id(page_data["category"])
        if not root_id:
            continue
        key = f"{page_data['category']}:{page_data['title']}"
        if key in existing_map:
            actions.append({"type": "append", "page_id": existing_map[key], "title": page_data["title"], "content": page_data["content"]})
            continue
        actions.append({"type": "create", "parent_id": root_id, "title": page_data["title"], "summary": page_data["summary"], "content": page_data["content"], "keywords": page_data.get("keywords", [])})
    return actions


def _execute_memopedia_actions(memopedia: Memopedia, actions: List[Dict[str, Any]], dry_run: bool) -> None:
    for action in actions:
        if dry_run:
            LOGGER.info(f"[DRY RUN] Would {action['type']}: {action.get('title', '')}")
            continue
        if action["type"] == "append":
            memopedia.append_to_content(action["page_id"], action["content"])
        elif action["type"] == "create":
            memopedia.create_page(parent_id=action["parent_id"], title=action["title"], summary=action["summary"], content=action["content"], keywords=action["keywords"])

def fetch_messages(db_path: Path, limit: int = 100, offset: int = 0, thread_id: str | None = None) -> List[Message]:
    """Fetch messages from the database.
    
    Args:
        db_path: Path to the memory.db file
        limit: Maximum number of messages to return
        offset: Number of messages to skip from the beginning
        thread_id: If specified, only fetch from this thread. Otherwise fetch from all threads.
    """
    conn = init_db(str(db_path), check_same_thread=False)

    # Get threads to process
    if thread_id:
        threads = [thread_id]
    else:
        # Order threads by their earliest message timestamp
        cur = conn.execute("""
            SELECT t.id, MIN(m.created_at) as first_msg_ts
            FROM threads t
            LEFT JOIN messages m ON t.id = m.thread_id
            GROUP BY t.id
            ORDER BY first_msg_ts ASC NULLS LAST
        """)
        threads = [row[0] for row in cur.fetchall()]

    all_messages: List[Message] = []
    total_to_fetch = offset + limit  # Need to fetch offset+limit then slice
    
    for tid in threads:
        page = 0
        while len(all_messages) < total_to_fetch:
            batch = get_messages_paginated(conn, tid, page=page, page_size=100)
            if not batch:
                break
            all_messages.extend(batch)
            page += 1
            if len(all_messages) >= total_to_fetch:
                break
        if len(all_messages) >= total_to_fetch:
            break

    conn.close()
    # Apply offset and limit
    return all_messages[offset:offset + limit]


def format_messages_for_prompt(messages: List[Message]) -> str:
    """Format messages for the extraction prompt."""
    lines: List[str] = []
    for msg in messages:
        role = msg.role
        if role == "model":
            role = "assistant"
        content = (msg.content or "").strip()
        if not content:
            continue
        lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


# NOTE: format_existing_pages() has been removed and consolidated into
# Memopedia.get_tree_markdown() in sai_memory/memopedia/core.py
# Use: memopedia.get_tree_markdown(include_keywords=True, show_markers=False)

def extract_knowledge_from_text(
    client,
    text: str,
    memopedia: Memopedia,
    source_type: str = "text",
) -> List[Dict[str, Any]]:
    """Extract knowledge from arbitrary text (e.g., system prompt)."""
    LOGGER.info(f"Extracting knowledge from {source_type}...")

    existing_pages = memopedia.get_tree_markdown(include_keywords=True, show_markers=False)

    prompt_template = load_prompt("memopedia_system_prompt_extraction")
    prompt = prompt_template.format(
        existing_pages=existing_pages,
        text=text,
    )

    response_schema = {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["people", "events", "plans"],
                        },
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["category", "title", "summary", "content"],
                },
            },
        },
        "required": ["pages"],
    }

    try:
        response_text = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            response_schema=response_schema,
        )

        if not response_text:
            LOGGER.warning("Empty response from LLM")
            return []

        # Parse JSON
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]

        data = json.loads(response_text.strip())
        pages = data.get("pages", [])

        result = []
        for page in pages:
            if all(key in page for key in ["category", "title", "summary", "content"]):
                result.append(page)
                LOGGER.info(f"  Extracted from {source_type}: [{page['category']}] {page['title']}")

        return result

    except Exception as e:
        LOGGER.error(f"Error extracting from {source_type}: {e}")
        return []


def apply_edits(content: str, edits: List[Dict[str, str]]) -> str:
    """Apply edit operations to content.

    All operations use unified fields:
    - target: The string to search for (or null for append_end)
    - content: The content to add or replace with

    Supported operations:
    - append_after: Insert content after target string
    - replace: Replace target with content
    - append_end: Append content at the end (target is ignored)
    """
    result = content

    for edit in edits:
        operation = edit.get("operation")
        target = edit.get("target", "")
        insert_content = edit.get("content", "")

        if operation == "append_after":
            if target and target in result:
                result = result.replace(target, target + insert_content, 1)
            else:
                LOGGER.warning(f"Target not found for append_after: {target if target else '(empty)'}")
                # Fallback: append at end
                result = result + "\n" + insert_content

        elif operation == "replace":
            if target and target in result:
                result = result.replace(target, insert_content, 1)
            else:
                LOGGER.warning(f"Target not found for replace: {target if target else '(empty)'}")

        elif operation == "append_end":
            if insert_content:
                result = result + "\n\n" + insert_content

        else:
            LOGGER.warning(f"Unknown edit operation: {operation}")

    return result


def refine_page_content(
    client,
    title: str,
    summary: str,
    keywords: List[str],
    existing_content: str,
    new_info: str,
) -> tuple[str, str, List[str]]:
    """Use LLM to generate edit operations and apply them to existing content.

    Returns:
        Tuple of (new_content, new_summary, new_keywords)
    """
    # If no existing content, just return new info with original summary/keywords
    if not existing_content:
        return new_info, summary, keywords

    prompt_template = load_prompt("memopedia_refine_content")
    prompt = prompt_template.format(
        title=title,
        summary=summary,
        keywords=", ".join(keywords) if keywords else "(なし)",
        existing_content=existing_content,
        new_info=new_info,
    )

    response_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
            },
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["append_after", "replace", "append_end"],
                        },
                        "target": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["operation", "content"],
                },
            },
        },
        "required": ["summary", "keywords", "edits"],
    }

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            response_schema=response_schema,
        )

        if not response:
            LOGGER.warning("Empty response from LLM for refine")
            return existing_content + "\n\n" + new_info, summary, keywords

        # Parse JSON response
        response_text = response.strip()
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]

        data = json.loads(response_text.strip())
        edits = data.get("edits", [])
        new_summary = data.get("summary", summary)
        new_keywords = data.get("keywords", keywords)

        if not edits:
            LOGGER.info(f"No edits needed for {title}")
            if new_summary != summary:
                LOGGER.info(f"  Summary updated: {new_summary}")
            if new_keywords != keywords:
                LOGGER.info(f"  Keywords updated: {new_keywords}")
            return existing_content, new_summary, new_keywords

        LOGGER.info(f"Applying {len(edits)} edit(s) to {title}")
        for edit in edits:
            LOGGER.debug(f"  Edit: {edit.get('operation')} - {str(edit)}")

        if new_summary != summary:
            LOGGER.info(f"  Summary updated: {new_summary}")
        if new_keywords != keywords:
            LOGGER.info(f"  Keywords updated: {new_keywords}")

        return apply_edits(existing_content, edits), new_summary, new_keywords

    except json.JSONDecodeError as e:
        LOGGER.warning(f"Failed to parse edit JSON for {title}: {e}")
        return existing_content + "\n\n" + new_info, summary, keywords
    except Exception as e:
        LOGGER.warning(f"Failed to refine content for {title}: {e}")
        return existing_content + "\n\n" + new_info, summary, keywords


def extract_knowledge(
    client,
    messages: List[Message],
    memopedia: Memopedia,
    batch_size: int = 20,
    max_retries: int = 2,
    dry_run: bool = False,
    refine_writes: bool = False,
    episode_context_conn=None,
    debug_log_path=None,
) -> List[Dict[str, Any]]:
    """Extract knowledge from messages using the LLM.

    Args:
        client: LLM client
        messages: Messages to process
        memopedia: Memopedia instance
        batch_size: Number of messages per LLM call
        max_retries: Max retries when LLM returns empty pages
        dry_run: If True, don't write to DB
        refine_writes: If True, use LLM to refine content when appending
        episode_context_conn: Database connection for fetching episode context (arasuji).
            If provided, episode context will be included in the prompt.

    Note:
        Pages are applied to Memopedia immediately after each batch extraction.
        This ensures that subsequent batches see the updated page list.
    """
    all_pages: List[Dict[str, Any]] = []

    # Response schema for structured output
    response_schema = {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["people", "terms", "plans"],
                        },
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "content": {"type": "string"},
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["category", "title", "summary", "content", "keywords"],
                },
            },
        },
        "required": ["pages"],
    }

    # Process in batches
    for i in range(0, len(messages), batch_size):
        batch = messages[i:i + batch_size]
        LOGGER.info(f"Processing messages {i+1}-{i+len(batch)} of {len(messages)}")

        conversation = format_messages_for_prompt(batch)
        if not conversation.strip():
            continue

        existing_pages = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)

        prompt_template = load_prompt("memopedia_extraction")
        prompt = prompt_template.format(
            existing_pages=existing_pages,
            conversation=conversation,
        )

        # Add episode context if available
        if episode_context_conn is not None:
            try:
                from sai_memory.arasuji.context import get_episode_context_for_timerange
                # Get time range from batch
                batch_start = min(m.created_at for m in batch) if batch else 0
                batch_end = max(m.created_at for m in batch) if batch else 0
                episode_ctx = get_episode_context_for_timerange(
                    episode_context_conn, batch_start, batch_end
                )
                if episode_ctx:
                    prompt = f"""## これまでの出来事の流れ（参考）

以下は、この会話が行われるより前の出来事のあらすじです。
過去の経緯や文脈を理解するために参照してください。

{episode_ctx}

---

{prompt}"""
                    LOGGER.info(f"  Added episode context ({len(episode_ctx)} chars)")
            except Exception as e:
                LOGGER.warning(f"Failed to add episode context: {e}")

        # Retry loop for empty responses
        batch_pages: List[Dict[str, Any]] = []
        for attempt in range(max_retries + 1):
            # Debug log: write prompt
            if debug_log_path and attempt == 0:
                from datetime import datetime
                with open(debug_log_path, "a", encoding="utf-8") as f:
                    f.write("\n" + "=" * 80 + "\n")
                    f.write(f"[MEMOPEDIA] {datetime.now().isoformat()}\n")
                    f.write("=" * 80 + "\n")
                    f.write("--- PROMPT ---\n")
                    f.write(prompt)
                    f.write("\n")

            try:
                text = client.generate(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    response_schema=response_schema,
                )

                # Debug log: write response
                if debug_log_path and attempt == 0:
                    with open(debug_log_path, "a", encoding="utf-8") as f:
                        f.write("--- RESPONSE ---\n")
                        f.write(text or "(empty)")
                        f.write("\n")

                if not text:
                    LOGGER.warning("Empty response from LLM")
                    if attempt < max_retries:
                        LOGGER.info(f"Retrying ({attempt + 1}/{max_retries})...")
                        continue
                    break

                # Parse JSON from response
                try:
                    if "```json" in text:
                        text = text.split("```json")[1].split("```")[0]
                    elif "```" in text:
                        text = text.split("```")[1].split("```")[0]

                    data = json.loads(text.strip())
                    pages = data.get("pages", [])

                    # Check for empty pages array - treat as retriable error
                    if not pages:
                        LOGGER.warning("LLM returned empty pages array")
                        if attempt < max_retries:
                            LOGGER.info(f"Retrying ({attempt + 1}/{max_retries})...")
                            continue
                        LOGGER.warning("Max retries reached, skipping this batch")
                        break

                    # Success - extract valid pages
                    for page in pages:
                        if all(key in page for key in ["category", "title", "summary", "content"]):
                            batch_pages.append(page)
                            LOGGER.info(f"  Extracted: [{page['category']}] {page['title']}")

                    # Got results, exit retry loop
                    break

                except json.JSONDecodeError as e:
                    LOGGER.warning(f"Failed to parse JSON response: {e}")
                    LOGGER.debug(f"Response text: {text}")
                    if attempt < max_retries:
                        LOGGER.info(f"Retrying ({attempt + 1}/{max_retries})...")
                        continue
                    break

            except Exception as e:
                LOGGER.error(f"Error during extraction: {e}")
                if attempt < max_retries:
                    LOGGER.info(f"Retrying ({attempt + 1}/{max_retries})...")
                    continue
                break

        # Apply batch pages to Memopedia immediately
        # This ensures subsequent batches see the updated page list
        if batch_pages:
            apply_pages_to_memopedia(
                memopedia,
                batch_pages,
                dry_run=dry_run,
                client=client if refine_writes else None,
            )
            all_pages.extend(batch_pages)

    return all_pages


def apply_pages_to_memopedia(
    memopedia: Memopedia,
    pages: List[Dict[str, Any]],
    dry_run: bool = False,
    client=None,  # If provided, use refine mode for existing pages
) -> None:
    existing_map = {}
    for page_data in pages:
        existing = memopedia.find_by_title(page_data["title"], page_data["category"])
        if existing:
            existing_map[f"{page_data['category']}:{page_data['title']}"] = existing.id
            if client and not dry_run:
                refined_content, new_summary, new_keywords = refine_page_content(
                    client,
                    title=existing.title,
                    summary=existing.summary,
                    keywords=existing.keywords,
                    existing_content=existing.content,
                    new_info=page_data["content"],
                )
                page_data["content"] = refined_content
                page_data["summary"] = new_summary
                page_data["keywords"] = new_keywords
    actions = build_memopedia_actions(pages, existing_map)
    _execute_memopedia_actions(memopedia, actions, dry_run=dry_run)


def list_available_models() -> None:
    """Print available models and exit."""
    from saiverse.model_configs import MODEL_CONFIGS, get_model_display_name

    print("\n利用可能なモデル一覧:")
    print("-" * 60)
    for model_id, config in sorted(MODEL_CONFIGS.items()):
        provider = config.get("provider", "unknown")
        display_name = get_model_display_name(model_id)
        if display_name != model_id:
            print(f"  {model_id}")
            print(f"    表示名: {display_name}")
            print(f"    Provider: {provider}")
        else:
            print(f"  {model_id} (provider: {provider})")
    print("-" * 60)
    print(f"合計: {len(MODEL_CONFIGS)} モデル\n")




class RetriableExtractionError(Exception):
    pass


class FatalExtractionError(Exception):
    pass


def run(args):

    # Handle --list-models
    if args.list_models:
        list_available_models()
        return 0

    # Require persona_id for most operations
    if not args.persona_id:
        LOGGER.error("persona_id is required (unless using --list-models)")
        return 1

    # Check if persona exists
    runtime = load_runtime_config(args.persona_id)
    db_path = runtime.db_path
    if not db_path.exists():
        LOGGER.error(f"Persona database not found: {db_path}")
        return 1

    # Initialize Memopedia
    conn = init_db(str(db_path), check_same_thread=False)
    memopedia = Memopedia(conn)

    # Handle --export
    if args.export:
        LOGGER.info(f"Exporting Memopedia to: {args.export}")
        export_data = memopedia.export_json()
        with open(args.export, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        LOGGER.info(f"Exported {len(export_data['pages'])} pages")
        conn.close()
        return 0

    # Handle --clear (standalone or with --import)
    if args.clear and not args.import_file:
        LOGGER.info("Clearing all Memopedia pages...")
        deleted = memopedia.clear_all_pages()
        LOGGER.info(f"Deleted {deleted} pages")
        conn.close()
        return 0

    # Handle --import
    if args.import_file:
        LOGGER.info(f"Importing Memopedia from: {args.import_file}")
        with open(args.import_file, "r", encoding="utf-8") as f:
            import_data = json.load(f)
        imported = memopedia.import_json(import_data, clear_existing=args.clear)
        LOGGER.info(f"Imported {imported} pages")
        conn.close()
        return 0

    LOGGER.info(f"Building Memopedia for persona: {args.persona_id}")
    LOGGER.info(f"Database: {db_path}")
    LOGGER.info(f"Message limit: {args.limit}")
    LOGGER.info(f"Dry run: {args.dry_run}")
    LOGGER.info(f"Refine writes: {args.refine_writes}")

    # Initialize LLM client - search by model ID, filename, or partial match
    resolved_model_id, model_config = find_model_config(args.model)

    if resolved_model_id:
        # Found a matching config
        if resolved_model_id != args.model:
            LOGGER.info(f"Resolved model '{args.model}' -> '{resolved_model_id}'")
        # Use the "model" field from config for actual API calls (may differ from filename)
        actual_model_id = model_config.get("model", resolved_model_id)
        context_length = model_config.get("context_length", 128000)
        auto_provider = model_config.get("provider", "gemini")
    else:
        # No config found - error out instead of falling back
        LOGGER.error(f"Model '{args.model}' not found in config.")
        LOGGER.error("Use --list-models to see available options.")
        conn.close()
        return 1

    # Use explicit provider if specified, otherwise use auto-detected
    provider = args.provider if args.provider else auto_provider

    LOGGER.info(f"Using model: {actual_model_id}")
    LOGGER.info(f"Using provider: {provider}")

    # Import factory directly to avoid circular import
    # (llm_clients/__init__.py imports tools which imports persona which imports llm_clients)
    from llm_clients.factory import get_llm_client
    client = get_llm_client(actual_model_id, provider, context_length, config=model_config)

    # Process system prompt first if provided
    if args.system_prompt:
        system_prompt_path = Path(args.system_prompt)
        if not system_prompt_path.exists():
            LOGGER.error(f"System prompt file not found: {args.system_prompt}")
            conn.close()
            return 1

        LOGGER.info(f"Processing system prompt: {args.system_prompt}")
        system_prompt_text = system_prompt_path.read_text(encoding="utf-8")

        # Extract knowledge from system prompt as a single "message"
        system_prompt_pages = extract_knowledge_from_text(
            client, system_prompt_text, memopedia, source_type="system_prompt"
        )
        LOGGER.info(f"Extracted {len(system_prompt_pages)} pages from system prompt")

        if system_prompt_pages and not args.dry_run:
            apply_pages_to_memopedia(
                memopedia, system_prompt_pages, dry_run=False,
                client=client if args.refine_writes else None
            )

    # Fetch messages
    LOGGER.info(f"Fetching messages (offset={args.offset}, limit={args.limit}, thread={args.thread or 'all'})...")
    messages = fetch_messages(db_path, limit=args.limit, offset=args.offset, thread_id=args.thread)
    LOGGER.info(f"Fetched {len(messages)} messages")

    if not messages and not args.system_prompt:
        LOGGER.warning("No messages found")
        conn.close()
        return 0

    if messages:
        # Initialize arasuji tables if using episode context
        episode_context_conn = None
        if args.with_episode_context:
            from sai_memory.arasuji import init_arasuji_tables
            init_arasuji_tables(conn)
            episode_context_conn = conn
            LOGGER.info("Episode context enabled (arasuji)")

        # Extract knowledge (pages are applied immediately after each batch)
        LOGGER.info("Extracting knowledge from messages...")
        pages = extract_knowledge(
            client,
            messages,
            memopedia,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            refine_writes=args.refine_writes,
            episode_context_conn=episode_context_conn,
        )
        LOGGER.info(f"Extracted and applied {len(pages)} pages total")

    # Show final state
    if not args.dry_run:
        LOGGER.info("\n" + "=" * 60)
        LOGGER.info("Final Memopedia state:")
        LOGGER.info("=" * 60)
        print(memopedia.export_all_markdown())

    conn.close()
    LOGGER.info("Done!")