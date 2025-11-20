#!/usr/bin/env python3
"""Process pending task creation requests for all personas."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from persona.tasks.creation import process_all_personas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Override the ~/.saiverse/personas base directory.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    results = process_all_personas(args.base)
    if not results:
        logging.info("No task requests processed.")
        return
    for persona_id, request_ids in results.items():
        logging.info("Persona %s: processed %d request(s): %s", persona_id, len(request_ids), ", ".join(request_ids))


if __name__ == "__main__":
    main()
