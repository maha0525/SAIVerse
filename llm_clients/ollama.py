"""Ollama client with automatic Gemini fallback."""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Iterator, List, Optional

import requests

from .base import LLMClient
from .gemini import GeminiClient


class OllamaClient(LLMClient):
    """Client for Ollama API."""

    def __init__(self, model: str, context_length: int, supports_images: bool = False) -> None:
        super().__init__(supports_images=supports_images)
        self.model = model
        self.context_length = context_length
        self.fallback_client: Optional[LLMClient] = None
        base_env = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST")
        probed = self._probe_base(base_env)
        if probed is None:
            try:
                logging.info("No reachable Ollama; falling back to Gemini 1.5 Flash")
                self.fallback_client = GeminiClient("gemini-1.5-flash")
                self.base = ""
                self.url = ""
            except Exception as exc:
                logging.warning("Gemini fallback unavailable: %s", exc)
                self.base = "http://127.0.0.1:11434"
                self.url = f"{self.base}/v1/chat/completions"
        else:
            self.base = probed
            self.url = f"{self.base}/v1/chat/completions"

    def _probe_base(self, preferred: Optional[str]) -> Optional[str]:
        """Pick a reachable Ollama base URL with quick connect timeouts."""
        candidates: List[str] = []
        if preferred:
            for part in str(preferred).split(","):
                part = part.strip()
                if part:
                    candidates.append(part)
        candidates += [
            "http://127.0.0.1:11434",
            "http://localhost:11434",
            "http://host.docker.internal:11434",
            "http://172.17.0.1:11434",
        ]
        seen = set()
        for base in candidates:
            if base in seen:
                continue
            seen.add(base)
            try:
                url_v1 = f"{base}/v1/models"
                response = requests.get(url_v1, timeout=(2, 2))
                if response.ok:
                    logging.info("Using Ollama base: %s (v1)", base)
                    return base
            except Exception:
                pass
            try:
                url_legacy = f"{base}/api/version"
                response_legacy = requests.get(url_legacy, timeout=(2, 2))
                if response_legacy.ok:
                    logging.info("Using Ollama base: %s (legacy)", base)
                    return base
            except Exception:
                continue
        logging.warning("No responsive Ollama endpoint detected during probe")
        return None

    def generate(self, messages: List[Dict[str, str]], tools: Optional[list] | None = None) -> str:
        payload_v1 = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_ctx": self.context_length},
            "response_format": {"type": "json_object"},
        }
        if self.fallback_client is not None:
            return self.fallback_client.generate(messages, tools)
        try:
            response = requests.post(self.url, json=payload_v1, timeout=(3, 300))
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logging.debug("Raw ollama v1 response: %s", content)
            return content
        except Exception:
            logging.exception("Ollama v1 endpoint failed; trying legacy /api/chat")
            try:
                response = requests.post(
                    f"{self.base}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "options": {"num_ctx": self.context_length},
                    },
                    timeout=(3, 300),
                )
                response.raise_for_status()
                data = response.json()
                content = data.get("message", {}).get("content", "")
                logging.debug("Raw ollama /api/chat response: %s", content)
                return content
            except Exception:
                logging.exception("Ollama legacy /api/chat failed")
                return "エラーが発生しました。"

    def generate_stream(
        self, messages: List[Dict[str, str]], tools: Optional[list] | None = None
    ) -> Iterator[str]:
        if self.fallback_client is not None:
            yield from self.fallback_client.generate_stream(messages, tools)
            return
        try:
            response = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_ctx": self.context_length},
                },
                timeout=(3, 300),
                stream=True,
            )
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = line.decode("utf-8")
                if chunk.startswith("data: "):
                    chunk = chunk[len("data: ") :]
                if chunk.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    yield delta
                except json.JSONDecodeError:
                    logging.warning("Failed to parse stream chunk: %s", chunk)
        except Exception:
            logging.exception("Ollama call failed")
            yield "エラーが発生しました。"


__all__ = ["OllamaClient"]
