#!/usr/bin/env python
"""Import a Playbook JSON file into the playbooks table.

Usage:
  python scripts/import_playbook.py --file path/to/playbook.json \
      [--scope public|personal|building] [--persona-id PERSONA] [--building-id BUILDING]

Notes:
- scope=personal なら persona-id が必要。
- scope=building なら building-id が必要。
- description/name は JSON から取るが、--name/--description で上書きも可能。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# プロジェクトルートを追加
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.defs.save_playbook import save_playbook  # type: ignore


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a Playbook JSON into DB")
    parser.add_argument("--file", required=True, help="Playbook JSON path")
    parser.add_argument("--scope", default="public", choices=["public", "personal", "building"], help="Playbook scope")
    parser.add_argument("--persona-id", dest="persona_id", help="Owner persona (for personal scope)")
    parser.add_argument("--building-id", dest="building_id", help="Building id (for building scope)")
    parser.add_argument("--name", help="Override playbook name")
    parser.add_argument("--description", help="Override description")
    args = parser.parse_args()

    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))

    name = args.name or data.get("name")
    if not name:
        raise SystemExit("Playbook name is required (in JSON or --name)")

    description = args.description or data.get("description", "")

    save_playbook(
        name=name,
        description=description,
        scope=args.scope,
        created_by_persona_id=args.persona_id,
        building_id=args.building_id,
        playbook_json=json.dumps(data, ensure_ascii=False),
    )
    print(f"Imported playbook '{name}' (scope={args.scope}).")


if __name__ == "__main__":
    main()

