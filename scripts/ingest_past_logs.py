#!/usr/bin/env python3
from __future__ import annotations

"""
Ingest existing conversation logs into Cognee-based long-term memory.

Usage examples:
  python scripts/ingest_past_logs.py --persona-id alice --file /path/to/log.json
  python scripts/ingest_past_logs.py --persona-id alice --file ./chat.ndjson --start 101 --end 200

Notes:
- Expects JSON array of messages or NDJSON. Each item should have at least
  {"role": "user|assistant|system", "content": "..."}. System messages are skipped.
- If the file is plain text, non-empty lines are treated as user messages.
- Per-persona storage is handled inside CogneeMemory; ensure Gemini/OpenAI keys
  are available in the environment. Prefer Gemini with LLM_PROVIDER=gemini.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Iterable
import re
import io

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional
    def load_dotenv(*args, **kwargs):
        return False


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from integrations.cognee_memory import CogneeMemory  # noqa: E402


def _suppress_logs():
    """Reduce noisy third-party logs early at script start.
    Sets env var used by our Cognee adapter and clamps common loggers.
    """
    import logging
    os.environ.setdefault("SAIVERSE_THIRDPARTY_LOG_LEVEL", "ERROR")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    os.environ.setdefault("LITELLM_LOGGING", "FALSE")
    for name in (
        "litellm",
        "httpx",
        "httpcore",
        "urllib3",
        "cognee",
        "cognee.shared.logging_utils",
    ):
        try:
            lg = logging.getLogger(name)
            lg.setLevel(logging.ERROR)
            lg.propagate = False
        except Exception:
            pass


class _FilteredStderr(io.TextIOBase):
    def __init__(self, underlying, patterns: list[re.Pattern[str]]):
        self._u = underlying
        self._buf = ""
        self._pats = patterns
        self._ansi = re.compile(r"\x1B\[[0-9;?]*[A-Za-z]")
        self._redact = re.compile(r"(api_key|Authorization)=([^ ,]+)")
        self._drop_box = re.compile(r"[\u2500-\u257F╭╮╯╰│─]+")
        self._drop_paths = re.compile(r"(/site-packages/|^\s*File \"|^\s*\/home\/|^\s*During handling of the above)")
        self._drop_trace = re.compile(r"^\s*Traceback \(most recent call last\):", re.IGNORECASE)
    def writable(self):
        return True
    def write(self, s):
        self._buf += s
        out = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            plain = self._ansi.sub("", line)
            # Drop verbose trace frames & box-drawing stacks, keep high-level summaries
            if self._drop_trace.search(plain) or self._drop_box.search(plain) or self._drop_paths.search(plain):
                continue
            if not any(p.search(plain) for p in self._pats):
                safe_line = self._redact.sub(r"\1=REDACTED", line)
                out.append(safe_line + "\n")
        if out:
            self._u.write(''.join(out))
        return len(s)
    def flush(self):
        if self._buf:
            plain = self._ansi.sub("", self._buf)
            if not (self._drop_trace.search(plain) or self._drop_box.search(plain) or self._drop_paths.search(plain)) and not any(p.search(plain) for p in self._pats):
                self._u.write(self._redact.sub(r"\1=REDACTED", self._buf))
        self._buf = ""
        self._u.flush()


def _read_json_array(path: Path) -> Optional[List[Dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("log"), list):
                return data["log"]
            if isinstance(data.get("messages"), list):
                return data["messages"]
    except Exception:
        return None
    return None


def _read_ndjson(path: Path) -> Optional[List[Dict]]:
    items: List[Dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    return None
                if isinstance(obj, dict):
                    items.append(obj)
                else:
                    return None
        return items
    except Exception:
        return None


def _read_plain_lines(path: Path) -> List[Dict]:
    out: List[Dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            # default to user lines; simple heuristics for prefixes
            role = "user"
            low = text.lower()
            if low.startswith("assistant:") or low.startswith("ai:"):
                role = "assistant"
                text = text.split(":", 1)[1].strip()
            elif low.startswith("user:"):
                role = "user"
                text = text.split(":", 1)[1].strip()
            out.append({"role": role, "content": text})
    return out


def read_log_any(path: str) -> List[Dict]:
    p = Path(os.path.expanduser(os.path.expandvars(path)))
    if not p.exists():
        raise FileNotFoundError(f"Log file not found: {p}")

    # Try JSON array or wrapped formats
    arr = _read_json_array(p)
    if arr is not None:
        return arr

    # Try NDJSON
    nd = _read_ndjson(p)
    if nd is not None:
        return nd

    # Fallback: plain text lines
    return _read_plain_lines(p)


def slice_items(items: List[Dict], start: int, end: Optional[int], limit: Optional[int]) -> tuple[List[Dict], int, int, int]:
    n = len(items)
    s_idx = max(0, start - 1)
    if end is not None:
        e_idx = min(n, end)
        # end is 1-based inclusive
        e_idx = max(s_idx, e_idx)
    elif limit is not None:
        e_idx = min(n, s_idx + max(0, limit))
    else:
        e_idx = n
    return items[s_idx:e_idx], s_idx, e_idx, n


def iter_messages(items: Iterable[Dict]) -> Iterable[tuple[str, str]]:
    for obj in items:
        role = (obj.get("role") or "").strip().lower()
        if role == "system":
            continue
        text = (obj.get("content") or obj.get("text") or "").strip()
        if not text:
            continue
        speaker = "user" if role in ("user", "human") else "ai"
        yield speaker, text


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest past logs into Cognee memory per persona.")
    ap.add_argument("--persona-id", required=True, help="Persona ID for per-persona storage")
    ap.add_argument("--file", required=True, help="Path to log file (JSON array, NDJSON, or plain text)")
    ap.add_argument("--conv-id", default=None, help="Conversation id tag (default: import:<file_basename>)")
    ap.add_argument("--start", type=int, default=1, help="1-based start index in log (default: 1)")
    ap.add_argument("--end", type=int, default=None, help="1-based end index (inclusive). Incompatible with --limit")
    ap.add_argument("--limit", type=int, default=None, help="Max number of items to process from start")
    ap.add_argument("--dry-run", action="store_true", help="Only print what would be processed")
    ap.add_argument("--wait-cognify", action="store_true", help="Ingest後にcognifyを同期実行して完了を待つ")
    ap.add_argument("--quiet", action="store_true", help="サードパーティの冗長ログを抑制する（stderrフィルタ含む）")
    ap.add_argument("--verify-lance", action="store_true", help="LanceDBの総レコード件数を前後で表示して確認する")
    args = ap.parse_args()

    # Prefer Gemini if key is available and provider not explicitly set
    if not os.getenv("LLM_PROVIDER") and (os.getenv("GEMINI_FREE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        os.environ["LLM_PROVIDER"] = "gemini"

    if args.quiet:
        _suppress_logs()
        # Install stderr filter for noisy prints
        noisy = [
            r"Give Feedback / Get Help: https?://github.com/BerriAI/litellm/issues/new",
            r"LiteLLM\.Info: If you need to debug this error, use `litellm\._turn_on_debug\(\)`\.",
            r"LiteLLM completion\(\) model= ",
            r"EmbeddingRateLimiter initialized",
            r"Langfuse client is disabled",
            r"Pipeline run (started|completed):",
            r"Coroutine task (started|completed):",
            r"Ontology file 'None' not found\. No owl ontology will be attached to the graph\.",
            r"\[info\s*\].*extract_graph_from_data",
            r"\[info\s*\].*extract_chunks_from_documents",
            r"\[info\s*\].*resolve_data_directories",
            r"\[info\s*\].*ingest_data",
            r"\[info\s*\].*check_permissions_on_dataset",
            r"\[info\s*\].*classify_documents",
        ]
        pats = [re.compile(x) for x in noisy]
        sys.stderr = _FilteredStderr(sys.stderr, pats)  # type: ignore[assignment]
    items = read_log_any(args.file)

    # Optional: capture LanceDB counts before ingest
    pre_counts = None
    if args.verify_lance:
        try:
            pre_counts = _lancedb_counts(args.persona_id)
            path_used = pre_counts.get('path') or '?'
            print(f"LanceDB(before): total_rows={pre_counts.get('total', '?')} tables={pre_counts.get('tables', {})}")
            print(f"LanceDB path: {path_used}")
        except Exception as e:
            print(f"WARN: failed to read LanceDB before ingest: {e}")
    sliced, s_idx, e_idx, total = slice_items(items, args.start, args.end, args.limit)
    file_base = Path(args.file).name
    conv_id = args.conv_id or f"import:{file_base}"

    print(f"File: {args.file}")
    print(f"Persona: {args.persona_id}")
    print(f"Slice: [{s_idx+1}..{e_idx}] of total {total}")
    print(f"Conversation tag: {conv_id}")

    if args.dry_run:
        kept = sum(1 for _ in iter_messages(sliced))
        print(f"Dry-run: would ingest {kept} messages")
        return

    mem = CogneeMemory(args.persona_id)

    ingested = 0
    for local_idx, (speaker, text) in enumerate(iter_messages(sliced), start=s_idx + 1):
        mem.remember(text=text, conv_id=conv_id, speaker=speaker, meta={"source": args.file})
        ingested += 1
        if ingested % 50 == 0:
            print(f".. {ingested} messages ingested (last index {local_idx})")

    if ingested > 0 and args.wait_cognify:
        print("Finalizing (cognify) ... this may take a while")
        try:
            mem.finalize(wait=True)
        except Exception as e:
            print(f"WARN: finalize failed: {e}")
    print(f"Done. Ingested {ingested} messages into persona:{args.persona_id}")

    # Optional: capture LanceDB counts after ingest
    if args.verify_lance:
        try:
            post_counts = _lancedb_counts(args.persona_id)
            path_used = post_counts.get('path') or '?'
            print(f"LanceDB(after): total_rows={post_counts.get('total', '?')} tables={post_counts.get('tables', {})}")
            print(f"LanceDB path: {path_used}")
        except Exception as e:
            print(f"WARN: failed to read LanceDB after ingest: {e}")


def _lancedb_counts(persona_id: str) -> Dict[str, object]:
    """Return LanceDB table counts, trying persona-scoped path first then common defaults.

    This helps when Cognee stores DBs under its default root (~/.cognee) instead of the
    persona-scoped directory.
    """
    import lancedb  # type: ignore

    db_roots = [
        Path.home() / ".saiverse" / "personas" / persona_id / "cognee_system" / "databases",
        Path.home() / ".cognee" / "databases",
        Path.home() / ".saiverse" / "cognee" / "databases",
    ]
    # include Cognee package-scoped databases dir if present
    try:
        import importlib
        mod = importlib.import_module("cognee")
        pkg_root = Path(getattr(mod, "__file__", "")).resolve().parent / ".cognee_system" / "databases"
        db_roots.append(pkg_root)
    except Exception:
        pass
    # Build candidate directories: known default + any dir containing 'lance'
    candidates = []
    for root in db_roots:
        if not root.exists():
            continue
        # standard path
        candidates.append(root / "cognee.lancedb")
        # scan for any directory that looks like a lance db
        try:
            for child in root.iterdir():
                try:
                    if child.is_dir() and "lance" in child.name.lower():
                        candidates.append(child)
                except Exception:
                    continue
        except Exception:
            pass
    tried: list[tuple[str, Dict[str, object]]] = []
    for base in candidates:
        try:
            db = lancedb.connect(str(base))
            names = []
            try:
                names = db.table_names()
            except Exception:
                names = []
            tables: Dict[str, int] = {}
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
            # prefer the first with any content; otherwise keep trying
            tried.append((str(base), res))
            if total > 0 or tables:
                return res
        except Exception:
            continue
    # Fallback to the first attempted path info, even if empty
    # choose any first tried or fallback to the first constructed candidate path
    fallback = tried[0][1] if tried else None
    if fallback:
        return fallback
    first = None
    for c in candidates:
        first = c
        break
    return {"total": 0, "tables": {}, "path": str(first) if first else "<none>"}


if __name__ == "__main__":
    main()
