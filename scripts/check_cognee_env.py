#!/usr/bin/env python3
from __future__ import annotations

"""
Quickly check if .env is loaded and Cognee config is visible from inside scripts.

Usage (one line):
  python scripts/check_cognee_env.py --persona-id eris_city_a --print-lancedb
  python scripts/check_cognee_env.py --persona-id eris_city_a --probe-add --print-lancedb

Notes:
  - --probe-add will ingest a tiny test line and call finalize(wait=True).
  - No secrets are printed. Only presence flags and resolved config are shown.
"""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from integrations.cognee_memory import CogneeMemory  # noqa: E402


def _cognee_pkg_db_root() -> Path | None:
    """Return Cognee package-scoped databases directory if present.

    Typically something like:
      <venv>/site-packages/cognee/.cognee_system/databases
    """
    try:
        import importlib
        mod = importlib.import_module("cognee")
        p = Path(getattr(mod, "__file__", "")).resolve().parent
        cand = p / ".cognee_system" / "databases"
        return cand if cand.exists() else cand  # return even if absent (for printing)
    except Exception:
        return None


def lancedb_counts(persona_id: str) -> Dict[str, object]:
    import lancedb  # type: ignore
    db_roots = [
        Path.home() / ".saiverse" / "personas" / persona_id / "cognee_system" / "databases",
        Path.home() / ".cognee" / "databases",
        Path.home() / ".saiverse" / "cognee" / "databases",
    ]
    pkg_root = _cognee_pkg_db_root()
    if pkg_root is not None:
        db_roots.append(pkg_root)
    # Build candidate directories: known default + any dir containing 'lance'
    candidates = []
    for root in db_roots:
        if not root.exists():
            continue
        candidates.append(root / "cognee.lancedb")
        try:
            for child in root.iterdir():
                try:
                    if child.is_dir() and "lance" in child.name.lower():
                        candidates.append(child)
                except Exception:
                    continue
        except Exception:
            pass
    tried = []
    for base in candidates:
        try:
            db = lancedb.connect(str(base))
            names = []
            try:
                names = db.table_names()
            except Exception:
                names = []
            tables = {}
            total = 0
            for n in names:
                try:
                    t = db.open_table(n)
                    c = t.count_rows()
                    tables[n] = int(c)
                    total += int(c)
                except Exception:
                    continue
            res = {"total": total, "tables": tables, "path": str(base)}
            tried.append(res)
            if total > 0 or tables:
                return res
        except Exception:
            continue
    return tried[0] if tried else {"total": 0, "tables": {}, "path": str(db_roots[0] / 'cognee.lancedb')}


def main() -> None:
    ap = argparse.ArgumentParser(description="Check .env and Cognee visibility from inside Python")
    ap.add_argument("--persona-id", required=True)
    ap.add_argument("--probe-add", action="store_true", help="Ingest a tiny test line and finalize")
    ap.add_argument("--print-lancedb", action="store_true", help="Print LanceDB counts before/after")
    ap.add_argument("--try-recall", action="store_true", help="Run a small recall for 'PROBE' and show hit count")
    ap.add_argument("--scan-lancedb", action="store_true", help="Scan common directories for any *lance* db and print tables")
    ap.add_argument("--print-backend", action="store_true", help="Print Cognee vector backend/embedding config if available")
    ap.add_argument("--print-rel", action="store_true", help="Print relational DB path/config")
    ap.add_argument("--debug", action="store_true", help="Enable INFO logs for this run")
    args = ap.parse_args()

    if args.debug:
        import logging
        os.environ["SAIVERSE_THIRDPARTY_LOG_LEVEL"] = os.getenv("SAIVERSE_THIRDPARTY_LOG_LEVEL", "INFO")
        logging.basicConfig(level=logging.INFO)

    # Show presence flags from current process env
    print("ENV loaded?", bool(os.getenv("GEMINI_FREE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY")))
    print("LLM_PROVIDER=", os.getenv("LLM_PROVIDER"))
    print("SAIVERSE_COGNEE_GEMINI_MODEL=", os.getenv("SAIVERSE_COGNEE_GEMINI_MODEL"))
    print("SAIVERSE_COGNEE_GEMINI_EMBED_MODEL=", os.getenv("SAIVERSE_COGNEE_GEMINI_EMBED_MODEL"))
    print("SAIVERSE_COGNEE_AUTOCG=", os.getenv("SAIVERSE_COGNEE_AUTOCG"))

    m = CogneeMemory(args.persona_id)
    env = m._provider_env()  # type: ignore[attr-defined]
    print("provider_env loaded?", bool(env))
    if env:
        print("provider=", env.get("LLM_PROVIDER"))
        print("model=", env.get("LLM_MODEL"))
        print("embed_model=", env.get("EMBEDDING_MODEL"))
        print("system_root=", env.get("SYSTEM_ROOT_DIRECTORY"))
        print("data_root=", env.get("DATA_ROOT_DIRECTORY"))

    if args.print_lancedb:
        pre = lancedb_counts(args.persona_id)
        print("LanceDB(before): total_rows=", pre.get("total"), "tables=", pre.get("tables"))
        print("LanceDB path:", pre.get("path"))

    if args.probe_add:
        text = f"PROBE {datetime.now().isoformat()}"
        m.remember(text=text, conv_id="probe", speaker="user", meta={"source": "check_cognee_env.py"})
        m.finalize(wait=True)
        print("probe_add: done")
        try:
            dbg = m.get_debug()
            print("last_debug:", {k: dbg.get(k) for k in ("provider", "model", "embedding_model", "phase")})
        except Exception:
            pass

    if args.try_recall:
        bundle = m.recall("PROBE", k=5)
        texts = bundle.get("texts", []) if isinstance(bundle, dict) else []
        print("recall('PROBE') hits:", len(texts))
        for t in texts[:3]:
            print(" -", (t or "").strip()[:120])

    if args.scan_lancedb:
        roots = [
            Path.home() / ".saiverse" / "personas" / args.persona_id / "cognee_system" / "databases",
            Path.home() / ".cognee" / "databases",
            Path.home() / ".saiverse" / "cognee" / "databases",
        ]
        pkg_root = _cognee_pkg_db_root()
        if pkg_root is not None:
            roots.append(pkg_root)
        for root in roots:
            print("Scan root:", str(root))
            for p in (list(root.iterdir()) if root.exists() else []):
                try:
                    if p.is_dir() and ("lance" in p.name.lower() or p.suffix == ".lancedb"):
                        import lancedb  # type: ignore
                        try:
                            db = lancedb.connect(str(p))
                            names = db.table_names()
                        except Exception:
                            names = []
                        print("  LanceDB path:", str(p), "tables:", names)
                except Exception as e:
                    print("  Scan error:", p, e)

    if args.print_backend:
        try:
            import importlib
            b_mod = importlib.import_module("cognee.base_config")
            get_base_config = getattr(b_mod, "get_base_config", None)
            if callable(get_base_config):
                bcfg = get_base_config()
                print("base_config.system_root_directory:", getattr(bcfg, "system_root_directory", None))
                print("base_config.data_root_directory:", getattr(bcfg, "data_root_directory", None))
            emb_cfg_mod = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
            get_embedding_config = getattr(emb_cfg_mod, "get_embedding_config", None)
            if callable(get_embedding_config):
                cfg = get_embedding_config()
                print("embedding_config:", cfg)
            v_mod = importlib.import_module("cognee.infrastructure.databases.vector.config")
            get_vectordb_config = getattr(v_mod, "get_vectordb_config", None)
            if callable(get_vectordb_config):
                vcfg = get_vectordb_config()
                print("vector_config.url:", getattr(vcfg, "vector_db_url", None))
        except Exception as e:
            print("embedding_config error:", e)
        try:
            import importlib
            adapter_mod = importlib.import_module("cognee.infrastructure.databases.vector.lancedb.LanceDBAdapter")
            Adapter = getattr(adapter_mod, "LanceDBAdapter", None)
            print("vector_backend: LanceDBAdapter available?", bool(Adapter))
        except Exception as e:
            print("vector_backend detect error:", e)

    if args.print_rel:
        try:
            import importlib
            rel_mod = importlib.import_module("cognee.infrastructure.databases.relational.config")
            get_relational_config = getattr(rel_mod, "get_relational_config")
            rcfg = get_relational_config()
            print("relational.db_path:", getattr(rcfg, "db_path", None))
            print("relational.db_name:", getattr(rcfg, "db_name", None))
            print("relational.provider:", getattr(rcfg, "db_provider", None))
        except Exception as e:
            print("relational_config error:", e)

    if args.print_lancedb:
        post = lancedb_counts(args.persona_id)
        print("LanceDB(after): total_rows=", post.get("total"), "tables=", post.get("tables"))
        print("LanceDB path:", post.get("path"))


if __name__ == "__main__":
    main()
