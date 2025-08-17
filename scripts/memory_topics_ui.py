#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import sys

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

# Ensure repo root on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

import gradio as gr

from memory_core import MemoryCore
from memory_core.config import Config


def _build_mc(persona_id: str, location_base: Optional[str], collection_prefix: Optional[str]) -> Tuple[MemoryCore, str]:
    cfg = Config.from_env()
    cfg.storage_backend = "qdrant"  # force Qdrant to read per-persona DB

    base = location_base or cfg.qdrant_location or str(Path.home() / ".saiverse" / "qdrant")
    base = os.path.expandvars(os.path.expanduser(base))
    per_loc = str(Path(base) / "persona" / persona_id)
    cfg.qdrant_location = per_loc

    pref = collection_prefix or (cfg.qdrant_collection_prefix or "saiverse")
    cfg.qdrant_collection_prefix = f"{pref}_{persona_id}"

    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=True)
    status = f"DB: {cfg.qdrant_location}\nPrefix: {cfg.qdrant_collection_prefix}\nBackend: {type(mc.storage).__name__}, Embedder: {type(mc.embedder).__name__}"
    return mc, status


def _format_topics(mc: MemoryCore, show_disabled: bool = False) -> Tuple[List[List[str]], List[Tuple[str, str]]]:
    from datetime import datetime
    topics = mc.storage.list_topics()  # type: ignore[attr-defined]
    if not show_disabled:
        topics = [t for t in topics if not getattr(t, "disabled", False)]
    # sort by strength desc then updated_at desc (as timestamp)
    def _ts(x: Optional[datetime]) -> float:
        try:
            return x.timestamp() if isinstance(x, datetime) else 0.0
        except Exception:
            return 0.0
    topics.sort(key=lambda t: (-(float(getattr(t, "strength", 0.0) or 0.0)), _ts(getattr(t, "updated_at", None))))

    rows: List[List[str]] = []
    choices: List[Tuple[str, str]] = []
    for t in topics:
        cnt = len(t.entry_ids) if t.entry_ids else 0
        title = (t.title or "").strip() or "(untitled)"
        strength = f"{float(getattr(t, 'strength', 0.0) or 0.0):.3f}"
        updated_at_obj = getattr(t, "updated_at", None)
        try:
            updated_at = updated_at_obj.isoformat() if updated_at_obj else ""
        except Exception:
            updated_at = str(updated_at_obj) if updated_at_obj else ""

        rows.append([
            t.id,
            title,
            t.summary or "",
            strength,
            str(cnt),
            updated_at,
        ])
        label = f"[{t.id[:8]}] {title} ({cnt})"
        choices.append((label, t.id))
    return rows, choices


def _load_topics(persona_id: str, location_base: str, collection_prefix: str, show_disabled: bool, state: dict):
    if not persona_id.strip():
        return gr.update(value=[]), gr.update(choices=[], value=None), "Persona ID is required", state
    mc, status = _build_mc(persona_id.strip(), location_base or None, collection_prefix or None)
    rows, choices = _format_topics(mc, show_disabled=bool(show_disabled))
    state["cfg"] = {
        "persona_id": persona_id.strip(),
        "location_base": location_base or None,
        "collection_prefix": collection_prefix or None,
        "show_disabled": bool(show_disabled),
    }
    # keep MemoryCore in state so subsequent callbacks reuse it
    state["mc"] = mc
    return rows, gr.update(choices=choices, value=(choices[0][1] if choices else None)), status, state


def _show_topic_paged(topic_id: str, page_size: str | int, page: int, state: dict):
    mc: Optional[MemoryCore] = state.get("mc")
    if not mc or not topic_id:
        return "No topic selected.", [], "", page
    topics = mc.storage.list_topics()  # type: ignore[attr-defined]
    topic = next((t for t in topics if t.id == topic_id), None)
    if not topic:
        return "Topic not found.", [], "", page
    # Build details markdown
    title = (topic.title or "(untitled)").strip()
    summary = topic.summary or ""
    strength = round(float(topic.strength or 0.0), 3)
    total_cnt = len(topic.entry_ids or [])
    md = f"# {title}\n\n- ID: {topic.id}\n- Entries: {total_cnt}\n- Strength: {strength}\n\n**Summary**\n\n{summary}\n\n**Entries**\n"
    # Normalize page_size
    try:
        ps = int(page_size) if not (isinstance(page_size, str) and page_size.upper() == "ALL") else total_cnt
    except Exception:
        ps = 100
    if ps <= 0:
        ps = total_cnt if total_cnt > 0 else 1
    # Collect all entries (we sort by timestamp, then slice)
    all_rows: List[List[str]] = []
    for sid in (topic.entry_ids or []):
        e = mc.storage.get_entry(sid)  # type: ignore[attr-defined]
        if not e:
            continue
        ts = getattr(e, "timestamp", None)
        ts_str = str(ts) if ts else ""
        meta = getattr(e, "meta", {}) or {}
        assign = meta.get("assign_llm") or {}
        backend = assign.get("backend") or ""
        model = assign.get("model") or ""
        status = meta.get("assign_llm_status") or ""
        retries = meta.get("assign_llm_retries")
        retries_str = str(retries) if retries is not None else ""
        all_rows.append([ts_str, e.speaker or "", (e.raw_text or "").strip(), backend, model, status, retries_str])
    try:
        all_rows.sort(key=lambda r: r[0])
    except Exception:
        pass
    # Compute pagination
    total = len(all_rows)
    pages = max(1, (total + ps - 1) // ps) if ps > 0 else 1
    cur = max(1, min(int(page) if page else 1, pages))
    start = (cur - 1) * ps
    end = start + ps
    page_rows = all_rows[start:end]
    info = f"Total {total} entries. Showing {start+1 if total>0 else 0}-{min(end, total)} (page {cur}/{pages}, page size {ps})."
    return md, page_rows, info, cur


with gr.Blocks() as demo:
    gr.Markdown("""
    # Memory Topics Explorer
    Inspect per-persona memory topics stored in Qdrant.
    - Use the same persona_id / location / prefix as used by `ingest_persona_log.py`.
    """)
    with gr.Row():
        persona_id = gr.Textbox(label="Persona ID", placeholder="e.g., eris", scale=2)
        location_base = gr.Textbox(label="Location Base", value=str(Path.home() / ".saiverse" / "qdrant"), scale=3)
        collection_prefix = gr.Textbox(label="Collection Prefix", value="saiverse", scale=2)
        load_btn = gr.Button("Load", variant="primary")
    with gr.Row():
        show_disabled = gr.Checkbox(label="Show disabled topics", value=False)
    status = gr.Markdown("")
    state = gr.State({})

    with gr.Row():
        topics_df = gr.Dataframe(
            headers=["id", "title", "summary", "strength", "entries", "updated_at"],
            row_count=(0), interactive=False
        )
    with gr.Row():
        topic_select = gr.Dropdown(label="Select Topic", choices=[], interactive=True)
    with gr.Row():
        topic_md = gr.Markdown()
    with gr.Row():
        entries_per_page = gr.Dropdown(label="Entries per page", choices=["50", "100", "200", "500", "ALL"], value="100", scale=1)
        current_page = gr.Number(label="Page", value=1, precision=0, minimum=1, scale=1)
        entries_info = gr.Markdown("")
    entries_df = gr.Dataframe(headers=["timestamp", "speaker", "text", "assign_backend", "assign_model", "assign_status", "assign_retries"], row_count=0, interactive=False, wrap=True)

    load_btn.click(_load_topics, inputs=[persona_id, location_base, collection_prefix, show_disabled, state], outputs=[topics_df, topic_select, status, state])
    # Reload topics when toggling disabled visibility
    show_disabled.change(_load_topics, inputs=[persona_id, location_base, collection_prefix, show_disabled, state], outputs=[topics_df, topic_select, status, state])
    topic_select.change(_show_topic_paged, inputs=[topic_select, entries_per_page, current_page, state], outputs=[topic_md, entries_df, entries_info, current_page])
    entries_per_page.change(_show_topic_paged, inputs=[topic_select, entries_per_page, current_page, state], outputs=[topic_md, entries_df, entries_info, current_page])
    current_page.change(_show_topic_paged, inputs=[topic_select, entries_per_page, current_page, state], outputs=[topic_md, entries_df, entries_info, current_page])

if __name__ == "__main__":
    # Gradio will run on http://127.0.0.1:7860 by default
    demo.launch()
