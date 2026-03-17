#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._shared.config import prepare_script_runtime
from scripts.memopedia.build_memopedia_core import run


def main() -> int:
    prepare_script_runtime()
    parser = argparse.ArgumentParser(
        description="Build Memopedia from SAIMemory conversation logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("persona_id", nargs="?", help="Persona ID to process")
    parser.add_argument("--limit", type=int, default=100)
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    parser.add_argument("--model", default=BUILTIN_DEFAULT_LITE_MODEL)
    parser.add_argument("--provider")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--system-prompt", type=str)
    parser.add_argument("--refine-writes", action="store_true")
    parser.add_argument("--export", type=str, metavar="FILE")
    parser.add_argument("--import", type=str, metavar="FILE", dest="import_file")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--thread", type=str, metavar="THREAD_ID")
    parser.add_argument("--with-episode-context", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
