"""Ollama client with automatic Gemini fallback."""
from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

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
                self.chat_url = ""
            except Exception as exc:
                logging.warning("Gemini fallback unavailable: %s", exc)
                self.base = "http://127.0.0.1:11434"
                self.url = f"{self.base}/v1/chat/completions"
                self.chat_url = f"{self.base}/api/chat"
        else:
            self.base = probed
            self.url = f"{self.base}/v1/chat/completions"
            self.chat_url = f"{self.base}/api/chat"

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

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
    ) -> str:
        logging.info(
            "OllamaClient.generate invoked (model=%s supports_schema=%s messages=%d)",
            self.model,
            bool(response_schema),
            len(messages),
        )

        if self.fallback_client is not None and not self.url:
            return self.fallback_client.generate(
                messages,
                tools,
                response_schema=response_schema,
                temperature=temperature,
            )

        options: Dict[str, Any] = {"num_ctx": self.context_length}
        if temperature is not None:
            options["temperature"] = temperature

        schema_payload: Optional[Dict[str, Any]] = copy.deepcopy(response_schema) if isinstance(response_schema, dict) else None
        format_payload_v1: Optional[Dict[str, Any]] = None
        if schema_payload is not None:
            format_payload_v1 = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_payload.get("title") or "saiverse_structured_output",
                    "schema": schema_payload,
                    "strict": True,
                },
            }

        if schema_payload is not None and self.chat_url:
            try:
                payload_chat: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": options,
                    "format": schema_payload,
                }
                response = requests.post(self.chat_url, json=payload_chat, timeout=(3, 300))
                chat_preview = response.text[:400] + "…" if response.text and len(response.text) > 400 else response.text
                logging.debug(
                    "Ollama /api/chat response status=%s body_preview=%s",
                    response.status_code,
                    chat_preview if chat_preview is not None else "(None)",
                )
                response.raise_for_status()
                data = response.json()
                content = data.get("message", {}).get("content", "")
                if content:
                    logging.debug("Raw ollama /api/chat response: %s", content)
                    return content
                logging.warning("Ollama /api/chat returned empty content. raw=%s", chat_preview)
            except Exception:
                logging.exception("Ollama /api/chat structured call failed; falling back to /v1 endpoint")

        payload_v1: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if format_payload_v1:
            payload_v1["format"] = format_payload_v1

        try:
            response = requests.post(self.url, json=payload_v1, timeout=(3, 300))
            preview = response.text[:400] + "…" if response.text and len(response.text) > 400 else response.text
            logging.debug(
                "Ollama v1 response status=%s body_preview=%s",
                response.status_code,
                preview if preview is not None else "(None)",
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                logging.warning(
                    "Ollama v1 returned empty content. keys=%s raw=%s",
                    list(data.keys()),
                    preview if preview is not None else "(None)",
                )
            logging.debug("Raw ollama v1 response: %s", content)
            return content
        except Exception:
            logging.exception("Ollama v1 endpoint failed")
            if self.fallback_client is not None:
                return self.fallback_client.generate(
                    messages,
                    tools,
                    response_schema=response_schema,
                    temperature=temperature,
                )
            if not self.chat_url:
                return "エラーが発生しました。"
            try:
                legacy_payload: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": options,
                }
                if schema_payload is not None:
                    legacy_payload["format"] = schema_payload
                response = requests.post(
                    self.chat_url,
                    json=legacy_payload,
                    timeout=(3, 300),
                )
                legacy_preview = response.text[:400] + "…" if response.text and len(response.text) > 400 else response.text
                logging.debug(
                    "Ollama fallback /api/chat status=%s body_preview=%s",
                    response.status_code,
                    legacy_preview if legacy_preview is not None else "(None)",
                )
                response.raise_for_status()
                data = response.json()
                content = data.get("message", {}).get("content", "")
                if not content:
                    logging.warning(
                        "Ollama fallback /api/chat returned empty content. keys=%s raw=%s",
                        list(data.keys()),
                        legacy_preview if legacy_preview is not None else "(None)",
                    )
                logging.debug("Raw ollama fallback /api/chat response: %s", content)
                return content
            except Exception:
                logging.exception("Ollama fallback /api/chat failed")
                return "エラーが発生しました。"

    def generate_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
    ) -> Iterator[str]:
        if self.fallback_client is not None and not self.url:
            yield from self.fallback_client.generate_stream(
                messages,
                tools,
                response_schema=response_schema,
                temperature=temperature,
            )
            return
        try:
            stream_options: Dict[str, Any] = {"num_ctx": self.context_length}
            if temperature is not None:
                stream_options["temperature"] = temperature
            stream_payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "options": stream_options,
            }
            if response_schema:
                stream_payload["format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_schema.get("title") or "saiverse_structured_output",
                        "schema": response_schema,
                        "strict": True,
                    },
                }
            # When no schema is requested we allow plain-text responses. Ollama defaults to
            # unstructured text, so we intentionally avoid forcing any response_format.

            response = requests.post(
                self.url,
                json=stream_payload,
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
            if self.fallback_client is not None:
                yield from self.fallback_client.generate_stream(
                    messages,
                    tools,
                    response_schema=response_schema,
                    temperature=temperature,
                )
            else:
                yield "エラーが発生しました。"


__all__ = ["OllamaClient"]
