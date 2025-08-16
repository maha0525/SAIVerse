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


def _format_topics(mc: MemoryCore) -> Tuple[List[List[str]], List[Tuple[str, str]]]:
    from datetime import datetime
    topics = mc.storage.list_topics()  # type: ignore[attr-defined]
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


def _load_topics(persona_id: str, location_base: str, collection_prefix: str, state: dict):
    if not persona_id.strip():
        return gr.update(value=[]), gr.update(choices=[], value=None), "Persona ID is required", state
    mc, status = _build_mc(persona_id.strip(), location_base or None, collection_prefix or None)
    rows, choices = _format_topics(mc)
    state["cfg"] = {
        "persona_id": persona_id.strip(),
        "location_base": location_base or None,
        "collection_prefix": collection_prefix or None,
    }
    # keep MemoryCore in state so subsequent callbacks reuse it
    state["mc"] = mc
    return rows, gr.update(choices=choices, value=(choices[0][1] if choices else None)), status, state


def _show_topic(topic_id: str, state: dict):
    mc: Optional[MemoryCore] = state.get("mc")
    if not mc or not topic_id:
        return "No topic selected.", []
    topics = mc.storage.list_topics()  # type: ignore[attr-defined]
    topic = next((t for t in topics if t.id == topic_id), None)
    if not topic:
        return "Topic not found.", []
    # Build details markdown
    title = (topic.title or "(untitled)").strip()
    summary = topic.summary or ""
    strength = round(float(topic.strength or 0.0), 3)
    cnt = len(topic.entry_ids or [])
    md = f"# {title}\n\n- ID: {topic.id}\n- Entries: {cnt}\n- Strength: {strength}\n\n**Summary**\n\n{summary}\n\n**Entries**\n"
    # Collect latest entries and show as a table
    rows: List[List[str]] = []
    for sid in (topic.entry_ids or [])[-100:]:
        e = mc.storage.get_entry(sid)  # type: ignore[attr-defined]
        if not e:
            continue
        ts = getattr(e, "timestamp", None)
        ts_str = str(ts) if ts else ""
        rows.append([ts_str, e.speaker or "", (e.raw_text or "").strip()])
    # sort by timestamp asc
    try:
        from datetime import datetime
        rows.sort(key=lambda r: r[0])
    except Exception:
        pass
    return md, rows


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
    entries_df = gr.Dataframe(headers=["timestamp", "speaker", "text"], row_count=0, interactive=False, wrap=True)

    load_btn.click(_load_topics, inputs=[persona_id, location_base, collection_prefix, state], outputs=[topics_df, topic_select, status, state])
    topic_select.change(_show_topic, inputs=[topic_select, state], outputs=[topic_md, entries_df])

if __name__ == "__main__":
    # Gradio will run on http://127.0.0.1:7860 by default
    demo.launch()
