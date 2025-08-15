from __future__ import annotations

from typing import Dict, List, Optional
import json
from datetime import datetime

from .schemas import Topic
from .llm import LLMClient
from .embeddings import EmbeddingProvider
from .storage import InMemoryStorage


def _sim(a: List[float], b: List[float]) -> float:
    # cosine similarity
    import math

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def assign_topic(
    recent_dialog: List[Dict],
    candidate_topics: List[Topic],
    embedder: EmbeddingProvider,
    threshold: float = 0.35,
) -> Dict:
    """
    Heuristic topic assignment:
    - Embed the concatenated recent dialog and compare to topic centroids.
    - If best similarity >= threshold -> BEST_MATCH; else NEW.
    """

    joined = []
    for turn in recent_dialog[-6:]:
        spk = turn.get("speaker", "?")
        txt = turn.get("text", "")
        joined.append(f"{spk}: {txt}")
    blob = "\n".join(joined)

    query_vec = embedder.embed([blob])[0]

    best_id: Optional[str] = None
    best_score = -1.0
    for t in candidate_topics:
        if t.centroid_embedding:
            s = _sim(query_vec, t.centroid_embedding)
            if s > best_score:
                best_score = s
                best_id = t.id

    if best_id is not None and best_score >= threshold:
        return {
            "decision": "BEST_MATCH",
            "topic_id": best_id,
            "new_topic": None,
            "reason": f"best cosine={best_score:.3f} above threshold",
        }

    # Propose NEW topic title/summary based on last user-like utterance
    last_text = ""
    for turn in reversed(recent_dialog):
        if turn.get("speaker") in ("user", "system", "human"):
            last_text = turn.get("text", "").strip()
            if last_text:
                break
    if not last_text and recent_dialog:
        last_text = recent_dialog[-1].get("text", "")

    title = (last_text[:24] + "…") if len(last_text) > 24 else last_text
    summary = last_text[:160]

    return {
        "decision": "NEW",
        "topic_id": None,
        "new_topic": {"title": title or "新しい話題", "summary": summary or None},
        "reason": "no adequate match found",
    }


def assign_topic_llm(
    recent_dialog: List[Dict],
    candidate_topics: List[Topic],
    llm: LLMClient,
) -> Dict:
    """LLMにプロンプトを渡して決定JSONを返す。"""
    # Build prompt as spec-like
    lines = ["Recent dialog (last N turns):"]
    for turn in recent_dialog[-6:]:
        spk = "U" if turn.get("speaker") in ("user", "human") else "A"
        lines.append(f"- {spk}: {turn.get('text','')}")
    lines.append("Existing topics:")
    for t in candidate_topics:
        summ = t.summary or ""
        lines.append(f"- [id={t.id}] \"{t.title}\" — summary: {summ}")
    lines.append(
        "Task:\n1) Match BEST\n2) Else NEW {title,summary}\nOutput JSON only: {decision, topic_id, new_topic, reason}"
    )
    prompt = "\n".join(lines)
    res = llm.assign_topic(prompt)
    try:
        preview = json.dumps(res, ensure_ascii=False) if isinstance(res, dict) else str(res)
        if len(preview) > 600:
            preview = preview[:600] + "…"
        print("LLM decision raw:\n" + preview)
    except Exception:
        pass
    # Normalize output shape defensively
    try:
        decision = (res or {}).get("decision")
        if decision == "NEW":
            nt = (res or {}).get("new_topic")
            if isinstance(nt, str):
                # Coerce string into {title,summary}
                title = (nt[:24] + "…") if len(nt) > 24 else nt
                res["new_topic"] = {"title": title, "summary": nt[:160]}
            elif nt is None:
                res["new_topic"] = {"title": "新しい話題", "summary": None}
        elif decision == "BEST_MATCH":
            # Ensure topic_id exists; otherwise fall back to NEW
            if not res.get("topic_id"):
                txt = ""
                res = {
                    "decision": "NEW",
                    "topic_id": None,
                    "new_topic": {"title": "新しい話題", "summary": None},
                    "reason": "missing topic_id in BEST_MATCH",
                }
    except Exception:
        # If normalization fails, return a minimal NEW structure
        res = {
            "decision": "NEW",
            "topic_id": None,
            "new_topic": {"title": "新しい話題", "summary": None},
            "reason": "normalization error",
        }
    try:
        norm_preview = json.dumps(res, ensure_ascii=False)
        if len(norm_preview) > 600:
            norm_preview = norm_preview[:600] + "…"
        print("LLM decision normalized:\n" + norm_preview)
    except Exception:
        pass
    return res
