from __future__ import annotations

import time
import os
from typing import List, Dict

from sai_memory.logging_utils import debug
from sai_memory.memory.storage import (
    get_thread_overview,
    set_thread_overview,
    get_messages_paginated,
)


def _load_summary_prompt_text() -> str:
    # Priority: explicit env text > file > default
    env_txt = os.getenv("SAIMEMORY_SUMMARY_PROMPT")
    if env_txt:
        return env_txt
    path = os.getenv("SAIMEMORY_SUMMARY_PROMPT_FILE")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return (
        "You are a helpful assistant. Create a terse, factual thread overview focusing on goals, decisions, and unresolved questions."
    )


def _build_summary_prompt(messages: List[Dict[str, str]], max_chars: int) -> List[Dict[str, str]]:
    text = []
    acc = 0
    for m in messages:
        chunk = f"{m['role']}: {m['content']}\n"
        if acc + len(chunk) > max_chars:
            break
        text.append(chunk)
        acc += len(chunk)
    # Allow overriding the summary style via env var.
    prompt = _load_summary_prompt_text()
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Summarize the following conversation into a compact overview:"},
        {"role": "user", "content": "\n".join(text)},
    ]


def update_overview_with_llm(
    conn,
    provider,
    *,
    thread_id: str,
    max_chars: int,
    retries: int = 3,
    backoff_s: int = 30,
) -> str | None:
    pages = []
    page = 0
    page_size = 200
    while True:
        batch = get_messages_paginated(conn, thread_id, page=page, page_size=page_size)
        if not batch:
            break
        pages.extend([{"role": m.role, "content": m.content} for m in batch])
        page += 1

    if not pages:
        return None

    msgs = _build_summary_prompt(pages, max_chars)
    attempt = 0
    while attempt < retries:
        attempt += 1
        try:
            debug("summary:build", attempt=attempt, thread_id=thread_id, max_chars=max_chars)
            res = provider.generate(msgs)
            text = res.get("text", "").strip()
            if text:
                set_thread_overview(conn, thread_id, text)
                debug("summary:saved", thread_id=thread_id, length=len(text))
                return text
            return None
        except Exception as e:
            emsg = str(e)
            debug("summary:error", attempt=attempt, error=emsg)
            if "503" in emsg and attempt < retries:
                time.sleep(backoff_s)
                continue
            raise

    return None
