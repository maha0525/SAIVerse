#!/usr/bin/env python3
"""CLI entrypoint for Chronicle generation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.arasuji.build_arasuji_core import run_cli


def main() -> None:
    """Run Chronicle CLI."""
    run_cli()


if __name__ == "__main__":
    main()
