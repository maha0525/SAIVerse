#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from saiverse_memory import SAIMemoryAdapter

LOGGER = logging.getLogger("sai_memory.migrate")

load_dotenv()

PERSONA_ROOT = Path.home() / ".saiverse" / "personas"
CITIES_ROOT = Path.home() / ".saiverse" / "cities"


def iter_persona_dirs(targets: Optional[List[str]]) -> Iterator[Tuple[str, Path]]:
    root = PERSONA_ROOT
    if not root.exists():
        LOGGER.error("Persona directory %s does not exist", root)
        return

    if targets:
        for name in targets:
            path = root / name
            if path.exists():
                yield name, path
            else:
                LOGGER.warning("Persona %s not found under %s", name, root)
        return

    for path in sorted(root.iterdir()):
        if path.is_dir():
            yield path.name, path


def load_messages(persona_dir: Path, include_archives: bool) -> List[dict]:
    msgs: List[dict] = []
    main_path = persona_dir / "log.json"
    if main_path.exists():
        msgs.extend(_read_json_file(main_path))
    else:
        LOGGER.warning("No log.json for persona %s", persona_dir.name)

    if include_archives:
        archive_dir = persona_dir / "old_log"
        if archive_dir.exists():
            for path in sorted(archive_dir.glob("*.json")):
                msgs.extend(_read_json_file(path))

    msgs.sort(key=_message_sort_key)
    return msgs


def load_building_messages(persona_id: str, include_archives: bool) -> List[dict]:
    if not CITIES_ROOT.exists():
        return []

    collected: List[dict] = []
    persona_aliases = _persona_aliases(persona_id)
    for city_dir in CITIES_ROOT.iterdir():
        buildings_dir = city_dir / "buildings"
        if not buildings_dir.exists():
            continue
        for building_dir in buildings_dir.iterdir():
            if not building_dir.is_dir():
                continue
            collected.extend(_load_building_log(building_dir / "log.json", persona_aliases))
            if include_archives:
                old_dir = building_dir / "old_log"
                if old_dir.exists():
                    for path in sorted(old_dir.glob("*.json")):
                        collected.extend(_load_building_log(path, persona_aliases))

    collected.sort(key=_message_sort_key)
    return collected


def _load_building_log(path: Path, persona_aliases: set[str]) -> List[dict]:
    data = _read_json_file(path)
    if not data:
        return []

    out: List[dict] = []
    pending_users: List[dict] = []
    for msg in data:
        role = msg.get("role")
        if role == "user":
            pending_users.append(msg)
        elif role == "assistant" and msg.get("persona_id") in persona_aliases:
            if pending_users:
                out.extend(pending_users)
                pending_users = []
            out.append(msg)
        elif role == "assistant":
            pending_users = []
        else:
            pending_users = []
    return out


def _persona_aliases(persona_id: str) -> set[str]:
    aliases = {persona_id}
    if "_" in persona_id:
        aliases.add(persona_id.split("_", 1)[0])
    return aliases


def _read_json_file(path: Path) -> List[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.error("Failed to read %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        LOGGER.warning("Skipping %s (expected list of messages)", path)
        return []
    return [msg for msg in data if isinstance(msg, dict)]


def _message_sort_key(msg: dict) -> Tuple[float, int]:
    ts = msg.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).timestamp(), 0
        except ValueError:
            pass
    return (0.0, 1)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fill_missing_timestamps(messages: List[dict], default_start: Optional[str]) -> List[dict]:
    out = [dict(m) for m in messages]
    dt_list = [_parse_iso(m.get("timestamp")) for m in out]
    fallback_base = _parse_iso(default_start) or datetime.utcnow()

    if all(dt is None for dt in dt_list):
        for idx, msg in enumerate(out):
            msg["timestamp"] = (fallback_base + timedelta(seconds=idx)).isoformat()
        return out

    idx = 0
    while idx < len(out):
        if dt_list[idx] is None:
            start_idx = idx
            while idx < len(out) and dt_list[idx] is None:
                idx += 1
            end_idx = idx - 1
            prev_dt = dt_list[start_idx - 1] if start_idx > 0 else fallback_base
            next_dt = dt_list[idx] if idx < len(out) else None
            block_len = end_idx - start_idx + 1

            if prev_dt is None and next_dt is None:
                prev_dt = fallback_base - timedelta(seconds=1)
                next_dt = fallback_base + timedelta(seconds=block_len + 1)
            elif prev_dt is None:
                prev_dt = (fallback_base if fallback_base < next_dt else next_dt) - timedelta(seconds=block_len + 1)
            elif next_dt is None:
                next_dt = prev_dt + timedelta(seconds=block_len + 1)

            span = (next_dt - prev_dt).total_seconds()
            step = span / (block_len + 1)
            if step <= 0:
                step = 1.0

            for offset in range(block_len):
                dt = prev_dt + timedelta(seconds=step * (offset + 1))
                out[start_idx + offset]["timestamp"] = dt.isoformat()
                dt_list[start_idx + offset] = dt
        else:
            idx += 1

    return out


def should_skip_existing(adapter: SAIMemoryAdapter, append: bool) -> bool:
    if append:
        return False
    if adapter.conn is None:
        return False
    with adapter._db_lock:  # type: ignore[attr-defined]
        cur = adapter.conn.execute("SELECT COUNT(*) FROM messages")
        count = cur.fetchone()[0]
    if count > 0:
        LOGGER.info("Skipping persona %s because memory.db already has %d messages (use --reset or --append)", adapter.persona_id, count)
        return True
    return False


def _deduplicate_messages(messages: List[dict]) -> List[dict]:
    seen = set()
    unique: List[dict] = []
    for msg in messages:
        key = (
            msg.get("role"),
            msg.get("content"),
            msg.get("timestamp"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(msg)
    unique.sort(key=_message_sort_key)
    return unique


def migrate_persona(
    persona_id: str,
    persona_dir: Path,
    *,
    reset: bool,
    append: bool,
    include_archives: bool,
    include_buildings: bool,
    default_start: Optional[str],
) -> None:
    db_path = persona_dir / "memory.db"
    if reset and db_path.exists():
        db_path.unlink()
        LOGGER.info("Removed existing memory DB for %s", persona_id)

    adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    if not adapter.is_ready():
        LOGGER.warning("SAIMemory adapter not ready for %s; skipping", persona_id)
        return

    if should_skip_existing(adapter, append):
        return

    messages = load_messages(persona_dir, include_archives)
    if include_buildings:
        building_msgs = load_building_messages(persona_id, include_archives)
        if building_msgs:
            messages.extend(building_msgs)

    if not messages:
        LOGGER.info("No messages to import for %s", persona_id)
        return

    messages = _deduplicate_messages(messages)
    if not messages:
        LOGGER.info("No messages to import for %s", persona_id)
        return

    messages = _fill_missing_timestamps(messages, default_start)
    messages.sort(key=_message_sort_key)

    imported = 0
    for msg in messages:
        adapter.append_persona_message(msg)
        imported += 1
    LOGGER.info("Imported %d messages into %s", imported, persona_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import persona log.json into SAIMemory DBs.")
    parser.add_argument("--persona", action="append", dest="personas", help="Persona IDs to import (if omitted, run for all)")
    parser.add_argument("--reset", action="store_true", help="Remove existing memory.db before import")
    parser.add_argument("--append", action="store_true", help="Append even if messages already exist")
    parser.add_argument("--include-archives", action="store_true", help="Include old_log/*.json archives as well")
    parser.add_argument("--include-buildings", action="store_true", help="Also import messages from building logs where the persona speaks")
    parser.add_argument("--default-start", help="Fallback ISO timestamp for earliest messages without timestamps")
    parser.add_argument("--log-level", default="INFO", help="Logging level (default INFO)")
    return parser.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(levelname)s:%(name)s:%(message)s",
    )


if __name__ == "__main__":
    args = parse_args()
    configure_logging(args.log_level)

    for persona_id, persona_dir in iter_persona_dirs(args.personas):
        try:
            migrate_persona(
                persona_id,
                persona_dir,
                reset=args.reset,
                append=args.append,
                include_archives=args.include_archives,
                include_buildings=args.include_buildings,
                default_start=args.default_start,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            LOGGER.exception("Failed to import persona %s: %s", persona_id, exc)
