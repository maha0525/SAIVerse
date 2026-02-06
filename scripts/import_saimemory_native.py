#!/usr/bin/env python3
"""Import SAIMemory threads from native SAIVerse JSON format.

Restores threads with all metadata, thread overviews, and Stelis info.
Default behavior: replaces existing threads with the same thread_id.

Usage:
    python scripts/import_saimemory_native.py <persona_id> <json_file> [options]

Examples:
    # Import with replace (default)
    python scripts/import_saimemory_native.py air_city_a export.json

    # Preview without writing
    python scripts/import_saimemory_native.py air_city_a export.json --dry-run

    # Skip embedding generation (faster)
    python scripts/import_saimemory_native.py air_city_a export.json --no-embed

    # Import as new thread
    python scripts/import_saimemory_native.py air_city_a export.json --new-thread edited_v2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

LOGGER = logging.getLogger("import_saimemory_native")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import SAIMemory threads from native SAIVerse JSON format."
    )
    parser.add_argument("persona_id", help="Target persona ID (e.g. air_city_a)")
    parser.add_argument("json_file", type=Path, help="Path to native JSON file")
    parser.add_argument(
        "--new-thread", dest="new_thread_suffix",
        help="Import as a new thread with this suffix (instead of replacing original).",
    )
    parser.add_argument(
        "--no-embed", action="store_true",
        help="Skip embedding generation (faster import, but no semantic search).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be imported without writing.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip confirmation prompt.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging.",
    )
    return parser.parse_args()


def load_native_json(path: Path) -> dict:
    """Load and validate native JSON file."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")

    fmt = data.get("format")
    if fmt != "saiverse_saimemory_v1":
        raise ValueError(f"Unsupported format: {fmt!r} (expected 'saiverse_saimemory_v1')")

    return data


def main() -> int:
    args = parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load file
    try:
        data = load_native_json(args.json_file)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        LOGGER.error("Failed to load file: %s", e)
        return 1

    threads = data.get("threads", [])
    source_persona = data.get("persona_id", "unknown")
    total_msgs = sum(len(t.get("messages", [])) for t in threads)

    LOGGER.info("File: %s", args.json_file)
    LOGGER.info("Source persona: %s", source_persona)
    LOGGER.info("Threads: %d, Messages: %d", len(threads), total_msgs)

    # Warn if persona_id differs
    if source_persona != args.persona_id:
        LOGGER.warning(
            "Source persona (%s) differs from target (%s).",
            source_persona, args.persona_id,
        )
        if not args.force:
            answer = input("Continue anyway? [y/N] ").strip().lower()
            if answer != "y":
                LOGGER.info("Aborted.")
                return 0

    # Remap thread_ids if --new-thread is specified
    if args.new_thread_suffix:
        for thread in threads:
            old_id = thread["thread_id"]
            new_id = f"{args.persona_id}:{args.new_thread_suffix}"
            LOGGER.info("Remapping thread: %s -> %s", old_id, new_id)
            thread["thread_id"] = new_id

    # Dry run
    if args.dry_run:
        for t in threads:
            msgs = t.get("messages", [])
            LOGGER.info(
                "[DRY-RUN] Thread: %s (%d messages, stelis=%s)",
                t.get("thread_id"),
                len(msgs),
                "yes" if t.get("stelis") else "no",
            )
            for msg in msgs[:3]:
                content = msg.get("content", "")[:80]
                LOGGER.info(
                    "  [%s] %s%s",
                    msg.get("role", "?"),
                    content,
                    "..." if len(msg.get("content", "")) > 80 else "",
                )
            if len(msgs) > 3:
                LOGGER.info("  ... and %d more messages", len(msgs) - 3)
        return 0

    # Confirm replace
    if not args.force:
        LOGGER.info("This will REPLACE existing threads with the same thread_id.")
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            LOGGER.info("Aborted.")
            return 0

    # Import
    from saiverse_memory.native_export import import_threads_native

    def progress(current: int, total: int, message: str) -> None:
        LOGGER.info("[%d/%d] %s", current, total, message)

    try:
        result = import_threads_native(
            persona_id=args.persona_id,
            data=data,
            replace=True,
            skip_embed=args.no_embed,
            progress_callback=progress,
        )
    except Exception as e:
        LOGGER.exception("Import failed: %s", e)
        return 1

    LOGGER.info(
        "Import complete: %d thread(s), %d message(s)",
        result["threads_imported"],
        result["messages_imported"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
