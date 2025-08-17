#!/usr/bin/env python3
from __future__ import annotations

"""
Heuristic topic boundary detector for persona logs.

- Reads a log JSON (same format as ingest_persona_log.py expects).
- Embeds each turn (skipping role=system and empty text).
- Computes a boundary score at each position i as the drop in
  consecutive-turn similarity ahead of i versus behind i.
  delta_i = mean(sim[i : i+W]) - mean(sim[i-W : i]) where
  sim[j] = cos(emb[j], emb[j+1]).
- Picks boundaries by threshold (mean + alpha*std) or by top-K with
  a minimal segment length constraint.

Outputs a list of boundary indices (1-based). Use --json to emit JSON.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from memory_core import MemoryCore  # noqa: E402
from memory_core.config import Config  # noqa: E402


def _read_log(persona_id: Optional[str], file_path: Optional[str]) -> List[Dict]:
    if file_path:
        home = Path(os.path.expanduser(os.path.expandvars(file_path)))
    else:
        if not persona_id:
            raise ValueError("persona_id or --file is required")
        home = Path.home() / ".saiverse" / "personas" / persona_id / "log.json"
    if not home.exists():
        raise FileNotFoundError(f"Persona log not found: {home}")
    data = json.loads(home.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("log"), list):
        return data["log"]
    raise RuntimeError("Unsupported log format")


def _cos(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def detect_boundaries(
    texts: List[str],
    embedder,
    win: int = 5,
    alpha: float = 1.0,
    min_seg: int = 15,
    topk: Optional[int] = None,
) -> Tuple[List[int], List[float]]:
    """Return boundary indices (positions where next segment starts), 1-based.
    Also return raw delta scores (length N-1 with None at edges via -inf).
    """
    n = len(texts)
    if n <= 2:
        return [], []
    # E5最適化: 'passage: ' プレフィックス（埋め込み種類で分岐しない簡易版）
    etexts = [f"passage: {t}" for t in texts]
    vecs = embedder.embed(etexts)
    # Adjacent similarity
    sim = []  # length n-1
    for i in range(n - 1):
        sim.append(_cos(vecs[i], vecs[i + 1]))
    # Delta across a window: drop ahead vs behind
    import math
    W = max(1, int(win))
    deltas: List[float] = [float("-inf")] * (n - 1)
    for i in range(W, (n - 1) - W):
        prev_mean = sum(sim[i - W : i]) / float(W)
        next_mean = sum(sim[i : i + W]) / float(W)
        deltas[i] = prev_mean - next_mean
    # Threshold or top-k with segment constraints
    # Compute stats on finite deltas
    finite_vals = [d for d in deltas if math.isfinite(d)]
    mu = sum(finite_vals) / len(finite_vals) if finite_vals else 0.0
    var = sum((d - mu) ** 2 for d in finite_vals) / len(finite_vals) if finite_vals else 0.0
    sd = math.sqrt(max(0.0, var))

    candidates = list(range(W, (n - 1) - W))
    candidates.sort(key=lambda i: deltas[i], reverse=True)

    accepted: List[int] = []  # store positions in [0..n-2] (between i and i+1)
    def _ok_position(pos: int) -> bool:
        # Enforce minimal segment length between consecutive boundaries
        prev_cut = 0 if not accepted else (accepted[-1] + 1)
        # We can't ensure future distance here in a simple greedy; we'll filter later
        return True

    if topk is not None and topk > 0:
        for i in candidates:
            if not _ok_position(i):
                continue
            accepted.append(i)
            if len(accepted) >= topk:
                break
    else:
        thr = mu + alpha * sd
        for i in candidates:
            if deltas[i] >= thr and _ok_position(i):
                accepted.append(i)

    # Enforce min segment length globally: start=0, cuts at accepted, end=n-1
    cuts = [c for c in sorted(accepted)]
    final_cuts: List[int] = []
    last_start = 0
    for c in cuts:
        if c + 1 - last_start >= min_seg:
            final_cuts.append(c)
            last_start = c + 1
    # Optionally ensure tail is not too short; if so, drop last boundary
    if final_cuts and (n - 1) - (final_cuts[-1] + 1) < max(3, min_seg // 2):
        final_cuts.pop()

    # Convert to 1-based message index for "next segment start"
    next_starts = [c + 2 for c in final_cuts]  # e.g., boundary between i and i+1 => start at i+2 (1-based)
    return next_starts, deltas


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect topic boundaries in a persona log via heuristics.")
    ap.add_argument("persona_id", nargs="?", help="Persona ID (used if --file omitted)")
    ap.add_argument("--file", default=None, help="Explicit path to log.json (overrides persona_id)")
    ap.add_argument("--start", type=int, default=1, help="1-based start index in log (default: 1)")
    ap.add_argument("--limit", type=int, default=None, help="Max number of items to process")
    ap.add_argument("--win", type=int, default=5, help="Window size for boundary scoring (default: 5)")
    ap.add_argument("--alpha", type=float, default=1.0, help="Threshold: mean + alpha*std (ignored if --topk)")
    ap.add_argument("--min-seg", type=int, default=15, help="Minimum segment length in units (default: 15). A unit is normally one message, but 'user→assistant' is grouped into one unit.")
    ap.add_argument("--topk", type=int, default=None, help="Pick top-K boundaries instead of threshold")
    ap.add_argument("--json", action="store_true", help="Emit JSON to stdout instead of text")
    ap.add_argument("--out", default=None, help="Write detailed boundary report (text) to this file")
    ap.add_argument("--context", type=int, default=3, help="Number of messages to show before/after each boundary (default: 3)")
    ap.add_argument("--max-chars", type=int, default=240, help="Max characters per message line in report (default: 240)")
    args = ap.parse_args()

    raw = _read_log(args.persona_id, args.file)
    # Filter and slice (skip system/empty)
    filtered: List[Tuple[int, str, str]] = []  # (orig_index_1based, role, text)
    for i, m in enumerate(raw, start=1):
        role = (m.get("role") or "").lower()
        if role == "system":
            continue
        text = m.get("content") or ""
        if not text.strip():
            continue
        filtered.append((i, role, text))
    if not filtered:
        print("No usable messages found.")
        return
    start_idx = max(0, (args.start or 1) - 1)
    end_idx = start_idx + args.limit if args.limit is not None else len(filtered)
    sliced = filtered[start_idx:end_idx]
    # Prepare embedder
    cfg = Config.from_env()
    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=False, llm_backend=None)
    # Build units: group (user|human) followed by (assistant|ai|bot) into a single unit
    def _role_tag(r: str) -> str:
        r = (r or "").lower()
        if r in ("user", "human"): return "U"
        if r in ("assistant", "ai", "bot"): return "A"
        return r[:1].upper() or "?"

    units: List[Dict] = []
    i = 0
    while i < len(sliced):
        idx1, role1, text1 = sliced[i]
        # Try to pair U -> A
        if i + 1 < len(sliced):
            idx2, role2, text2 = sliced[i + 1]
            if role1 in ("user", "human") and role2 in ("assistant", "ai", "bot"):
                combined = f"U: {text1}\nA: {text2}"
                units.append({
                    "start_slice_idx": i + 1,
                    "end_slice_idx": i + 2,
                    "start_orig_idx": idx1,
                    "end_orig_idx": idx2,
                    "text": combined,
                })
                i += 2
                continue
        # Single unit
        combined = f"{_role_tag(role1)}: {text1}"
        units.append({
            "start_slice_idx": i + 1,
            "end_slice_idx": i + 1,
            "start_orig_idx": idx1,
            "end_orig_idx": idx1,
            "text": combined,
        })
        i += 1

    texts = [u["text"] for u in units]
    next_starts_units, deltas = detect_boundaries(
        texts, mc.embedder, win=args.win, alpha=args.alpha, min_seg=args.min_seg, topk=args.topk
    )
    # Map boundaries back to original indices
    boundaries = []
    for ns_u in next_starts_units:
        if 1 <= ns_u <= len(units):
            u = units[ns_u - 1]
            start_slice_msg = u["start_slice_idx"]
            orig_idx = u["start_orig_idx"]
            boundaries.append({
                "segment_start_in_slice": start_slice_msg,
                "segment_start_in_original": orig_idx,
                "score": deltas[ns_u - 2] if 2 <= ns_u <= len(deltas) + 1 else None,
            })

    if args.json:
        print(json.dumps({
            "count": len(boundaries),
            "boundaries": boundaries,
            "params": {"win": args.win, "alpha": args.alpha, "min_seg": args.min_seg, "topk": args.topk},
        }, ensure_ascii=False, indent=2))
    # Detailed file report (context before/after)
    if args.out:
        def _fmt_line(idx1: int, role: str, text: str) -> str:
            tag = "U" if role in ("user", "human") else ("A" if role in ("assistant", "ai", "bot") else role[:1].upper())
            t = (text or "").strip().replace("\n", " ")
            if args.max_chars and len(t) > args.max_chars:
                t = t[: args.max_chars] + "…"
            return f"[orig#{idx1:>4}] {tag}: {t}"

        lines: List[str] = []
        lines.append(f"Boundary Report\nTotal usable messages: {len(sliced)} (filtered total {len(filtered)})")
        lines.append(f"Detected boundaries: {len(boundaries)}  params(win={args.win}, alpha={args.alpha}, min_seg={args.min_seg}, topk={args.topk})")
        lines.append("")
        C = max(0, int(args.context))
        for bi, b in enumerate(boundaries, 1):
            si = b["segment_start_in_slice"]
            oi = b["segment_start_in_original"]
            sc = b.get("score")
            lines.append(f"=== Boundary #{bi}  start_at_slice#{si} (orig#{oi})  score={sc:.3f}" if sc is not None else f"=== Boundary #{bi}  start_at_slice#{si} (orig#{oi})")
            # Before context
            start_ctx = max(1, si - C)
            if start_ctx <= si - 1:
                lines.append("-- Before --")
                for k in range(start_ctx, si):
                    idx1, role, text = sliced[k - 1]
                    lines.append(_fmt_line(idx1, role, text))
            # After context
            end_ctx = min(len(sliced), si + C - 1)
            lines.append("-- After --")
            for k in range(si, end_ctx + 1):
                idx1, role, text = sliced[k - 1]
                lines.append(_fmt_line(idx1, role, text))
            lines.append("")
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not args.json and not args.out:
        print(f"Messages (usable) in slice: {len(sliced)} (from filtered total {len(filtered)})  |  Units: {len(units)}")
        print(f"Detected boundaries: {len(boundaries)}")
        for b in boundaries:
            si = b["segment_start_in_slice"]
            oi = b["segment_start_in_original"]
            sc = b["score"]
            print(f"- start_at_slice#{si} (orig#{oi}) score={sc:.3f}" if sc is not None else f"- start_at_slice#{si} (orig#{oi})")


if __name__ == "__main__":
    main()
