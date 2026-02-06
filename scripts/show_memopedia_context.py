"""
Display the Memopedia context that would be injected into LLM prompts.

This script shows what get_tree_markdown() generates, to help analyze
and optimize the context size.

Usage:
    python scripts/show_memopedia_context.py --persona eris_city_a
    python scripts/show_memopedia_context.py --persona eris_city_a --output context.txt
    python scripts/show_memopedia_context.py --persona eris_city_a --stats
"""

from __future__ import annotations

import argparse
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


def main():
    parser = argparse.ArgumentParser(
        description="Display Memopedia context used in LLM prompts"
    )
    parser.add_argument(
        "--persona",
        required=True,
        help="Persona ID (e.g., 'eris_city_a')"
    )
    parser.add_argument(
        "--output",
        help="Save context to file instead of printing to console"
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show statistics about the context"
    )

    args = parser.parse_args()

    # Initialize memory adapter
    print(f"Loading Memopedia for persona: {args.persona}")
    adapter = SAIMemoryAdapter(args.persona)
    memopedia = Memopedia(adapter.conn)

    # Generate context using the unified method from Memopedia
    context = memopedia.get_tree_markdown(include_keywords=True, show_markers=False)
    
    # Statistics
    char_count = len(context)
    line_count = len(context.split('\n'))
    
    if context == "(まだページはありません)":
        print("\nMemopedia is empty - no context generated.")
        adapter.close()
        sys.exit(0)
    
    print(f"\n{'='*60}")
    print("Memopedia Context Statistics")
    print(f"{'='*60}")
    print(f"Total characters: {char_count:,}")
    print(f"Total lines: {line_count:,}")
    
    # Analyze by category
    categories = {"人物": 0, "用語": 0, "予定": 0}
    for line in context.split('\n'):
        for cat_name in categories:
            if line.startswith(f"### {cat_name}"):
                # Count lines until next category or end
                start_idx = context.find(line)
                rest = context[start_idx:]
                
                # Find next category header
                next_cat_idx = len(rest)
                for other_cat in categories:
                    if other_cat != cat_name:
                        idx = rest.find(f"\n### {other_cat}")
                        if idx != -1 and idx < next_cat_idx:
                            next_cat_idx = idx
                
                section = rest[:next_cat_idx]
                categories[cat_name] = len(section)
    
    if any(categories.values()):
        print("\nBreakdown by category:")
        for cat_name, size in categories.items():
            if size > 0:
                percentage = (size / char_count) * 100
                print(f"  {cat_name}: {size:,} chars ({percentage:.1f}%)")
    
    # Count pages
    page_count = context.count('\n- ') - context.count('\n  - ')  # Top-level pages only
    print(f"\nApproximate page count: {page_count}")
    
    if args.stats:
        # Just show stats, don't print content
        print(f"\n{'='*60}")
    else:
        # Output content
        if args.output:
            output_path = Path(args.output)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(context)
            print(f"\nContext saved to: {output_path}")
            print(f"Use: cat {output_path} | head -n 50  # to preview")
        else:
            print(f"\n{'='*60}")
            print("Context Preview (first 100 lines):")
            print(f"{'='*60}\n")
            
            lines = context.split('\n')
            preview_lines = lines[:100]
            for line in preview_lines:
                print(line)
            
            if len(lines) > 100:
                print(f"\n... ({len(lines) - 100} more lines)")
                print("\nUse --output <file> to save full content")
    
    adapter.close()


if __name__ == "__main__":
    main()
