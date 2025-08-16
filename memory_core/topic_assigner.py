from __future__ import annotations

from typing import Dict, List, Optional, Tuple
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
    # Avoid empty/generic fallbacks
    if not title:
        title = "会話トピック"
    summary = last_text[:160] if last_text else None

    return {
        "decision": "NEW",
        "topic_id": None,
        "new_topic": {"title": title, "summary": summary},
        "reason": "no adequate match found",
    }


def _derive_fallback_title(recent_dialog: List[Dict]) -> str:
    last_text = ""
    for turn in reversed(recent_dialog):
        if turn.get("speaker") in ("user", "human", "ai", "assistant"):  # prefer human, but accept any
            last_text = (turn.get("text", "") or "").strip()
            if last_text:
                break
    if not last_text and recent_dialog:
        last_text = (recent_dialog[-1].get("text", "") or "").strip()
    title = (last_text[:24] + "…") if last_text and len(last_text) > 24 else (last_text or "会話トピック")
    return title


def _is_generic_title(title: Optional[str]) -> bool:
    if not title:
        return True
    t = (title or "").strip().lower()
    if not t:
        return True
    generics = {"新しい話題", "new topic", "topic", "null", "なし", "n/a", "na", "misc", "general"}
    return t in generics


def assign_topic_llm(
    recent_dialog: List[Dict],
    candidate_topics: List[Topic],
    llm: LLMClient,
) -> Dict:
    """LLMにプロンプトを渡して決定JSONを返す。"""
    # Build prompt as spec-like
    lines = [
        "You are a topic assigner. Produce compact, meaningful Japanese titles.",
        "Constraints:",
        "- If NEW, title must be specific to the dialog (<= 24 chars).",
        "- Do not output generic titles like '新しい話題', 'New topic', 'null', 'topic', 'misc'.",
        "- 'summary' should be <=160 chars and informative.",
        "- If unsure, compose a concise label from key nouns/phrases in the latest user utterance.",
        "- Output JSON ONLY with keys: decision, topic_id, new_topic, reason.",
        "",
        "Recent dialog (last N turns):",
    ]
    for turn in recent_dialog[-6:]:
        spk = "U" if turn.get("speaker") in ("user", "human") else "A"
        lines.append(f"- {spk}: {turn.get('text','')}")
    lines.append("Existing topics:")
    for t in candidate_topics:
        summ = t.summary or ""
        title = t.title or "(untitled)"
        lines.append(f"- [id={t.id}] \"{title}\" — summary: {summ}")
    lines.append(
        "Task:\n1) If matches an existing topic id -> decision='BEST_MATCH' + topic_id.\n2) Else -> decision='NEW' + new_topic={title,summary}.\nJSON only: {decision, topic_id, new_topic, reason}"
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
    def _normalize_and_validate(r: Dict, strict: bool = False) -> Tuple[Dict, bool]:
        """Normalize LLM output and validate title. Return (res, ok)."""
        try:
            decision = (r or {}).get("decision")
            if decision == "NEW":
                nt = (r or {}).get("new_topic")
                if isinstance(nt, str):
                    # Coerce string into {title,summary}
                    title = (nt[:24] + "…") if len(nt) > 24 else nt
                    r["new_topic"] = {"title": title, "summary": nt[:160]}
                elif nt is None:
                    r["new_topic"] = {"title": None, "summary": None}
                # Validate title
                title = (r.get("new_topic") or {}).get("title")
                if _is_generic_title(title):
                    return r, False
            elif decision == "BEST_MATCH":
                # Ensure topic_id exists; otherwise fall back to NEW suggestion from content
                if not r.get("topic_id"):
                    r = {
                        "decision": "NEW",
                        "topic_id": None,
                        "new_topic": {"title": None, "summary": None},
                        "reason": "missing topic_id in BEST_MATCH",
                    }
                    return r, False
            else:
                # Unknown decision → force NEW
                r = {"decision": "NEW", "topic_id": None, "new_topic": {"title": None, "summary": None}, "reason": "invalid decision"}
                return r, False
        except Exception:
            return {"decision": "NEW", "topic_id": None, "new_topic": {"title": None, "summary": None}, "reason": "normalization error"}, False
        return r, True

    res, ok = _normalize_and_validate(res)
    if not ok:
        # One retry with stricter instruction
        retry_lines = [
            "Your previous title was invalid/generic.",
            "Produce a concise, specific Japanese title (<=24 chars) from key phrases.",
            "Do NOT use generic words like '新しい話題', 'topic', 'null'.",
            "Output JSON ONLY with {decision, topic_id, new_topic, reason}.",
            "",
            prompt,
        ]
        res_retry = llm.assign_topic("\n".join(retry_lines))
        try:
            preview2 = json.dumps(res_retry, ensure_ascii=False) if isinstance(res_retry, dict) else str(res_retry)
            if len(preview2) > 600: preview2 = preview2[:600] + "…"
            print("LLM decision raw (retry):\n" + preview2)
        except Exception:
            pass
        res2, ok2 = _normalize_and_validate(res_retry)
        if ok2:
            res = res2
        else:
            # Derive a non-generic title from dialog as last resort
            title = _derive_fallback_title(recent_dialog)
            summary = None
            res = {"decision": "NEW", "topic_id": None, "new_topic": {"title": title, "summary": summary}, "reason": "fallback title"}
    try:
        norm_preview = json.dumps(res, ensure_ascii=False)
        if len(norm_preview) > 600:
            norm_preview = norm_preview[:600] + "…"
        print("LLM decision normalized:\n" + norm_preview)
    except Exception:
        pass
    return res
