"""Export/Import Memopedia pages for a persona.

Usage:
    python scripts/export_memopedia.py <persona_id> export [--format json|markdown] [--output FILE]
    python scripts/export_memopedia.py <persona_id> import <file> [--clear]

Examples:
    # Export as JSON (default)
    python scripts/export_memopedia.py eris_city_a export

    # Export as Markdown
    python scripts/export_memopedia.py eris_city_a export --format markdown

    # Export to specific file
    python scripts/export_memopedia.py eris_city_a export --output backup.json

    # Import from JSON (merge with existing)
    python scripts/export_memopedia.py eris_city_a import backup.json

    # Import from JSON (replace existing)
    python scripts/export_memopedia.py eris_city_a import backup.json --clear
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _init(persona_id: str):
    from sai_memory.memory.storage import init_db
    from sai_memory.memopedia import Memopedia, init_memopedia_tables

    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = init_db(str(db_path), check_same_thread=False)
    init_memopedia_tables(conn)
    return conn, Memopedia(conn)


def cmd_export(args):
    conn, memopedia = _init(args.persona_id)

    if args.format == "markdown":
        content = memopedia.export_all_markdown()
        ext = "md"
    else:
        data = memopedia.export_json()
        content = json.dumps(data, ensure_ascii=False, indent=2)
        ext = "json"

    if args.output:
        out_path = Path(args.output)
    else:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_path = Path(f"{args.persona_id}_memopedia_{timestamp}.{ext}")

    out_path.write_text(content, encoding="utf-8")
    print(f"Exported to {out_path} ({len(content):,} chars)")
    conn.close()


def cmd_import(args):
    conn, memopedia = _init(args.persona_id)

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"File not found: {file_path}")
        conn.close()
        sys.exit(1)

    data = json.loads(file_path.read_text(encoding="utf-8"))
    page_count = len(data.get("pages", []))

    if args.clear:
        print(f"Importing {page_count} pages (clearing existing pages)")
    else:
        print(f"Importing {page_count} pages (merging with existing)")

    memopedia.import_json(data, clear_existing=args.clear)
    print("Import complete")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Export/Import Memopedia pages")
    parser.add_argument("persona_id", help="Persona ID")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="Export Memopedia")
    p_export.add_argument("--format", choices=["json", "markdown"], default="json")
    p_export.add_argument("--output", "-o", help="Output file path")

    p_import = sub.add_parser("import", help="Import Memopedia from JSON")
    p_import.add_argument("file", help="JSON file to import")
    p_import.add_argument("--clear", action="store_true", help="Clear existing pages before import")

    args = parser.parse_args()

    if args.command == "export":
        cmd_export(args)
    elif args.command == "import":
        cmd_import(args)


if __name__ == "__main__":
    main()
