#!/usr/bin/env python3
"""Export SAIMemory threads to native SAIVerse JSON format.

All metadata (tags, with, token_count, etc.), thread overviews,
and Stelis thread info are fully preserved for round-trip editing.

Usage:
    python scripts/export_saimemory_native.py <persona_id> [options]

Examples:
    # Export all threads to stdout
    python scripts/export_saimemory_native.py air_city_a

    # Export specific thread to file
    python scripts/export_saimemory_native.py air_city_a --thread building_1 --output export.json

    # Export with time range
    python scripts/export_saimemory_native.py air_city_a --start 2026-01-01T00:00:00 --end 2026-02-01T00:00:00
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export SAIMemory threads to native SAIVerse JSON format."
    )
    parser.add_argument("persona", help="Persona ID (e.g. air_city_a)")
    parser.add_argument(
        "--output", default="-",
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--thread", action="append", dest="threads",
        help="Thread suffix or full ID to export. Repeatable. If omitted, export all threads.",
    )
    parser.add_argument("--start", help="Start ISO timestamp (inclusive)")
    parser.add_argument("--end", help="End ISO timestamp (inclusive)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from saiverse_memory.native_export import export_threads_native

    try:
        data = export_threads_native(
            persona_id=args.persona,
            thread_suffixes=args.threads,
            start=args.start,
            end=args.end,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Export failed: {e}", file=sys.stderr)
        return 1

    # Summary
    total_threads = len(data.get("threads", []))
    total_msgs = sum(len(t.get("messages", [])) for t in data.get("threads", []))
    print(f"Exported {total_threads} thread(s), {total_msgs} message(s)", file=sys.stderr)

    # Output
    if args.output == "-":
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Written to {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
