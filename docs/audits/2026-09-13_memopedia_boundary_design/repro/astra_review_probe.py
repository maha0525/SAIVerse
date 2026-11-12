"""Focused design review: isolated route calls and two missing boundaries.

No product changes; no HTTP server, production data, embeddings or LLM calls.
Original Opus result files are left intact. Run from this worktree's Python path.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile


HERE = Path(__file__).resolve().parent
SCRATCH = Path(tempfile.mkdtemp(prefix="saiverse_astra_memopedia_review_"))
os.environ["SAIVERSE_PROBE_ROOT"] = str(SCRATCH)
os.environ["SAIVERSE_LOG_PATH"] = str(SCRATCH / "tools.log")
sys.path.insert(0, str(HERE))


def refuse_network(*args, **kwargs):
    raise RuntimeError("Network is disabled in this review probe")


socket.create_connection = refuse_network
socket.socket.connect = refuse_network
socket.socket.connect_ex = refuse_network
original_connect = sqlite3.connect


def isolated_connect(database, *args, **kwargs):
    if str(database) != ":memory:":
        path = Path(database).resolve()
        if not path.is_relative_to(SCRATCH.resolve()):
            raise RuntimeError(f"Refused DB outside review scratch: {path}")
    return original_connect(database, *args, **kwargs)


sqlite3.connect = isolated_connect

import probe_env  # noqa: E402

original_setup = probe_env.setup_home


def setup_with_database(name, *, fresh=True):
    # This run has a fresh unique scratch root; never remove another run's files.
    home = original_setup(name, fresh=False)
    db_path = home / "user_data" / "database" / "saiverse.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path):
        pass
    return home


probe_env.setup_home = setup_with_database


def focused_cases():
    import memopedia_fixture as fx

    fx.new_world("astra_additional")
    from api.routes.people.memopedia import export_memopedia, import_memopedia
    from sai_memory.memopedia import Memopedia

    target = fx.ProbePersona("probe_astra_write_failure")
    target.seed(
        core_memories=("Synthetic core A",), trashed_core_memory=None,
        knowledge_title="Original knowledge", with_fragment=False,
        with_desk=False, with_chronicle=False, with_theme=False,
    )
    before = target.observe()
    target.adapter.conn.execute(
        "CREATE TEMP TRIGGER review_fail_insert BEFORE INSERT ON memopedia_pages "
        "WHEN NEW.title = 'Rejected by storage' BEGIN "
        "SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    target.adapter.conn.commit()
    body = {"version": 1, "pages": [
        {"id": "new-ok", "parent_id": "root_terms", "category": "terms",
         "title": "New knowledge", "content": "Valid text"},
        {"id": "new-fail", "parent_id": "root_terms", "category": "terms",
         "title": "Rejected by storage", "content": "Also valid text"},
    ]}
    response = import_memopedia(
        target.persona_id, body=body, clear=True, manager=fx.make_manager(target),
    )
    after = target.observe()
    failure = {"input": body, "response": response,
               "before": before, "after": after,
               "method": "Valid input; SQLite trigger injects a write failure"}
    assert response["success"] is True and response["imported_count"] == 1
    assert all(r["title"] != "Original knowledge" for r in after["保存表の行"])
    target.close()

    source = fx.ProbePersona("probe_astra_page_source")
    source.seed(
        core_memories=(), trashed_core_memory=None, with_desk=False,
        with_chronicle=False, with_theme=True, with_fragment=True,
    )
    destination = fx.ProbePersona("probe_astra_page_destination")
    manager = fx.make_manager(source, destination)
    exported = export_memopedia(source.persona_id, manager=manager)
    assert {p["category"] for p in exported["pages"]} == {"terms", "theme"}
    response = import_memopedia(
        destination.persona_id, body=exported, clear=False, manager=manager,
    )
    theme = destination.adapter.conn.execute(
        "SELECT parent_id, metadata FROM memopedia_pages WHERE category = 'theme'"
    ).fetchone()
    parent_exists = destination.adapter.conn.execute(
        "SELECT 1 FROM memopedia_pages WHERE id = ?", (theme[0],),
    ).fetchone() is not None
    tree = Memopedia(destination.adapter.conn).get_tree()
    tree_has_theme = "テーマページ" in json.dumps(tree, ensure_ascii=False)
    roundtrip = {
        "exported": exported, "response": response,
        "source_counts": source.counts(), "destination_counts": destination.counts(),
        "theme_parent_exists": parent_exists, "theme_metadata": theme[1],
        "theme_in_tree": tree_has_theme,
        "method": "Unmodified export/import routes; only legitimate terms/theme input",
    }
    assert source.counts()["Fragment"] == 1
    assert destination.counts()["Fragment"] == 0
    assert not parent_exists and not tree_has_theme
    source.close()
    destination.close()
    return {"runtime_write_failure": failure, "ordinary_page_roundtrip": roundtrip}


def main():
    results = {"scratch": str(SCRATCH), "scope": "Direct route calls; no HTTP or speech"}
    for name in ("exp1_delete_all", "exp4_invalid_and_partial_failure"):
        results[name] = importlib.import_module(name).main()
    results.update(focused_cases())
    output = HERE / "astra_review_probe_result.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "delete_response": results["exp1_delete_all"]["API の応答 (原文)"],
        "existing_invalid_scenes": len(results["exp4_invalid_and_partial_failure"]["場面"]),
        "valid_input_write_failure": results["runtime_write_failure"]["response"],
        "roundtrip": results["ordinary_page_roundtrip"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
