#!/usr/bin/env python
"""Import a Playbook JSON file into the playbooks table.

Usage:
  python scripts/import_playbook.py --file path/to/playbook.json \
      [--scope public|personal|building] [--persona-id PERSONA] [--building-id BUILDING] \
      [--router-callable | --no-router-callable] [--user-selectable | --no-user-selectable]

Notes:
- scope=personal なら persona-id が必要。
- scope=building なら building-id が必要。
- description/name は JSON から取るが、--name/--description で上書きも可能。
- --router-callable: playbookをrouterから呼び出せるようにする。指定しない場合はJSONのrouter_callableフィールドを参照。
- --user-selectable: meta playbookをUIで選択可能にする。指定しない場合はJSONのuser_selectableフィールドを参照。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# プロジェクトルートを追加
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from builtin_data.tools.save_playbook import save_playbook  # type: ignore


def infer_scope_from_path(path: Path) -> tuple[str, str | None, str | None]:
    """Infer playbook scope from file path.

    Returns: (scope, persona_id, building_id)
    - */playbooks/public/*.json → ("public", None, None)
    - */playbooks/building/<building_id>/*.json → ("building", None, building_id)
    - */playbooks/personal/<persona_id>/*.json → ("personal", persona_id, None)
    
    Works for both:
    - user_data/playbooks/...
    - builtin_data/playbooks/...
    - sea/playbooks/... (legacy)
    """
    parts = path.resolve().parts
    try:
        playbooks_idx = parts.index("playbooks")
        if playbooks_idx + 1 < len(parts):
            scope_dir = parts[playbooks_idx + 1]
            if scope_dir == "public":
                return ("public", None, None)
            elif scope_dir == "building" and playbooks_idx + 2 < len(parts):
                building_id = parts[playbooks_idx + 2]
                return ("building", None, building_id)
            elif scope_dir == "personal" and playbooks_idx + 2 < len(parts):
                persona_id = parts[playbooks_idx + 2]
                return ("personal", persona_id, None)
    except ValueError:
        pass
    return ("public", None, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a Playbook JSON into DB")
    parser.add_argument("--file", required=True, help="Playbook JSON path")
    parser.add_argument("--scope", default=None, choices=["public", "personal", "building"], help="Playbook scope (auto-inferred from path if not specified)")
    parser.add_argument("--persona-id", dest="persona_id", help="Owner persona (for personal scope, auto-inferred from path if not specified)")
    parser.add_argument("--building-id", dest="building_id", help="Building id (for building scope, auto-inferred from path if not specified)")
    parser.add_argument("--name", help="Override playbook name")
    parser.add_argument("--description", help="Override description")
    parser.add_argument("--router-callable", dest="router_callable", action="store_true", help="Mark playbook as callable from router")
    parser.add_argument("--no-router-callable", dest="router_callable", action="store_false", help="Mark playbook as not callable from router")
    parser.add_argument("--user-selectable", dest="user_selectable", action="store_true", help="Mark meta playbook as selectable by user in UI")
    parser.add_argument("--no-user-selectable", dest="user_selectable", action="store_false", help="Mark meta playbook as not selectable by user in UI")
    parser.set_defaults(router_callable=None, user_selectable=None)
    args = parser.parse_args()

    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))

    # Infer scope from path if not explicitly set
    inferred_scope, inferred_persona_id, inferred_building_id = infer_scope_from_path(path)
    scope = args.scope or inferred_scope
    persona_id = args.persona_id or inferred_persona_id
    building_id = args.building_id or inferred_building_id

    name = args.name or data.get("name")
    if not name:
        raise SystemExit("Playbook name is required (in JSON or --name)")

    description = args.description or data.get("description", "")

    save_playbook(
        name=name,
        description=description,
        scope=scope,
        created_by_persona_id=persona_id,
        building_id=building_id,
        playbook_json=json.dumps(data, ensure_ascii=False),
        router_callable=args.router_callable,
        user_selectable=args.user_selectable,
    )
    router_status = "router-callable" if (args.router_callable if args.router_callable is not None else data.get("router_callable", False)) else "not router-callable"
    user_status = "user-selectable" if (args.user_selectable if args.user_selectable is not None else data.get("user_selectable", False)) else "not user-selectable"
    print(f"Imported playbook '{name}' (scope={scope}, {router_status}, {user_status}).")


if __name__ == "__main__":
    main()

