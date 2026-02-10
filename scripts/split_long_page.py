#!/usr/bin/env python3
"""
Split a long Memopedia page into multiple child pages using LLM analysis.

This script:
1. Analyzes a long page's content to identify semantic sections
2. Proposes child page titles and content divisions
3. Creates child pages under the original page
4. Rewrites the parent page as a concise overview with links

Usage:
    python scripts/split_long_page.py --persona air --page <page_id>
    python scripts/split_long_page.py --persona air --page <page_id> --model gemini-2.5-flash-lite-preview-09-2025
    python scripts/split_long_page.py --persona air --page <page_id> --dry-run
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

# Environment variable for default model
ENV_MODEL = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.5-flash-lite-preview-09-2025")


def get_llm_response(client, prompt: str, response_schema: dict) -> dict:
    """Get structured response from LLM."""
    messages = [{"role": "user", "content": prompt}]

    # Use generate() method - the correct LLM client API
    response = client.generate(
        messages=messages,
        response_schema=response_schema,
    )

    # generate() returns a string directly
    content = response

    # Try to extract JSON from response
    try:
        if isinstance(content, dict):
            return content
        return json.loads(content)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        import re
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse LLM response as JSON: {content}")


def main():
    parser = argparse.ArgumentParser(
        description="Split a long Memopedia page into semantic child pages"
    )
    parser.add_argument(
        "--persona",
        required=True,
        help="Persona ID (e.g., 'air', 'air_city_a')"
    )
    parser.add_argument(
        "--page",
        required=True,
        help="Page ID to split"
    )
    parser.add_argument(
        "--model",
        default=ENV_MODEL,
        help=f"LLM model to use (default: {ENV_MODEL}, env: MEMORY_WEAVE_MODEL)"
    )
    parser.add_argument(
        "--max-children",
        type=int,
        default=10,
        help="Maximum number of child pages to create (default: 10)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show split proposal without making changes"
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

    # Get the page to split
    page = memopedia.get_page(args.page)
    if page is None:
        print(f"Error: Page not found: {args.page}")
        sys.exit(1)

    print(f"\nTarget page: {page.title}")
    print(f"Category: {page.category}")
    print(f"Current content length: {len(page.content or '')} characters")

    if not page.content or len(page.content) < 1000:
        print("\nPage content is too short to split (< 1000 chars)")
        sys.exit(0)

    # Load context from Memopedia and Chronicle
    memopedia_context = ""
    chronicle_context = ""

    try:
        memopedia_context = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
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
        context_entries = get_episode_context(adapter.conn, max_entries=30)
        if context_entries:
            chronicle_context = format_episode_context(context_entries)
            LOGGER.info(f"Loaded Chronicle context ({len(chronicle_context)} chars)")
    except Exception as e:
        LOGGER.warning(f"Failed to load Chronicle context: {e}")

    # Build analysis prompt
    context_section = ""
    if memopedia_context or chronicle_context:
        context_section = "\n## 参考情報\n"
        if chronicle_context:
            context_section += f"\n### エピソード記憶（Chronicle）\n{chronicle_context[:2000]}\n"
        if memopedia_context:
            context_section += f"\n### 知識ベース（Memopedia）\n{memopedia_context[:2000]}\n"

    prompt = f"""以下のMemopediaページが非常に長いため、複数の子ページに分割したいです。
内容を分析し、意味的なまとまりごとにセクションを識別して、適切な子ページの構成を提案してください。

## 対象ページ
- タイトル: {page.title}
- カテゴリ: {page.category}
- 概要: {page.summary or '(なし)'}
- キーワード: {', '.join(page.keywords) if page.keywords else '(なし)'}

## ページ内容
{page.content}
{context_section}
## タスク
上記のページ内容を分析し、{args.max_children}個以内の子ページに分割してください。

### 子ページについて
各子ページには以下を含めてください：

1. **子ページのタイトル**: 簡潔で分かりやすいタイトル
2. **子ページの概要**: 1-2文で何について書かれているか
3. **キーワード**: そのセクションを表すキーワード3-5個
4. **内容**: その子ページに含める具体的な内容

### 親ページについて
親ページには「{page.title}」という人物・概念の**本質的な説明**を2-3段落で書いてください。
- 「分割しました」「子ページがあります」といったメタ的な説明は不要です
- 独立した紹介文として、この人物・概念が何者で、何を重視し、どんな特徴があるかを簡潔に説明してください
- 元のページ冒頭の要約（イタリック部分）を参考にしつつ、より洗練させてください

分割の基準:
- 時系列よりも**テーマ・概念のまとまり**を優先
- 各子ページは独立して理解できる内容にする
- 関連する情報はまとめて同じ子ページに
- 重複を避ける（必要なら要約して参照形式に）

JSON形式で回答してください。"""

    response_schema = {
        "type": "object",
        "properties": {
            "parent_summary": {
                "type": "string",
                "description": "親ページに残す本質的な説明（2-3段落、人物・概念の紹介文として独立して読める内容）"
            },
            "children": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "子ページのタイトル"},
                        "summary": {"type": "string", "description": "子ページの概要（1-2文）"},
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "キーワードリスト"
                        },
                        "content": {"type": "string", "description": "子ページに含める内容"}
                    },
                    "required": ["title", "summary", "content"]
                },
                "description": f"子ページのリスト（最大{args.max_children}個）"
            },
            "reasoning": {
                "type": "string",
                "description": "分割の理由と方針"
            }
        },
        "required": ["parent_summary", "children"]
    }

    # Get LLM client
    print(f"\n[Analysis] Analyzing page content with LLM ({args.model})...")

    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client

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

    client = get_llm_client(actual_model_id, provider, context_length, config=model_config)

    try:
        result = get_llm_response(client, prompt, response_schema)
    except Exception as e:
        print(f"Error analyzing page: {e}")
        sys.exit(1)

    parent_summary = result.get("parent_summary", "")
    children = result.get("children", [])
    reasoning = result.get("reasoning", "")

    if not children:
        print("\nLLM did not propose any child pages.")
        if reasoning:
            print(f"Reason: {reasoning}")
        sys.exit(0)

    # Show proposal
    print(f"\n[Proposal] Split into {len(children)} child pages:")
    print("\n" + "=" * 60)
    
    if reasoning:
        print(f"\n分割方針:\n{reasoning}\n")
    
    print(f"親ページ「{page.title}」の新しい概要:")
    print(f"{parent_summary}\n")
    print("=" * 60)

    for i, child in enumerate(children, 1):
        print(f"\n{i}. {child['title']}")
        print(f"   概要: {child['summary']}")
        if child.get('keywords'):
            print(f"   キーワード: {', '.join(child['keywords'])}")
        print(f"   内容長: {len(child['content'])} 文字")
        
        if args.verbose:
            print("\n   --- 内容プレビュー ---")
            preview = child['content'][:500]
            print(f"   {preview}...")
            print("   --- (以下省略) ---")

    # Stage 2: Check for missing important information
    print("\n[Stage 2] Checking for information lost during split...")
    
    # Combine all child content
    all_children_content = "\n\n---\n\n".join([
        f"# {child['title']}\n\n{child['content']}"
        for child in children
    ])
    
    missing_info_prompt = f"""以下は、元のページ内容と分割後の子ページ内容です。
元のページにあった重要な情報が、分割後のどのページにも含まれていないかチェックしてください。

## 元のページ内容
{page.content}

## 分割後の全子ページ内容
{all_children_content}

## タスク
元のページにはあるが、分割後のどのページにも含まれていない**重要な情報**を抽出し、
親ページに追記するための**自然な文章**として整形してください。

特に以下のような情報に注目：
- 具体的な日付・数値（誕生日、記念日、年齢など）
- 固有名詞（人名、地名、作品名など）
- 具体的な事実やエピソード
- 重要な引用や発言

**出力形式:**
- 「〜が欠落している」という指摘形式ではなく、**情報そのものを記述した文章**にしてください
- 親ページに追記しても違和感のない、自然な箇条書きや短い段落として
- 見出し「## 主な事実・日付」などのセクションにまとめて配置できる形式で

**例:**
× [日付] 誕生日: 1月14日であることが欠落している
○ 誕生日は1月14日。

些細な表現の違いや重複は無視し、**本質的に失われた重要情報**だけを抽出してください。

JSON形式で回答してください。"""

    missing_info_schema = {
        "type": "object",
        "properties": {
            "missing_section": {
                "type": "string",
                "description": "親ページに追記する、欠落情報をまとめたセクション全体（Markdown形式、見出しを含む）"
            },
            "has_missing_info": {
                "type": "boolean",
                "description": "重要な欠落情報があるかどうか"
            },
            "assessment": {
                "type": "string",
                "description": "情報欠落の程度についての簡潔な評価（1-2文）"
            }
        },
        "required": ["missing_section", "has_missing_info"]
    }
    
    try:
        missing_result = get_llm_response(client, missing_info_prompt, missing_info_schema)
    except Exception as e:
        print(f"Warning: Could not check for missing information: {e}")
        missing_result = {"has_missing_info": False, "missing_section": ""}
    
    has_missing_info = missing_result.get("has_missing_info", False)
    missing_section = missing_result.get("missing_section", "")
    assessment = missing_result.get("assessment", "")
    
    if has_missing_info and missing_section:
        print("\n⚠ Important information was lost during split.")
        if assessment:
            print(f"評価: {assessment}")
        
        print("\n--- 追加されるセクション ---")
        # Show preview of what will be added
        preview_lines = missing_section.split('\n')[:10]
        for line in preview_lines:
            print(line)
        if len(missing_section.split('\n')) > 10:
            print("... (以下省略)")
        print("--- end ---")
        
        # Add missing information section to parent summary
        parent_summary = parent_summary + "\n\n" + missing_section
        print("\n→ This section will be added to the parent page.")
    else:
        print("✓ No significant information loss detected.")

    # Execute split
    if args.dry_run:
        print("\n[DRY RUN] Would create the above child pages. No changes made.")
    else:
        confirm = input(f"\nCreate {len(children)} child pages? [y/N]: ")
        if confirm.lower() != 'y':
            print("Cancelled.")
            sys.exit(0)

        print("\nCreating child pages...")
        
        # Create child pages
        created_pages = []
        for i, child in enumerate(children, 1):
            child_title = child['title']
            child_summary = child['summary']
            child_keywords = child.get('keywords', [])
            child_content = child['content']
            
            print(f"  [{i}/{len(children)}] Creating: {child_title}")
            
            child_page = memopedia.create_page(
                parent_id=page.id,
                title=child_title,
                summary=child_summary,
                content=child_content,
                keywords=child_keywords
            )
            
            created_pages.append(child_page)
            LOGGER.info(f"Created child page: {child_page.id}")

        # Update parent page
        print(f"\nUpdating parent page: {page.title}")
        
        # Build new parent content with links to children
        new_parent_content = parent_summary + "\n\n## 詳細ページ\n\n"
        for child_page in created_pages:
            new_parent_content += f"- **{child_page.title}**: {child_page.summary}\n"
        
        memopedia.update_page(
            page_id=page.id,
            content=new_parent_content,
            summary=page.summary  # Keep original summary
        )
        
        print("\n✓ Split complete!")
        print(f"  Parent page updated: {page.title}")
        print(f"  Created {len(created_pages)} child pages")

    adapter.close()
    print("Done.")


if __name__ == "__main__":
    main()
