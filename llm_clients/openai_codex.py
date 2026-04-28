"""LLM client for OpenAI's Codex backend via ChatGPT subscription OAuth.

Phase 1: text-only generation.
Phase 2: tool calling, structured output (response_schema), reasoning_effort.
Phase 3: real SSE streaming + reasoning extraction + image input.

Authentication is delegated to the Codex CLI: this client reads ~/.codex/auth.json
that `codex login` produces and reuses its access_token + account_id. We do not
refresh tokens ourselves; if the token has expired, the user must run `codex login`
or let the Codex CLI refresh it.

Cloudflare in front of chatgpt.com fingerprints clients by their TLS handshake.
Plain `requests` / `httpx` get challenged; we use `curl_cffi` to impersonate
Chrome's TLS fingerprint, the same way Codex CLI's reqwest client gets through.
"""
from __future__ import annotations

import base64
import codecs
import json
import logging
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from curl_cffi import requests as cffi_requests
from filelock import FileLock, Timeout as FileLockTimeout

from .base import LLMClient
from .openai_message_preparer import prepare_openai_messages
from .schema_utils import normalize_schema_for_strict_json_output

LOG = logging.getLogger("saiverse.llm_clients.openai_codex")

CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_AUTH_LOCK_FILE = CODEX_AUTH_FILE.with_suffix(".json.lock")
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_CLI_VERSION = "0.45.0"
CODEX_IMPERSONATE = "chrome124"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
SUPPORTED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
# Pre-emptively refresh when access_token has less than this many seconds left
TOKEN_REFRESH_LEEWAY_SECONDS = 60.0


class CodexHTTPError(RuntimeError):
    """Raised when the Codex backend returns a non-2xx HTTP response.

    Carries the status code and the response body so callers can decide
    whether to refresh auth (401) or surface to the user (everything else).
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Codex backend returned status={status_code}: {body[:1000]}"
        )


def _build_user_agent() -> str:
    arch = platform.machine() or "unknown"
    system = platform.system() or "unknown"
    release = platform.release() or "0"
    return (
        f"{CODEX_ORIGINATOR}/{CODEX_CLI_VERSION} "
        f"({system} {release}; {arch}) python-saiverse"
    )


def _extract_json_object_candidate(text: str) -> str:
    """Trim wrapping markdown fences / leading prose to expose a JSON object."""
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
        * `generate_stream(...)` yields per-token text deltas; `{"type":"thinking", "content":...}`
          dicts for reasoning summary deltas
        * Token usage via `_store_usage`
        * Reasoning text captured via `_store_reasoning` (consume_reasoning())
        * Image input through `{"type": "image_url"}` content parts
        * `reasoning_effort` parameter (low/medium/high/xhigh)
    Out of scope:
        * Auto refresh of expired OAuth tokens (delegated to Codex CLI)
    """

    def __init__(
        self,
        model: str,
        supports_images: bool = False,
        timeout: float = 120.0,
        max_image_bytes: Optional[int] = None,
        max_image_embeds: Optional[int] = None,
        **_: Any,
    ) -> None:
        super().__init__(supports_images=supports_images)
        self.model = model
        self._timeout = float(timeout)
        self._params: Dict[str, Any] = {}
        self._session: Optional[Any] = None
        # Reuse OpenAIClient's defaults: 5 MB cap on image bytes for OpenAI-style
        # vision endpoints. SAIVerse normally provides image attachments via
        # `metadata.attachments` rather than expanded content lists, so we run
        # `prepare_openai_messages` first to materialize them.
        self._max_image_bytes = max_image_bytes if (max_image_bytes and max_image_bytes > 0) else 5 * 1024 * 1024
        self._max_image_embeds = max_image_embeds

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
    def _flatten_content_text(content: Any) -> str:
        """Flatten content to plain text. Used for system / assistant / tool roles."""
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

    def _user_content_to_parts(self, content: Any) -> List[Dict[str, Any]]:
        """Convert OpenAI Chat Completions style content to Responses API parts.

        Accepts:
            * str → [{"type":"input_text","text":...}]
            * list of {"type":"text","text":...} or {"type":"image_url","image_url":{"url":...}}
              or already-Responses items {"type":"input_text"} / {"type":"input_image"}
        Returns:
            list of {"type":"input_text"|"input_image", ...} parts.
        """
        if isinstance(content, str):
            return [{"type": "input_text", "text": content}]
        if not isinstance(content, list):
            text = "" if content is None else str(content)
            return [{"type": "input_text", "text": text}]

        parts: List[Dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                if part is not None:
                    parts.append({"type": "input_text", "text": str(part)})
                continue
            ptype = part.get("type")
            if ptype in ("input_text", "input_image"):
                parts.append(part)
                continue
            if ptype == "text":
                parts.append({"type": "input_text", "text": part.get("text") or ""})
                continue
            if ptype == "image_url":
                # Chat Completions form: {"type":"image_url","image_url":{"url":"data:..."}}
                # Responses API form:    {"type":"input_image","image_url":"data:..."}
                image_url = part.get("image_url")
                url: Optional[str] = None
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                elif isinstance(image_url, str):
                    url = image_url
                if url:
                    parts.append({"type": "input_image", "image_url": url})
                else:
                    LOG.debug("image_url part missing url, skipping: %s", part)
                continue
            # unknown type: try text fallback
            text = part.get("text") or part.get("content")
            if text:
                parts.append({"type": "input_text", "text": str(text)})
            else:
                LOG.debug("skipping unsupported content part type=%s", ptype)
        if not parts:
            parts.append({"type": "input_text", "text": ""})
        return parts

    @staticmethod
    def _to_responses_tools(tools: List[Any]) -> List[Dict[str, Any]]:
        """Convert SAIVerse Chat Completions tool spec to Responses API form."""
        out: List[Dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            t_type = tool.get("type")
            if t_type == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                params = fn.get("parameters") or {"type": "object", "properties": {}}
                out.append(
                    {
                        "type": "function",
                        "name": fn.get("name", ""),
                        "description": fn.get("description", "") or "",
                        "parameters": normalize_schema_for_strict_json_output(params),
                        "strict": False,
                    }
                )
            elif t_type == "function" and "name" in tool:
                normalized = dict(tool)
                if isinstance(normalized.get("parameters"), dict):
                    normalized["parameters"] = normalize_schema_for_strict_json_output(
                        normalized["parameters"]
                    )
                normalized.setdefault("strict", False)
                out.append(normalized)
            else:
                LOG.debug("skipping unsupported tool spec: %s", tool)
        return out

    def _build_input(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Lower SAIVerse-style messages into Responses API input items."""
        instructions_parts: List[str] = []
        input_items: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")

            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id") or msg.get("call_id") or "",
                        "output": self._flatten_content_text(msg.get("content")),
                    }
                )
                continue

            if role == "assistant" and msg.get("tool_calls"):
                text_part = self._flatten_content_text(msg.get("content"))
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

            if role == "system":
                text = self._flatten_content_text(msg.get("content"))
                if text:
                    instructions_parts.append(text)
                continue

            if role == "user":
                parts = self._user_content_to_parts(msg.get("content"))
                input_items.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": parts,
                    }
                )
                continue

            if role == "assistant":
                text = self._flatten_content_text(msg.get("content"))
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
                continue

            LOG.debug("skipping message with unsupported role=%r", role)

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

    def _expand_attachments(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Materialize SAIVerse's `metadata.attachments` image list into OpenAI
        Chat Completions style `image_url` parts so `_user_content_to_parts`
        can lower them to Responses API `input_image` parts.

        This mirrors what OpenAIClient does (it calls the same helper before
        sending). Without it, image attachments dangling on the metadata side
        are silently dropped by the time the request leaves the client.
        """
        try:
            return prepare_openai_messages(
                messages=messages,
                supports_images=self.supports_images,
                max_image_bytes=self._max_image_bytes,
                max_image_embeds=self._max_image_embeds,
                convert_system_to_user=False,
                reasoning_passback_field=None,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "prepare_openai_messages failed; falling back to raw messages "
                "(image attachments may be dropped): %s",
                exc,
            )
            return messages

    def _build_body(
        self,
        messages: List[Dict[str, Any]],
        temperature: float | None,
        tools: Optional[List[Any]],
        response_schema: Optional[Dict[str, Any]],
        reasoning_effort: Optional[str],
    ) -> Dict[str, Any]:
        prepared_messages = self._expand_attachments(messages)
        instructions, input_items = self._build_input(prepared_messages)
        body: Dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "stream": True,  # Codex backend requires stream=True
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions

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
                    "schema": normalize_schema_for_strict_json_output(response_schema),
                }
            }
        elif response_schema and responses_tools:
            LOG.warning(
                "response_schema specified alongside tools; structured output is "
                "ignored for tool runs (matches OpenAIClient behavior)."
            )

        # Reasoning block: always send `summary: "auto"` so the backend emits
        # `response.reasoning_summary_text.delta` events (we capture them and
        # surface them as `consume_reasoning()` entries / streaming_thinking
        # events). Without `summary` set, reasoning models still think but the
        # SSE stream is silent on it.
        reasoning_block: Dict[str, Any] = {"summary": "auto"}
        effort = self._resolve_reasoning_effort(reasoning_effort)
        if effort:
            reasoning_block["effort"] = effort
        body["reasoning"] = reasoning_block

        return body

    @staticmethod
    def _drain_error_body(resp: Any) -> str:
        """Drain an unsuccessful streamed response into a string for diagnostics."""
        chunks: List[bytes] = []
        try:
            for chunk in resp.iter_content():
                if chunk:
                    chunks.append(
                        chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                    )
        except Exception as exc:  # noqa: BLE001
            LOG.debug("error draining error body: %s", exc)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        if not text:
            text = getattr(resp, "text", None) or ""
        return text

    @staticmethod
    def _decode_jwt_exp(token: str) -> Optional[datetime]:
        """Best-effort `exp` claim extraction from a JWT (signature unverified)."""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except Exception:  # noqa: BLE001
            return None
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return None
        try:
            return datetime.fromtimestamp(int(exp), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    def _seconds_until_expiry(self, access_token: str) -> Optional[float]:
        exp = self._decode_jwt_exp(access_token)
        if exp is None:
            return None
        return (exp - datetime.now(tz=timezone.utc)).total_seconds()

    def _warn_if_expiring_soon(self, access_token: str) -> None:
        seconds = self._seconds_until_expiry(access_token)
        if seconds is None:
            return
        if seconds <= 0:
            LOG.warning(
                "Codex access_token already expired %.0fs ago (will trigger 401 + refresh)",
                -seconds,
            )
        elif seconds < 5 * 60:
            LOG.info("Codex access_token expires in %.0fs", seconds)

    def _post_responses(
        self,
        body: Dict[str, Any],
    ) -> Any:
        headers = self._build_headers()
        # Best-effort expiry warning on the token we're about to send.
        access_token_for_log = headers.get("Authorization", "").removeprefix("Bearer ")
        if access_token_for_log:
            self._warn_if_expiring_soon(access_token_for_log)
        session = self._ensure_session()
        url = f"{CODEX_BASE_URL}/responses"
        LOG.info(
            "POST %s model=%s input_messages=%d tools=%d schema=%s effort=%s",
            url,
            self.model,
            len(body["input"]),
            len(body.get("tools") or []),
            "text" in body,
            (body.get("reasoning") or {}).get("effort"),
        )
        if LOG.isEnabledFor(logging.DEBUG):
            for idx, item in enumerate(body["input"]):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message":
                    content = item.get("content") or []
                    types = [
                        p.get("type") for p in content if isinstance(p, dict)
                    ]
                    LOG.debug(
                        "input[%d] role=%s content_types=%s",
                        idx,
                        item.get("role"),
                        types,
                    )
                else:
                    LOG.debug("input[%d] type=%s", idx, item.get("type"))
        resp = session.post(
            url,
            headers=headers,
            json=body,
            timeout=self._timeout,
            stream=True,
        )
        if not resp.ok:
            err_text = self._drain_error_body(resp)
            status = resp.status_code
            try:
                resp.close()
            except Exception:
                pass
            raise CodexHTTPError(status, err_text)
        return resp

    def _refresh_or_pickup_latest(self, prior_access_token: Optional[str]) -> None:
        """Make sure ~/.codex/auth.json holds a usable access_token.

        Acquires a file lock, then:
          1. Re-read auth.json. If `access_token` differs from `prior_access_token`
             AND has plenty of life left, assume another process (Codex CLI itself,
             or a parallel SAIVerse worker) refreshed it and we just pick it up.
          2. Otherwise, POST to OpenAI's refresh endpoint with the stored
             refresh_token. Persist the new tokens via an atomic rename.

        This avoids the `refresh_token_reused` failure mode where Codex CLI and
        SAIVerse race to spend the same one-shot refresh token.
        """
        try:
            lock = FileLock(str(CODEX_AUTH_LOCK_FILE), timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"could not initialize auth.json lock: {exc}") from exc

        try:
            with lock:
                latest = self._load_auth()
                latest_tokens = latest.get("tokens") or {}
                latest_access = latest_tokens.get("access_token")
                latest_refresh = latest_tokens.get("refresh_token")

                if latest_access and latest_access != prior_access_token:
                    seconds = self._seconds_until_expiry(latest_access)
                    if seconds is None or seconds > TOKEN_REFRESH_LEEWAY_SECONDS:
                        LOG.info(
                            "auth.json already holds a fresh access_token "
                            "(expires in %.0fs); skipping refresh",
                            seconds if seconds is not None else float("inf"),
                        )
                        return

                if not latest_refresh:
                    raise RuntimeError(
                        "auth.json has no refresh_token; run `codex login` again"
                    )

                LOG.info("refreshing Codex access_token via refresh_token grant")
                refreshed = self._request_refresh(latest_refresh)
                self._persist_refreshed_tokens(latest, refreshed)
        except FileLockTimeout as exc:
            raise RuntimeError(
                "could not acquire auth.json lock within 30s; another process "
                "may be refreshing concurrently"
            ) from exc

    def _request_refresh(self, refresh_token: str) -> Dict[str, Any]:
        """POST to https://auth.openai.com/oauth/token with refresh_token grant."""
        # Use a short-lived session that mirrors Codex CLI's headers but
        # without the chatgpt.com cookie jar from `self._session`.
        session = cffi_requests.Session(impersonate=CODEX_IMPERSONATE)
        try:
            payload = {
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            resp = session.post(
                CODEX_REFRESH_TOKEN_URL,
                headers={
                    "Content-Type": "application/json",
                    "originator": CODEX_ORIGINATOR,
                    "User-Agent": _build_user_agent(),
                },
                json=payload,
                timeout=30,
            )
            if not resp.ok:
                err = getattr(resp, "text", None) or ""
                if not err and hasattr(resp, "content"):
                    err = resp.content.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"refresh_token grant failed status={resp.status_code}: {err[:600]}"
                )
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"refresh response was not JSON: {exc}") from exc
        finally:
            try:
                session.close()
            except Exception:
                pass

    @staticmethod
    def _persist_refreshed_tokens(
        existing_auth: Dict[str, Any],
        refreshed: Dict[str, Any],
    ) -> None:
        """Atomically write back auth.json with the refreshed tokens."""
        tokens = dict(existing_auth.get("tokens") or {})
        new_id = refreshed.get("id_token")
        new_access = refreshed.get("access_token")
        new_refresh = refreshed.get("refresh_token")
        if new_id:
            tokens["id_token"] = new_id
        if new_access:
            tokens["access_token"] = new_access
        if new_refresh:
            tokens["refresh_token"] = new_refresh
        existing_auth["tokens"] = tokens
        existing_auth["last_refresh"] = datetime.now(tz=timezone.utc).isoformat()

        tmp_path = CODEX_AUTH_FILE.with_suffix(".json.tmp")
        # Indent for human readability, matching Codex CLI's serialize_pretty
        tmp_path.write_text(
            json.dumps(existing_auth, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp_path, CODEX_AUTH_FILE)
        LOG.info("auth.json updated with refreshed tokens")

    def _post_with_auth_retry(self, body: Dict[str, Any]) -> Any:
        """Run `_post_responses`, transparently refreshing once on 401."""
        try:
            return self._post_responses(body)
        except CodexHTTPError as exc:
            if exc.status_code != 401:
                raise
            LOG.warning(
                "received 401 from Codex (likely expired access_token); "
                "attempting auth refresh and retry. body=%s",
                exc.body[:300],
            )
            # Capture the access_token we just used so we can detect whether
            # auth.json was refreshed by some other process before we lock.
            try:
                prior_auth = self._load_auth()
                prior_access = (prior_auth.get("tokens") or {}).get("access_token")
            except Exception:  # noqa: BLE001
                prior_access = None
            try:
                self._refresh_or_pickup_latest(prior_access)
            except Exception as refresh_err:
                raise RuntimeError(
                    f"Codex OAuth token expired and auto-refresh failed: "
                    f"{refresh_err}. Run `codex login` to renew."
                ) from refresh_err
            return self._post_responses(body)

    @staticmethod
    def _iter_sse_events(resp: Any) -> Iterator[Dict[str, Any]]:
        """Yield each parsed SSE `data:` line as a dict.

        Reads raw bytes from `resp.iter_content()` rather than `resp.iter_lines()`
        so that UTF-8 multi-byte characters that straddle network chunks are
        decoded correctly. `iter_lines()` can split on a `\\n` byte that lives
        mid-multibyte and produce mojibake (typically Japanese / emoji deltas).
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""

        def _emit(line: str) -> Optional[Dict[str, Any]]:
            if not line.startswith("data:"):
                return None
            data_str = line[len("data:") :].strip()
            if not data_str:
                return None
            try:
                return json.loads(data_str)
            except json.JSONDecodeError:
                LOG.debug("could not parse SSE payload: %s", data_str[:200])
                return None

        for chunk in resp.iter_content():
            if not chunk:
                continue
            if isinstance(chunk, bytes):
                buffer += decoder.decode(chunk)
            else:
                buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                event = _emit(line.rstrip("\r"))
                if event is not None:
                    yield event

        # Flush any remaining bytes / line at EOF
        buffer += decoder.decode(b"", final=True)
        if buffer:
            for line in buffer.split("\n"):
                event = _emit(line.rstrip("\r"))
                if event is not None:
                    yield event

    def _iter_chunks(self, resp: Any) -> Iterator[Any]:
        """Process the SSE stream, yielding chunks for streaming consumers and
        a final ("__done__", state) tuple for aggregators.

        Yield types:
            * `str` — visible text delta (output_text.delta)
            * `dict {"type": "thinking", "content": str}` — reasoning summary or
              detail delta. SEA's streaming consumer picks these up as
              streaming_thinking events.
            * `("__done__", state_dict)` — sentinel emitted exactly once at the
              very end, with everything aggregators need:
                  text, function_calls, reasoning_summary_text,
                  reasoning_full_text, usage_input, usage_output, usage_cached
        """
        delta_buffer: List[str] = []
        final_text: Optional[str] = None
        pending_calls: Dict[str, Dict[str, str]] = {}
        reasoning_summaries: Dict[int, List[str]] = {}
        reasoning_full: List[str] = []
        usage_input = 0
        usage_output = 0
        usage_cached = 0

        for event in self._iter_sse_events(resp):
            event_type = event.get("type")

            if event_type == "response.output_text.delta":
                delta = event.get("delta") or ""
                if delta:
                    delta_buffer.append(delta)
                    yield delta

            elif event_type == "response.output_text.done":
                final_text = event.get("text") or "".join(delta_buffer)

            elif event_type == "response.reasoning_summary_text.delta":
                delta = event.get("delta") or ""
                if delta:
                    idx = event.get("summary_index") or 0
                    reasoning_summaries.setdefault(int(idx), []).append(delta)
                    yield {"type": "thinking", "content": delta}

            elif event_type == "response.reasoning_text.delta":
                delta = event.get("delta") or ""
                if delta:
                    reasoning_full.append(delta)
                    yield {"type": "thinking", "content": delta}

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
                    pending_calls[item_id] = {
                        "call_id": item.get("call_id") or "",
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments")
                        or pending_calls.get(item_id, {}).get("arguments", ""),
                    }

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
            entry for entry in pending_calls.values() if entry.get("name")
        ]

        summary_text = "\n\n".join(
            "".join(parts) for _, parts in sorted(reasoning_summaries.items())
        )
        full_text = "".join(reasoning_full)

        yield (
            "__done__",
            {
                "text": final_text,
                "function_calls": function_calls,
                "reasoning_summary_text": summary_text,
                "reasoning_full_text": full_text,
                "usage_input": usage_input,
                "usage_output": usage_output,
                "usage_cached": usage_cached,
            },
        )

    def _finalize(
        self,
        state: Dict[str, Any],
        tools: Optional[List[Any]],
    ) -> None:
        """Persist usage, reasoning, and tool detection from the streamed state."""
        self._store_usage(
            input_tokens=state["usage_input"],
            output_tokens=state["usage_output"],
            model=self.model,
            cached_tokens=state["usage_cached"],
        )

        reasoning_entries: List[Dict[str, str]] = []
        if state["reasoning_summary_text"]:
            reasoning_entries.append(
                {"text": state["reasoning_summary_text"], "type": "summary"}
            )
        if state["reasoning_full_text"]:
            reasoning_entries.append(
                {"text": state["reasoning_full_text"], "type": "reasoning"}
            )
        if reasoning_entries:
            self._store_reasoning(reasoning_entries)

        if tools:
            function_calls = state["function_calls"]
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
            else:
                detection = {"type": "text", "content": state["text"]}
            self._store_tool_detection(detection)

        LOG.info(
            "response complete: chars=%d function_calls=%d reasoning_chars=%d "
            "in_tokens=%d out_tokens=%d cached=%d",
            len(state["text"]),
            len(state["function_calls"]),
            len(state["reasoning_summary_text"]) + len(state["reasoning_full_text"]),
            state["usage_input"],
            state["usage_output"],
            state["usage_cached"],
        )

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
        """Synchronous run: returns text / tool-detection dict / parsed dict.

        See class docstring for return-type matrix.
        """
        body = self._build_body(
            messages,
            temperature=temperature,
            tools=tools,
            response_schema=response_schema,
            reasoning_effort=reasoning_effort,
        )
        resp = self._post_with_auth_retry(body)
        state: Optional[Dict[str, Any]] = None
        try:
            for chunk in self._iter_chunks(resp):
                if isinstance(chunk, tuple) and chunk and chunk[0] == "__done__":
                    state = chunk[1]
                    break
                # ignore intermediate text/thinking chunks in synchronous mode
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if state is None:
            raise RuntimeError("Codex SSE stream ended without a terminal state")

        self._finalize(state, tools)

        text = state["text"]
        function_calls = state["function_calls"]

        if tools:
            detection = self.consume_tool_detection() or {"type": "text", "content": text}
            # _finalize already stored detection; re-store so a later consume call sees it
            self._store_tool_detection(detection)
            return detection

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

        # Unused locally but documents intent that function_calls is dropped here
        _ = function_calls
        return text

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        reasoning_effort: Optional[str] = None,
        **_: Any,
    ) -> Iterator[Any]:
        """Stream text deltas and reasoning summary deltas.

        Yields:
            * `str` chunks for visible text deltas
            * `{"type": "thinking", "content": str}` dicts for reasoning summary
              / reasoning text deltas (SEA picks these up as streaming_thinking
              events)
        Side effects (after the iterator is exhausted):
            * `consume_usage()` populated
            * `consume_reasoning()` populated with summary + full reasoning text
            * `consume_tool_detection()` populated when `tools` was non-empty

        For `response_schema` requests the assembled text is buffered, parsed,
        and the parsed JSON is dropped — callers that need the parsed value
        should use `generate(...)` directly. We still yield raw text deltas so
        the streaming pipeline can render progress.
        """
        body = self._build_body(
            messages,
            temperature=temperature,
            tools=tools,
            response_schema=response_schema,
            reasoning_effort=reasoning_effort,
        )
        resp = self._post_with_auth_retry(body)
        state: Optional[Dict[str, Any]] = None
        try:
            for chunk in self._iter_chunks(resp):
                if isinstance(chunk, tuple) and chunk and chunk[0] == "__done__":
                    state = chunk[1]
                    break
                yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass

        if state is None:
            raise RuntimeError("Codex SSE stream ended without a terminal state")

        self._finalize(state, tools)


__all__ = ["OpenAICodexClient"]
