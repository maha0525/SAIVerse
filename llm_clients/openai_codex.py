"""LLM client for OpenAI's Codex backend via ChatGPT subscription OAuth.

Phase 1: text-only generation. No tool use, no streaming output, no structured output.

Authentication is delegated to the Codex CLI: this client reads ~/.codex/auth.json
that `codex login` produces and reuses its access_token + account_id. We do not
refresh tokens ourselves; if the token has expired, the user must run `codex login`
or let the Codex CLI refresh it.

Cloudflare in front of chatgpt.com fingerprints clients by their TLS handshake.
Plain `requests` / `httpx` get challenged; we use `curl_cffi` to impersonate
Chrome's TLS fingerprint, the same way Codex CLI's reqwest client gets through.
"""
from __future__ import annotations

import json
import logging
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from .base import LLMClient

LOG = logging.getLogger("saiverse.llm_clients.openai_codex")

CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_CLI_VERSION = "0.45.0"
CODEX_IMPERSONATE = "chrome124"


def _build_user_agent() -> str:
    arch = platform.machine() or "unknown"
    system = platform.system() or "unknown"
    release = platform.release() or "0"
    return (
        f"{CODEX_ORIGINATOR}/{CODEX_CLI_VERSION} "
        f"({system} {release}; {arch}) python-saiverse"
    )


class OpenAICodexClient(LLMClient):
    """OpenAI Codex backend client (ChatGPT subscription OAuth).

    Phase 1 scope:
        * `generate(messages)` returns the assistant's final text
        * Token usage is recorded via `_store_usage`
    Out of scope (Phase 2+):
        * Tool / function calling
        * Streaming output to the caller
        * Structured output (response_schema)
        * Reasoning extraction
        * Multimodal input (images)
    """

    def __init__(
        self,
        model: str,
        supports_images: bool = False,
        timeout: float = 120.0,
        **_: Any,
    ) -> None:
        super().__init__(supports_images=supports_images)
        self.model = model
        self._timeout = float(timeout)
        self._params: Dict[str, Any] = {}
        self._session: Optional[Any] = None

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        if parameters:
            self._params.update(parameters)

    def _ensure_session(self) -> Any:
        if self._session is None:
            self._session = cffi_requests.Session(impersonate=CODEX_IMPERSONATE)
        return self._session

    def _load_auth(self) -> Dict[str, Any]:
        if not CODEX_AUTH_FILE.exists():
            raise RuntimeError(
                f"{CODEX_AUTH_FILE} not found. Run `codex login` first, "
                'and ensure ~/.codex/config.toml has '
                '`cli_auth_credentials_store_mode = "file"`.'
            )
        return json.loads(CODEX_AUTH_FILE.read_text(encoding="utf-8"))

    def _build_headers(self) -> Dict[str, str]:
        auth = self._load_auth()
        tokens = auth.get("tokens") or {}
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if not access_token:
            raise RuntimeError(
                "auth.json has no tokens.access_token; run `codex login`"
            )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "originator": CODEX_ORIGINATOR,
            "User-Agent": _build_user_agent(),
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers

    @staticmethod
    def _flatten_content(content: Any) -> str:
        """Best-effort flatten of OpenAI-style multi-part content into plain text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content")
                    if text:
                        parts.append(str(text))
                elif part is not None:
                    parts.append(str(part))
            return "\n".join(parts)
        return "" if content is None else str(content)

    def _build_input(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        instructions_parts: List[str] = []
        input_items: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            text = self._flatten_content(msg.get("content"))
            if role == "system":
                if text:
                    instructions_parts.append(text)
                continue
            if role not in ("user", "assistant"):
                LOG.debug("skipping message with unsupported role=%r", role)
                continue
            content_type = "input_text" if role == "user" else "output_text"
            input_items.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [{"type": content_type, "text": text}],
                }
            )
        instructions = "\n\n".join(instructions_parts)
        return instructions, input_items

    def _build_body(
        self,
        messages: List[Dict[str, Any]],
        temperature: float | None,
    ) -> Dict[str, Any]:
        instructions, input_items = self._build_input(messages)
        body: Dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "stream": True,  # Codex backend requires stream=True
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        # GPT-5 family models served via the Codex backend reject `temperature`
        # and `top_p` (reasoning models). We accept those params at the API
        # boundary for interface compatibility but do not forward them.
        if temperature is not None:
            LOG.debug("dropping unsupported temperature=%s for Codex backend", temperature)
        if "max_output_tokens" in self._params and self._params["max_output_tokens"] is not None:
            body["max_output_tokens"] = self._params["max_output_tokens"]
        return body

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str:
        if tools:
            LOG.warning(
                "OpenAICodexClient: tools not yet supported (Phase 1); "
                "ignoring %d tool(s)",
                len(tools),
            )
        if response_schema:
            LOG.warning(
                "OpenAICodexClient: response_schema not yet supported (Phase 1); ignoring"
            )

        body = self._build_body(messages, temperature)
        headers = self._build_headers()
        session = self._ensure_session()
        url = f"{CODEX_BASE_URL}/responses"

        LOG.info(
            "POST %s model=%s input_messages=%d",
            url,
            self.model,
            len(body["input"]),
        )
        resp = session.post(
            url,
            headers=headers,
            json=body,
            timeout=self._timeout,
            stream=True,
        )
        if not resp.ok:
            # When stream=True, body bytes haven't been fetched yet, so .text/.content
            # may be empty. Drain the stream into a buffer to get the error payload.
            chunks: List[bytes] = []
            try:
                for chunk in resp.iter_content():
                    if chunk:
                        chunks.append(
                            chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                        )
            except Exception as exc:  # noqa: BLE001
                LOG.debug("error draining error body: %s", exc)
            err_text = b"".join(chunks).decode("utf-8", errors="replace")
            if not err_text:
                err_text = getattr(resp, "text", None) or ""
            try:
                resp.close()
            except Exception:
                pass
            raise RuntimeError(
                f"Codex backend returned status={resp.status_code}: {err_text[:1000]}"
            )

        try:
            return self._consume_sse(resp)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _consume_sse(self, resp: Any) -> str:
        """Parse a Responses API SSE stream and return the final assistant text."""
        delta_buffer: List[str] = []
        final_text: Optional[str] = None
        usage_input = 0
        usage_output = 0
        usage_cached = 0

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = (
                raw_line.decode("utf-8", errors="replace")
                if isinstance(raw_line, bytes)
                else raw_line
            )
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:") :].strip()
            if not data_str:
                continue
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                LOG.debug("could not parse SSE payload: %s", data_str[:200])
                continue

            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                delta_buffer.append(event.get("delta") or "")
            elif event_type == "response.output_text.done":
                final_text = event.get("text") or "".join(delta_buffer)
            elif event_type == "response.completed":
                response_obj = event.get("response") or {}
                usage = response_obj.get("usage") or {}
                usage_input = int(usage.get("input_tokens") or 0)
                usage_output = int(usage.get("output_tokens") or 0)
                cached_details = usage.get("input_tokens_details") or {}
                usage_cached = int(cached_details.get("cached_tokens") or 0)
                if final_text is None:
                    parts: List[str] = []
                    for item in response_obj.get("output") or []:
                        for content_part in item.get("content") or []:
                            if content_part.get("type") == "output_text":
                                parts.append(content_part.get("text") or "")
                    if parts:
                        final_text = "".join(parts)
            elif event_type == "response.failed":
                response_obj = event.get("response") or {}
                error = response_obj.get("error") or {}
                raise RuntimeError(f"Codex response failed: {error}")

        if final_text is None:
            final_text = "".join(delta_buffer)

        self._store_usage(
            input_tokens=usage_input,
            output_tokens=usage_output,
            model=self.model,
            cached_tokens=usage_cached,
        )
        LOG.info(
            "response complete: chars=%d input_tokens=%d output_tokens=%d cached=%d",
            len(final_text),
            usage_input,
            usage_output,
            usage_cached,
        )
        return final_text


__all__ = ["OpenAICodexClient"]
