"""LLM client for OpenAI's Codex backend via ChatGPT subscription OAuth.

Phase 1: text-only generation.
Phase 2: tool calling, structured output (response_schema), reasoning_effort.

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
from typing import Any, Dict, Iterator, List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from .base import LLMClient

LOG = logging.getLogger("saiverse.llm_clients.openai_codex")

CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_CLI_VERSION = "0.45.0"
CODEX_IMPERSONATE = "chrome124"
SUPPORTED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")


def _build_user_agent() -> str:
    arch = platform.machine() or "unknown"
    system = platform.system() or "unknown"
    release = platform.release() or "0"
    return (
        f"{CODEX_ORIGINATOR}/{CODEX_CLI_VERSION} "
        f"({system} {release}; {arch}) python-saiverse"
    )


def _extract_json_object_candidate(text: str) -> str:
    """Trim wrapping markdown fences / leading prose to expose a JSON object.

    Mirrors the helper in llm_clients.openai so structured output parsing
    works even when the model wraps the JSON in ```json ... ```.
    """
    candidate = (text or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("```"):
        for segment in candidate.split("```"):
            segment = segment.strip()
            if segment.startswith("json"):
                segment = segment[4:].strip()
            if segment.startswith("{") and segment.endswith("}"):
                return segment
    if candidate.startswith("{") and candidate.endswith("}"):
        return candidate
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        return candidate[start : end + 1]
    return candidate


class OpenAICodexClient(LLMClient):
    """OpenAI Codex backend client (ChatGPT subscription OAuth).

    Implemented:
        * `generate(messages)` returns the assistant's final text
        * `generate(messages, tools=[...])` returns tool-detection dict
        * `generate(messages, response_schema={...})` returns parsed dict
        * Token usage via `_store_usage`
        * `reasoning_effort` parameter (low/medium/high/xhigh)
    Out of scope (Phase 3+):
        * Real per-token streaming to the caller (currently single yield)
        * Reasoning extraction (response.reasoning_summary.* events)
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

    @staticmethod
    def _to_responses_tools(tools: List[Any]) -> List[Dict[str, Any]]:
        """Convert SAIVerse OpenAI Chat Completions tool spec to Responses API form.

        SAIVerse stores tools as `{"type": "function", "function": {name, description, parameters}}`
        (Chat Completions wire format). The Codex backend uses Responses API which
        flattens this into `{"type": "function", "name": ..., "description": ..., "parameters": ..., "strict": false}`.
        """
        out: List[Dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            t_type = tool.get("type")
            if t_type == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                out.append(
                    {
                        "type": "function",
                        "name": fn.get("name", ""),
                        "description": fn.get("description", "") or "",
                        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                        "strict": False,
                    }
                )
            elif t_type == "function" and "name" in tool:
                # Already in Responses API shape
                out.append({**tool, "strict": tool.get("strict", False)})
            else:
                LOG.debug("skipping unsupported tool spec: %s", tool)
        return out

    def _build_input(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Lower SAIVerse-style messages into Responses API input items.

        Handles four message shapes:
            * role=system → folded into `instructions`
            * role=user/assistant with plain content → message item
            * role=assistant with tool_calls → message + function_call items
            * role=tool → function_call_output item
        """
        instructions_parts: List[str] = []
        input_items: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")

            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id") or msg.get("call_id") or "",
                        "output": self._flatten_content(msg.get("content")),
                    }
                )
                continue

            if role == "assistant" and msg.get("tool_calls"):
                text_part = self._flatten_content(msg.get("content"))
                if text_part:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text_part}],
                        }
                    )
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args_value = fn.get("arguments")
                    if isinstance(args_value, dict):
                        args_value = json.dumps(args_value, ensure_ascii=False)
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.get("id") or "",
                            "name": fn.get("name", ""),
                            "arguments": args_value or "",
                        }
                    )
                continue

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

    def _resolve_reasoning_effort(self, override: Optional[str]) -> Optional[str]:
        candidate = override or self._params.get("reasoning_effort")
        if candidate is None:
            return None
        candidate = str(candidate).strip().lower()
        if candidate not in SUPPORTED_REASONING_EFFORTS:
            LOG.warning(
                "ignoring unsupported reasoning_effort=%r (allowed: %s)",
                candidate,
                ", ".join(SUPPORTED_REASONING_EFFORTS),
            )
            return None
        return candidate

    def _build_body(
        self,
        messages: List[Dict[str, Any]],
        temperature: float | None,
        tools: Optional[List[Any]],
        response_schema: Optional[Dict[str, Any]],
        reasoning_effort: Optional[str],
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
        # and `top_p` (reasoning models). We accept those at the API boundary
        # for interface compatibility but do not forward them.
        if temperature is not None:
            LOG.debug("dropping unsupported temperature=%s for Codex backend", temperature)
        if "max_output_tokens" in self._params and self._params["max_output_tokens"] is not None:
            body["max_output_tokens"] = self._params["max_output_tokens"]

        responses_tools = self._to_responses_tools(tools or [])
        if responses_tools:
            body["tools"] = responses_tools
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True

        if response_schema and not responses_tools:
            schema_name = (
                response_schema.get("title")
                if isinstance(response_schema, dict)
                else None
            ) or "codex_output_schema"
            body["text"] = {
                "format": {
                    "name": schema_name,
                    "type": "json_schema",
                    "strict": True,
                    "schema": response_schema,
                }
            }
        elif response_schema and responses_tools:
            LOG.warning(
                "response_schema specified alongside tools; structured output is "
                "ignored for tool runs (matches OpenAIClient behavior)."
            )

        effort = self._resolve_reasoning_effort(reasoning_effort)
        if effort:
            body["reasoning"] = {"effort": effort}

        return body

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        reasoning_effort: Optional[str] = None,
        **_: Any,
    ) -> Any:
        """Run a single Responses API turn.

        Returns:
            * `str` when `tools` is empty / None and `response_schema` is None.
            * `dict` (`{"type": "tool_call", "tool_name": ..., "tool_args": ...}`
              or `{"type": "text", "content": ...}`) when `tools` is provided.
            * `dict` (parsed JSON) when `response_schema` is provided.
        """
        body = self._build_body(
            messages,
            temperature=temperature,
            tools=tools,
            response_schema=response_schema,
            reasoning_effort=reasoning_effort,
        )
        headers = self._build_headers()
        session = self._ensure_session()
        url = f"{CODEX_BASE_URL}/responses"

        LOG.info(
            "POST %s model=%s input_messages=%d tools=%d schema=%s effort=%s",
            url,
            self.model,
            len(body["input"]),
            len(body.get("tools") or []),
            bool(response_schema),
            (body.get("reasoning") or {}).get("effort"),
        )
        resp = session.post(
            url,
            headers=headers,
            json=body,
            timeout=self._timeout,
            stream=True,
        )
        if not resp.ok:
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
            sse_result = self._consume_sse(resp)
        finally:
            try:
                resp.close()
            except Exception:
                pass

        text = sse_result["text"]
        function_calls = sse_result["function_calls"]

        # tool path: caller passed `tools` and expects a tool-detection dict
        if tools:
            if function_calls:
                first = function_calls[0]
                args_str = first.get("arguments") or ""
                try:
                    args = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    LOG.warning(
                        "tool_call arguments are not valid JSON; falling back to {}: %r",
                        args_str[:300],
                    )
                    args = {}
                detection = {
                    "type": "tool_call",
                    "tool_name": first.get("name") or "",
                    "tool_args": args,
                    "raw_tool_call": first,
                }
                self._store_tool_detection(detection)
                return detection
            detection = {"type": "text", "content": text}
            self._store_tool_detection(detection)
            return detection

        # structured output path: parse JSON and return the dict
        if response_schema:
            candidate = _extract_json_object_candidate(text)
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                preview = candidate.replace("\n", "\\n")[:300]
                LOG.warning(
                    "failed to parse structured output from Codex response: %s (candidate=%r)",
                    exc,
                    preview,
                )
                raise RuntimeError(
                    f"Failed to parse JSON for structured output: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise RuntimeError(
                    f"structured output expected a JSON object, got {type(parsed).__name__}"
                )
            return parsed

        # plain text path
        return text

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        reasoning_effort: Optional[str] = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Phase 1 stub: collect the full response, then yield it as a single chunk.

        SEA's speak path calls `generate_stream` to interpose on token deltas. We
        don't surface deltas yet; instead we run `generate` to completion and yield
        the assembled text once. Real delta passthrough is Phase 3.

        If `tools` or `response_schema` is passed, `generate` returns a dict, which
        is not meaningful for the streaming text channel. We log a warning and yield
        a string projection to keep callers from crashing.
        """
        result = self.generate(
            messages,
            tools=tools,
            response_schema=response_schema,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            **kwargs,
        )
        if isinstance(result, dict):
            LOG.warning(
                "generate_stream received non-text result (%s); coercing to string",
                result.get("type") or "structured",
            )
            text = result.get("content") or json.dumps(result, ensure_ascii=False)
        else:
            text = result
        if text:
            yield text

    def _consume_sse(self, resp: Any) -> Dict[str, Any]:
        """Parse a Responses API SSE stream, capturing both text and function calls.

        Returns:
            {
              "text": <assembled assistant text>,
              "function_calls": [
                {"call_id": str, "name": str, "arguments": <json string>},
                ...
              ],
            }
        """
        delta_buffer: List[str] = []
        final_text: Optional[str] = None
        # ordered map: item_id -> {call_id, name, arguments}
        pending_calls: Dict[str, Dict[str, str]] = {}
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

            elif event_type == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    item_id = item.get("id") or ""
                    pending_calls[item_id] = {
                        "call_id": item.get("call_id") or "",
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    }

            elif event_type == "response.function_call_arguments.delta":
                item_id = event.get("item_id") or ""
                entry = pending_calls.get(item_id)
                if entry is not None:
                    entry["arguments"] += event.get("delta") or ""

            elif event_type == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    item_id = item.get("id") or ""
                    finalized = {
                        "call_id": item.get("call_id") or "",
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or pending_calls.get(item_id, {}).get("arguments", ""),
                    }
                    pending_calls[item_id] = finalized

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

        function_calls = [
            entry
            for entry in pending_calls.values()
            if entry.get("name")
        ]

        self._store_usage(
            input_tokens=usage_input,
            output_tokens=usage_output,
            model=self.model,
            cached_tokens=usage_cached,
        )
        LOG.info(
            "response complete: chars=%d function_calls=%d in_tokens=%d out_tokens=%d cached=%d",
            len(final_text),
            len(function_calls),
            usage_input,
            usage_output,
            usage_cached,
        )
        return {"text": final_text, "function_calls": function_calls}


__all__ = ["OpenAICodexClient"]
