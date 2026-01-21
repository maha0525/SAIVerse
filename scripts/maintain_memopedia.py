#!/usr/bin/env python3
"""
Maintain and optimize Memopedia knowledge base.

This script performs automated maintenance tasks:
- merge-similar: LLM identifies and merges redundant pages
- split-large: Split pages exceeding 5000 characters into smaller ones
- fix-markdown: Fix common markdown formatting issues (literal \n, etc.)

Usage:
    python scripts/maintain_memopedia.py <persona_id> --auto
    python scripts/maintain_memopedia.py <persona_id> --merge-similar
    python scripts/maintain_memopedia.py <persona_id> --split-large
    python scripts/maintain_memopedia.py <persona_id> --fix-markdown
    python scripts/maintain_memopedia.py <persona_id> --auto --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

# Skip tool imports to avoid circular import issue
os.environ["SAIVERSE_SKIP_TOOL_IMPORTS"] = "1"

from sai_memory.memory.storage import init_db
from sai_memory.memopedia import Memopedia
from sai_memory.memopedia.storage import get_children, update_page as storage_update_page
from model_configs import find_model_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

# Constants
SPLIT_THRESHOLD = 5000  # Characters


def _get_prompts_dir() -> Path:
    """Get prompts directory using data_paths or fallback to legacy."""
    try:
        from data_paths import PROMPTS_DIR as DATA_PROMPTS_DIR, BUILTIN_DATA_DIR
        return BUILTIN_DATA_DIR / DATA_PROMPTS_DIR
    except ImportError:
        return Path(__file__).resolve().parents[1] / "system_prompts"


PROMPTS_DIR = _get_prompts_dir()


def load_prompt(name: str) -> str:
    """Load a prompt template, checking user_data first then builtin_data."""
    try:
        from data_paths import load_prompt as dp_load_prompt
        return dp_load_prompt(name)
    except ImportError:
        # Fallback to legacy
        path = PROMPTS_DIR / f"{name}.txt"
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8")


def get_persona_db_path(persona_id: str) -> Path:
    """Get the path to a persona's memory.db file."""
    return Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"


def get_all_descendant_ids(memopedia: Memopedia, page_id: str) -> set:
    """Get all descendant page IDs (children, grandchildren, etc.) of a page."""
    descendants = set()
    children = get_children(memopedia.conn, page_id)
    for child in children:
        descendants.add(child.id)
        descendants.update(get_all_descendant_ids(memopedia, child.id))
    return descendants


def format_page_list(memopedia: Memopedia) -> str:
    """Format page tree as a hierarchical list for LLM with parent-child relationships."""
    tree = memopedia.get_tree()
    lines: List[str] = []
    
    def _list_pages(pages: List[Dict], category: str, depth: int = 0, parent_id: Optional[str] = None) -> None:
        for page in pages:
            if page["id"].startswith("root_"):
                # Skip root pages but still process their children
                for child in page.get("children", []):
                    _list_pages([child], category, depth, None)
                continue
            keywords = page.get("keywords", [])
            kw_str = f" [キーワード: {', '.join(keywords)}]" if keywords else ""
            indent = "  " * depth
            parent_str = f" (親: {parent_id})" if parent_id else ""
            lines.append(f"{indent}- id={page['id']} | {category} | {page['title']}: {page['summary']}{kw_str}{parent_str}")
            for child in page.get("children", []):
                _list_pages([child], category, depth + 1, page["id"])
    
    for category in ["people", "events", "plans"]:
        _list_pages(tree.get(category, []), category)
    
    return "\n".join(lines) if lines else "(ページなし)"


# =============================================================================
# Fix Markdown
# =============================================================================

def fix_markdown_content(content: str) -> Tuple[str, bool]:
    """Fix common markdown issues in content.
    
    Returns:
        Tuple of (fixed_content, was_modified)
    """
    original = content
    
    # Fix literal \n before markdown syntax
    # \n### -> actual newline + ###
    content = re.sub(r'\\n(#{1,6}\s)', r'\n\1', content)
    
    # \n- or \n* -> actual newline + list marker
    content = re.sub(r'\\n([-*]\s)', r'\n\1', content)
    
    # \n followed by numbered list (1. 2. etc)
    content = re.sub(r'\\n(\d+\.\s)', r'\n\1', content)
    
    # \n\n -> actual double newline (paragraph break)
    content = re.sub(r'\\n\\n', r'\n\n', content)
    
    # Single \n at end of bullet point or before common patterns
    # Only replace \n when it's clearly meant to be a newline
    # (after punctuation + space pattern)
    content = re.sub(r'。\\n', '。\n', content)
    content = re.sub(r'。\\n', '。\n', content)
    
    return content, content != original


def run_fix_markdown(memopedia: Memopedia, dry_run: bool = False) -> List[str]:
    """Fix markdown formatting issues in all pages.
    
    Returns:
        List of page titles that were fixed
    """
    fixed_pages: List[str] = []
    tree = memopedia.get_tree()
    
    def _process_pages(pages: List[Dict]) -> None:
        for page in pages:
            if page["id"].startswith("root_"):
                for child in page.get("children", []):
                    _process_pages([child])
                continue
            
            full_page = memopedia.get_page(page["id"])
            if not full_page:
                continue
            
            fixed_content, was_modified = fix_markdown_content(full_page.content)
            
            if was_modified:
                fixed_pages.append(full_page.title)
                if not dry_run:
                    memopedia.update_page(
                        full_page.id,
                        content=fixed_content,
                        edit_source="auto_maintenance"
                    )
                    LOGGER.info(f"Fixed markdown: {full_page.title}")
                else:
                    LOGGER.info(f"[DRY RUN] Would fix markdown: {full_page.title}")
            
            for child in page.get("children", []):
                _process_pages([child])
    
    for category in ["people", "events", "plans"]:
        _process_pages(tree.get(category, []))
    
    return fixed_pages


# =============================================================================
# Split Large Pages
# =============================================================================

def run_split_large(
    memopedia: Memopedia,
    client,
    dry_run: bool = False,
    threshold: int = SPLIT_THRESHOLD,
) -> List[str]:
    """Split pages that exceed the character threshold.
    
    Returns:
        List of page titles that were split
    """
    split_pages: List[str] = []
    tree = memopedia.get_tree()
    
    # Response schema for split decision
    response_schema = {
        "type": "object",
        "properties": {
            "should_split": {"type": "boolean"},
            "reason": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["title", "summary", "content"],
                },
            },
            "remaining_content": {"type": "string"},
        },
        "required": ["should_split"],
    }
    
    def _process_pages(pages: List[Dict]) -> None:
        for page in pages:
            if page["id"].startswith("root_"):
                for child in page.get("children", []):
                    _process_pages([child])
                continue
            
            full_page = memopedia.get_page(page["id"])
            if not full_page:
                continue
            
            content_len = len(full_page.content or "")
            if content_len > threshold:
                LOGGER.info(f"Page '{full_page.title}' has {content_len} chars, analyzing for split...")
                
                prompt = f"""以下のMemopediaページは{content_len}文字あり、長すぎます。
子ページに分割すべきセクションがあれば提案してください。

タイトル: {full_page.title}
サマリー: {full_page.summary}

内容:
{full_page.content}

## 指示
- 分割すべきセクションがあれば、should_split: true として sections に分割案を記載
- 分割後の親ページに残すべき内容を remaining_content に記載
- 分割不要（全体で一貫性がある）なら should_split: false
- 各 section には title, summary, content を含める
"""
                
                try:
                    LOGGER.info("Calling LLM for split analysis...")
                    response_text = client.generate(
                        messages=[{"role": "user", "content": prompt}],
                        tools=[],
                        response_schema=response_schema,
                    )
                    LOGGER.info(f"LLM response received: {len(response_text) if response_text else 0} chars")
                    
                    if not response_text:
                        continue
                    
                    # Parse JSON
                    if "```json" in response_text:
                        response_text = response_text.split("```json")[1].split("```")[0]
                    elif "```" in response_text:
                        response_text = response_text.split("```")[1].split("```")[0]
                    
                    data = json.loads(response_text.strip())
                    
                    if not data.get("should_split"):
                        LOGGER.info(f"LLM decided not to split: {data.get('reason', 'no reason')}")
                        continue
                    
                    sections = data.get("sections", [])
                    remaining = data.get("remaining_content", "")
                    
                    if not sections:
                        continue
                    
                    split_pages.append(full_page.title)
                    
                    if dry_run:
                        LOGGER.info(f"[DRY RUN] Would split: {full_page.title} into {len(sections)} child pages")
                        for sec in sections:
                            LOGGER.info(f"  - {sec.get('title')}")
                    else:
                        # Create child pages
                        for sec in sections:
                            memopedia.create_page(
                                parent_id=full_page.id,
                                title=sec["title"],
                                summary=sec["summary"],
                                content=sec["content"],
                                edit_source="auto_maintenance",
                            )
                            LOGGER.info(f"Created child page: {sec['title']}")
                        
                        # Update parent with remaining content
                        if remaining:
                            memopedia.update_page(
                                full_page.id,
                                content=remaining,
                                edit_source="auto_maintenance",
                            )
                        
                        LOGGER.info(f"Split completed: {full_page.title}")
                
                except Exception as e:
                    LOGGER.error(f"Error splitting page {full_page.title}: {e}")
            
            for child in page.get("children", []):
                _process_pages([child])
    
    for category in ["people", "events", "plans"]:
        _process_pages(tree.get(category, []))
    
    return split_pages


# =============================================================================
# Merge Similar Pages
# =============================================================================

def run_merge_similar(
    memopedia: Memopedia,
    client,
    dry_run: bool = False,
) -> List[Tuple[str, str]]:
    """Find and merge similar/redundant pages.
    
    LLM analyzes the entire page list and decides which pages should be merged.
    
    Returns:
        List of (page1_title, page2_title) tuples that were merged
    """
    merged_pairs: List[Tuple[str, str]] = []
    
    # Get page list
    page_list = format_page_list(memopedia)
    
    if page_list == "(ページなし)":
        LOGGER.info("No pages to analyze for merging")
        return merged_pairs
    
    LOGGER.info(f"Analyzing {page_list.count(chr(10)) + 1} pages for potential merges...")
    LOGGER.debug(f"Page list:\n{page_list}")
    
    # Step 1: Ask LLM to identify merge candidates
    find_candidates_schema = {
        "type": "object",
        "properties": {
            "merge_pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "page_id_1": {"type": "string"},
                        "page_id_2": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["page_id_1", "page_id_2", "reason"],
                },
            },
        },
        "required": ["merge_pairs"],
    }
    
    # Build a set of all pages and their descendants for validation
    all_pages_descendants: Dict[str, set] = {}
    
    def _collect_descendants(pages: List[Dict]) -> None:
        for page in pages:
            if page["id"].startswith("root_"):
                for child in page.get("children", []):
                    _collect_descendants([child])
                continue
            all_pages_descendants[page["id"]] = get_all_descendant_ids(memopedia, page["id"])
            for child in page.get("children", []):
                _collect_descendants([child])
    
    tree = memopedia.get_tree()
    for category in ["people", "events", "plans"]:
        _collect_descendants(tree.get(category, []))
    
    find_prompt = f"""以下はMemopediaのページ一覧です。

{page_list}

## 指示
明らかに重複または類似しており、統合すべきページペアがあれば挙げてください。
統合すべきペアがなければ、merge_pairs を空配列で返してください。

注意:
- 「似ている」だけでは統合しない。明確に同一トピックを扱っている場合のみ
- 親子関係にあるページは統合対象外
- 異なるカテゴリのページも統合対象外
"""
    
    try:
        LOGGER.info("Calling LLM to find merge candidates...")
        response_text = client.generate(
            messages=[{"role": "user", "content": find_prompt}],
            tools=[],
            response_schema=find_candidates_schema,
        )
        LOGGER.info(f"LLM response received: {len(response_text) if response_text else 0} chars")
        
        if not response_text:
            return merged_pairs
        
        # Parse JSON
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        
        data = json.loads(response_text.strip())
        merge_pairs = data.get("merge_pairs", [])
        
        if not merge_pairs:
            LOGGER.info("LLM found no pages to merge")
            return merged_pairs
        
        LOGGER.info(f"LLM identified {len(merge_pairs)} merge candidates")
        
        # Step 2: Merge each pair
        merge_content_schema = {
            "type": "object",
            "properties": {
                "merged_title": {"type": "string"},
                "merged_summary": {"type": "string"},
                "merged_content": {"type": "string"},
                "merged_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["merged_title", "merged_summary", "merged_content", "merged_keywords"],
        }
        
        for pair in merge_pairs:
            page1 = memopedia.get_page(pair["page_id_1"])
            page2 = memopedia.get_page(pair["page_id_2"])
            
            if not page1 or not page2:
                LOGGER.warning(f"Could not find pages: {pair['page_id_1']}, {pair['page_id_2']}")
                continue
            
            # Skip if one is a descendant of the other
            page1_descendants = all_pages_descendants.get(page1.id, set())
            page2_descendants = all_pages_descendants.get(page2.id, set())
            
            if page2.id in page1_descendants:
                LOGGER.warning(f"Skipping merge: '{page2.title}' is a descendant of '{page1.title}'")
                continue
            if page1.id in page2_descendants:
                LOGGER.warning(f"Skipping merge: '{page1.title}' is a descendant of '{page2.title}'")
                continue
            
            LOGGER.info(f"Merging: '{page1.title}' + '{page2.title}' - {pair.get('reason', '')}")
            
            if dry_run:
                merged_pairs.append((page1.title, page2.title))
                LOGGER.info(f"[DRY RUN] Would merge: {page1.title} + {page2.title}")
                continue
            
            merge_prompt = f"""以下の2つのMemopediaページを1つに統合してください。

## ページ1
タイトル: {page1.title}
サマリー: {page1.summary}
キーワード: {page1.keywords}
内容:
{page1.content}

## ページ2
タイトル: {page2.title}
サマリー: {page2.summary}
キーワード: {page2.keywords}
内容:
{page2.content}

## 指示
- 両方の情報を統合した新しいコンテンツを生成
- 重複する情報は1つにまとめる
- 矛盾する情報がある場合は両方記載するか、より新しい/正確な方を採用
- 適切なタイトル、サマリー、キーワードを提案
"""
            
            try:
                merge_response = client.generate(
                    messages=[{"role": "user", "content": merge_prompt}],
                    tools=[],
                    response_schema=merge_content_schema,
                )
                
                if not merge_response:
                    continue
                
                if "```json" in merge_response:
                    merge_response = merge_response.split("```json")[1].split("```")[0]
                elif "```" in merge_response:
                    merge_response = merge_response.split("```")[1].split("```")[0]
                
                merged_data = json.loads(merge_response.strip())
                
                # Update page1 with merged content
                memopedia.update_page(
                    page1.id,
                    title=merged_data["merged_title"],
                    summary=merged_data["merged_summary"],
                    content=merged_data["merged_content"],
                    keywords=merged_data["merged_keywords"],
                    edit_source="auto_maintenance",
                )
                
                # Transfer children of page2 to page1 before deleting
                page2_children = get_children(memopedia.conn, page2.id)
                if page2_children:
                    LOGGER.info(f"Transferring {len(page2_children)} children from '{page2.title}' to '{page1.title}'")
                    for child in page2_children:
                        storage_update_page(
                            memopedia.conn,
                            child.id,
                            parent_id=page1.id,
                        )
                        LOGGER.info(f"  - Transferred child: {child.title}")
                
                # Delete page2 (now without children, so they won't be deleted)
                memopedia.delete_page(
                    page2.id,
                    edit_source="auto_maintenance",
                )
                
                merged_pairs.append((page1.title, page2.title))
                LOGGER.info(f"Merged into: {merged_data['merged_title']}")
                
            except Exception as e:
                LOGGER.error(f"Error merging pages: {e}")
        
    except Exception as e:
        LOGGER.error(f"Error finding merge candidates: {e}")
    
    return merged_pairs


# =============================================================================
# Group Shallow Pages
# =============================================================================

# Minimum number of pages in a category to trigger grouping
GROUP_SHALLOW_THRESHOLD = 10


def get_depth1_pages_by_category(memopedia: Memopedia) -> Dict[str, List[Dict]]:
    """Get all depth-1 pages (direct children of root categories) organized by category."""
    tree = memopedia.get_tree()
    result: Dict[str, List[Dict]] = {}
    
    for category in ["people", "events", "plans"]:
        pages = []
        for root_page in tree.get(category, []):
            if not root_page["id"].startswith("root_"):
                continue
            # Get direct children of root
            for child in root_page.get("children", []):
                child_count = len(child.get("children", []))
                pages.append({
                    "id": child["id"],
                    "title": child["title"],
                    "summary": child.get("summary", ""),
                    "keywords": child.get("keywords", []),
                    "child_count": child_count,
                })
        result[category] = pages
    
    return result


def format_pages_for_grouping(pages: List[Dict]) -> str:
    """Format a list of pages for LLM grouping analysis."""
    lines = []
    for p in pages:
        kw = f" [キーワード: {', '.join(p['keywords'])}]" if p.get("keywords") else ""
        child_str = f" (子ページ: {p['child_count']})" if p.get("child_count", 0) > 0 else ""
        lines.append(f"- id={p['id']} | {p['title']}: {p['summary']}{kw}{child_str}")
    return "\n".join(lines)


def run_group_shallow(
    memopedia: Memopedia,
    client,
    dry_run: bool = False,
    threshold: int = GROUP_SHALLOW_THRESHOLD,
) -> List[Dict]:
    """Group shallow (depth-1) pages into new parent pages.
    
    LLM analyzes pages directly under root categories and suggests groupings
    by theme. New parent pages are created and pages are moved underneath.
    
    Returns:
        List of grouping operations performed: [{"parent_title": str, "children": [str]}]
    """
    grouped: List[Dict] = []
    
    depth1_by_category = get_depth1_pages_by_category(memopedia)
    
    # Response schema for grouping suggestion
    response_schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parent_title": {"type": "string"},
                        "parent_summary": {"type": "string"},
                        "page_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["parent_title", "parent_summary", "page_ids", "reason"],
                },
            },
        },
        "required": ["groups"],
    }
    
    for category, pages in depth1_by_category.items():
        if len(pages) < threshold:
            LOGGER.info(f"Category '{category}' has {len(pages)} pages (< {threshold}), skipping grouping")
            continue
        
        LOGGER.info(f"Analyzing '{category}' with {len(pages)} depth-1 pages for grouping...")
        
        # Get the root page ID for this category
        root_id = f"root_{category}"
        
        page_list = format_pages_for_grouping(pages)
        
        prompt = f"""以下は「{category}」カテゴリの直下にあるMemopediaページの一覧です。
現在{len(pages)}ページが同じ階層にフラットに並んでいます。

{page_list}

## 指示
これらのページを意味的なグループに分類し、新しい親ページを作成してその下に移動させる提案をしてください。

注意:
- 1つのグループには最低2ページ以上を含めること
- すべてのページをグループに入れる必要はない（単独で残すべきページは残してよい）
- グループ名は抽象的すぎず、具体的すぎず、適切な粒度で
- 明確な共通テーマがあるページのみをグループ化する
- すでに子ページを持っているページ（child_count > 0）は、他のページの子にはしないこと
- 類似したトピックを扱うページをまとめるのであって、マージするわけではない
- グループにできるものがなければ、groups を空配列で返す

例えば：
- 複数のChatbotUI関連ページ → 「ChatbotUI」親ページの下へ
- 複数のGoogle Drive API関連ページ → 「Google Drive API連携」親ページの下へ
- 複数のAI人格・意識関連ページ → 「AI人格と意識」親ページの下へ
"""
        
        try:
            LOGGER.info("Calling LLM for grouping analysis...")
            response_text = client.generate(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                response_schema=response_schema,
            )
            
            if not response_text:
                LOGGER.warning(f"Empty response for category {category}")
                continue
            
            # Parse JSON
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            
            data = json.loads(response_text.strip())
            groups = data.get("groups", [])
            
            if not groups:
                LOGGER.info(f"LLM found no suitable groupings for '{category}'")
                continue
            
            LOGGER.info(f"LLM suggested {len(groups)} groupings for '{category}'")
            
            # Build a set of valid page IDs for validation
            valid_page_ids = {p["id"] for p in pages}
            
            # Build a set of pages that already have children
            pages_with_children = {p["id"] for p in pages if p.get("child_count", 0) > 0}
            
            for group in groups:
                parent_title = group["parent_title"]
                parent_summary = group["parent_summary"]
                page_ids = group["page_ids"]
                reason = group.get("reason", "")
                
                # Validate page IDs
                valid_ids = [pid for pid in page_ids if pid in valid_page_ids]
                
                # Exclude pages that already have children
                valid_ids = [pid for pid in valid_ids if pid not in pages_with_children]
                
                if len(valid_ids) < 2:
                    LOGGER.warning(f"Group '{parent_title}' has fewer than 2 valid pages after filtering, skipping")
                    continue
                
                # Get titles for logging
                page_titles = [p["title"] for p in pages if p["id"] in valid_ids]
                
                if dry_run:
                    LOGGER.info(f"[DRY RUN] Would create group '{parent_title}' with {len(valid_ids)} pages:")
                    for title in page_titles[:5]:
                        LOGGER.info(f"  - {title}")
                    if len(page_titles) > 5:
                        LOGGER.info(f"  ... and {len(page_titles) - 5} more")
                    grouped.append({
                        "parent_title": parent_title,
                        "children": page_titles,
                        "category": category,
                    })
                else:
                    # Create the new parent page
                    new_parent = memopedia.create_page(
                        parent_id=root_id,
                        title=parent_title,
                        summary=parent_summary,
                        content=f"このページは関連するトピックをグループ化したものです。\n\n{reason}",
                        edit_source="auto_maintenance",
                    )
                    LOGGER.info(f"Created parent page: {parent_title} (id: {new_parent.id})")
                    
                    # Move pages under the new parent
                    for page_id in valid_ids:
                        storage_update_page(
                            memopedia.conn,
                            page_id,
                            parent_id=new_parent.id,
                        )
                    
                    LOGGER.info(f"Moved {len(valid_ids)} pages under '{parent_title}'")
                    
                    grouped.append({
                        "parent_title": parent_title,
                        "children": page_titles,
                        "category": category,
                    })
        
        except Exception as e:
            LOGGER.error(f"Error grouping pages in category '{category}': {e}")
            import traceback
            traceback.print_exc()
    
    return grouped


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Maintain and optimize Memopedia knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 全自動メンテナンス
  python scripts/maintain_memopedia.py air_city_a --auto

  # 個別実行
  python scripts/maintain_memopedia.py air_city_a --merge-similar
  python scripts/maintain_memopedia.py air_city_a --split-large
  python scripts/maintain_memopedia.py air_city_a --group-shallow
  python scripts/maintain_memopedia.py air_city_a --fix-markdown

  # ドライラン（変更せずに対象を表示）
  python scripts/maintain_memopedia.py air_city_a --auto --dry-run
""",
    )
    parser.add_argument("persona_id", help="Persona ID to process")
    parser.add_argument("--auto", action="store_true", help="Run all maintenance tasks")
    parser.add_argument("--merge-similar", action="store_true", help="Find and merge similar/redundant pages")
    parser.add_argument("--split-large", action="store_true", help=f"Split pages exceeding {SPLIT_THRESHOLD} characters")
    parser.add_argument("--group-shallow", action="store_true", help="Group shallow pages into parent pages by theme")
    parser.add_argument("--fix-markdown", action="store_true", help="Fix markdown formatting issues")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    parser.add_argument("--model", default="gemini-2.0-flash", help="Model for LLM operations")
    parser.add_argument("--provider", help="Override provider detection")

    args = parser.parse_args()

    # Check if any operation is specified
    if not any([args.auto, args.merge_similar, args.split_large, args.group_shallow, args.fix_markdown]):
        parser.error("Specify at least one operation: --auto, --merge-similar, --split-large, --group-shallow, or --fix-markdown")

    # Check if persona exists
    db_path = get_persona_db_path(args.persona_id)
    if not db_path.exists():
        LOGGER.error(f"Persona database not found: {db_path}")
        sys.exit(1)

    # Initialize Memopedia
    conn = init_db(str(db_path), check_same_thread=False)
    memopedia = Memopedia(conn)

    LOGGER.info(f"Maintaining Memopedia for persona: {args.persona_id}")
    LOGGER.info(f"Dry run: {args.dry_run}")

    # Initialize LLM client if needed
    client = None
    if args.auto or args.merge_similar or args.split_large or args.group_shallow:
        resolved_model_id, model_config = find_model_config(args.model)
        if resolved_model_id:
            actual_model_id = model_config.get("model", resolved_model_id)
            context_length = model_config.get("context_length", 128000)
            provider = args.provider or model_config.get("provider", "gemini")
        else:
            LOGGER.error(f"Model '{args.model}' not found in config.")
            LOGGER.error("Use --list-models to see available options.")
            conn.close()
            sys.exit(1)

        LOGGER.info(f"Using model: {actual_model_id}")

        from llm_clients.factory import get_llm_client
        client = get_llm_client(actual_model_id, provider, context_length, config=model_config)

    # Results tracking
    results = {
        "markdown_fixed": [],
        "pages_split": [],
        "pages_merged": [],
        "pages_grouped": [],
    }

    # Run operations
    # Note: markdown is fixed LAST because merge/split may break markdown formatting
    if args.auto or args.merge_similar:
        LOGGER.info("=== Merge Similar Pages ===")
        results["pages_merged"] = run_merge_similar(memopedia, client, dry_run=args.dry_run)

    if args.auto or args.split_large:
        LOGGER.info("=== Split Large Pages ===")
        results["pages_split"] = run_split_large(memopedia, client, dry_run=args.dry_run)

    if args.auto or args.group_shallow:
        LOGGER.info("=== Group Shallow Pages ===")
        results["pages_grouped"] = run_group_shallow(memopedia, client, dry_run=args.dry_run)

    if args.auto or args.fix_markdown:
        LOGGER.info("=== Fix Markdown ===")
        results["markdown_fixed"] = run_fix_markdown(memopedia, dry_run=args.dry_run)

    # Summary
    LOGGER.info("\n" + "=" * 60)
    LOGGER.info("Maintenance Summary")
    LOGGER.info("=" * 60)
    
    if results["markdown_fixed"]:
        LOGGER.info(f"Markdown fixed ({len(results['markdown_fixed'])}):")
        for title in results["markdown_fixed"]:
            LOGGER.info(f"  - {title}")
    else:
        LOGGER.info("Markdown fixed: 0")

    if results["pages_split"]:
        LOGGER.info(f"Pages split ({len(results['pages_split'])}):")
        for title in results["pages_split"]:
            LOGGER.info(f"  - {title}")
    else:
        LOGGER.info("Pages split: 0")

    if results["pages_merged"]:
        LOGGER.info(f"Pages merged ({len(results['pages_merged'])}):")
        for t1, t2 in results["pages_merged"]:
            LOGGER.info(f"  - {t1} + {t2}")
    else:
        LOGGER.info("Pages merged: 0")

    if results["pages_grouped"]:
        LOGGER.info(f"Pages grouped ({len(results['pages_grouped'])}):")
        for g in results["pages_grouped"]:
            LOGGER.info(f"  - {g['parent_title']} ({len(g['children'])} children)")
    else:
        LOGGER.info("Pages grouped: 0")

    conn.close()
    LOGGER.info("Done!")


if __name__ == "__main__":
    main()
