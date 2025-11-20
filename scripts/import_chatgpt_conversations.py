#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from saiverse_memory import SAIMemoryAdapter
from tools.utilities.chatgpt_importer import ChatGPTExport, ConversationRecord

UTC = timezone.utc


def format_datetime(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    iso = dt.astimezone(UTC).replace(microsecond=0).isoformat()
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import ChatGPT conversations.json exports into SAIMemory.",
    )

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--source",
        required=True,
        help="Path to conversations.json or the ChatGPT export ZIP.",
    )
    shared.add_argument(
        "--preview",
        type=int,
        default=120,
        help="Preview length when listing conversations (default 120 characters).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", parents=[shared], help="List available conversations in the export.")
    list_parser.add_argument(
        "--output",
        choices=("table", "json"),
        default="table",
        help="Output format for listing (default table).",
    )

    import_parser = subparsers.add_parser(
        "import",
        parents=[shared],
        help="Import selected conversations into a persona memory DB.",
    )
    import_parser.add_argument(
        "--persona",
        required=True,
        help="Target persona ID (maps to ~/.saiverse/personas/<persona>/memory.db).",
    )
    import_parser.add_argument(
        "--select",
        nargs="+",
        help="Conversation indexes or IDs to import. If omitted, interactive selection is used.",
    )
    import_parser.add_argument(
        "--select-all",
        action="store_true",
        help="Import all conversations without prompting.",
    )
    import_parser.add_argument(
        "--roles",
        default="user,assistant",
        help="Comma-separated list of roles to import (default: user,assistant).",
    )
    import_parser.add_argument(
        "--thread-suffix",
        help="Optional thread suffix override. If omitted, each conversation uses its ChatGPT conversation ID.",
    )
    import_parser.add_argument(
        "--no-header",
        action="store_true",
        help="Disable inserting a system header message before each imported conversation.",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to memory.db.",
    )
    import_parser.add_argument(
        "--output",
        choices=("human", "json"),
        default="human",
        help="Output format for import results (default human-readable).",
    )

    return parser


def load_export(path_str: str) -> ChatGPTExport:
    path = Path(path_str).expanduser()
    return ChatGPTExport(path)


def handle_list(export: ChatGPTExport, *, preview: int, output: str) -> None:
    if output == "json":
        data = export.summaries(preview_chars=preview)
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    records = export.conversations
    if not records:
        print("No conversations found in export.")
        return

    headers, rows = build_summary_rows(records, preview)
    print_table(headers, rows)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return textwrap.shorten(value, width=width, placeholder="…")


def handle_import(export: ChatGPTExport, args: argparse.Namespace) -> None:
    records = export.conversations
    if not records:
        print("No conversations found in export.")
        return

    if args.select_all:
        selected = list(records)
    else:
        selected = resolve_selection(records, args.select)
        if not selected:
            selected = prompt_for_selection(records, preview=args.preview)

    if not selected:
        print("No conversations selected; aborting.")
        return

    allowed_roles = parse_roles(args.roles)
    include_roles: Optional[Sequence[str]] = list(allowed_roles) if allowed_roles else None

    header_enabled = not args.no_header

    results: list[dict[str, object]] = []

    adapter: Optional[SAIMemoryAdapter] = None
    if not args.dry_run:
        adapter = SAIMemoryAdapter(args.persona)
        if not adapter.is_ready():
            print(f"SAIMemory adapter is not ready for persona {args.persona}.")
            return

    try:
        for record in selected:
            payloads = list(record.iter_memory_payloads(include_roles=include_roles))
            thread_suffix = resolve_thread_suffix(record, args.thread_suffix)
            if header_enabled:
                header_ts = record.create_time or record.update_time or datetime.now(tz=UTC)
                origin_id = record.conversation_id or record.identifier
                header_text = (
                    f"[Imported ChatGPT conversation \"{record.title}\" "
                    f"({origin_id}) created {format_datetime(header_ts)}]"
                )
                payloads.insert(
                    0,
                    {
                        "role": "system",
                        "content": header_text,
                        "timestamp": format_datetime(header_ts),
                    },
                )

            import_result = {
                "id": record.identifier,
                "title": record.title,
                "thread_suffix": thread_suffix,
                "messages_imported": len(payloads),
            }

            if args.dry_run:
                import_result["status"] = "skipped (dry-run)"
            else:
                for payload in payloads:
                    metadata = payload.get("metadata")
                    if not isinstance(metadata, dict):
                        metadata = {}
                        payload["metadata"] = metadata
                    tags = metadata.get("tags")
                    if isinstance(tags, list):
                        tag_list = [str(tag) for tag in tags if tag]
                    elif tags is None:
                        tag_list = []
                    else:
                        tag_list = [str(tags)]
                    if "conversation" not in tag_list:
                        tag_list.append("conversation")
                    metadata["tags"] = tag_list
                    adapter.append_persona_message(payload, thread_suffix=thread_suffix)  # type: ignore[arg-type]
                import_result["status"] = "imported"

            results.append(import_result)

    finally:
        if adapter is not None:
            adapter.close()

    if args.output == "json":
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        for item in results:
            status = item["status"]
            suffix = item.get("thread_suffix", "")
            suffix_note = f" thread={suffix}" if suffix else ""
            print(f"[{status}] {item['title']} ({item['messages_imported']} messages){suffix_note}")


def build_summary_rows(records: Sequence[ConversationRecord], preview: int) -> tuple[list[str], list[list[str]]]:
    headers = ["Idx", "ID", "Title", "Created (UTC)", "Updated (UTC)", "Msgs", "Preview"]
    rows: list[list[str]] = []
    for idx, record in enumerate(records):
        summary = record.to_summary_dict(preview_chars=preview)
        created = summary["create_time"] or "-"
        updated = summary["update_time"] or "-"
        preview_text = summary["first_user_preview"] or ""
        identifier = (summary["id"] or "")[:12]
        rows.append(
            [
                f"{idx}",
                identifier,
                _truncate(summary["title"] or "", 30),
                created,
                updated,
                str(summary["message_count"]),
                preview_text,
            ]
        )
    return headers, rows


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        print("No data.")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    header_line = " | ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        line = " | ".join((row[i]).ljust(widths[i]) for i in range(len(headers)))
        print(line)


def parse_roles(value: Optional[str]) -> List[str]:
    if not value:
        return []
    roles = []
    for entry in value.split(","):
        name = entry.strip()
        if name:
            roles.append(name)
    return roles


def resolve_thread_suffix(record: ConversationRecord, override: Optional[str]) -> str:
    if override:
        return override
    if record.conversation_id:
        return str(record.conversation_id)
    return str(record.identifier)


def resolve_selection(records: Sequence[ConversationRecord], selectors: Optional[Sequence[str]]) -> List[ConversationRecord]:
    if not selectors:
        return []

    resolved: List[ConversationRecord] = []
    for raw in selectors:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            continue
        for token in parts:
            record = None
            if token.isdigit():
                idx = int(token)
                if 0 <= idx < len(records):
                    record = records[idx]
            if record is None:
                record = next(
                    (rec for rec in records if token in {rec.identifier, rec.conversation_id}),
                    None,
                )
            if record is None:
                matches = [rec for rec in records if rec.identifier.startswith(token)]
                if len(matches) == 1:
                    record = matches[0]
            if record is None:
                raise ValueError(f"Unknown conversation selector: {token}")
            if record not in resolved:
                resolved.append(record)
    return resolved


def prompt_for_selection(records: Sequence[ConversationRecord], *, preview: int) -> List[ConversationRecord]:
    if not records:
        return []

    headers, rows = build_summary_rows(records, preview)
    print_table(headers, rows)

    attempts = 3
    while attempts > 0:
        attempts -= 1
        try:
            raw = input("Enter comma-separated conversation indexes (empty to cancel): ")
        except EOFError:
            return []
        selectors = [token.strip() for token in raw.split(",") if token.strip()]
        if not selectors:
            return []
        try:
            return resolve_selection(records, selectors)
        except ValueError as exc:
            print(exc)
    return []


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        export = load_export(args.source)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load ChatGPT export: {exc}")
        return 1

    if args.command == "list":
        handle_list(export, preview=args.preview, output=args.output)
        return 0

    if args.command == "import":
        try:
            handle_import(export, args)
        except ValueError as exc:
            print(f"Import aborted: {exc}")
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
