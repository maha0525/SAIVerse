"""Ollama client."""
from __future__ import annotations

import copy
import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

from .base import LLMClient


# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds


def _is_rate_limit_error(err: Exception) -> bool:
    """Check if the error is a rate limit that should be retried."""
    msg = str(err).lower()
    return (
        "rate" in msg
        or "429" in msg
        or "quota" in msg
        or "overload" in msg
    )


def _is_server_error(err: Exception) -> bool:
    """Check if the error is a server error (5xx) that should be retried."""
    if isinstance(err, requests.exceptions.HTTPError):
        response = getattr(err, 'response', None)
        if response is not None and response.status_code >= 500:
            return True
    msg = str(err).lower()
    return "503" in msg or "502" in msg or "504" in msg or "unavailable" in msg


def _is_timeout_error(err: Exception) -> bool:
    """Check if the error is a timeout that should be retried."""
    if isinstance(err, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    msg = str(err).lower()
    return "timeout" in msg or "timed out" in msg


def _should_retry(err: Exception) -> bool:
    """Check if the error should trigger a retry."""
    return _is_rate_limit_error(err) or _is_server_error(err) or _is_timeout_error(err)


# Allowed request parameters for Ollama (similar to OpenAI)
OLLAMA_ALLOWED_REQUEST_PARAMS = {
    "temperature",
    "top_p",
    "top_k",
    "num_predict",  # max_tokens equivalent
    "stop",
    "seed",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "mirostat",
    "mirostat_tau",
    "mirostat_eta",
}


class OllamaClient(LLMClient):
    """Client for Ollama API."""

    # Class-level probe cache: avoids re-probing on every instance creation
    _probe_cache: Optional[str] = None
    _probe_done: bool = False

    def __init__(
        self,
        model: str,
        context_length: int,
        supports_images: bool = False,
        request_kwargs: Optional[Dict[str, Any]] = None,
        base_url: Optional[str] = None,
    ) -> None:
        super().__init__(supports_images=supports_images)
        self.model = model
        self.context_length = context_length
        self._request_kwargs: Dict[str, Any] = dict(request_kwargs or {})
        # Use explicit base_url parameter first, then environment variables
        base_env = base_url or os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST")

        # Reuse cached probe result if available (class-level cache)
        if OllamaClient._probe_done and not base_url:
            probed = OllamaClient._probe_cache
        else:
            probed = self._probe_base(base_env)
            if not base_url:
                OllamaClient._probe_cache = probed
                OllamaClient._probe_done = True

        if probed is None:
            logging.warning("No responsive Ollama endpoint found during initialization")
            self.base = "http://127.0.0.1:11434"
        else:
            self.base = probed
        self.url = f"{self.base}/v1/chat/completions"
        self.chat_url = f"{self.base}/api/chat"

    @classmethod
    def reset_probe_cache(cls) -> None:
        """Reset the cached probe result so the next instance will re-probe."""
        cls._probe_cache = None
        cls._probe_done = False

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
        tools: Optional[list] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str | Dict[str, Any]:
        """Unified generate method.
        
        Args:
            messages: Conversation messages
            tools: Tool specifications. If provided, returns Dict with tool detection.
                   If None or empty, returns str with text response.
            response_schema: Optional JSON schema for structured output
            temperature: Optional temperature override
            
        Returns:
            str: Text response when tools is None or empty
            Dict: Tool detection result when tools is provided
        """
        tools_spec = tools or []
        use_tools = bool(tools_spec)
        
        logging.info(
            "OllamaClient.generate invoked (model=%s use_tools=%s supports_schema=%s messages=%d)",
            self.model,
            use_tools,
            bool(response_schema),
            len(messages),
        )

        options: Dict[str, Any] = {"num_ctx": self.context_length}
        for key in ("temperature", "top_p", "top_k", "num_predict", "stop", "seed",
                    "repeat_penalty", "presence_penalty", "frequency_penalty",
                    "mirostat", "mirostat_tau", "mirostat_eta"):
            if key in self._request_kwargs:
                options[key] = self._request_kwargs[key]
        if temperature is not None:
            options["temperature"] = temperature

        # Tool mode: return Dict with tool detection
        if use_tools:
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": options,
                "tools": tools_spec,
            }
            data = None
            last_tool_error: Optional[Exception] = None

            for attempt in range(MAX_RETRIES):
                try:
                    response = requests.post(self.url, json=payload, timeout=(3, 300))
                    preview = response.text[:500] if response.text else "(empty)"
                    logging.debug(
                        "Ollama v1 tool detection response (attempt %d/%d) status=%s preview=%s",
                        attempt + 1, MAX_RETRIES, response.status_code, preview,
                    )
                    response.raise_for_status()
                    data = response.json()
                    break  # Success, exit retry loop
                except Exception as e:
                    last_tool_error = e
                    if _should_retry(e) and attempt < MAX_RETRIES - 1:
                        backoff = INITIAL_BACKOFF * (2 ** attempt)
                        logging.warning(
                            "[ollama] Retryable tool detection error (attempt %d/%d): %s. Retrying in %.1fs...",
                            attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                        )
                        time.sleep(backoff)
                        continue
                    logging.exception("Ollama tool detection call failed")
                    raise RuntimeError("Ollama tool detection call failed")

            if data is None:
                raise RuntimeError(f"Ollama tool detection call failed after {MAX_RETRIES} retries: {last_tool_error}")

            # Store usage if available (Ollama may include it in OpenAI-compatible response)
            usage = data.get("usage")
            if usage:
                self._store_usage(
                    input_tokens=usage.get("prompt_tokens", 0) or 0,
                    output_tokens=usage.get("completion_tokens", 0) or 0,
                )

            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls", [])

            if tool_calls:
                tc = tool_calls[0]
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                try:
                    tool_args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    logging.warning("Tool call arguments invalid JSON: %s", func.get("arguments"))
                    tool_args = {}

                if content.strip():
                    return {
                        "type": "both",
                        "content": content,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    }
                else:
                    return {
                        "type": "tool_call",
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    }
            else:
                # Check for empty text response without tool call
                if not content.strip():
                    logging.error(
                        "[ollama] Empty text response without tool call. "
                        "Model returned empty content."
                    )
                    raise RuntimeError("Ollama returned empty response without tool call")
                return {"type": "text", "content": content}

        # Non-tool mode: return str
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
            for attempt in range(MAX_RETRIES):
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
                        "Ollama /api/chat response (attempt %d/%d) status=%s body_preview=%s",
                        attempt + 1, MAX_RETRIES, response.status_code,
                        chat_preview if chat_preview is not None else "(None)",
                    )
                    response.raise_for_status()
                    data = response.json()
                    content = data.get("message", {}).get("content", "")
                    if content:
                        logging.debug("Raw ollama /api/chat response: %s", content)
                        return content
                    logging.warning("Ollama /api/chat returned empty content. raw=%s", chat_preview)
                    break  # Got response but empty - fall through to v1 endpoint
                except Exception as e:
                    if _should_retry(e) and attempt < MAX_RETRIES - 1:
                        backoff = INITIAL_BACKOFF * (2 ** attempt)
                        logging.warning(
                            "[ollama] Retryable /api/chat error (attempt %d/%d): %s. Retrying in %.1fs...",
                            attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                        )
                        time.sleep(backoff)
                        continue
                    logging.exception("Ollama /api/chat structured call failed; falling back to /v1 endpoint")
                    break  # Fall through to v1 endpoint

        payload_v1: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if format_payload_v1:
            payload_v1["format"] = format_payload_v1

        last_v1_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                response = requests.post(self.url, json=payload_v1, timeout=(3, 300))
                preview = response.text[:400] + "…" if response.text and len(response.text) > 400 else response.text
                logging.debug(
                    "Ollama v1 response (attempt %d/%d) status=%s body_preview=%s",
                    attempt + 1, MAX_RETRIES, response.status_code,
                    preview if preview is not None else "(None)",
                )
                response.raise_for_status()
                data = response.json()

                # Store usage if available
                usage = data.get("usage")
                if usage:
                    self._store_usage(
                        input_tokens=usage.get("prompt_tokens", 0) or 0,
                        output_tokens=usage.get("completion_tokens", 0) or 0,
                    )

                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content.strip():
                    logging.error(
                        "[ollama] Empty text response. "
                        "Model returned empty content. keys=%s raw=%s",
                        list(data.keys()),
                        preview if preview is not None else "(None)",
                    )
                    raise RuntimeError("Ollama returned empty response")
                logging.debug("Raw ollama v1 response: %s", content)
                return content
            except Exception as e:
                last_v1_error = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logging.warning(
                        "[ollama] Retryable v1 error (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("Ollama v1 endpoint failed")
                break  # Fall through to legacy fallback

        # Legacy fallback to /api/chat
        if not self.chat_url:
            raise RuntimeError(f"Ollama v1 endpoint failed and no fallback available: {last_v1_error}")

        last_legacy_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
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
                    "Ollama fallback /api/chat (attempt %d/%d) status=%s body_preview=%s",
                    attempt + 1, MAX_RETRIES, response.status_code,
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
            except Exception as e:
                last_legacy_error = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logging.warning(
                        "[ollama] Retryable fallback error (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("Ollama fallback /api/chat failed")
                raise RuntimeError("Ollama API call failed on all endpoints")

        raise RuntimeError(f"Ollama API call failed after {MAX_RETRIES} retries: {last_legacy_error}")

    def generate_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Iterator[str]:
        # For structured output, use non-streaming to get complete JSON
        if response_schema:
            last_schema_error: Optional[Exception] = None
            for attempt in range(MAX_RETRIES):
                try:
                    stream_options: Dict[str, Any] = {"num_ctx": self.context_length}
                    for key in ("temperature", "top_p", "top_k", "num_predict", "stop", "seed",
                                "repeat_penalty", "presence_penalty", "frequency_penalty",
                                "mirostat", "mirostat_tau", "mirostat_eta"):
                        if key in self._request_kwargs:
                            stream_options[key] = self._request_kwargs[key]
                    if temperature is not None:
                        stream_options["temperature"] = temperature
                    stream_payload: Dict[str, Any] = {
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "options": stream_options,
                        "format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": response_schema.get("title") or "saiverse_structured_output",
                                "schema": response_schema,
                                "strict": True,
                            },
                        },
                    }
                    response = requests.post(self.url, json=stream_payload, timeout=(3, 300))
                    response.raise_for_status()
                    data = response.json()
                    _ = data.get("choices", [{}])[0].get("message", {}).get("content", "")  # Structured output captured but not yielded
                    yield ""
                    return
                except Exception as e:
                    last_schema_error = e
                    if _should_retry(e) and attempt < MAX_RETRIES - 1:
                        backoff = INITIAL_BACKOFF * (2 ** attempt)
                        logging.warning(
                            "[ollama] Retryable structured output error (attempt %d/%d): %s. Retrying in %.1fs...",
                            attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                        )
                        time.sleep(backoff)
                        continue
                    logging.exception("Ollama structured output call failed")
                    raise RuntimeError("Ollama structured output failed")
            raise RuntimeError(f"Ollama structured output failed after {MAX_RETRIES} retries: {last_schema_error}")
        
        # Normal streaming mode (no response_schema)
        last_stream_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                stream_options: Dict[str, Any] = {"num_ctx": self.context_length}
                # Apply request_kwargs to options
                for key in ("temperature", "top_p", "top_k", "num_predict", "stop", "seed",
                            "repeat_penalty", "presence_penalty", "frequency_penalty",
                            "mirostat", "mirostat_tau", "mirostat_eta"):
                    if key in self._request_kwargs:
                        stream_options[key] = self._request_kwargs[key]
                # Override with explicit temperature parameter if provided
                if temperature is not None:
                    stream_options["temperature"] = temperature
                stream_payload: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "options": stream_options,
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
                return  # Stream completed successfully
            except Exception as e:
                last_stream_error = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logging.warning(
                        "[ollama] Retryable streaming error (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("Ollama call failed")
                raise RuntimeError("Ollama streaming failed")

        raise RuntimeError(f"Ollama streaming failed after {MAX_RETRIES} retries: {last_stream_error}")

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """DEPRECATED: Use generate(messages, tools=[...]) instead.
        
        This method is kept for backward compatibility with existing code.
        It simply delegates to generate() with tools specified.
        """
        import warnings
        warnings.warn(
            "generate_with_tool_detection() is deprecated. Use generate(messages, tools=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        tools_spec = tools or []
        if not tools_spec:
            result = self.generate(messages, temperature=temperature)
            if isinstance(result, str):
                return {"type": "text", "content": result}
            return result
        return self.generate(messages, tools=tools_spec, temperature=temperature)

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        """Apply model-specific request parameters."""
        if not isinstance(parameters, dict):
            return
        for key, value in parameters.items():
            if key not in OLLAMA_ALLOWED_REQUEST_PARAMS:
                continue
            if value is None:
                self._request_kwargs.pop(key, None)
            else:
                self._request_kwargs[key] = value


__all__ = ["OllamaClient"]
