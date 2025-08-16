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


@dataclass
class DummyLLM(LLMClient):
    """
    Simple rule-based LLM stub:
    - If any existing topic title appears in recent dialog, match it.
    - Else propose NEW using last user turn.
    """

    def assign_topic(self, prompt: str) -> Dict:
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
        if not self._has_ollama():
            # Fallback to dummy behavior if ollama isn't installed
            print("LLM notice: Ollama CLI not found; fallback to DummyLLM")
            return DummyLLM().assign_topic(prompt)
        # Construct system+user prompt for JSON-only output
        system = (
            "You are a topic assigner for a memory system. "
            "Respond ONLY valid minified JSON with keys: decision, topic_id, new_topic, reason."
        )
        user = prompt
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
            # If not parseable, fallback
            print("LLM notice: OllamaCLI output not JSON; fallback to DummyLLM")
            return DummyLLM().assign_topic(prompt)
        except Exception:
            print("LLM error: OllamaCLI exception; fallback to DummyLLM")
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
        try:
            # Lazy import to avoid heavy deps if not used
            from llm_clients import OllamaClient as _OllamaClient  # type: ignore
        except Exception:
            print("LLM notice: llm_clients.OllamaClient not found; fallback to DummyLLM")
            return DummyLLM().assign_topic(prompt)

        system = (
            "You are a topic assigner for a memory system. "
            "Respond ONLY valid minified JSON with keys: decision, topic_id, new_topic, reason."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
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
            print("LLM notice: OllamaHTTP output not JSON; fallback to DummyLLM")
            return DummyLLM().assign_topic(prompt)
        except Exception:
            print("LLM error: OllamaHTTP exception; fallback to DummyLLM")
            return DummyLLM().assign_topic(prompt)
