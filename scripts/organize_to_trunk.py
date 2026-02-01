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
    python scripts/organize_to_trunk.py --persona air --list-trunks
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

# Skip tool imports to avoid circular import issue
os.environ["SAIVERSE_SKIP_TOOL_IMPORTS"] = "1"

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memopedia import Memopedia
from model_configs import find_model_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

# Environment variable for default model
ENV_MODEL = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.0-flash")


def get_llm_response(client, prompt: str, response_schema: dict) -> dict:
    """Get structured response from LLM."""
    messages = [{"role": "user", "content": prompt}]

    # Use generate() method - the correct LLM client API
    # response_schema enables structured output (JSON mode)
    response = client.generate(
        messages=messages,
        response_schema=response_schema,
    )

    # generate() returns a string directly
    content = response

    # Try to extract JSON from response
    try:
        # If content is already a dict, return it
        if isinstance(content, dict):
            return content
        # Otherwise parse as JSON
        return json.loads(content)
    except json.JSONDecodeError:
        # Try to find JSON in the response (may have extra text around it)
        import re
        # Search for nested JSON objects too
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
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
        "--all-categories",
        action="store_true",
        help="Search for pages across all categories (not just the trunk's category)"
    )
    parser.add_argument(
        "--auto-subcategories",
        action="store_true",
        help="Automatically create intermediate trunk pages when too many pages are selected"
    )
    parser.add_argument(
        "--subcategory-threshold",
        type=int,
        default=20,
        help="Minimum number of pages to trigger auto-subcategory creation (default: 20)"
    )
    parser.add_argument(
        "--model",
        default=ENV_MODEL,
        help=f"LLM model to use for selection (default: {ENV_MODEL}, env: MEMORY_WEAVE_MODEL)"
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
    if args.all_categories:
        # Get pages from all categories
        all_categories = ["people", "terms", "plans"]
        unorganized = []
        for cat in all_categories:
            unorganized.extend(memopedia.get_unorganized_pages(cat))
        print("Searching across all categories...")
    else:
        unorganized = memopedia.get_unorganized_pages(category)

    if not unorganized:
        if args.all_categories:
            print("No unorganized pages found in any category")
        else:
            print(f"No unorganized pages found in category '{category}'")
        sys.exit(0)

    print(f"Found {len(unorganized)} unorganized pages")

    if args.verbose:
        print("\nUnorganized pages:")
        for p in unorganized:
            cat_label = f" [{p.category}]" if args.all_categories else ""
            print(f"  - {p.title}{cat_label}: {p.summary[:50]}..." if p.summary else f"  - {p.title}{cat_label}")

    # Build prompt for LLM
    pages_list = "\n".join([
        f"- ID: {p.id}\n  Title: {p.title}\n  Category: {p.category}\n  Summary: {p.summary or '(なし)'}\n  Keywords: {', '.join(p.keywords) if p.keywords else '(なし)'}"
        for p in unorganized
    ])

    cross_category_note = ""
    if args.all_categories:
        cross_category_note = """
注意: 候補ページは複数のカテゴリ（people/terms/plans）から選ばれています。
カテゴリが異なっても、内容的に関連性があればトランクに移動する候補として選んでください。
例: 人物に関連する用語ページを人物トランクの下に移動することは有効です。
"""

    prompt = f"""以下は Memopedia の未整理ページのリストです。
この中から、指定された Trunk（カテゴリフォルダ）に移動すべきページを選んでください。

## 移動先 Trunk
- タイトル: {trunk.title}
- カテゴリ: {trunk.category}
- 概要: {trunk.summary or '(なし)'}
- 内容: {trunk.content[:500] if trunk.content else '(なし)'}
{cross_category_note}
## 未整理ページ一覧
{pages_list}

## タスク
上記の未整理ページの中から、「{trunk.title}」に関連する可能性があるページを選んでください。
この段階ではあくまで候補なので、少しでも関連しそうなものは含めてください。
後で詳細を確認して最終判断します。

関連性の判断基準:
- タイトルやキーワードが Trunk のテーマに合致している
- 概要の内容が Trunk の範囲に含まれる

JSON形式で回答してください。"""

    response_schema = {
        "type": "object",
        "properties": {
            "page_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "移動候補となるページIDのリスト"
            },
            "reasoning": {
                "type": "string",
                "description": "選択の理由（簡潔に）"
            }
        },
        "required": ["page_ids"]
    }

    # Get LLM client
    print(f"\n[Stage 1] Asking LLM ({args.model}) to select candidate pages...")

    # Find model config (same pattern as build_arasuji.py)
    resolved_model_id, model_config = find_model_config(args.model)

    if resolved_model_id:
        if resolved_model_id != args.model:
            LOGGER.info(f"Resolved model '{args.model}' -> '{resolved_model_id}'")
        actual_model_id = model_config.get("model", resolved_model_id)
        context_length = model_config.get("context_length", 128000)
        provider = model_config.get("provider", "gemini")
    else:
        LOGGER.error(f"Model '{args.model}' not found in config.")
        LOGGER.error("Use --list-models to see available options.")
        adapter.close()
        sys.exit(1)

    LOGGER.info(f"Using model: {actual_model_id}")
    LOGGER.info(f"Using provider: {provider}")

    # Import factory directly to avoid circular import
    from llm_clients.factory import get_llm_client

    client = get_llm_client(actual_model_id, provider, context_length, config=model_config)

    # Load context from Memopedia and Chronicle (like build_arasuji.py)
    memopedia_context = ""
    chronicle_context = ""

    try:
        memopedia_context = memopedia.get_tree_markdown(include_keywords=True, show_markers=False)
        if memopedia_context and memopedia_context != "(まだページはありません)":
            LOGGER.info(f"Loaded Memopedia context ({len(memopedia_context)} chars)")
        else:
            memopedia_context = ""
    except Exception as e:
        LOGGER.warning(f"Failed to load Memopedia context: {e}")

    try:
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.context import get_episode_context, format_episode_context
        init_arasuji_tables(adapter.conn)
        context_entries = get_episode_context(adapter.conn, max_entries=50)
        if context_entries:
            chronicle_context = format_episode_context(context_entries)
            LOGGER.info(f"Loaded Chronicle context ({len(chronicle_context)} chars)")
    except Exception as e:
        LOGGER.warning(f"Failed to load Chronicle context: {e}")

    try:
        result = get_llm_response(client, prompt, response_schema)
    except Exception as e:
        print(f"Error getting LLM response: {e}")
        sys.exit(1)

    candidate_ids = result.get("page_ids", [])
    reasoning = result.get("reasoning", "")

    if not candidate_ids:
        print("LLM selected no candidate pages.")
        if reasoning:
            print(f"Reason: {reasoning}")
        sys.exit(0)

    # Validate selected IDs
    valid_ids = set(p.id for p in unorganized)
    candidate_ids = [pid for pid in candidate_ids if pid in valid_ids]

    if not candidate_ids:
        print("No valid page IDs in LLM selection.")
        sys.exit(0)

    # Show candidates
    print(f"\n[Stage 1] Found {len(candidate_ids)} candidate pages:")
    for pid in candidate_ids:
        page = next((p for p in unorganized if p.id == pid), None)
        if page:
            print(f"  - {page.title}")

    if reasoning:
        print(f"Reasoning: {reasoning}")

    # Stage 2: Verify each candidate by reading full content
    print("\n[Stage 2] Verifying candidates with full content...")

    # Build verification prompt with all context
    candidate_pages = [p for p in unorganized if p.id in candidate_ids]

    # Format detailed page info including full content
    detailed_pages = "\n\n".join([
        f"""### {i+1}. {p.title}
- ID: {p.id}
- Category: {p.category}
- Keywords: {', '.join(p.keywords) if p.keywords else '(なし)'}
- Summary: {p.summary or '(なし)'}
- Content:
{p.content or '(なし)'}"""
        for i, p in enumerate(candidate_pages)
    ])

    context_section = ""
    if memopedia_context or chronicle_context:
        context_section = "\n## 参考情報\n"
        if chronicle_context:
            context_section += f"\n### エピソード記憶（Chronicle）\n{chronicle_context[:3000]}\n"
        if memopedia_context:
            context_section += f"\n### 知識ベース（Memopedia）\n{memopedia_context[:3000]}\n"

    verify_prompt = f"""以下のページが Trunk「{trunk.title}」に本当に移動すべきかどうかを判断してください。
各ページの詳細内容を確認し、関連性のあるものだけを選んでください。

## 移動先 Trunk
- タイトル: {trunk.title}
- カテゴリ: {trunk.category}
- 概要: {trunk.summary or '(なし)'}
- 内容: {trunk.content[:1000] if trunk.content else '(なし)'}
{context_section}
## 候補ページ（詳細）
{detailed_pages}

## タスク
上記の候補ページの中から、本当に「{trunk.title}」に移動すべきページだけを選んでください。

判断基準:
- ページの内容が Trunk のテーマと明確に関連している
- 単なる名前の一致ではなく、実際の内容が関係している
- 不明確な場合は含めない

JSON形式で回答してください。"""

    verify_schema = {
        "type": "object",
        "properties": {
            "page_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "最終的に移動すべきページIDのリスト"
            },
            "rejected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"}
                    }
                },
                "description": "除外したページとその理由"
            },
            "reasoning": {
                "type": "string",
                "description": "全体的な判断理由"
            }
        },
        "required": ["page_ids"]
    }

    try:
        verify_result = get_llm_response(client, verify_prompt, verify_schema)
    except Exception as e:
        print(f"Error in verification stage: {e}")
        # Fall back to using stage 1 results
        print("Falling back to stage 1 candidates...")
        verify_result = {"page_ids": candidate_ids}

    selected_ids = verify_result.get("page_ids", [])
    rejected = verify_result.get("rejected", [])
    final_reasoning = verify_result.get("reasoning", "")

    # Validate final IDs
    selected_ids = [pid for pid in selected_ids if pid in candidate_ids]

    if not selected_ids:
        print("\n[Stage 2] All candidates were rejected after detailed review.")
        if rejected:
            print("\nRejection reasons:")
            for r in rejected:
                page = next((p for p in unorganized if p.id == r.get("id")), None)
                title = page.title if page else r.get("id", "?")
                print(f"  - {title}: {r.get('reason', 'Unknown')}")
        sys.exit(0)

    # Show final selection
    print(f"\n[Stage 2] Verified {len(selected_ids)} pages to move (rejected {len(candidate_ids) - len(selected_ids)}):")
    for pid in selected_ids:
        page = next((p for p in unorganized if p.id == pid), None)
        if page:
            cat_label = f" [{page.category}]" if args.all_categories else ""
            print(f"  ✓ {page.title}{cat_label}")

    if rejected:
        print(f"\nRejected ({len(rejected)}):")
        for r in rejected:
            page = next((p for p in unorganized if p.id == r.get("id")), None)
            title = page.title if page else r.get("id", "?")
            print(f"  ✗ {title}: {r.get('reason', '')}")

    if final_reasoning:
        print(f"\nReasoning: {final_reasoning}")

    # Stage 3: Auto-subcategory clustering (if enabled and threshold exceeded)
    subcategory_plan = None
    if args.auto_subcategories and len(selected_ids) >= args.subcategory_threshold:
        print(f"\n[Stage 3] Too many pages ({len(selected_ids)}) selected. Creating subcategories...")
        
        # Get full page details for clustering
        selected_pages = [p for p in unorganized if p.id in selected_ids]
        
        # Format page info for clustering
        pages_for_clustering = "\n\n".join([
            f"""### {p.title}
- Category: {p.category}
- Keywords: {', '.join(p.keywords) if p.keywords else '(なし)'}
- Summary: {p.summary or '(なし)'}
- Content preview: {p.content[:200] if p.content else '(なし)'}..."""
            for p in selected_pages
        ])
        
        clustering_prompt = f"""以下の{len(selected_ids)}ページを、親Trunk「{trunk.title}」の下に整理したいです。
ページ数が多すぎるため、意味的なまとまり（クラスタ）ごとに中間trunkページを作成し、階層化します。

## 親Trunk
- タイトル: {trunk.title}
- カテゴリ: {trunk.category}
- 概要: {trunk.summary or '(なし)'}

## 整理対象ページ一覧
{pages_for_clustering}

## タスク
上記のページを3-7個程度の意味的なクラスタ（サブカテゴリ）に分類してください。
各クラスタには以下を指定：

1. **中間trunkのタイトル**: 簡潔で分かりやすい（例：「エリスのアイデンティティ」「エリスとの思い出」）
2. **中間trunkの概要**: 1-2文でこのクラスタが何を含むか
3. **含まれるページID**: このクラスタに属するページのIDリスト

分類の基準:
- 各クラスタは5-15ページ程度が理想（少なすぎず多すぎず）
- テーマや概念の一貫性を重視
- 将来的に参照しやすい構造に

JSON形式で回答してください。"""

        clustering_schema = {
            "type": "object",
            "properties": {
                "subcategories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "trunk_title": {"type": "string", "description": "中間trunkのタイトル"},
                            "trunk_summary": {"type": "string", "description": "中間trunkの概要（1-2文）"},
                            "page_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "このクラスタに含まれるページIDのリスト"
                            }
                        },
                        "required": ["trunk_title", "trunk_summary", "page_ids"]
                    },
                    "description": "サブカテゴリ（中間trunk）のリスト"
                },
                "reasoning": {
                    "type": "string",
                    "description": "クラスタリングの方針"
                }
            },
            "required": ["subcategories"]
        }
        
        try:
            cluster_result = get_llm_response(client, clustering_prompt, clustering_schema)
        except Exception as e:
            print(f"Warning: Could not perform clustering: {e}")
            print("Proceeding without subcategories...")
            cluster_result = None
        
        if cluster_result:
            subcategory_plan = cluster_result.get("subcategories", [])
            cluster_reasoning = cluster_result.get("reasoning", "")
            
            if subcategory_plan:
                print(f"\n提案: {len(subcategory_plan)}個の中間trunkに分類")
                if cluster_reasoning:
                    print(f"方針: {cluster_reasoning}\n")
                
                for i, subcat in enumerate(subcategory_plan, 1):
                    print(f"{i}. {subcat['trunk_title']}")
                    print(f"   概要: {subcat['trunk_summary']}")
                    print(f"   ページ数: {len(subcat['page_ids'])}")



    # Execute move
    if args.dry_run:
        print("\n[DRY RUN] Would move the above pages. No changes made.")
    else:
        confirm = input(f"\nMove these {len(selected_ids)} pages to '{trunk.title}'? [y/N]: ")
        if confirm.lower() != 'y':
            print("Cancelled.")
            sys.exit(0)

        # If subcategories were planned, create intermediate trunks first
        if subcategory_plan:
            print("\n中間trunkページを作成中...")
            
            # Create intermediate trunk pages
            trunk_id_map = {}  # Maps trunk_title -> trunk_id
            for i, subcat in enumerate(subcategory_plan, 1):
                trunk_title = subcat['trunk_title']
                trunk_summary = subcat['trunk_summary']
                
                print(f"  [{i}/{len(subcategory_plan)}] Creating trunk: {trunk_title}")
                
                # Create intermediate trunk as child of main trunk
                intermediate_trunk = memopedia.create_page(
                    parent_id=args.trunk,
                    title=trunk_title,
                    summary=trunk_summary,
                    content=f"{trunk_summary}\n\nこのページは「{trunk.title}」の下位カテゴリとして自動生成されました。",
                    is_trunk=True
                )
                
                trunk_id_map[trunk_title] = intermediate_trunk.id
                LOGGER.info(f"Created intermediate trunk: {intermediate_trunk.id}")
            
            # Move pages to their respective intermediate trunks
            print("\nページを中間trunkに移動中...")
            total_moved = 0
            
            for subcat in subcategory_plan:
                trunk_title = subcat['trunk_title']
                page_ids = subcat.get('page_ids', [])
                
                # Filter to only valid IDs
                valid_page_ids = [pid for pid in page_ids if pid in selected_ids]
                
                if valid_page_ids:
                    intermediate_trunk_id = trunk_id_map[trunk_title]
                    result = memopedia.move_pages_to_trunk(valid_page_ids, intermediate_trunk_id)
                    moved_count = result.get('moved_count', 0)
                    total_moved += moved_count
                    print(f"  ✓ {trunk_title}: {moved_count} pages moved")
            
            print(f"\n✓ Moved {total_moved} pages into {len(subcategory_plan)} intermediate trunks under '{trunk.title}'")
        else:
            # Direct move without subcategories
            result = memopedia.move_pages_to_trunk(selected_ids, args.trunk)
            print(f"\nMoved {result['moved_count']} pages to '{result['trunk_title']}'")


    adapter.close()
    print("Done.")


if __name__ == "__main__":
    main()
