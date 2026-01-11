#!/usr/bin/env python3
"""
Organize Memopedia pages into trunks using LLM assistance.

This script:
1. Gets all unorganized pages (direct children of root) for a category
2. Gets the specified trunk's title and summary
3. Asks LLM to select which pages should be moved to the trunk
4. Executes the move

Usage:
    python scripts/organize_to_trunk.py --persona air --trunk <trunk_id>
    python scripts/organize_to_trunk.py --persona air --trunk <trunk_id> --category people
    python scripts/organize_to_trunk.py --persona air --trunk <trunk_id> --dry-run
"""

import argparse
import json
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memopedia import Memopedia
from llm_clients import get_llm_client


def get_llm_response(client, prompt: str, response_schema: dict) -> dict:
    """Get structured response from LLM."""
    messages = [{"role": "user", "content": prompt}]

    response = client.chat(
        messages=messages,
        response_schema=response_schema,
    )

    # Parse response
    if hasattr(response, 'content'):
        content = response.content
    elif isinstance(response, dict):
        content = response.get('content', '')
    else:
        content = str(response)

    # Try to extract JSON from response
    try:
        # If content is already a dict, return it
        if isinstance(content, dict):
            return content
        # Otherwise parse as JSON
        return json.loads(content)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        import re
        json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        raise ValueError(f"Could not parse LLM response as JSON: {content}")


def main():
    parser = argparse.ArgumentParser(
        description="Organize Memopedia pages into trunks using LLM assistance"
    )
    parser.add_argument(
        "--persona",
        required=True,
        help="Persona ID (e.g., 'air', 'air_city_a')"
    )
    parser.add_argument(
        "--trunk",
        help="Trunk page ID to organize pages into"
    )
    parser.add_argument(
        "--list-trunks",
        action="store_true",
        help="List all trunks and exit"
    )
    parser.add_argument(
        "--category",
        choices=["people", "terms", "plans"],
        help="Category to organize (auto-detected from trunk if not specified)"
    )
    parser.add_argument(
        "--model",
        default="gemini-2.0-flash",
        help="LLM model to use for selection (default: gemini-2.0-flash)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be moved without actually moving"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output"
    )

    args = parser.parse_args()

    # Initialize memory adapter
    print(f"Loading Memopedia for persona: {args.persona}")
    adapter = SAIMemoryAdapter(args.persona)
    memopedia = Memopedia(adapter.conn)

    # List trunks mode
    if args.list_trunks:
        trunks = memopedia.get_trunks()
        if not trunks:
            print("No trunks found.")
        else:
            # Group by category
            by_category = {"people": [], "terms": [], "plans": []}
            for t in trunks:
                if t.category in by_category:
                    by_category[t.category].append(t)

            category_names = {"people": "人物", "terms": "用語", "plans": "予定"}
            for cat, pages in by_category.items():
                if pages:
                    print(f"\n{category_names[cat]} ({cat}):")
                    for p in pages:
                        summary_preview = f" - {p.summary[:40]}..." if p.summary else ""
                        print(f"  - {p.title}{summary_preview}")
                        print(f"    ID: {p.id}")
        adapter.close()
        sys.exit(0)

    # Check --trunk is provided
    if not args.trunk:
        print("Error: --trunk is required (or use --list-trunks to see available trunks)")
        sys.exit(1)

    # Get trunk info
    trunk = memopedia.get_page(args.trunk)
    if trunk is None:
        print(f"Error: Trunk not found: {args.trunk}")
        sys.exit(1)

    if not trunk.is_trunk:
        print(f"Warning: Page '{trunk.title}' is not marked as a trunk.")
        confirm = input("Continue anyway? [y/N]: ")
        if confirm.lower() != 'y':
            sys.exit(0)

    # Determine category
    category = args.category or trunk.category
    if not category:
        print("Error: Could not determine category. Please specify --category")
        sys.exit(1)

    print(f"Target trunk: {trunk.title} (category: {category})")

    # Get unorganized pages
    unorganized = memopedia.get_unorganized_pages(category)

    if not unorganized:
        print(f"No unorganized pages found in category '{category}'")
        sys.exit(0)

    print(f"Found {len(unorganized)} unorganized pages")

    if args.verbose:
        print("\nUnorganized pages:")
        for p in unorganized:
            print(f"  - {p.title}: {p.summary[:50]}..." if p.summary else f"  - {p.title}")

    # Build prompt for LLM
    pages_list = "\n".join([
        f"- ID: {p.id}\n  Title: {p.title}\n  Summary: {p.summary or '(なし)'}\n  Keywords: {', '.join(p.keywords) if p.keywords else '(なし)'}"
        for p in unorganized
    ])

    prompt = f"""以下は Memopedia の未整理ページのリストです。
この中から、指定された Trunk（カテゴリフォルダ）に移動すべきページを選んでください。

## 移動先 Trunk
- タイトル: {trunk.title}
- 概要: {trunk.summary or '(なし)'}
- 内容: {trunk.content[:500] if trunk.content else '(なし)'}

## 未整理ページ一覧
{pages_list}

## タスク
上記の未整理ページの中から、「{trunk.title}」に関連するページを選んでください。
関連性が高いと思われるページのIDをリストで返してください。

関連性の判断基準:
- タイトルやキーワードが Trunk のテーマに合致している
- 概要の内容が Trunk の範囲に含まれる
- 関連性が明確でない場合は含めない

JSON形式で回答してください。"""

    response_schema = {
        "type": "object",
        "properties": {
            "page_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "移動すべきページIDのリスト"
            },
            "reasoning": {
                "type": "string",
                "description": "選択の理由（簡潔に）"
            }
        },
        "required": ["page_ids"]
    }

    # Get LLM client
    print(f"\nAsking LLM ({args.model}) to select pages...")
    client = get_llm_client(args.model)

    try:
        result = get_llm_response(client, prompt, response_schema)
    except Exception as e:
        print(f"Error getting LLM response: {e}")
        sys.exit(1)

    selected_ids = result.get("page_ids", [])
    reasoning = result.get("reasoning", "")

    if not selected_ids:
        print("LLM selected no pages to move.")
        if reasoning:
            print(f"Reason: {reasoning}")
        sys.exit(0)

    # Validate selected IDs
    valid_ids = set(p.id for p in unorganized)
    selected_ids = [pid for pid in selected_ids if pid in valid_ids]

    if not selected_ids:
        print("No valid page IDs in LLM selection.")
        sys.exit(0)

    # Show selection
    print(f"\nLLM selected {len(selected_ids)} pages to move:")
    for pid in selected_ids:
        page = next((p for p in unorganized if p.id == pid), None)
        if page:
            print(f"  - {page.title}")

    if reasoning:
        print(f"\nReasoning: {reasoning}")

    # Execute move
    if args.dry_run:
        print("\n[DRY RUN] Would move the above pages. No changes made.")
    else:
        confirm = input(f"\nMove these {len(selected_ids)} pages to '{trunk.title}'? [y/N]: ")
        if confirm.lower() != 'y':
            print("Cancelled.")
            sys.exit(0)

        result = memopedia.move_pages_to_trunk(selected_ids, args.trunk)
        print(f"\nMoved {result['moved_count']} pages to '{result['trunk_title']}'")

    adapter.close()
    print("Done.")


if __name__ == "__main__":
    main()
