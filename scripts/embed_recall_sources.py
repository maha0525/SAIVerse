"""Generate embeddings for Chronicle and Memopedia (unified recall sources).

Usage:
    python scripts/embed_recall_sources.py <persona_id>
    python scripts/embed_recall_sources.py <persona_id> --status

Examples:
    python scripts/embed_recall_sources.py air_city_a
    python scripts/embed_recall_sources.py air_city_a --status
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Generate embeddings for unified recall")
    parser.add_argument("persona_id", help="Persona ID (e.g., air_city_a)")
    parser.add_argument("--status", action="store_true", help="Show embedding status only")
    args = parser.parse_args()

    from sai_memory.memory.storage import init_db
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.memopedia import init_memopedia_tables

    db_path = Path.home() / ".saiverse" / "personas" / args.persona_id / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = init_db(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)
    init_memopedia_tables(conn)

    from sai_memory.unified_recall import (
        count_chronicle_embeddings,
        count_memopedia_embeddings,
        get_chronicle_entries_without_embeddings,
        get_memopedia_pages_without_embeddings,
        embed_chronicle_entries,
        embed_memopedia_pages,
    )

    # Status
    chronicle_embedded = count_chronicle_embeddings(conn)
    chronicle_missing = len(get_chronicle_entries_without_embeddings(conn, level=1))
    memopedia_embedded = count_memopedia_embeddings(conn)
    memopedia_missing = len(get_memopedia_pages_without_embeddings(conn))

    print(f"Chronicle Lv1: {chronicle_embedded} embedded, {chronicle_missing} missing")
    print(f"Memopedia:     {memopedia_embedded} embedded, {memopedia_missing} missing")

    if args.status:
        conn.close()
        return

    if chronicle_missing == 0 and memopedia_missing == 0:
        print("\nAll up to date.")
        conn.close()
        return

    # Initialize embedder
    from saiverse_memory import SAIMemoryAdapter
    adapter = SAIMemoryAdapter(args.persona_id)
    if not adapter.can_embed():
        print("Embedding model not available")
        conn.close()
        sys.exit(1)

    embedder = adapter.embedder
    print(f"\nUsing embedder: {embedder.model_name}")

    if chronicle_missing > 0:
        print(f"\nEmbedding {chronicle_missing} Chronicle Lv1 entries...")
        n = embed_chronicle_entries(conn, embedder, level=1)
        print(f"  Done: {n} entries embedded")

    if memopedia_missing > 0:
        print(f"\nEmbedding {memopedia_missing} Memopedia pages...")
        n = embed_memopedia_pages(conn, embedder)
        print(f"  Done: {n} pages embedded")

    print("\nComplete.")
    conn.close()


if __name__ == "__main__":
    main()
