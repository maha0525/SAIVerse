from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional


class LLMClient:
    def assign_topic(self, prompt: str) -> Dict:
        """Return JSON-decoded decision dict from model output."""
        raise NotImplementedError
    # Telemetry for caller
    last_status: str = "ok"          # "ok" | "fallback_dummy"
    last_retries: int = 0            # retry count within last call


@dataclass
class DummyLLM(LLMClient):
    """
    Simple rule-based LLM stub:
    - If any existing topic title appears in recent dialog, match it.
    - Else propose NEW using last user turn.
    """

    def assign_topic(self, prompt: str) -> Dict:
        self.last_status = "ok"
        self.last_retries = 0
        try:
            print("LLM used: DummyLLM")
            # Extract minimal context from prompt for demo
            # Find existing topic lines
            topics = []
            for line in prompt.splitlines():
                if line.strip().startswith("- [id="):
                    topics.append(line)
            # Last user line
            last_user = ""
            last_user_idx = prompt.rfind("- U:")
            if last_user_idx != -1:
                last_user = prompt[last_user_idx:].splitlines()[0][4:].strip()
            # Naive match
            for tline in topics:
                # format: - [id=topic_07] "TITLE" — summary: xxx
                if '"' in tline:
                    title = tline.split('"')[1]
                    if title and title in prompt:
                        tid = tline.split(']')[0].split('=')[-1]
                        return {
                            "decision": "BEST_MATCH",
                            "topic_id": tid,
                            "new_topic": None,
                            "reason": "title substring matched",
                        }
            return {
                "decision": "NEW",
                "topic_id": None,
                "new_topic": {
                    "title": (last_user[:24] + "…") if len(last_user) > 24 else last_user,
                    "summary": last_user[:160] if last_user else None,
                },
                "reason": "no match by rules",
            }
        except Exception:
            return {"decision": "NEW", "topic_id": None, "new_topic": None, "reason": "fallback"}


@dataclass
class OllamaLLM(LLMClient):
    model: str = "qwen2.5:3b"
    timeout: int = 30

    def _has_ollama(self) -> bool:
        return shutil.which("ollama") is not None

    def assign_topic(self, prompt: str) -> Dict:
        self.last_status = "ok"
        self.last_retries = 0
        if not self._has_ollama():
            # Fallback to dummy behavior if ollama isn't installed
            print("LLM notice: Ollama CLI not found; fallback to DummyLLM")
            self.last_status = "fallback_dummy"
            return DummyLLM().assign_topic(prompt)
        # Construct system+user prompt for JSON-only output
        system = (
            "You are a topic assigner for a memory system. "
            "Respond ONLY valid minified JSON with keys: decision, topic_id, new_topic, reason. "
            "Use Japanese for all strings (title, summary, reason)."
        )
        user = prompt
        last_err = None
        for attempt in range(3):
            try:
                proc = subprocess.run(
                    ["ollama", "run", self.model],
                    input=f"SYSTEM:\n{system}\n\nUSER:\n{user}",
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
                out = proc.stdout.strip()
                print("LLM used: OllamaCLI (subprocess)")
                if out:
                    preview = out if len(out) <= 600 else (out[:600] + "…")
                    print("LLM raw (first 600 chars):\n" + preview)
                # Try to find JSON block
                start = out.find("{")
                end = out.rfind("}")
                if start != -1 and end != -1 and end >= start:
                    blob = out[start : end + 1]
                    return json.loads(blob)
                raise ValueError("ollama_cli_non_json")
            except Exception as e:
                last_err = e
                self.last_retries = attempt + 1
                if attempt < 2:
                    print(f"LLM notice: OllamaCLI parse/call failed (attempt {attempt+1}); retrying…")
                    continue
        print(f"LLM error: OllamaCLI failed after retries; fallback to DummyLLM ({last_err})")
        self.last_status = "fallback_dummy"
        return DummyLLM().assign_topic(prompt)


@dataclass
class OllamaHTTPAssignLLM(LLMClient):
    """
    Ollama (OpenAI互換HTTP) 経由で Topic assigner を実行。
    既存の llm_clients.OllamaClient を利用。
    """
    model: str = "qwen2.5:3b"
    context_length: int = 8192

    def assign_topic(self, prompt: str) -> Dict:
        self.last_status = "ok"
        self.last_retries = 0
        try:
            # Lazy import to avoid heavy deps if not used
            from llm_clients import OllamaClient as _OllamaClient  # type: ignore
        except Exception:
            print("LLM notice: llm_clients.OllamaClient not found; fallback to DummyLLM")
            self.last_status = "fallback_dummy"
            return DummyLLM().assign_topic(prompt)

        system = (
            "You are a topic assigner for a memory system. "
            "Respond ONLY valid minified JSON with keys: decision, topic_id, new_topic, reason. "
            "Use Japanese for all strings (title, summary, reason)."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        last_err = None
        for attempt in range(3):
            try:
                client = _OllamaClient(self.model, self.context_length)
                out = client.generate(messages)
                if out:
                    print("LLM used: OllamaHTTP (llm_clients.OllamaClient)")
                    preview = out if len(out) <= 600 else (out[:600] + "…")
                    print("LLM raw (first 600 chars):\n" + preview)
                # Try to parse JSON
                start = out.find("{")
                end = out.rfind("}")
                if start != -1 and end != -1 and end >= start:
                    blob = out[start : end + 1]
                    return json.loads(blob)
                raise ValueError("ollama_http_non_json")
            except Exception as e:
                last_err = e
                self.last_retries = attempt + 1
                if attempt < 2:
                    print(f"LLM notice: OllamaHTTP parse/call failed (attempt {attempt+1}); retrying…")
                    continue
        print(f"LLM error: OllamaHTTP failed after retries; fallback to DummyLLM ({last_err})")
        self.last_status = "fallback_dummy"
        return DummyLLM().assign_topic(prompt)


@dataclass
class GeminiAssignLLM(LLMClient):
    """Google Gemini backend for topic assignment using google-genai.
    Requires GEMINI_FREE_API_KEY or GEMINI_API_KEY.
    """
    model: str = "gemini-2.0-flash"

    def assign_topic(self, prompt: str) -> Dict:
        try:
            from google import genai
            from google.genai import types as gtypes
            import os
            import time
        except Exception:
            print("LLM notice: google-genai not available; using DummyLLM")
            self.last_status = "fallback_dummy"
            return DummyLLM().assign_topic(prompt)

        key = os.getenv("GEMINI_FREE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not key:
            print("LLM notice: GEMINI_*_API_KEY not set; using DummyLLM")
            self.last_status = "fallback_dummy"
            return DummyLLM().assign_topic(prompt)
        client = genai.Client(api_key=key)
        def _call():
            return client.models.generate_content(
                model=self.model,
                contents=[gtypes.Content(role="user", parts=[gtypes.Part(text=prompt)])],
                config=gtypes.GenerateContentConfig(
                    system_instruction=(
                        "You are a topic assigner for a memory system. "
                        "Respond ONLY valid minified JSON with keys: decision, topic_id, new_topic, reason. "
                        "Use Japanese for all strings (title, summary, reason)."
                    ),
                    response_mime_type="application/json",
                ),
            )
        attempt = 0
        last_err = None
        while attempt < 3:
            try:
                resp = _call()
            except Exception as e:
                s = str(e)
                last_err = e
                if "RESOURCE_EXHAUSTED" in s or "429" in s:
                    print("LLM notice: Gemini 429/RESOURCE_EXHAUSTED; waiting 60s then retrying…")
                    import time as _t
                    _t.sleep(60)
                    attempt += 1
                    self.last_retries = attempt
                    continue
                else:
                    print(f"LLM notice: Gemini exception (attempt {attempt+1}); retrying…")
                    attempt += 1
                    self.last_retries = attempt
                    continue
            # parse response
            text = getattr(resp, "text", None)
            if not text and getattr(resp, "candidates", None):
                try:
                    cand = resp.candidates[0]
                    if getattr(cand, "content", None) and cand.content.parts:
                        text = cand.content.parts[0].text
                except Exception as e:
                    last_err = e
                    text = None
            if not text:
                print("LLM notice: Gemini returned empty; retrying…")
                attempt += 1
                self.last_retries = attempt
                continue
            try:
                return json.loads(text)
            except Exception:
                s = text.strip()
                if s.startswith("```"):
                    s = "\n".join(s.splitlines()[1:])
                    if s.endswith("```"):
                        s = "\n".join(s.splitlines()[:-1])
                start = s.find("{")
                end = s.rfind("}")
                if start != -1 and end != -1 and end >= start:
                    blob = s[start : end + 1]
                    try:
                        return json.loads(blob)
                    except Exception as e:
                        last_err = e
                print("LLM notice: Gemini output not JSON-parsable; retrying…")
                attempt += 1
                self.last_retries = attempt
        print(f"LLM error: Gemini failed after retries; fallback to DummyLLM ({last_err})")
        self.last_status = "fallback_dummy"
        return DummyLLM().assign_topic(prompt)
