"""Organize memory notes into Memopedia pages.

Usage:
    python scripts/organize_memory_notes.py <persona_id> run [--model MODEL]
    python scripts/organize_memory_notes.py <persona_id> plan [--dry-run] [--model MODEL]
    python scripts/organize_memory_notes.py <persona_id> status

Examples:
    # Show current status
    python scripts/organize_memory_notes.py air_city_a status

    # Run: group notes and write directly to Memopedia (no LLM content generation)
    python scripts/organize_memory_notes.py air_city_a run

    # Plan: preview grouping without writing (dry-run)
    python scripts/organize_memory_notes.py air_city_a plan --dry-run
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def show_status(conn):
    """Show current note organization status."""
    from sai_memory.memory.storage import (
        count_unplanned_notes,
        count_planned_groups,
        count_unresolved_notes,
        get_planned_group_labels,
        get_planned_notes_by_group,
    )

    total = count_unresolved_notes(conn)
    unplanned = count_unplanned_notes(conn)
    n_groups = count_planned_groups(conn)

    print(f"Total unresolved notes: {total}")
    print(f"  Unplanned (need plan): {unplanned}")
    print(f"  Planned groups (need exec): {n_groups}")

    if n_groups > 0:
        labels = get_planned_group_labels(conn)
        for label in labels:
            group_notes = get_planned_notes_by_group(conn, label)
            if group_notes:
                action = group_notes[0].action
                target = group_notes[0].target_page_id or group_notes[0].suggested_title
                print(f"    [{label}] {len(group_notes)} notes → {action} (target: {target})")
                for n in group_notes:
                    print(f"      - {n.content}")


def _init_db(persona_id: str):
    from sai_memory.memory.storage import init_db
    from sai_memory.arasuji import init_arasuji_tables

    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = init_db(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)
    return conn


def _init_llm(model_name: str = None):
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client

    model_name = model_name or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
    resolved_model_id, model_config = find_model_config(model_name)
    if not resolved_model_id:
        print(f"Model '{model_name}' not found")
        sys.exit(1)

    provider = model_config.get("provider", "gemini")
    context_length = model_config.get("context_length", 128000)
    client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)
    print(f"Using model: {model_config.get('model', resolved_model_id)} / {provider}")
    return client


def _init_memopedia(conn):
    from sai_memory.memopedia import Memopedia, init_memopedia_tables
    init_memopedia_tables(conn)
    return Memopedia(conn)


def cmd_plan(args):
    conn = _init_db(args.persona_id)

    from sai_memory.memory.storage import count_unplanned_notes, get_unplanned_notes
    n_unplanned = count_unplanned_notes(conn)
    if n_unplanned == 0:
        print("No unplanned notes to organize.")
        show_status(conn)
        conn.close()
        return

    print(f"Found {n_unplanned} unplanned notes")
    notes = get_unplanned_notes(conn, limit=200)
    for n in notes:
        print(f"  - {n.content}")

    client = _init_llm(args.model)
    memopedia = _init_memopedia(conn)
    tree = memopedia.get_tree()

    if args.dry_run:
        from sai_memory.memory.note_organizer import (
            _format_memopedia_tree_for_plan,
            _format_notes_for_plan,
            _build_plan_prompt,
            _parse_plan_response,
        )

        tree_text = _format_memopedia_tree_for_plan(tree)
        notes_text = _format_notes_for_plan(notes)
        prompt = _build_plan_prompt(notes_text, tree_text)

        print(f"\n--- Prompt ({len(prompt)} chars) ---")
        print(prompt[:500] + "..." if len(prompt) > 500 else prompt)

        response = client.generate(messages=[{"role": "user", "content": prompt}], tools=[])
        valid_ids = {n.id for n in notes}
        groups = _parse_plan_response(response or "", valid_ids)

        print(f"\n--- Plan Result ({len(groups)} groups) ---")
        for g in groups:
            target = g.get("target_page_id") or g.get("suggested_title") or "?"
            print(f"  [{g['group_label']}] {len(g['note_ids'])} notes → {g['action']} (target: {target})")
            for nid in g["note_ids"]:
                note = next((n for n in notes if n.id == nid), None)
                if note:
                    print(f"    - {note.content}")

        print("\n(dry-run mode, no metadata written)")
    else:
        from sai_memory.memory.note_organizer import plan_notes
        groups = plan_notes(client, conn, tree, persona_id=args.persona_id)
        print(f"\n--- Plan Complete ({len(groups)} groups) ---")
        for g in groups:
            target = g.get("target_page_id") or g.get("suggested_title") or "?"
            print(f"  [{g['group_label']}] {len(g['note_ids'])} notes → {g['action']} (target: {target})")
        print()
        show_status(conn)

    conn.close()


def cmd_run(args):
    """Group notes, assign targets, and write directly to Memopedia."""
    conn = _init_db(args.persona_id)

    from sai_memory.memory.storage import count_unplanned_notes
    n_unplanned = count_unplanned_notes(conn)
    if n_unplanned == 0:
        print("No unplanned notes to organize.")
        show_status(conn)
        conn.close()
        return

    print(f"Found {n_unplanned} unplanned notes")

    client = _init_llm(args.model)
    memopedia = _init_memopedia(conn)

    from sai_memory.memory.note_organizer import organize_notes
    results = organize_notes(client, conn, memopedia, persona_id=args.persona_id)

    print(f"\n--- Results ({len(results)} groups) ---")
    for r in results:
        print(f"  [{r.group_label}] {r.action} → page {r.page_id[:12]}... ({r.note_count} notes)")

    total = sum(r.note_count for r in results)
    print(f"\nTotal: {total} notes resolved")
    print()
    show_status(conn)
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Organize memory notes into Memopedia pages")
    parser.add_argument("persona_id", help="Persona ID (e.g., air_city_a)")
    parser.add_argument("command", choices=["run", "plan", "status"],
                        help="Command: run (group+write), plan (dry-run preview), status")
    parser.add_argument("--model", type=str, default=None, help="Model to use")
    parser.add_argument("--dry-run", action="store_true", help="(plan) Show plan without writing")
    args = parser.parse_args()

    if args.command == "status":
        conn = _init_db(args.persona_id)
        show_status(conn)
        conn.close()
    elif args.command == "plan":
        cmd_plan(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
